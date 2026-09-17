"""Windows 稳定安装目录部署器。

这个模块只由显式 ``--deploy`` 构建调用。它不负责生成发布包，也不读取统计正文、
报告或用户配置；职责是关闭目标安装中的进程、校验候选目录并把它切换到稳定的
``HextechCompanion`` 路径。目录切换失败时恢复上一版本和部署前 cohort 指针。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from hextech.contracts import CatalogManifestV2
from hextech.contracts.data_pipeline import require_identifier
from hextech.infrastructure.persistence.cohort_recovery import (
    CohortCandidate,
    parse_utc,
    validate_generation_cohort,
)
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation.validation import SnapshotValidationError
from hextech.modules.vision.diagnostic_settings import (
    RoiDumpMode,
    overlay_diagnostic_settings_path,
    set_roi_dump_mode,
)
from tooling.build.manifest import BUNDLE_MANIFEST_SCHEMA_VERSION, RUNTIME_CONTRACT_VERSIONS
from tooling.build.deploy_lineage import refresh_checkpoint_errors, validated_launch_generation, validated_game_stats_generation


APP_EXE_NAME = "Hextech伴生终端.exe"
APP_LAUNCHER_NAME = "启动 Hextech.bat"
APP_GUIDE_NAME = "README_首次使用.txt"
APP_SHORTCUT_NAME = "Hextech伴生终端.lnk"
STABLE_INSTALL_NAME = "HextechCompanion"
RELEASE_DIR_PREFIX = "HextechCompanion-"
DEPLOYMENT_VERIFY_TIMEOUT_SECONDS = 150.0
ROLLBACK_FILESYSTEM_RETRY_SECONDS = 3.0
ROLLBACK_FILESYSTEM_RETRY_INTERVAL_SECONDS = 0.1
SOURCE_RUNTIME_MODULES = frozenset(
    {
        "hextech.bootstrap.overlay",
        "hextech.infrastructure.vision.sidecar",
        "hextech.interfaces.overlay",
        "hextech.interfaces.overlay.host",
    }
)
SOURCE_RUNTIME_LAUNCHERS = frozenset({"hextech-overlay.exe", "hextech-overlay"})
PROCESS_ROLE_FLAGS = {
    "data_service": "--data-service",
    "supervisor": "--runtime-supervisor",
    "overlay_host": "--game-overlay",
    "vision_sidecar": "--overlay-sidecar",
}
TRANSIENT_PROCESS_FLAGS = frozenset({"--acquisition-worker"})
RUNTIME_BUILD_STATE_SPECS = (
    (Path("state/startup_timing.v1.json"), 1),
    (Path("state/game_overlay_sidecar_status.json"), 2),
    (Path("state/game_overlay_slots.v1.json"), 3),
    (Path("state/game_overlay_visibility.v1.json"), 2),
    (Path("reports/overlay_sessions/latest.json"), 2),
)
RUNTIME_COHORT_STATE_FILES = (
    Path("catalog/current.v2.json"),
    Path("sources/aramkit/current.v2.json"),
    Path("sources/blitz/current.v2.json"),
    Path("sources/apex/current.v2.json"),
    Path("sources/mayhem/current.v2.json"),
    Path("snapshots/current.v2.json"),
    Path("snapshots/previous.v2.json"),
    Path("state/data-service/refresh_schedule.v1.json"),
    Path("state/data-service/refresh_checkpoint.v1.json"),
    Path("state/data-service/cohort_recovery_point.v1.json"),
    Path("state/data-service/cohort_selection.v1.json"),
    Path("state/data-service/catalog_adoption_checkpoint.v1.json"),
    Path("state/data-service/promotion_journal.v1.json"),
)


class DeploymentError(RuntimeError):
    """部署候选不可安全提升或回滚时抛出。"""


def _deployment_step(message: str) -> None:
    print(f"[deploy] {message}", flush=True)


@dataclass(frozen=True)
class ProcessIdentity:
    """防止等待期间 PID 复用导致误终止。"""

    pid: int
    create_time: float
    name: str = ""
    executable: str = ""
    command_line: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeploymentResult:
    install_dir: Path
    previous_dir: Path | None
    shortcut_path: Path | None
    removed_shortcuts: tuple[Path, ...]
    restarted: bool
    started: bool
    verified: bool
    process_ids: tuple[tuple[str, int], ...]
    build_id: str


@dataclass(frozen=True)
class _RuntimeStateFileSnapshot:
    path: Path
    existed: bool
    content: bytes


def default_install_dir() -> Path:
    """返回 Windows 稳定安装目录；调用方仍需显式启用部署。"""

    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/")
    return Path(f"{system_drive}\\{STABLE_INSTALL_NAME}")


def default_deployment_lock() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(local_app_data) / "HextechNexus" if local_app_data else Path.home() / ".hextech_nexus"
    return root / "locks" / "package_deploy.lock"


def _normalized_path(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attributes & 0x400)


def validate_install_dir(install_dir: Path) -> Path:
    """限制部署目标，避免参数错误覆盖任意目录。"""

    raw = Path(install_dir).expanduser()
    if not raw.is_absolute():
        raise DeploymentError("部署目录必须是绝对路径")
    target = raw.resolve(strict=False)
    if target.name.casefold() != STABLE_INSTALL_NAME.casefold():
        raise DeploymentError(f"稳定部署目录名必须是 {STABLE_INSTALL_NAME}")
    if target == Path(target.anchor):
        raise DeploymentError("禁止把磁盘根目录作为部署目标")
    if target.exists() and _is_reparse_point(target):
        raise DeploymentError("部署目标不得是符号链接或 reparse point")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def validate_package_dir(package_dir: Path) -> Path:
    """检查最小便携包契约，不让半成品进入稳定安装目录。"""

    root = Path(package_dir).resolve()
    required = (
        root / APP_EXE_NAME,
        root / APP_LAUNCHER_NAME,
        root / APP_GUIDE_NAME,
        root / "_internal" / "bundle_manifest.json",
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise DeploymentError(f"部署候选缺少文件：{', '.join(missing)}")
    if (root / "var").exists():
        raise DeploymentError("部署候选不得携带 var 运行态")
    try:
        manifest = json.loads((root / "_internal" / "bundle_manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"bundle manifest 无法读取：{exc}") from exc
    if not isinstance(manifest, dict) or int(manifest.get("schema_version") or 0) != BUNDLE_MANIFEST_SCHEMA_VERSION:
        raise DeploymentError("bundle manifest schema 无效")
    if not str(manifest.get("build_id") or "").strip():
        raise DeploymentError("bundle manifest 缺少 build_id")
    if manifest.get("runtime_contracts") != RUNTIME_CONTRACT_VERSIONS:
        raise DeploymentError("bundle manifest 运行契约不匹配")
    return root


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """校验部署复制完整性；包内不允许链接到目录外部。"""

    fingerprint: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise DeploymentError(f"部署包不得包含符号链接：{path.relative_to(root)}")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        fingerprint[path.relative_to(root).as_posix()] = (path.stat().st_size, digest.hexdigest())
    return fingerprint


def _command_tokens(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return (value,)
    return ()


def _is_managed_runtime_process(*, name: str, executable: str, command_line: tuple[str, ...]) -> bool:
    """只识别本产品 EXE，以及源码启动的 Overlay/Sidecar。"""

    process_name = (Path(executable).name if executable else name).casefold()
    if process_name == APP_EXE_NAME.casefold():
        return True
    if process_name in SOURCE_RUNTIME_LAUNCHERS:
        return True
    folded = tuple(token.casefold() for token in command_line)
    for index, token in enumerate(folded[:-1]):
        if token == "-m" and folded[index + 1] in SOURCE_RUNTIME_MODULES:
            return True
    python_process = process_name in {"python.exe", "pythonw.exe", "python", "pythonw"}
    return python_process and any(flag in folded for flag in ("--game-overlay", "--overlay-sidecar"))


def _matching_deployment_processes() -> list[ProcessIdentity]:
    """枚举部署必须清空的稳定版、旧版、便携版与源码识别进程。"""

    matches: list[ProcessIdentity] = []
    for process in psutil.process_iter(("pid", "name", "exe", "cmdline", "create_time")):
        try:
            name = str(process.info.get("name") or "")
            executable = str(process.info.get("exe") or "")
            command_line = _command_tokens(process.info.get("cmdline"))
            if _is_managed_runtime_process(name=name, executable=executable, command_line=command_line):
                matches.append(
                    ProcessIdentity(
                        pid=int(process.pid),
                        create_time=float(process.info["create_time"]),
                        name=name,
                        executable=executable,
                        command_line=command_line,
                    )
                )
        except (psutil.NoSuchProcess, OSError, TypeError, ValueError):
            continue
        except psutil.AccessDenied as exc:
            raise DeploymentError(f"无法检查进程 PID {process.pid}；请以管理员权限部署") from exc
    return matches


def _same_process(identity: ProcessIdentity) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.create_time) > 0.01:
            return None
        return process
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise DeploymentError(f"无法访问进程 PID {identity.pid}；请以管理员权限部署") from exc


def _kill_process_tree(identity: ProcessIdentity) -> None:
    process = _same_process(identity)
    if process is None:
        return
    try:
        descendants = process.children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    except psutil.AccessDenied as exc:
        raise DeploymentError(f"无法枚举进程树 PID {identity.pid}；请以管理员权限部署") from exc
    for child in reversed(descendants):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise DeploymentError(f"无法强制结束子进程 PID {child.pid}；请以管理员权限部署") from exc
    process = _same_process(identity)
    if process is not None:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            raise DeploymentError(f"无法强制结束进程 PID {identity.pid}；请以管理员权限部署") from exc


def shutdown_existing_install(executable: Path, *, timeout: float = 12.0) -> bool:
    """强制清空所有 Hextech EXE 与源码 Overlay/Sidecar，并拦住回收竞态。"""

    if Path(executable).name.casefold() != APP_EXE_NAME.casefold():
        raise DeploymentError(f"部署关闭目标必须是 {APP_EXE_NAME}")
    deadline = time.monotonic() + max(0.1, timeout)
    found = False
    while True:
        identities = _matching_deployment_processes()
        if not identities:
            return found
        found = True
        _deployment_step(f"强制结束旧运行时 pids={[item.pid for item in identities]}")
        for identity in identities:
            _kill_process_tree(identity)
        if time.monotonic() >= deadline:
            remaining = _matching_deployment_processes()
            raise DeploymentError(f"旧版或源码运行时仍未退出：pids={[item.pid for item in remaining]}")
        time.sleep(0.1)


def validate_shortcut_path(shortcut_path: Path) -> Path:
    """只接受既有快捷方式，防止拼写错误在桌面静默制造重复入口。"""

    shortcut_path = Path(shortcut_path).resolve(strict=False)
    if shortcut_path.suffix.casefold() != ".lnk":
        raise DeploymentError("快捷方式必须使用 .lnk 后缀")
    if not shortcut_path.parent.is_dir():
        raise DeploymentError("快捷方式父目录不存在")
    if not shortcut_path.is_file():
        raise DeploymentError(f"既有快捷方式不存在，拒绝创建：{shortcut_path}")
    return shortcut_path


def _desktop_roots(canonical_shortcut: Path) -> tuple[Path, ...]:
    """返回 Windows 桌面合并视图背后的物理目录，且不递归扫描。"""

    candidates = [canonical_shortcut.parent]
    user_profile = os.environ.get("USERPROFILE", "").strip()
    public_profile = os.environ.get("PUBLIC", "").strip()
    if user_profile:
        candidates.append(Path(user_profile) / "Desktop")
    if public_profile:
        candidates.append(Path(public_profile) / "Desktop")

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalized_path(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.is_dir():
            roots.append(candidate.resolve(strict=False))
    return tuple(roots)


def _read_shortcut_target(shortcut_path: Path) -> Path:
    """只读取既有 ``.lnk`` 的目标；不调用 ``Save``，因此不会创建文件。"""

    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(shortcut_path))
        target = str(shortcut.TargetPath or "").strip()
    except Exception as exc:
        raise DeploymentError(f"快捷方式目标读取失败：{shortcut_path}: {exc}") from exc
    if not target:
        raise DeploymentError(f"快捷方式目标为空：{shortcut_path}")
    return Path(target).resolve(strict=False)


def _is_managed_shortcut_target(target: Path, stable_executable: Path, release_root: Path) -> bool:
    """仅识别稳定安装 EXE 和本仓 releases 下的正式便携包 EXE。"""

    if _normalized_path(target) == _normalized_path(stable_executable):
        return True
    if target.name.casefold() != APP_EXE_NAME.casefold():
        return False
    if not target.parent.name.casefold().startswith(RELEASE_DIR_PREFIX.casefold()):
        return False
    try:
        return os.path.commonpath((_normalized_path(target), _normalized_path(release_root))) == _normalized_path(
            release_root
        )
    except ValueError:
        return False


def _looks_like_hextech_shortcut(shortcut_path: Path) -> bool:
    return "hextech" in shortcut_path.stem.casefold()


def _managed_duplicate_shortcuts(
    canonical_shortcut: Path,
    stable_executable: Path,
    release_root: Path,
) -> tuple[Path, ...]:
    duplicates: list[Path] = []
    canonical_normalized = _normalized_path(canonical_shortcut)
    for root in _desktop_roots(canonical_shortcut):
        try:
            shortcuts = tuple(root.glob("*.lnk"))
        except OSError as exc:
            raise DeploymentError(f"桌面快捷方式扫描失败：{root}: {exc}") from exc
        for shortcut_path in shortcuts:
            if _normalized_path(shortcut_path) == canonical_normalized:
                continue
            suspicious_name = _looks_like_hextech_shortcut(shortcut_path)
            if suspicious_name and _is_reparse_point(shortcut_path):
                raise DeploymentError(f"拒绝处理 reparse point 快捷方式：{shortcut_path}")
            try:
                target = _read_shortcut_target(shortcut_path)
            except DeploymentError:
                if suspicious_name:
                    raise
                continue
            if not _is_managed_shortcut_target(target, stable_executable, release_root):
                continue
            if _is_reparse_point(shortcut_path):
                raise DeploymentError(f"拒绝处理 reparse point 快捷方式：{shortcut_path}")
            duplicates.append(shortcut_path.resolve(strict=False))
    return tuple(sorted(duplicates, key=lambda path: _normalized_path(path)))


def converge_desktop_shortcuts(
    canonical_shortcut: Path,
    stable_executable: Path,
    release_root: Path,
) -> tuple[Path, ...]:
    """删除同应用的非规范桌面入口，并验证最终只保留规范入口。"""

    canonical_shortcut = validate_shortcut_path(canonical_shortcut)
    duplicates = _managed_duplicate_shortcuts(canonical_shortcut, stable_executable, release_root)
    removed: list[Path] = []
    for duplicate in duplicates:
        # 删除前再次读取目标，防止扫描后被替换成用户的其他快捷方式。
        target = _read_shortcut_target(duplicate)
        if not _is_managed_shortcut_target(target, stable_executable, release_root):
            raise DeploymentError(f"快捷方式目标在清理前发生变化：{duplicate}")
        try:
            duplicate.unlink()
        except OSError as exc:
            raise DeploymentError(f"重复快捷方式删除失败：{duplicate}: {exc}") from exc
        removed.append(duplicate)

    remaining = _managed_duplicate_shortcuts(canonical_shortcut, stable_executable, release_root)
    if remaining:
        raise DeploymentError(f"重复快捷方式清理后仍存在：{', '.join(str(path) for path in remaining)}")
    return tuple(removed)


def update_shortcut(shortcut_path: Path, target_exe: Path) -> Path:
    """更新既有快捷方式并保留其余 shell 属性；本函数绝不创建新快捷方式。"""

    shortcut_path = validate_shortcut_path(shortcut_path)
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(shortcut_path))
        shortcut.TargetPath = str(target_exe)
        shortcut.WorkingDirectory = str(target_exe.parent)
        shortcut.IconLocation = f"{target_exe},0"
        shortcut.Save()
    except Exception as exc:
        raise DeploymentError(f"快捷方式更新失败：{exc}") from exc
    return shortcut_path


def _start_install(executable: Path) -> subprocess.Popen[bytes]:
    if os.name == "nt":
        # 部署常因 C:\ 根目录权限而在提权进程中运行；交给当前用户的 Explorer
        # 启动，避免正式客户端长期继承管理员 token。后续硬验收负责确认真实 EXE。
        return subprocess.Popen(
            ["explorer.exe", str(executable)],
            cwd=str(executable.parent),
            creationflags=int(subprocess.CREATE_NEW_PROCESS_GROUP),
        )
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent))
    time.sleep(1.0)
    if process.poll() is not None:
        raise DeploymentError(f"新客户端启动后提前退出：code={process.returncode}")
    return process


def _packaged_var_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HextechNexus" / "var"
    app_data = os.environ.get("APPDATA", "").strip()
    if app_data:
        return Path(app_data) / "HextechNexus" / "var"
    return Path.home() / ".hextech_nexus" / "var"


def _snapshot_runtime_cohort_state(root: Path | None = None) -> tuple[_RuntimeStateFileSnapshot, ...]:
    """原样备份部署会切换的小型 pointer/schedule；不读取业务正文。"""

    runtime_root = (root or _packaged_var_dir()).resolve()
    snapshots: list[_RuntimeStateFileSnapshot] = []
    for relative in RUNTIME_COHORT_STATE_FILES:
        path = (runtime_root / relative).resolve()
        if runtime_root not in path.parents:
            raise DeploymentError(f"运行态 cohort 备份路径越界：{relative}")
        try:
            snapshots.append(_RuntimeStateFileSnapshot(path=path, existed=True, content=path.read_bytes()))
        except FileNotFoundError:
            snapshots.append(_RuntimeStateFileSnapshot(path=path, existed=False, content=b""))
        except OSError as exc:
            raise DeploymentError(f"运行态 cohort 指针无法备份：{relative}: {exc}") from exc
    return tuple(snapshots)


def _restore_runtime_cohort_state(snapshots: tuple[_RuntimeStateFileSnapshot, ...]) -> None:
    """原子恢复部署前 pointer/schedule；候选新增 immutable 文件保留为未引用缓存。"""

    for snapshot in snapshots:
        if not snapshot.existed:
            snapshot.path.unlink(missing_ok=True)
            continue
        snapshot.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = snapshot.path.with_name(f".{snapshot.path.name}.deploy-restore-{os.getpid()}")
        try:
            temporary.write_bytes(snapshot.content)
            os.replace(temporary, snapshot.path)
        finally:
            temporary.unlink(missing_ok=True)


def _expected_cohort(source_manifest: dict[str, object]) -> dict[str, object] | None:
    cohort = source_manifest.get("cohort_seed")
    return {**cohort, "_source_fingerprint": source_manifest.get("source_fingerprint", "")} if isinstance(cohort, dict) and cohort else None


def _validated_runtime_candidate(
    root: Path,
    generation_id: str,
    cache: dict[tuple[str, str], CohortCandidate] | None,
) -> CohortCandidate:
    key = (str(root.resolve()), generation_id)
    if cache is not None and key in cache:
        return cache[key]
    candidate = validate_generation_cohort(root, generation_id)
    if cache is not None:
        cache[key] = candidate
    return candidate


def _validated_catalog_generation_manifest(
    root: Path,
    catalog_id: str,
) -> CatalogManifestV2:
    catalog_id = require_identifier(
        catalog_id,
        field_name="catalog_generation_id",
    )
    catalog_root = root / "catalog" / "generations" / catalog_id
    manifest_path = catalog_root / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest must be an object")
        manifest = CatalogManifestV2.from_mapping(payload)
        if manifest.catalog_generation_id != catalog_id:
            raise ValueError("catalog manifest identity mismatch")
        validate_catalog_files(catalog_root, manifest)
        return manifest
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("runtime recognition Catalog pointer/manifest 无效") from exc


def _validated_runtime_catalog_manifest(
    root: Path,
    pointer: dict[str, object],
) -> CatalogManifestV2:
    catalog_id = str(pointer.get("catalog_generation_id") or "")
    manifest = _validated_catalog_generation_manifest(root, catalog_id)
    manifest_path = root / "catalog" / "generations" / catalog_id / "manifest.json"
    if (
        pointer.get("schema_version") != 2
        or pointer.get("content_sha256") != manifest.content_sha256
        or pointer.get("manifest_sha256") != sha256_file(manifest_path)
    ):
        raise ValueError("runtime recognition Catalog pointer/manifest 无效")
    return manifest


def _resolve_runtime_recognition_expected(
    root: Path,
    expected: dict[str, object],
) -> tuple[dict[str, object], list[str]]:
    """允许安装器保留比 bundle 更新且完整的独立识别 Catalog。"""

    expected_id = str(
        expected.get("recognition_catalog_generation_id")
        or expected.get("catalog_generation_id")
        or ""
    )
    try:
        payload = json.loads(
            (root / "catalog" / "current.v2.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        return dict(expected), [
            "recognition Catalog current 不可读 "
            f"path=catalog/current.v2.json error={type(exc).__name__}"
        ]
    if not isinstance(payload, dict):
        return dict(expected), ["recognition Catalog current 不是对象"]
    actual_id = str(payload.get("catalog_generation_id") or "")
    if actual_id == expected_id:
        if str(expected.get("recognition_catalog_generation_id") or ""):
            try:
                _validated_runtime_catalog_manifest(root, payload)
            except ValueError as exc:
                return dict(expected), [
                    "runtime recognition Catalog 未通过完整验证："
                    f"catalog={actual_id} error={type(exc).__name__}"
                ]
        return dict(expected), []
    # Old one-Catalog packages did not authorize independent Catalog adoption.
    if not str(expected.get("recognition_catalog_generation_id") or ""):
        return dict(expected), []
    try:
        actual_manifest = _validated_runtime_catalog_manifest(root, payload)
        expected_manifest = _validated_catalog_generation_manifest(
            root,
            expected_id,
        )
        if parse_utc(actual_manifest.created_at) < parse_utc(expected_manifest.created_at):
            return dict(expected), [
                "runtime recognition Catalog 早于 bundle seed："
                f"expected={expected_id} actual={actual_id}"
            ]
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        return dict(expected), [
            "runtime recognition Catalog 未通过完整验证："
            f"catalog={actual_id} error={type(exc).__name__}"
        ]
    return {
        **expected,
        "recognition_catalog_generation_id": actual_id,
    }, []


def _resolve_runtime_cohort_expected(
    root: Path,
    expected: dict[str, object],
    *,
    validation_cache: dict[tuple[str, str], CohortCandidate] | None = None,
) -> tuple[dict[str, object], list[str]]:
    """允许启动刷新晋升同 Catalog/production pool 的更新完整 generation。"""

    effective, recognition_errors = _resolve_runtime_recognition_expected(root, expected)
    if recognition_errors:
        return effective, recognition_errors
    expected = effective

    pointer_path = root / "snapshots" / "current.v2.json"
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return dict(expected), [f"cohort current 不可读 path=snapshots/current.v2.json error={type(exc).__name__}"]
    actual_generation_id = (
        str(pointer.get("current_generation_id") or "")
        if isinstance(pointer, dict)
        else ""
    )
    expected_generation_id = str(expected.get("generation_id") or "")
    if not actual_generation_id or actual_generation_id == expected_generation_id:
        return dict(expected), []
    try:
        baseline = _validated_runtime_candidate(
            root,
            expected_generation_id,
            validation_cache,
        )
        actual = _validated_runtime_candidate(
            root,
            actual_generation_id,
            validation_cache,
        )
    except (OSError, TypeError, ValueError, SnapshotValidationError) as exc:
        return dict(expected), [
            "启动刷新后的 runtime generation 未通过完整 cohort 验证："
            f"generation={actual_generation_id} error={type(exc).__name__}"
        ]
    if actual.sort_time < baseline.sort_time:
        return dict(expected), [
            "runtime generation 早于 bundle seed："
            f"expected={expected_generation_id} actual={actual_generation_id}"
        ]
    catalog_pointer = actual.pointers.get("catalog")
    actual_catalog_id = (
        str(catalog_pointer.get("catalog_generation_id") or "")
        if isinstance(catalog_pointer, dict)
        else ""
    )
    invariants = {
        "catalog_generation_id": (
            actual_catalog_id,
            str(expected.get("catalog_generation_id") or ""),
        ),
        "production_pool_id": (
            actual.production_pool_id,
            str(expected.get("production_pool_id") or ""),
        ),
        "production_pool_count": (
            actual.production_pool_count,
            int(expected.get("production_pool_count") or 0),
        ),
    }
    mismatches = [
        f"{field} expected={wanted} actual={observed}"
        for field, (observed, wanted) in invariants.items()
        if observed != wanted
    ]
    if mismatches:
        return dict(expected), [
            "启动刷新改变了部署候选的 Vision 身份合同：" + " | ".join(mismatches)
        ]
    source_run_ids = {
        source: str(actual.pointers[source].get("run_id") or "")
        for source in ("aramkit", "blitz", "apex", "mayhem")
        if source in actual.pointers
    }
    is_v3 = getattr(actual, "snapshot_schema_version", 2) == 3
    if (not source_run_ids.get("aramkit") or any(not value for value in source_run_ids.values())
            or (not is_v3 and len(source_run_ids) != 4)):
        return dict(expected), ["启动刷新后的完整 cohort 缺少来源 run identity"]
    return {
        **expected,
        "generation_id": actual.generation_id,
        "catalog_generation_id": actual_catalog_id,
        "source_run_ids": source_run_ids,
        **({"schema_version": 2, "snapshot_schema_version": 3, "units": dict(actual.units)} if is_v3 else {}),
    }, []


def _runtime_cohort_errors(root: Path, expected: dict[str, object]) -> list[str]:
    """核对五个 current 与调度状态，防止只靠 Sidecar 自报掩盖混合代。"""

    errors: list[str] = []
    generation_id = str(expected.get("generation_id") or "")
    # v3 statistics remain bound to the Catalog that produced their immutable
    # units, while recognition may independently adopt a newer Catalog.  Old
    # bundles omit the explicit recognition identity and retain one-Catalog
    # behavior through this fallback.
    catalog_id = str(expected.get("catalog_generation_id") or "")
    recognition_catalog_id = str(
        expected.get("recognition_catalog_generation_id") or catalog_id
    )
    source_run_ids = expected.get("source_run_ids")
    if not isinstance(source_run_ids, dict):
        return ["bundle cohort seed 缺少 source_run_ids"]
    is_v3 = expected.get("schema_version") == 2 and expected.get("snapshot_schema_version") == 3
    source_roles = {"aramkit", "blitz", "apex", "mayhem"}
    if (not set(source_run_ids).issubset(source_roles) or any(not isinstance(value, str) or not value for value in source_run_ids.values())
            or (is_v3 and "aramkit" not in source_run_ids)
            or (not is_v3 and set(source_run_ids) != source_roles)):
        return ["bundle cohort seed 来源 run identity 不完整或无效"]
    candidate = None
    if is_v3:
        try:
            candidate = validate_generation_cohort(root, generation_id)
            if (candidate.snapshot_schema_version != 3 or dict(candidate.units) != expected.get("units")
                    or {source: pointer.get("run_id") for source, pointer in candidate.pointers.items() if source != "catalog"}
                    != source_run_ids):
                return ["v3 runtime cohort unit closure 与部署候选不一致"]
        except (OSError, TypeError, ValueError, SnapshotValidationError) as exc:
            return [f"v3 runtime cohort unit closure 无效 error={type(exc).__name__}"]
    expected_fields = {
        Path("catalog/current.v2.json"): (
            "catalog_generation_id",
            recognition_catalog_id,
        ),
        Path("snapshots/current.v2.json"): ("current_generation_id", generation_id),
        **{
            Path(f"sources/{source}/current.v2.json"): ("run_id", str(source_run_ids.get(source) or ""))
            for source in (source_run_ids if is_v3 else ("aramkit", "blitz", "apex", "mayhem"))
        },
    }
    for relative, (field, value) in expected_fields.items():
        try:
            payload = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"cohort current 不可读 path={relative.as_posix()} error={type(exc).__name__}")
            continue
        actual = str(payload.get(field) or "") if isinstance(payload, dict) else ""
        if actual != value:
            errors.append(f"cohort current 不一致 path={relative.as_posix()} expected={value} actual={actual}")
        if relative.parts[0] == "sources" and isinstance(payload, dict):
            if candidate is not None:
                source_name = relative.parts[1]
                binding = candidate.pointers[source_name]
                if any(payload.get(key) != binding.get(key) for key in ("source", "manifest_sha256", "artifact")):
                    errors.append(f"v3 cohort source pointer 绑定不一致 source={source_name}")
            actual_catalog = str(payload.get("catalog_generation_id") or "")
            if actual_catalog != catalog_id:
                errors.append(
                    f"cohort source Catalog 不一致 path={relative.as_posix()} expected={catalog_id} actual={actual_catalog}"
                )
    schedule_path = root / "state" / "data-service" / "refresh_schedule.v1.json"
    try:
        schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cohort schedule 不可读 error={type(exc).__name__}")
        return errors
    if not isinstance(schedule, dict) or str(schedule.get("generation_id") or "") != generation_id:
        errors.append(f"cohort schedule generation 不一致 actual={getattr(schedule, 'get', lambda *_: '')('generation_id')}")
        return errors
    states = schedule.get("sources")
    if not isinstance(states, dict):
        errors.append("cohort schedule 缺少 sources")
        return errors
    expected_runs = {
        "catalog": recognition_catalog_id,
        **{key: str(value) for key, value in source_run_ids.items()},
    }
    for source, run_id in expected_runs.items():
        state = states.get(source)
        if not isinstance(state, dict):
            errors.append(f"cohort schedule 缺少来源：{source}")
            continue
        schedule_state = str(state.get("state") or "")
        if schedule_state not in {"ready", "due"} or str(state.get("failure_kind") or ""):
            errors.append(f"cohort schedule 来源状态无效：{source} state={schedule_state}")
        if str(state.get("current_run_id") or "") != run_id:
            errors.append(f"cohort schedule run 不一致 source={source}")
    for relative, field in (
        (Path("state/data-service/cohort_recovery_point.v1.json"), "generation_id"),
    ):
        path = root / relative
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"cohort 状态不可读 path={relative.as_posix()} error={type(exc).__name__}")
            continue
        actual = str(payload.get(field) or "") if isinstance(payload, dict) else ""
        if actual != generation_id:
            errors.append(
                f"cohort 状态 generation 不一致 path={relative.as_posix()} "
                f"expected={generation_id} actual={actual}"
            )
    checkpoint_path = root / "state" / "data-service" / "refresh_checkpoint.v1.json"
    # v3 incremental refresh proves the active state through the validated
    # generation/unit closure and refresh_schedule above.  The retired v1
    # checkpoint is historical evidence only; legacy cohorts still require
    # its original consistency checks.
    if not is_v3 and checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"refresh checkpoint 不可读 error={type(exc).__name__}")
        else:
            errors.extend(
                refresh_checkpoint_errors(
                    checkpoint,
                    catalog_id,
                    current_generation_id=generation_id,
                )
            )
    return errors


def _process_role(command_line: tuple[str, ...]) -> str | None:
    folded = {token.casefold() for token in command_line}
    if folded & TRANSIENT_PROCESS_FLAGS:
        return None
    for role, flag in PROCESS_ROLE_FLAGS.items():
        if flag in folded:
            return role
    return "desktop"


def _deployment_process_errors(executable: Path) -> tuple[list[str], dict[str, int]]:
    expected = _normalized_path(executable)
    errors: list[str] = []
    role_pids: dict[str, list[int]] = {"desktop": [], **{role: [] for role in PROCESS_ROLE_FLAGS}}
    for identity in _matching_deployment_processes():
        if not identity.executable or _normalized_path(identity.executable) != expected:
            errors.append(
                f"存在非稳定目录运行时 pid={identity.pid} path={identity.executable or '<unreadable>'}"
            )
            continue
        role = _process_role(identity.command_line)
        if role is not None:
            role_pids[role].append(identity.pid)
    for role, pids in role_pids.items():
        if len(pids) != 1:
            errors.append(f"角色数量不一致 role={role} count={len(pids)} pids={pids}")
    return errors, {role: pids[0] for role, pids in role_pids.items() if len(pids) == 1}


def _runtime_build_errors(
    *,
    expected_build_id: str,
    launch_started_at: float,
    sidecar_pid: int | None,
    desktop_pid: int | None = None,
    expected_debug_dump_enabled: bool | None = None,
    expected_cohort: dict[str, object] | None = None,
    cohort_validation_cache: dict[tuple[str, str], CohortCandidate] | None = None,
) -> list[str]:
    root = _packaged_var_dir()
    errors: list[str] = []
    effective_cohort = expected_cohort
    launch_generation_id = ""
    if expected_cohort is not None:
        effective_cohort, resolution_errors = _resolve_runtime_cohort_expected(
            root,
            expected_cohort,
            validation_cache=cohort_validation_cache,
        )
        errors.extend(resolution_errors)
        if not resolution_errors:
            launch_generation_id, launch_errors = validated_launch_generation(
                root, expected_cohort, build_id=expected_build_id, launch_started_at=launch_started_at,
                desktop_pid=desktop_pid, current_generation_id=str(effective_cohort.get("generation_id") or ""),
            )
            errors.extend(launch_errors)
            errors.extend(_runtime_cohort_errors(root, effective_cohort))
    payloads: dict[Path, dict[str, object]] = {}
    for relative_path, schema_version in RUNTIME_BUILD_STATE_SPECS:
        path = root / relative_path
        try:
            stat = path.stat()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"运行态不可读 path={relative_path.as_posix()} error={type(exc).__name__}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"运行态不是对象 path={relative_path.as_posix()}")
            continue
        payloads[relative_path] = payload
        if stat.st_mtime < launch_started_at - 1.0:
            errors.append(f"运行态未由本次启动刷新 path={relative_path.as_posix()}")
        if int(payload.get("schema_version") or 0) != schema_version:
            errors.append(
                f"运行态协议不一致 path={relative_path.as_posix()} "
                f"expected={schema_version} actual={payload.get('schema_version')}"
            )
        if str(payload.get("build_id") or "") != expected_build_id:
            errors.append(
                f"运行态 Build ID 不一致 path={relative_path.as_posix()} "
                f"actual={payload.get('build_id')}"
            )
    sidecar_path = Path("state/game_overlay_sidecar_status.json")
    sidecar = payloads.get(sidecar_path)
    if sidecar is not None:
        if sidecar.get("status") != "running":
            errors.append(f"Sidecar 尚未运行 status={sidecar.get('status')}")
        if sidecar_pid is not None and int(sidecar.get("pid") or 0) != sidecar_pid:
            errors.append(f"Sidecar PID 不一致 state={sidecar.get('pid')} process={sidecar_pid}")
        if (
            expected_debug_dump_enabled is not None
            and sidecar.get("debug_dump_enabled") is not expected_debug_dump_enabled
        ):
            errors.append(
                "Sidecar ROI 诊断状态不一致 "
                f"expected={expected_debug_dump_enabled} actual={sidecar.get('debug_dump_enabled')}"
            )
        if effective_cohort is not None:
            from tooling.build.recognition_contract import recognition_pool_contract

            try:
                recognition = recognition_pool_contract(root, effective_cohort)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(f"Sidecar recognition Catalog 无效：{type(exc).__name__}: {exc}")
                recognition = {}
            expected_pool_count = int(recognition.get("production_pool_count") or 0)
            accepted_generation_ids = {
                str(effective_cohort.get("generation_id") or ""),
                str((expected_cohort or {}).get("generation_id") or ""),
                launch_generation_id,
            }
            accepted_generation_ids.discard("")
            for field in ("data_generation_id", "vision_pool_generation_id"):
                actual_generation = str(sidecar.get(field) or "")
                if actual_generation not in accepted_generation_ids:
                    errors.append(
                        "Sidecar cohort 不一致 "
                        f"field={field} expected_one_of={sorted(accepted_generation_ids)} "
                        f"actual={actual_generation}"
                    )
            expected_fields = {
                **recognition,
                "production_pool_state": "ready",
                "rank_identity_count": expected_pool_count,
            }
            for field, expected in expected_fields.items():
                actual = sidecar.get(field)
                if actual != expected:
                    errors.append(f"Sidecar cohort 不一致 field={field} expected={expected} actual={actual}")
            matrix_rows = sidecar.get("matrix_rows")
            from hextech.modules.acquisition.hextech.production_pool import production_capability_status_valid

            invalid_matrices = (not production_capability_status_valid(sidecar, expected_pool_count)
                if sidecar.get("production_pool_schema_version") == 2 else not isinstance(matrix_rows, dict) or any(
                int(matrix_rows.get(channel) or 0) < expected_pool_count
                for channel in ("icon", "name", "alt_name")
            ))
            if invalid_matrices:
                errors.append(f"Sidecar 生产矩阵行数不足：{matrix_rows}")
            excluded = sidecar.get("excluded_reason_counts")
            if not isinstance(excluded, dict) or any(
                int(excluded.get(reason) or 0) != 0
                for reason in ("unresolved", "duplicate", "name_conflict")
            ):
                errors.append(f"Sidecar 生产池仍有未解决身份：{excluded}")
    if effective_cohort is not None and sidecar is not None:
        runtime_generation_id = str(effective_cohort.get("generation_id") or "")
        bundle_generation_id = str((expected_cohort or {}).get("generation_id") or "")
        accepted_generation_ids = {runtime_generation_id, bundle_generation_id, launch_generation_id}
        accepted_generation_ids.discard("")
        vision_generation_id = str(sidecar.get("vision_pool_generation_id") or "")
        stats_generation_id, pin_errors = validated_game_stats_generation(
            root, payloads.get(Path("state/game_overlay_visibility.v1.json"), {}),
            payloads.get(Path("reports/overlay_sessions/latest.json"), {}), runtime_generation_id,
        )
        errors.extend(pin_errors)
        generation_expectations = {
            Path("state/game_overlay_visibility.v1.json"): {
                "data_generation_id": stats_generation_id,
                "stats_generation_id": stats_generation_id,
                "vision_pool_generation_id": vision_generation_id,
            },
            Path("reports/overlay_sessions/latest.json"): {
                "generation_id": stats_generation_id,
                "stats_generation_id": stats_generation_id,
                "vision_pool_generation_id": vision_generation_id,
            },
        }
        for relative, fields in generation_expectations.items():
            payload = payloads.get(relative)
            if payload is None:
                continue
            for field, expected_value in fields.items():
                actual_value = str(payload.get(field) or "")
                if actual_value != expected_value:
                    errors.append(
                        "运行态 generation role 不一致 "
                        f"path={relative.as_posix()} field={field} "
                        f"expected={expected_value} actual={actual_value}"
                    )
        event = payloads.get(Path("state/game_overlay_slots.v1.json"))
        event_source = event.get("source") if isinstance(event, dict) else None
        if isinstance(event_source, dict):
            requires_vision_generation = bool(
                event_source.get("selection_window_active") is True
                or str(event.get("selection_type") or "") == "hextech"
                or event_source.get("data_generation_id")
                or event_source.get("vision_pool_generation_id")
            )
            for field in ("data_generation_id", "vision_pool_generation_id"):
                actual_value = str(event_source.get(field) or "")
                if requires_vision_generation and actual_value != vision_generation_id:
                    errors.append(
                        "Overlay event Vision generation 不一致 "
                        f"field={field} expected={vision_generation_id} actual={actual_value}"
                    )
        selection_path = root / "state" / "data-service" / "cohort_selection.v1.json"
        if selection_path.exists():
            try:
                selection = json.loads(selection_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"cohort selection 不可读 error={type(exc).__name__}")
            else:
                selected = (
                    str(selection.get("selected_generation_id") or "")
                    if isinstance(selection, dict)
                    else ""
                )
                writer_build = (
                    str(selection.get("writer_build_id") or "")
                    if isinstance(selection, dict)
                    else ""
                )
                if selected not in accepted_generation_ids:
                    errors.append(
                        "cohort selection generation 不一致 "
                        f"accepted={sorted(accepted_generation_ids)} actual={selected}"
                    )
                if writer_build != expected_build_id:
                    errors.append(
                        "cohort selection Build 不一致 "
                        f"expected={expected_build_id} actual={writer_build}"
                    )
    return errors


def verify_deployment(
    executable: Path,
    *,
    expected_build_id: str,
    launch_started_at: float,
    expected_debug_dump_enabled: bool | None = None,
    expected_cohort: dict[str, object] | None = None,
    timeout: float = DEPLOYMENT_VERIFY_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """等待稳定目录五个常驻角色与五份运行态身份同时收敛，否则部署失败。"""

    deadline = time.monotonic() + max(0.1, timeout)
    last_errors: list[str] = ["尚未开始验收"]
    cohort_validation_cache: dict[tuple[str, str], CohortCandidate] = {}
    while True:
        process_errors, role_pids = _deployment_process_errors(executable)
        runtime_errors = _runtime_build_errors(
            expected_build_id=expected_build_id,
            launch_started_at=launch_started_at,
            sidecar_pid=role_pids.get("vision_sidecar"),
            desktop_pid=role_pids.get("desktop"),
            expected_debug_dump_enabled=expected_debug_dump_enabled,
            expected_cohort=expected_cohort,
            cohort_validation_cache=cohort_validation_cache,
        )
        last_errors = process_errors + runtime_errors
        if not last_errors:
            return role_pids
        if time.monotonic() >= deadline:
            raise DeploymentError(f"部署后验收失败：{' | '.join(last_errors)}")
        time.sleep(0.25)


def _remove_tree(path: Path) -> None:
    if path.exists():
        if _is_reparse_point(path):
            raise DeploymentError(f"拒绝删除 reparse point：{path}")
        shutil.rmtree(path)


def _is_transient_windows_filesystem_error(exc: OSError) -> bool:
    """进程已退出后 Windows 仍可能短暂保留映像或目录句柄。"""

    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 145}


def _retry_rollback_filesystem(action) -> None:
    """只为回滚目录操作提供有界 transient-busy 重试，不吞掉永久错误。"""

    deadline = time.monotonic() + ROLLBACK_FILESYSTEM_RETRY_SECONDS
    while True:
        try:
            action()
            return
        except OSError as exc:
            if not _is_transient_windows_filesystem_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(ROLLBACK_FILESYSTEM_RETRY_INTERVAL_SECONDS)


def _remove_tree_for_rollback(path: Path) -> None:
    _retry_rollback_filesystem(lambda: _remove_tree(path))


def _replace_for_rollback(source: Path, target: Path) -> None:
    _retry_rollback_filesystem(lambda: os.replace(source, target))


def _backup_previous_install(previous: Path, backup: Path) -> bool:
    """为 `.previous` 建立经校验的临时备份，供轮转失败时恢复。"""

    if not previous.exists():
        return False
    if _is_reparse_point(previous):
        raise DeploymentError(f"拒绝备份 reparse point：{previous}")
    try:
        shutil.copytree(previous, backup)
        if _tree_fingerprint(previous) != _tree_fingerprint(backup):
            raise DeploymentError("紧急回滚目录备份校验失败")
    except Exception:
        # copytree 也可能在半途失败；临时副本不是可用回滚版本，不能残留成
        # 第二个看似正式的目录。
        _remove_tree(backup)
        raise
    return True


@dataclass(frozen=True)
class _DiagnosticSettingsSnapshot:
    path: Path
    existed: bool
    content: bytes


def _snapshot_diagnostic_settings() -> _DiagnosticSettingsSnapshot:
    """原样保存设置；损坏 JSON 也必须在部署失败时可恢复。"""

    # 部署器运行在源码 Python 中，但目标 EXE 的可写 var 固定在用户目录；不能
    # 误用源码态 run/var，否则部署验收会看到持久开关仍为关闭。
    path = overlay_diagnostic_settings_path(_packaged_var_dir())
    try:
        return _DiagnosticSettingsSnapshot(path=path, existed=True, content=path.read_bytes())
    except FileNotFoundError:
        return _DiagnosticSettingsSnapshot(path=path, existed=False, content=b"")
    except OSError as exc:
        raise DeploymentError(f"ROI 诊断设置无法备份：{path}: {exc}") from exc


def _restore_diagnostic_settings(snapshot: _DiagnosticSettingsSnapshot) -> None:
    """原子恢复部署前字节；原文件不存在时只移除本轮创建的设置。"""

    path = snapshot.path
    if not snapshot.existed:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.restore-{os.getpid()}")
    try:
        temporary.write_bytes(snapshot.content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def deploy_release(
    package_dir: Path,
    install_dir: Path,
    *,
    shortcut_path: Path | None = None,
    roi_dump_mode: RoiDumpMode = "preserve",
    shutdown_timeout: float = 12.0,
    lock_path: Path | None = None,
) -> DeploymentResult:
    """强停全部旧运行时，原子切换后启动稳定目录，并以运行态验收收口。"""

    source = validate_package_dir(package_dir)
    target = validate_install_dir(install_dir)
    source_manifest = json.loads((source / "_internal" / "bundle_manifest.json").read_text(encoding="utf-8"))
    expected_build_id = str(source_manifest["build_id"])
    expected_cohort = _expected_cohort(source_manifest)
    if roi_dump_mode not in {"preserve", "on", "off"}:
        raise DeploymentError(f"ROI dump mode 无效：{roi_dump_mode}")
    resolved_shortcut_input = validate_shortcut_path(shortcut_path) if shortcut_path is not None else None
    removed_shortcuts: tuple[Path, ...] = ()
    if _normalized_path(source) == _normalized_path(target):
        raise DeploymentError("发布目录不能与稳定安装目录相同")
    lock_file = Path(lock_path or default_deployment_lock())
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = target.with_name(f".{target.name}.deploying-{os.getpid()}-{stamp}")
    rollback = target.with_name(f".{target.name}.rollback-{os.getpid()}-{stamp}")
    previous = target.with_name(f"{target.name}.previous")
    previous_backup = target.with_name(f".{target.name}.previous-backup-{os.getpid()}-{stamp}")
    try:
        lock = FileLock(str(lock_file), timeout=0)
        with lock:
            if candidate.exists() or rollback.exists() or previous_backup.exists():
                raise DeploymentError("部署临时目录已存在")
            # 候选复制、manifest 和逐文件 hash 必须全部通过后，才允许关闭旧客户端。
            shutil.copytree(source, candidate)
            validate_package_dir(candidate)
            if _tree_fingerprint(source) != _tree_fingerprint(candidate):
                raise DeploymentError("部署候选复制校验失败")
            _deployment_step(f"候选复制校验通过 build_id={expected_build_id}")

            was_running = shutdown_existing_install(target / APP_EXE_NAME, timeout=shutdown_timeout)
            _deployment_step("旧版、便携版与源码识别进程已全部退出")
            old_moved = False
            new_installed = False
            previous_backup_created = False
            previous_rotated = False
            previous_rotation_started = False
            verified_process_ids: dict[str, int] = {}
            diagnostic_settings_changed = roi_dump_mode != "preserve"
            diagnostic_snapshot = _snapshot_diagnostic_settings() if diagnostic_settings_changed else None
            cohort_state_snapshot = _snapshot_runtime_cohort_state() if expected_cohort is not None else ()
            try:
                # `.previous` 是唯一紧急回滚目录。轮转前先复制并校验，避免
                # Windows 重命名短暂失败时因已删除旧目录而失去最后一个回滚版本。
                if target.exists():
                    previous_backup_created = _backup_previous_install(previous, previous_backup)
                    _deployment_step("紧急回滚目录备份校验通过")
                if target.exists():
                    _replace_for_rollback(target, rollback)
                    old_moved = True
                    _deployment_step("旧稳定安装已移入部署回滚目录")
                os.replace(candidate, target)
                new_installed = True
                validate_package_dir(target)
                _deployment_step(f"新版本已落盘：{target}")
                resolved_shortcut = (
                    update_shortcut(resolved_shortcut_input, target / APP_EXE_NAME)
                    if resolved_shortcut_input is not None
                    else None
                )
                # 只有候选已完成目录切换与规范快捷方式更新，才移除重复入口。
                # shutdown 失败时这里尚未执行，因此旧安装与用户桌面保持原状；后续
                # 清理失败则由下面的回滚路径恢复旧安装。
                if resolved_shortcut_input is not None:
                    removed_shortcuts = converge_desktop_shortcuts(
                        resolved_shortcut_input,
                        target / APP_EXE_NAME,
                        source.parent,
                    )
                if diagnostic_settings_changed:
                    assert diagnostic_snapshot is not None
                    set_roi_dump_mode(roi_dump_mode, path=diagnostic_snapshot.path)
                    _deployment_step(f"Overlay ROI 诊断模式已设置为：{roi_dump_mode}")
                launch_started_at = time.time()
                _start_install(target / APP_EXE_NAME)
                _deployment_step(f"已请求从稳定目录启动：{target / APP_EXE_NAME}")
                verified_process_ids = verify_deployment(
                    target / APP_EXE_NAME,
                    expected_build_id=expected_build_id,
                    launch_started_at=launch_started_at,
                    expected_debug_dump_enabled=(
                        None if roi_dump_mode == "preserve" else roi_dump_mode == "on"
                    ),
                    expected_cohort=expected_cohort,
                    timeout=max(DEPLOYMENT_VERIFY_TIMEOUT_SECONDS, shutdown_timeout),
                )
                _deployment_step(f"进程、协议与 Build 身份验收通过：{verified_process_ids}")
                # 候选已通过进程与 Build 身份验收后才轮转 emergency rollback。若现有 previous
                # 无法删除，下面的异常路径会恢复 target，不能为了发布先丢掉唯一
                # 回滚目录。
                if old_moved and rollback.exists():
                    previous_rotation_started = previous_backup_created
                    _remove_tree(previous)
                    _replace_for_rollback(rollback, previous)
                    previous_rotated = True
                    old_moved = False
                    if previous_backup_created:
                        _remove_tree(previous_backup)
                        previous_backup_created = False
            except Exception as exc:
                rollback_errors: list[str] = []
                old_install_available = False
                if new_installed:
                    try:
                        shutdown_existing_install(target / APP_EXE_NAME, timeout=3.0)
                    except Exception as cleanup_exc:
                        rollback_errors.append(f"关闭新版本失败：{cleanup_exc}")
                    try:
                        _remove_tree_for_rollback(target)
                    except Exception as cleanup_exc:
                        rollback_errors.append(f"移除新版本失败：{cleanup_exc}")
                if old_moved and rollback.exists():
                    try:
                        _replace_for_rollback(rollback, target)
                        old_install_available = True
                    except Exception as restore_exc:
                        rollback_errors.append(f"恢复上一版本失败：{restore_exc}")
                elif previous_rotated and previous.exists():
                    try:
                        # `.previous` 已暂存刚替换下来的正式版本；发布失败时把它
                        # 移回稳定目录，再由临时备份恢复原 `.previous`。
                        _replace_for_rollback(previous, target)
                        old_install_available = True
                    except Exception as restore_exc:
                        rollback_errors.append(f"恢复上一版本失败：{restore_exc}")
                elif not old_moved and not new_installed and target.is_dir():
                    old_install_available = True
                if previous_backup_created and previous_rotation_started:
                    try:
                        previous_unchanged = previous.exists() and (
                            _tree_fingerprint(previous) == _tree_fingerprint(previous_backup)
                        )
                    except Exception:
                        previous_unchanged = False
                    if previous_unchanged:
                        # 删除在触及文件前失败时，原 `.previous` 仍完整；只清除临时
                        # 备份即可，避免为一个 busy 目录再次破坏可用回滚版本。
                        try:
                            _remove_tree(previous_backup)
                            previous_backup_created = False
                        except Exception as cleanup_exc:
                            rollback_errors.append(f"清理紧急回滚临时备份失败：{cleanup_exc}")
                    else:
                        try:
                            _remove_tree_for_rollback(previous)
                        except Exception as cleanup_exc:
                            rollback_errors.append(f"清理不完整紧急回滚目录失败：{cleanup_exc}")
                        if previous_backup_created and not previous.exists():
                            try:
                                _replace_for_rollback(previous_backup, previous)
                                previous_backup_created = False
                            except Exception as restore_exc:
                                rollback_errors.append(f"恢复原紧急回滚目录失败：{restore_exc}")
                        elif previous_backup_created:
                            rollback_errors.append("无法恢复原紧急回滚目录：当前 .previous 仍被占用")
                elif previous_backup_created:
                    try:
                        _remove_tree(previous_backup)
                        previous_backup_created = False
                    except Exception as cleanup_exc:
                        rollback_errors.append(f"清理紧急回滚临时备份失败：{cleanup_exc}")
                if diagnostic_settings_changed:
                    try:
                        assert diagnostic_snapshot is not None
                        _restore_diagnostic_settings(diagnostic_snapshot)
                    except Exception as restore_exc:
                        rollback_errors.append(f"恢复 ROI 诊断设置失败：{restore_exc}")
                if cohort_state_snapshot:
                    try:
                        _restore_runtime_cohort_state(cohort_state_snapshot)
                    except Exception as restore_exc:
                        rollback_errors.append(f"恢复运行态 cohort 指针失败：{restore_exc}")
                if was_running and old_install_available:
                    try:
                        _start_install(target / APP_EXE_NAME)
                    except Exception as restart_exc:
                        rollback_errors.append(f"重启上一版本失败：{restart_exc}")
                if rollback_errors:
                    raise DeploymentError(
                        f"部署失败且回滚不完整：deploy={exc}; rollback={' | '.join(rollback_errors)}"
                    ) from exc
                raise

            previous_result = previous if previous.exists() else None
            return DeploymentResult(
                install_dir=target,
                previous_dir=previous_result,
                shortcut_path=resolved_shortcut,
                removed_shortcuts=removed_shortcuts,
                restarted=was_running,
                started=True,
                verified=True,
                process_ids=tuple(sorted(verified_process_ids.items())),
                build_id=expected_build_id,
            )
    except Timeout as exc:
        raise DeploymentError(f"已有部署任务持有锁：{lock_file}") from exc
    finally:
        _remove_tree(candidate)


def main(argv: list[str] | None = None) -> int:
    """提供可直接 UAC 提权的部署入口，避免外层 PowerShell 控制台中断。"""

    parser = argparse.ArgumentParser(description="部署 Hextech 稳定客户端并执行运行态硬验收")
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, default=default_install_dir())
    parser.add_argument("--shortcut", type=Path)
    parser.add_argument(
        "--roi-dump-mode",
        choices=("preserve", "on", "off"),
        default="preserve",
        help="部署后的受限 ROI 诊断模式，默认 preserve。",
    )
    parser.add_argument("--shutdown-timeout", type=float, default=12.0)
    args = parser.parse_args(argv)
    if args.shutdown_timeout <= 0:
        parser.error("--shutdown-timeout 必须大于 0")
    result = deploy_release(
        args.package_dir,
        args.install_dir,
        shortcut_path=args.shortcut,
        roi_dump_mode=args.roi_dump_mode,
        shutdown_timeout=args.shutdown_timeout,
    )
    print(
        json.dumps(
            {
                "ok": result.verified,
                "install_dir": str(result.install_dir),
                "previous_dir": str(result.previous_dir) if result.previous_dir is not None else "",
                "build_id": result.build_id,
                "process_ids": dict(result.process_ids),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


__all__ = [
    "APP_EXE_NAME",
    "DeploymentError",
    "DeploymentResult",
    "converge_desktop_shortcuts",
    "default_install_dir",
    "deploy_release",
    "shutdown_existing_install",
    "update_shortcut",
    "verify_deployment",
    "validate_install_dir",
    "validate_package_dir",
    "validate_shortcut_path",
]


if __name__ == "__main__":
    raise SystemExit(main())

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
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.release_store import closure
from hextech.modules.data.generation.validation import SnapshotValidationError
from hextech.modules.vision.diagnostic_settings import (
    RoiDumpMode,
    overlay_diagnostic_settings_path,
    set_roi_dump_mode,
)
from tooling.build.manifest import BUNDLE_MANIFEST_SCHEMA_VERSION, RUNTIME_CONTRACT_VERSIONS
from tooling.build.deploy_lineage import validated_launch_generation, validated_game_stats_generation


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
    "overlay_host": "--game-overlay",
    "vision_sidecar": "--overlay-sidecar",
}
TRANSIENT_PROCESS_FLAGS = frozenset({"--acquisition-worker"})
RUNTIME_BUILD_STATE_SPECS = (
    (Path("state/startup_timing.v1.json"), 1),
    (Path("state/game_overlay_sidecar_status.json"), 2),
    (Path("state/game_overlay_visibility.v1.json"), 2),
    (Path("reports/overlay_sessions/latest.json"), 2),
)
RUNTIME_COHORT_STATE_FILES = (
    Path("snapshots/current.v4.json"),
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
    # resolve() 会丢失原路径的链接身份；lstat 也能识别目标已不存在的链接。
    # 允许创建不存在的普通目录，但任何无法核验的现有路径都必须失败闭合。
    for component in (raw, *raw.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeploymentError(f"无法核验部署路径：{component}") from exc
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise DeploymentError(f"部署路径不得包含符号链接或 reparse point：{component}")
    try:
        target = raw.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise DeploymentError(f"无法核验部署路径：{raw}") from exc
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
    cohort = manifest.get("cohort_seed")
    if not isinstance(cohort, dict) or not cohort:
        raise DeploymentError("bundle manifest lacks verified v4 seed metadata")
    if cohort:
        from tooling.build.cohort_seed import collect_cohort_seed
        try:
            seed = collect_cohort_seed(root / "_internal/resources/seeds/runtime.v4/snapshots")
            if cohort != seed.metadata:
                raise ValueError("seed metadata differs from verified closure")
            hashes = manifest.get("seed_sha256")
            names = {seed.bundled_name(path): path for path in seed.files}
            if not isinstance(hashes, dict) or set(hashes) != set(names):
                raise ValueError("seed hash manifest does not cover exact closure")
            from hextech.modules.data.catalog.versioned import sha256_file
            if any(sha256_file(path) != hashes[name] for name, path in names.items()):
                raise ValueError("seed file differs from bundled hash")
        except (OSError, ValueError, RuntimeError) as exc:
            raise DeploymentError(f"bundle v4 seed validation failed: {exc}") from exc
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

    raw = Path(shortcut_path).expanduser()
    if raw.suffix.casefold() != ".lnk":
        raise DeploymentError("快捷方式必须使用 .lnk 后缀")
    # 先检查 raw 路径；resolve 后再检查会丢失 junction/symlink 身份。
    for component in (raw, *raw.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeploymentError(f"无法核验快捷方式路径：{component}") from exc
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise DeploymentError(f"快捷方式路径不得包含符号链接或 reparse point：{component}")
    shortcut_path = raw.resolve(strict=False)
    if not shortcut_path.parent.is_dir():
        raise DeploymentError("快捷方式父目录不存在")
    if not shortcut_path.is_file():
        raise DeploymentError(f"既有快捷方式不存在，拒绝创建：{shortcut_path}")
    return shortcut_path


def require_main_branch_for_stable_deploy() -> None:
    """standalone deploy.py 也必须从 main 执行，无法证明时拒绝。"""

    completed = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    branch = completed.stdout.strip() if completed.returncode == 0 else ""
    if branch != "main":
        raise DeploymentError(
            "稳定部署仅允许从 main 分支执行："
            f"current={branch or 'detached-or-unknown'}"
        )


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


def _expected_cohort(source_manifest: dict[str, object], *, seed_runtime: Path | None = None) -> dict[str, object] | None:
    cohort = source_manifest.get("cohort_seed")
    return {**cohort, "_source_fingerprint": source_manifest.get("source_fingerprint", ""), **({"_seed_runtime": str(seed_runtime)} if seed_runtime else {})} if isinstance(cohort, dict) and cohort else None


def _resolve_runtime_cohort_expected(root: Path, expected: dict[str, object], *, validation_cache=None) -> tuple[dict[str, object], list[str]]:
    """Validate each role independently, preserving fully proven newer runtime state."""
    try:
        from hextech.modules.data.generation.release_store import checked_file
        from tooling.build.recognition_contract import recognition_catalog_view
        baseline_root = Path(str(expected.get("_seed_runtime") or root))
        baseline = DataSnapshotClient(baseline_root / "snapshots").open_generation(str(expected["generation_id"]))
        baseline_catalog, *_ = closure(baseline_root, baseline.manifest)
        if baseline_catalog.generation_id != expected.get("catalog_generation_id"):
            raise ValueError("bundle statistics Catalog does not match baseline")
        if expected.get("statistics_manifest_sha256"):
            checked_file(baseline_root, f"snapshots/generations/{baseline.manifest.generation_id}/manifest.json",
                         digest=expected["statistics_manifest_sha256"])
        baseline_recognition = recognition_catalog_view(baseline_root, {"recognition_catalog": expected["recognition_catalog"]})
        if baseline_recognition.generation_id != expected.get("recognition_catalog_generation_id"):
            raise ValueError("bundle recognition Catalog does not match baseline")
        view = DataSnapshotClient(root / "snapshots").open_view()
        actual_statistics_catalog, *_ = closure(root, view.manifest)
        actual_recognition = recognition_catalog_view(root)
        def timestamp(value):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError("role timestamp requires timezone")
            return result
        if timestamp(view.manifest.created_at) < timestamp(baseline.manifest.created_at):
            raise ValueError("runtime statistics release predates bundle seed")
        if timestamp(actual_statistics_catalog.manifest.created_at) < timestamp(baseline_catalog.manifest.created_at):
            raise ValueError("runtime statistics Catalog predates bundle seed")
        if timestamp(actual_recognition.manifest.created_at) < timestamp(baseline_recognition.manifest.created_at):
            raise ValueError("runtime recognition Catalog predates bundle seed")
        from tooling.build.cohort_seed import collect_cohort_seed
        actual = collect_cohort_seed(root / "snapshots").metadata
        return {**expected, **actual}, []
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return dict(expected), [f"v4 runtime release validation failed: {type(exc).__name__}: {exc}"]


def _runtime_cohort_errors(root: Path, expected: dict[str, object]) -> list[str]:
    try:
        from tooling.build.cohort_seed import collect_cohort_seed
        actual = collect_cohort_seed(root / "snapshots").metadata
        for key in ("generation_id", "catalog_generation_id", "recognition_catalog_generation_id", "recognition_catalog", "production_pool_id", "production_pool_count", "units"):
            if actual.get(key) != expected.get(key):
                raise ValueError(f"release closure mismatch: {key}")
        return []
    except (OSError, ValueError, RuntimeError) as exc:
        return [f"v4 runtime release invalid: {type(exc).__name__}: {exc}"]


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
    cohort_validation_cache: dict[tuple[str, str], object] | None = None,
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
    """等待稳定目录三个常驻角色与五份运行态身份同时收敛，否则部署失败。"""

    deadline = time.monotonic() + max(0.1, timeout)
    last_errors: list[str] = ["尚未开始验收"]
    cohort_validation_cache: dict[tuple[str, str], object] = {}
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

    require_main_branch_for_stable_deploy()
    source = validate_package_dir(package_dir)
    target = validate_install_dir(install_dir)
    source_manifest = json.loads((source / "_internal" / "bundle_manifest.json").read_text(encoding="utf-8"))
    expected_build_id = str(source_manifest["build_id"])
    expected_cohort = _expected_cohort(source_manifest, seed_runtime=source / "_internal/resources/seeds/runtime.v4")
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
    preserve_recovery_state = False
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
                        # 新进程可能仍在写共享运行态。此时不能删目录、恢复指针/
                        # 诊断设置或重启旧版；连 finally 清理也必须保留恢复现场。
                        preserve_recovery_state = True
                        raise DeploymentError(
                            f"部署失败且回滚已停止：deploy={exc}; 关闭新版本失败：{cleanup_exc}; "
                            f"恢复现场已保留：target={target}; rollback={rollback}; "
                            f"previous={previous}; previous_backup={previous_backup}"
                        ) from exc
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
        if not preserve_recovery_state:
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
    require_main_branch_for_stable_deploy()
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
    "require_main_branch_for_stable_deploy",
]


if __name__ == "__main__":
    raise SystemExit(main())

"""Windows 稳定安装目录部署器。

这个模块只由显式 ``--deploy`` 构建调用。它不负责生成发布包，也不读取
Hextech 用户数据；职责是关闭目标安装中的进程、校验候选目录并把它切换到
稳定的 ``HextechCompanion`` 路径。目录切换失败时恢复上一版本。
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

from tooling.build.manifest import BUNDLE_MANIFEST_SCHEMA_VERSION, RUNTIME_CONTRACT_VERSIONS


APP_EXE_NAME = "Hextech伴生终端.exe"
APP_LAUNCHER_NAME = "启动 Hextech.bat"
APP_GUIDE_NAME = "README_首次使用.txt"
APP_SHORTCUT_NAME = "Hextech伴生终端.lnk"
STABLE_INSTALL_NAME = "HextechCompanion"
RELEASE_DIR_PREFIX = "HextechCompanion-"
DEPLOYMENT_VERIFY_TIMEOUT_SECONDS = 150.0
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
RUNTIME_BUILD_STATE_SPECS = (
    (Path("state/startup_timing.v1.json"), 1),
    (Path("state/game_overlay_sidecar_status.json"), 2),
    (Path("state/game_overlay_slots.v1.json"), 3),
    (Path("state/game_overlay_visibility.v1.json"), 2),
    (Path("reports/overlay_sessions/latest.json"), 2),
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


def _process_role(command_line: tuple[str, ...]) -> str:
    folded = {token.casefold() for token in command_line}
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
        role_pids[_process_role(identity.command_line)].append(identity.pid)
    for role, pids in role_pids.items():
        if len(pids) != 1:
            errors.append(f"角色数量不一致 role={role} count={len(pids)} pids={pids}")
    return errors, {role: pids[0] for role, pids in role_pids.items() if len(pids) == 1}


def _runtime_build_errors(
    *,
    expected_build_id: str,
    launch_started_at: float,
    sidecar_pid: int | None,
) -> list[str]:
    root = _packaged_var_dir()
    errors: list[str] = []
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
    return errors


def verify_deployment(
    executable: Path,
    *,
    expected_build_id: str,
    launch_started_at: float,
    timeout: float = DEPLOYMENT_VERIFY_TIMEOUT_SECONDS,
) -> dict[str, int]:
    """等待稳定目录五个角色与五份运行态身份同时收敛，否则部署失败。"""

    deadline = time.monotonic() + max(0.1, timeout)
    last_errors: list[str] = ["尚未开始验收"]
    while True:
        process_errors, role_pids = _deployment_process_errors(executable)
        runtime_errors = _runtime_build_errors(
            expected_build_id=expected_build_id,
            launch_started_at=launch_started_at,
            sidecar_pid=role_pids.get("vision_sidecar"),
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


def deploy_release(
    package_dir: Path,
    install_dir: Path,
    *,
    shortcut_path: Path | None = None,
    shutdown_timeout: float = 12.0,
    lock_path: Path | None = None,
) -> DeploymentResult:
    """强停全部旧运行时，原子切换后启动稳定目录，并以运行态验收收口。"""

    source = validate_package_dir(package_dir)
    target = validate_install_dir(install_dir)
    source_manifest = json.loads((source / "_internal" / "bundle_manifest.json").read_text(encoding="utf-8"))
    expected_build_id = str(source_manifest["build_id"])
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
            try:
                # `.previous` 是唯一紧急回滚目录。轮转前先复制并校验，避免
                # Windows 重命名短暂失败时因已删除旧目录而失去最后一个回滚版本。
                if target.exists():
                    previous_backup_created = _backup_previous_install(previous, previous_backup)
                    _deployment_step("紧急回滚目录备份校验通过")
                if target.exists():
                    os.replace(target, rollback)
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
                launch_started_at = time.time()
                _start_install(target / APP_EXE_NAME)
                _deployment_step(f"已请求从稳定目录启动：{target / APP_EXE_NAME}")
                verified_process_ids = verify_deployment(
                    target / APP_EXE_NAME,
                    expected_build_id=expected_build_id,
                    launch_started_at=launch_started_at,
                    timeout=max(DEPLOYMENT_VERIFY_TIMEOUT_SECONDS, shutdown_timeout),
                )
                _deployment_step(f"进程、协议与 Build 身份验收通过：{verified_process_ids}")
                # 候选已通过进程与 Build 身份验收后才轮转 emergency rollback。若现有 previous
                # 无法删除，下面的异常路径会恢复 target，不能为了发布先丢掉唯一
                # 回滚目录。
                if old_moved and rollback.exists():
                    previous_rotation_started = previous_backup_created
                    _remove_tree(previous)
                    os.replace(rollback, previous)
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
                        _remove_tree(target)
                    except Exception as cleanup_exc:
                        rollback_errors.append(f"移除新版本失败：{cleanup_exc}")
                if old_moved and rollback.exists():
                    try:
                        os.replace(rollback, target)
                        old_install_available = True
                    except Exception as restore_exc:
                        rollback_errors.append(f"恢复上一版本失败：{restore_exc}")
                elif previous_rotated and previous.exists():
                    try:
                        # `.previous` 已暂存刚替换下来的正式版本；发布失败时把它
                        # 移回稳定目录，再由临时备份恢复原 `.previous`。
                        os.replace(previous, target)
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
                            _remove_tree(previous)
                        except Exception as cleanup_exc:
                            rollback_errors.append(f"清理不完整紧急回滚目录失败：{cleanup_exc}")
                        if previous_backup_created and not previous.exists():
                            try:
                                os.replace(previous_backup, previous)
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
    parser.add_argument("--shutdown-timeout", type=float, default=12.0)
    args = parser.parse_args(argv)
    if args.shutdown_timeout <= 0:
        parser.error("--shutdown-timeout 必须大于 0")
    result = deploy_release(
        args.package_dir,
        args.install_dir,
        shortcut_path=args.shortcut,
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

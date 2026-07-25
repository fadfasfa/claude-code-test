"""Windows 稳定安装目录部署器。

这个模块只由显式 ``--deploy`` 构建调用。它不负责生成发布包，也不读取
Hextech 用户数据；职责是关闭目标安装中的进程、校验候选目录并把它切换到
稳定的 ``HextechCompanion`` 路径。目录切换失败时恢复上一版本。
"""

from __future__ import annotations

import ctypes
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
WM_CLOSE = 0x0010


class DeploymentError(RuntimeError):
    """部署候选不可安全提升或回滚时抛出。"""


@dataclass(frozen=True)
class ProcessIdentity:
    """防止等待期间 PID 复用导致误终止。"""

    pid: int
    create_time: float


@dataclass(frozen=True)
class DeploymentResult:
    install_dir: Path
    previous_dir: Path | None
    shortcut_path: Path | None
    removed_shortcuts: tuple[Path, ...]
    restarted: bool
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


def _matching_processes(executable: Path) -> list[ProcessIdentity]:
    expected = _normalized_path(executable)
    matches: list[ProcessIdentity] = []
    for process in psutil.process_iter(("pid", "exe", "create_time")):
        try:
            process_exe = str(process.info.get("exe") or "")
            if process_exe and _normalized_path(process_exe) == expected:
                matches.append(ProcessIdentity(int(process.pid), float(process.info["create_time"])))
        except (psutil.Error, OSError, TypeError, ValueError):
            continue
    return matches


def _same_process(identity: ProcessIdentity, executable: Path) -> psutil.Process | None:
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.create_time) > 0.01:
            return None
        if _normalized_path(process.exe()) != _normalized_path(executable):
            return None
        return process
    except (psutil.Error, OSError):
        return None


def _post_close_to_windows(pids: set[int]) -> int:
    """向指定 PID 的所有顶层窗口发送 WM_CLOSE，包括被隐藏的 Tk 窗口。"""

    if os.name != "nt" or not pids:
        return 0
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    sent = 0

    def callback(hwnd, _lparam):
        nonlocal sent
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) in pids:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            sent += 1
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return sent


def _wait_for_exit(identities: list[ProcessIdentity], executable: Path, timeout: float) -> list[ProcessIdentity]:
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = list(identities)
    while remaining and time.monotonic() < deadline:
        remaining = [item for item in remaining if _same_process(item, executable) is not None]
        if remaining:
            time.sleep(0.1)
    return remaining


def shutdown_existing_install(executable: Path, *, timeout: float = 12.0) -> bool:
    """先请求正常关闭，再只对同路径且同启动时间的残留 PID 升级终止。"""

    identities = _matching_processes(executable)
    if not identities:
        return False
    _post_close_to_windows({item.pid for item in identities})
    remaining = _wait_for_exit(identities, executable, timeout)
    if remaining:
        for identity in remaining:
            process = _same_process(identity, executable)
            if process is not None:
                process.terminate()
        remaining = _wait_for_exit(remaining, executable, 3.0)
    if remaining:
        for identity in remaining:
            process = _same_process(identity, executable)
            if process is not None:
                process.kill()
        remaining = _wait_for_exit(remaining, executable, 3.0)
    if remaining:
        raise DeploymentError(f"目标程序仍未退出：pids={[item.pid for item in remaining]}")
    return True


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
    """删除同应用的非规范桌面入口，并在停机前验证唯一性。"""

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
    creationflags = int(subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent), creationflags=creationflags)
    time.sleep(1.0)
    if process.poll() is not None:
        raise DeploymentError(f"新客户端启动后提前退出：code={process.returncode}")
    return process


def _remove_tree(path: Path) -> None:
    if path.exists():
        if _is_reparse_point(path):
            raise DeploymentError(f"拒绝删除 reparse point：{path}")
        shutil.rmtree(path)


def deploy_release(
    package_dir: Path,
    install_dir: Path,
    *,
    shortcut_path: Path | None = None,
    shutdown_timeout: float = 12.0,
    lock_path: Path | None = None,
) -> DeploymentResult:
    """先校验候选再切换稳定目录；旧客户端原本运行时才重启。"""

    source = validate_package_dir(package_dir)
    target = validate_install_dir(install_dir)
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
    try:
        lock = FileLock(str(lock_file), timeout=0)
        with lock:
            if candidate.exists() or rollback.exists():
                raise DeploymentError("部署临时目录已存在")
            # 候选复制、manifest 和逐文件 hash 必须全部通过后，才允许关闭旧客户端。
            shutil.copytree(source, candidate)
            validate_package_dir(candidate)
            if _tree_fingerprint(source) != _tree_fingerprint(candidate):
                raise DeploymentError("部署候选复制校验失败")

            # 旧 previous 无法收口时应在停机前失败，避免新版本已运行却返回部署失败。
            _remove_tree(previous)
            if resolved_shortcut_input is not None:
                removed_shortcuts = converge_desktop_shortcuts(
                    resolved_shortcut_input,
                    target / APP_EXE_NAME,
                    source.parent,
                )
            was_running = shutdown_existing_install(target / APP_EXE_NAME, timeout=shutdown_timeout)
            old_moved = False
            new_installed = False
            try:
                if target.exists():
                    os.replace(target, rollback)
                    old_moved = True
                os.replace(candidate, target)
                new_installed = True
                validate_package_dir(target)
                resolved_shortcut = (
                    update_shortcut(resolved_shortcut_input, target / APP_EXE_NAME)
                    if resolved_shortcut_input is not None
                    else None
                )
                if was_running:
                    _start_install(target / APP_EXE_NAME)
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
                elif not old_moved and not new_installed and target.is_dir():
                    old_install_available = True
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

            previous_result = None
            if old_moved and rollback.exists():
                os.replace(rollback, previous)
                previous_result = previous
            return DeploymentResult(
                install_dir=target,
                previous_dir=previous_result,
                shortcut_path=resolved_shortcut,
                removed_shortcuts=removed_shortcuts,
                restarted=was_running,
                build_id=str(
                    json.loads((target / "_internal" / "bundle_manifest.json").read_text(encoding="utf-8"))["build_id"]
                ),
            )
    except Timeout as exc:
        raise DeploymentError(f"已有部署任务持有锁：{lock_file}") from exc
    finally:
        _remove_tree(candidate)


__all__ = [
    "APP_EXE_NAME",
    "DeploymentError",
    "DeploymentResult",
    "converge_desktop_shortcuts",
    "default_install_dir",
    "deploy_release",
    "shutdown_existing_install",
    "update_shortcut",
    "validate_install_dir",
    "validate_package_dir",
    "validate_shortcut_path",
]

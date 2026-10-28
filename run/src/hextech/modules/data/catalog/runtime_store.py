"""运行时私有路径与来源暂态定位。

文件职责：
- 统一管理运行时文件定位、CSV 读取优先级和 DataFrame 缓存

核心输入：
- ``var/sources/*/current.v2.json`` 指向的完整来源 run
- 源码态或打包内 ``resources/catalog``

核心输出：
- 标准化后的 DataFrame
- 运行时文件路径解析结果

主要依赖：
- `hextech.modules.data.ports.paths`
- `hextech.modules.data.catalog.precomputed_cache`

维护提醒：
- Web 和 UI 对 CSV 的读取都应经由这里，避免各自实现路径和缓存策略

调用方: catalog.aliases、catalog.precomputed_cache、catalog.query_terminal; 关键依赖: pandas、scraping._paths、catalog.precomputed_cache。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional


from hextech.modules.data.ports.paths import RUNTIME_DATA_DIR
from hextech.modules.data.source_runs import resolve_current_artifact, source_root

APEX_CURRENT_POINTER_FILENAME = "current.v2.json"
SOURCE_POINTER_VERSION = 2
SYNERGY_REFRESH_STATUS_FILENAME = "synergy_refresh_status.json"

logger = logging.getLogger(__name__)
_PRIVATE_PERMISSION_WARNING_PATHS: set[str] = set()


def get_runtime_root_dir() -> Path:
    """返回运行态可变数据根目录。"""
    return Path(RUNTIME_DATA_DIR)


def get_runtime_state_dir() -> Path:
    """返回运行态状态文件目录。"""
    return get_runtime_root_dir() / "state"


def get_runtime_cache_dir() -> Path:
    """返回运行态缓存目录。"""
    return get_runtime_root_dir() / "cache"


def get_runtime_debug_dir() -> Path:
    """返回不会进入 Git 或发布包的运行态诊断目录。"""
    return get_runtime_root_dir() / "reports"


def get_runtime_lock_dir() -> Path:
    """返回运行态锁文件目录。"""
    return get_runtime_root_dir() / "locks"


def get_runtime_profile_dir() -> Path:
    """返回运行态浏览器 profile 目录。"""
    return get_runtime_root_dir() / "profiles"


def get_runtime_user_preferences_dir() -> Path:
    """返回用户可变偏好目录；不得混入 immutable snapshots。"""

    return get_runtime_root_dir() / "user-data" / "preferences"


def _join_under_dir(base_dir: Path, relative_name: str) -> Path:
    """拼接受控运行路径，拒绝绝对路径和上级目录穿越。"""
    candidate = (base_dir / relative_name).resolve()
    root = base_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"运行态路径越界：{relative_name}")
    return candidate


def _warn_private_permission_failure(path: Path, exc: Exception) -> None:
    key = str(path)
    if key in _PRIVATE_PERMISSION_WARNING_PATHS:
        return
    _PRIVATE_PERMISSION_WARNING_PATHS.add(key)
    logger.warning(
        "运行态私有权限收敛失败，沿用系统默认权限：path=%s error_type=%s",
        path,
        exc.__class__.__name__,
    )


def _restrict_windows_current_user(path: Path) -> None:
    """用 pywin32 尽量把运行态文件/目录收敛到当前用户可访问。"""
    import ntsecuritycon
    import win32api
    import win32security

    user_sid, _, _ = win32security.LookupAccountName(None, win32api.GetUserName())
    dacl = win32security.ACL()
    inherit_flags = win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        inherit_flags,
        ntsecuritycon.FILE_ALL_ACCESS,
        user_sid,
    )
    security_descriptor = win32security.SECURITY_DESCRIPTOR()
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    win32security.SetFileSecurity(
        str(path),
        win32security.DACL_SECURITY_INFORMATION,
        security_descriptor,
    )


def _tighten_private_runtime_path(path: Path, *, is_dir: bool) -> None:
    """best-effort 收敛运行态敏感路径权限；失败不阻塞主流程。"""
    try:
        if os.name == "nt":
            _restrict_windows_current_user(path)
            return
        os.chmod(path, 0o700 if is_dir else 0o600)
    except Exception as exc:
        _warn_private_permission_failure(path, exc)


def ensure_private_runtime_dir(directory: str | Path) -> Path:
    """创建并尽量收敛运行态私有目录权限。"""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    _tighten_private_runtime_path(path, is_dir=True)
    return path


def ensure_runtime_state_dir() -> Path:
    """创建运行态 state 目录，并按私有目录处理权限。"""
    return ensure_private_runtime_dir(get_runtime_state_dir())


def ensure_runtime_profile_dir() -> Path:
    """创建运行态浏览器 profile 根目录，并按私有目录处理权限。"""
    return ensure_private_runtime_dir(get_runtime_profile_dir())


def write_private_runtime_state_file(filename: str, content: str) -> str:
    """原子写入私有 state 文件，避免 token/端口文件落在宽权限目录中。"""
    target_path = Path(build_runtime_state_path(filename))
    ensure_private_runtime_dir(target_path.parent)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{target_path.stem}-",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        _tighten_private_runtime_path(Path(tmp_path), is_dir=False)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(content))
        os.replace(tmp_path, target_path)
        _tighten_private_runtime_path(target_path, is_dir=False)
        return str(target_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def build_runtime_state_path(filename: str) -> str:
    """生成运行态状态文件路径。"""
    return str(_join_under_dir(get_runtime_state_dir(), filename))


def build_runtime_cache_path(filename: str) -> str:
    """生成运行态缓存文件路径。"""
    return str(_join_under_dir(get_runtime_cache_dir(), filename))


def build_runtime_debug_path(filename: str) -> str:
    """生成受控调试文件路径，拒绝绝对路径和上级目录穿越。"""

    return str(_join_under_dir(get_runtime_debug_dir(), filename))


def build_runtime_lock_path(filename: str) -> str:
    """生成运行态锁文件路径。"""
    return str(_join_under_dir(get_runtime_lock_dir(), filename))


def build_runtime_profile_path(dirname: str) -> str:
    """生成运行态 profile 目录路径。"""
    return str(_join_under_dir(get_runtime_profile_dir(), dirname))


def build_runtime_user_preference_path(filename: str) -> str:
    """生成受控用户偏好文件路径。"""

    return str(_join_under_dir(get_runtime_user_preferences_dir(), filename))


def resolve_runtime_data_file(runtime_path: str) -> Optional[str]:
    """解析唯一运行态数据文件。"""
    return runtime_path if os.path.exists(runtime_path) else None


def get_runtime_synergy_data_dir() -> Path:
    """返回 Apex 来源 run 目录。"""
    return source_root("apex") / "runs"


def build_synergy_cleaned_data_path() -> str:
    """联动候选只来自已发布来源，不再写入 catalog。"""
    artifact = resolve_current_artifact("apex")
    return str(artifact or (source_root("apex") / "unavailable.json"))


def build_synergy_latest_pointer_path() -> str:
    """返回 Apex 来源 current 指针。"""
    return str(source_root("apex") / "current.v2.json")


def build_synergy_refresh_status_path() -> str:
    """返回协同刷新状态路径，用于记录 Apex blocked cooldown。"""
    return build_runtime_state_path(SYNERGY_REFRESH_STATUS_FILENAME)


def build_synergy_snapshot_path(timestamp_label: str) -> str:
    """按 run ID 生成 Apex artifact 路径。"""
    safe_label = str(timestamp_label or "").strip()
    if not re.fullmatch(r"\d{8}_\d{6}(?:_\d{2})?", safe_label):
        raise ValueError(f"协同快照时间标签非法：{timestamp_label}")
    return str(source_root("apex") / "runs" / safe_label / "synergy.json")


def load_synergy_latest_pointer() -> dict:
    """读取 Apex current 指针；损坏或 hash 不匹配时返回空字典。"""
    from hextech.modules.data.source_runs import load_source_current

    return load_source_current("apex")


def load_synergy_refresh_status() -> dict:
    """读取协同刷新状态；缺失或损坏时返回空字典。"""
    status_path = build_synergy_refresh_status_path()
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get_latest_synergy_snapshot_path() -> Optional[str]:
    """返回已发布 Apex artifact，不扫描失败 run。"""

    artifact = resolve_current_artifact("apex")
    return str(artifact) if artifact else None


def build_raw_synergy_data_path() -> str:
    """返回 Apex current artifact。"""
    return build_synergy_cleaned_data_path()


def build_synergy_data_path() -> str:
    """返回 DataService 可读取的已发布 Apex artifact。"""
    return build_raw_synergy_data_path()


def build_next_synergy_snapshot_path(timestamp_label: str) -> str:
    """生成不覆盖已有 Apex run artifact 的路径。"""
    base_path = Path(build_synergy_snapshot_path(timestamp_label))
    if not base_path.exists():
        return str(base_path)
    for index in range(1, 100):
        candidate = Path(build_synergy_snapshot_path(f"{timestamp_label}_{index:02d}"))
        if not candidate.exists():
            return str(candidate)
    raise FileExistsError(f"协同快照文件名连续冲突：{base_path.name}")

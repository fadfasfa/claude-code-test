from __future__ import annotations

"""运行时数据定位与 DataFrame 缓存。

文件职责：
- 统一管理运行时文件定位、CSV 读取优先级和 DataFrame 缓存

核心输入：
- 运行态 `data/runtime/**`
- 源码态或打包内 `data/static/version`

核心输出：
- 标准化后的 DataFrame
- 运行时文件路径解析结果

主要依赖：
- `hextech.scraping._paths`
- `hextech.catalog.precomputed_cache`

维护提醒：
- Web 和 UI 对 CSV 的读取都应经由这里，避免各自实现路径和缓存策略
"""

import glob
import json
import logging
import os
import re
import sys
import threading
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

import pandas as pd

from hextech.scraping._paths import BASE_DIR, BUNDLE_ROOT_DIR, STATIC_DATA_DIR

CSV_ENCODING = "utf-8-sig"
CSV_FILENAME_PATTERN = "Hextech_Data_*.csv"
CSV_MIN_VALID_ROWS = 300
SYNERGY_LEGACY_FILENAME = "Champion_Synergy.json"
SYNERGY_CLEANED_FILENAME = "Champion_Synergy_Cleaned.json"
SYNERGY_LATEST_POINTER_FILENAME = "Champion_Synergy_latest.v1.json"
SYNERGY_SNAPSHOT_PREFIX = "Champion_Synergy_"
SYNERGY_SNAPSHOT_PATTERN = "Champion_Synergy_*.json"
SYNERGY_POINTER_VERSION = 1
SYNERGY_REFRESH_STATUS_FILENAME = "synergy_refresh_status.json"
_SYNERGY_SNAPSHOT_RE = re.compile(r"^Champion_Synergy_\d{8}_\d{6}(?:_\d{2})?\.json$")
CSV_REQUIRED_COLUMNS = (
    "英雄ID",
    "英雄名称",
    "英雄评级",
    "英雄胜率",
    "英雄出场率",
    "海克斯阶级",
    "海克斯名称",
    "海克斯胜率",
    "海克斯出场率",
    "胜率差",
    "综合得分",
)

logger = logging.getLogger(__name__)
_PRIVATE_PERMISSION_WARNING_PATHS: set[str] = set()
_VALID_CSV_CACHE: dict[tuple[str, int], tuple[int, int, bool]] = {}
_VALID_CSV_CACHE_LOCK = threading.Lock()


def runtime_priority_paths(relative_name: str) -> list[str]:
    """返回稳定数据优先路径列表，源码态和打包态都以 data/static/version 为事实源。"""
    runtime_path = Path(STATIC_DATA_DIR) / relative_name
    candidates = [str(runtime_path)]
    for bundled_path in (
        Path(BUNDLE_ROOT_DIR) / "data" / "static" / "version" / relative_name,
        Path(BUNDLE_ROOT_DIR) / "data" / "static" / relative_name,
    ):
        bundled = str(bundled_path)
        if bundled not in candidates:
            candidates.append(bundled)
    return candidates


def _get_packaged_runtime_base_dir() -> Path:
    """返回打包场景下用户可写的运行态根目录。"""
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "HextechNexus"
    app_data = os.getenv("APPDATA", "").strip()
    if app_data:
        return Path(app_data) / "HextechNexus"
    return Path.home() / ".hextech_nexus"



def get_runtime_root_dir() -> Path:
    """返回运行态可变数据根目录。"""
    if getattr(sys, "frozen", False):
        return _get_packaged_runtime_base_dir() / "data" / "runtime"
    return Path(BASE_DIR) / "data" / "runtime"


def get_runtime_state_dir() -> Path:
    """返回运行态状态文件目录。"""
    return get_runtime_root_dir() / "state"


def get_runtime_cache_dir() -> Path:
    """返回运行态缓存目录。"""
    return get_runtime_root_dir() / "cache"


def get_runtime_debug_dir() -> Path:
    """返回不会进入 Git 或发布包的运行态诊断目录。"""

    return get_runtime_root_dir() / "debug"


def get_runtime_lock_dir() -> Path:
    """返回运行态锁文件目录。"""
    return get_runtime_root_dir() / "locks"


def get_runtime_profile_dir() -> Path:
    """返回运行态浏览器 profile 目录。"""
    return get_runtime_root_dir() / "profile"


def get_runtime_persisted_dir() -> Path:
    """返回运行态生成型持久化数据目录。"""
    return get_runtime_root_dir() / "persisted"


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


def build_runtime_persisted_path(filename: str) -> str:
    """生成运行态生成型持久化文件路径。"""
    return str(_join_under_dir(get_runtime_persisted_dir(), filename))


def runtime_data_fallback_paths(runtime_path: str, legacy_relative_name: str) -> list[str]:
    """返回运行态可变数据读取路径列表，只读取 data/runtime 主链路。"""

    return [runtime_path]


def resolve_runtime_data_file(runtime_path: str, legacy_relative_name: str) -> Optional[str]:
    """解析运行态可变数据文件，只读取 data/runtime 主链路。"""
    for candidate in runtime_data_fallback_paths(runtime_path, legacy_relative_name):
        if os.path.exists(candidate):
            return candidate
    return None


def resolve_runtime_file(relative_name: str) -> Optional[str]:
    """按运行时优先级解析一个文件的实际可用路径。"""
    for candidate in runtime_priority_paths(relative_name):
        if os.path.exists(candidate):
            return candidate
    return None


def get_runtime_data_dir() -> Path:
    """返回高频原始运行数据根目录；源码态和冻结态都收口到 data/runtime/raw。"""
    return get_runtime_root_dir() / "raw"


def _runtime_raw_dirs(subdir: str) -> list[Path]:
    return [get_runtime_data_dir() / subdir]


def get_runtime_hextech_data_dir() -> Path:
    """返回战报 CSV 原始数据目录。"""
    return get_runtime_data_dir() / "hextech"


def get_runtime_synergy_data_dir() -> Path:
    """返回协同原始数据目录。"""
    return get_runtime_data_dir() / "synergy"


def is_synergy_snapshot_filename(filename: str) -> bool:
    """判断文件名是否是协同数据时间快照，排除 latest 指针和旧固定名。"""
    return bool(_SYNERGY_SNAPSHOT_RE.fullmatch(str(filename or "").strip()))


def build_synergy_legacy_data_path() -> str:
    """返回运行态固定协同数据文件路径，仅作为 raw latest 兜底。"""
    return str(get_runtime_synergy_data_dir() / SYNERGY_LEGACY_FILENAME)


def build_synergy_cleaned_data_path() -> str:
    """返回前端优先读取的清洗后协同数据路径。"""
    return str(Path(STATIC_DATA_DIR) / SYNERGY_CLEANED_FILENAME)


def build_synergy_latest_pointer_path() -> str:
    """返回协同 latest 指针路径。"""
    return str(get_runtime_synergy_data_dir() / SYNERGY_LATEST_POINTER_FILENAME)


def build_synergy_refresh_status_path() -> str:
    """返回协同刷新状态路径，用于记录 Apex blocked cooldown。"""
    return build_runtime_state_path(SYNERGY_REFRESH_STATUS_FILENAME)


def build_synergy_snapshot_path(timestamp_label: str) -> str:
    """按时间标签生成协同数据快照路径。"""
    safe_label = str(timestamp_label or "").strip()
    if not re.fullmatch(r"\d{8}_\d{6}(?:_\d{2})?", safe_label):
        raise ValueError(f"协同快照时间标签非法：{timestamp_label}")
    return str(get_runtime_synergy_data_dir() / f"{SYNERGY_SNAPSHOT_PREFIX}{safe_label}.json")


def iter_synergy_snapshot_files() -> list[str]:
    """列出所有合法协同时间快照文件。"""
    files = []
    seen: set[str] = set()
    for snapshot_dir in _runtime_raw_dirs("synergy"):
        for path in snapshot_dir.glob(SYNERGY_SNAPSHOT_PATTERN):
            key = str(path.resolve())
            if key not in seen and path.is_file() and is_synergy_snapshot_filename(path.name):
                files.append(str(path))
                seen.add(key)
    files.sort(key=lambda item: (os.path.getmtime(item), os.path.basename(item)), reverse=True)
    return files


def load_synergy_latest_pointer() -> dict:
    """读取 latest 指针；指针损坏时返回空字典。"""
    for directory in _runtime_raw_dirs("synergy"):
        pointer_path = directory / SYNERGY_LATEST_POINTER_FILENAME
        try:
            with open(pointer_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        return payload if isinstance(payload, dict) else {}
    return {}


def load_synergy_refresh_status() -> dict:
    """读取协同刷新状态；缺失或损坏时返回空字典。"""
    status_path = build_synergy_refresh_status_path()
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_synergy_snapshot_from_pointer(pointer: dict | None = None) -> Optional[str]:
    """解析 latest 指针指向的时间快照路径。"""
    payload = pointer if isinstance(pointer, dict) else load_synergy_latest_pointer()
    if not isinstance(payload, dict) or payload.get("version") != SYNERGY_POINTER_VERSION:
        return None
    filename = os.path.basename(str(payload.get("filename") or "").strip())
    if not is_synergy_snapshot_filename(filename):
        return None
    for directory in _runtime_raw_dirs("synergy"):
        candidate = (directory / filename).resolve()
        root = directory.resolve()
        if candidate.parent == root and candidate.exists():
            return str(candidate)
    return None


def get_latest_synergy_snapshot_path() -> Optional[str]:
    """返回 latest 指针指向的快照；指针不可用时扫描最新合法快照。"""
    pointed = resolve_synergy_snapshot_from_pointer()
    if pointed:
        return pointed
    snapshots = iter_synergy_snapshot_files()
    return snapshots[0] if snapshots else None


def build_raw_synergy_data_path() -> str:
    """返回 Apex/raw 协同数据路径，不包含 cleaned 前端合并文件。"""
    snapshot = get_latest_synergy_snapshot_path()
    if snapshot:
        return snapshot
    return build_synergy_legacy_data_path()


def build_synergy_data_path() -> str:
    """返回当前可读协同数据路径，cleaned 前端文件优先，raw latest 兜底。"""
    cleaned = build_synergy_cleaned_data_path()
    if os.path.exists(cleaned):
        return cleaned
    return build_raw_synergy_data_path()


def build_next_synergy_snapshot_path(timestamp_label: str) -> str:
    """生成不覆盖已有文件的下一份协同快照路径。"""
    base_path = Path(build_synergy_snapshot_path(timestamp_label))
    if not base_path.exists():
        return str(base_path)
    for index in range(1, 100):
        candidate = Path(build_synergy_snapshot_path(f"{timestamp_label}_{index:02d}"))
        if not candidate.exists():
            return str(candidate)
    raise FileExistsError(f"协同快照文件名连续冲突：{base_path.name}")


def build_daily_csv_path(date_str: str) -> str:
    """按统一命名规则生成每日战报 CSV 路径。"""
    return str(get_runtime_hextech_data_dir() / f"Hextech_Data_{date_str}.csv")


def iter_runtime_csv_files() -> list[str]:
    """列出运行原始数据目录中的战报 CSV 文件。"""
    files: list[str] = []
    seen: set[str] = set()
    for directory in _runtime_raw_dirs("hextech"):
        for path in glob.glob(str(directory / CSV_FILENAME_PATTERN)):
            key = str(Path(path).resolve())
            if key not in seen:
                files.append(path)
                seen.add(key)
    return files


def _runtime_csv_sort_key(path: str) -> tuple[str, float]:
    """优先按文件名日期排序，同日快照再按 mtime 排序。"""

    match = re.search(r"Hextech_Data_(\d{4})-?(\d{2})-?(\d{2})", os.path.basename(path))
    date_key = "".join(match.groups()) if match else ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return date_key, mtime


def _runtime_csv_is_valid(path: str, min_rows: int = CSV_MIN_VALID_ROWS) -> bool:
    """校验 schema 与最小数据量，并按文件 stat 缓存结果。"""

    try:
        stat = os.stat(path)
    except OSError:
        return False
    cache_key = (stat.st_mtime_ns, stat.st_size)
    validation_key = (path, min_rows)
    with _VALID_CSV_CACHE_LOCK:
        cached = _VALID_CSV_CACHE.get(validation_key)
        if cached and cached[:2] == cache_key:
            return cached[2]

    try:
        valid = len(load_runtime_csv(path)) >= min_rows
    except (OSError, ValueError, TypeError, pd.errors.ParserError, UnicodeError):
        valid = False

    with _VALID_CSV_CACHE_LOCK:
        _VALID_CSV_CACHE[validation_key] = (cache_key[0], cache_key[1], valid)
    return valid


def get_latest_valid_csv(min_rows: int = CSV_MIN_VALID_ROWS) -> Optional[str]:
    """返回最新有效战报；新文件损坏时自动检查更早版本。"""

    files = sorted(iter_runtime_csv_files(), key=_runtime_csv_sort_key, reverse=True)
    for path in files:
        if _runtime_csv_is_valid(path, min_rows=min_rows):
            return path
        logger.info("忽略无效运行 CSV：%s", os.path.basename(path))
    return None


def get_latest_csv() -> Optional[str]:
    """返回最新运行 CSV，保留 UI/Web 可展示部分数据的兼容语义。"""

    files = sorted(iter_runtime_csv_files(), key=_runtime_csv_sort_key, reverse=True)
    return files[0] if files else None


def validate_runtime_csv_schema(
    df: pd.DataFrame,
    source: str = "",
    required_columns: Sequence[str] = CSV_REQUIRED_COLUMNS,
) -> None:
    """校验运行 CSV 的核心列，避免下游计算阶段才暴露 KeyError。"""
    if df.empty:
        return
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        detail = f"，source={source}" if source else ""
        raise ValueError(f"运行 CSV 缺少核心列：{missing}{detail}，当前列：{df.columns.tolist()}")


def load_runtime_csv(path: str) -> pd.DataFrame:
    """按统一编码读取并标准化运行 CSV。"""
    df = pd.read_csv(path, encoding=CSV_ENCODING)
    normalized = normalize_runtime_df(df)
    validate_runtime_csv_schema(normalized, source=os.path.basename(path))
    return normalized


def load_latest_runtime_df() -> pd.DataFrame:
    """读取最新运行 CSV；没有 CSV 时返回空 DataFrame。"""
    latest = get_latest_csv()
    if not latest:
        return pd.DataFrame()
    return load_runtime_csv(latest)


def detect_hero_id_column(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        name = str(col)
        if "英雄ID" in name or name == "ID":
            return name
    return None


def normalize_runtime_df(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名和英雄 ID 字段格式，降低上层视图适配分支。"""
    if df.empty:
        return df

    normalized = df.copy()
    normalized.columns = normalized.columns.astype(str).str.replace(" ", "", regex=False)
    id_col = detect_hero_id_column(normalized)
    if id_col:
        normalized[id_col] = (
            normalized[id_col]
            .astype(str)
            .str.strip()
            .str.replace(".0", "", regex=False)
        )
    if "胜率差" not in normalized.columns and {"海克斯胜率", "英雄胜率"}.issubset(normalized.columns):
        hex_winrate = pd.to_numeric(normalized["海克斯胜率"], errors="coerce")
        hero_winrate = pd.to_numeric(normalized["英雄胜率"], errors="coerce")
        normalized["胜率差"] = (hex_winrate - hero_winrate).fillna(0.0)
    if "综合得分" not in normalized.columns:
        normalized["综合得分"] = 0.0
    return normalized


@dataclass
class DataFrameCache:
    path: str = ""
    mtime: float = 0.0
    df: pd.DataFrame = field(default_factory=pd.DataFrame)


class CachedDataFrameLoader:
    def __init__(self, latest_path_getter: Callable[[], Optional[str]] = get_latest_csv):
        self._latest_path_getter = latest_path_getter
        self._cache = DataFrameCache()
        self._lock = threading.Lock()

    @property
    def cache_key(self) -> Tuple[str, float]:
        return self._cache.path, self._cache.mtime

    @property
    def cached_path(self) -> str:
        return self._cache.path

    @property
    def cached_mtime(self) -> float:
        return self._cache.mtime

    def get_df(self, force_refresh: bool = False) -> pd.DataFrame:
        """按文件路径与 mtime 做缓存，必要时重新解析最新 CSV。"""
        latest = self._latest_path_getter()
        if not latest:
            return pd.DataFrame()

        try:
            current_mtime = os.path.getmtime(latest)
        except OSError:
            return self._cache.df

        with self._lock:
            if (
                force_refresh
                or latest != self._cache.path
                or current_mtime != self._cache.mtime
            ):
                self._cache = DataFrameCache(
                    path=latest,
                    mtime=current_mtime,
                    df=load_runtime_csv(latest),
                )
            return self._cache.df


def load_precomputed_champion_list():
    from hextech.catalog.precomputed_cache import load_precomputed_champion_list as _load

    return _load()


def load_precomputed_hextech_for_hero(hero_name: str):
    from hextech.catalog.precomputed_cache import load_precomputed_hextech_for_hero as _load

    return _load(hero_name)


def has_precomputed_hextech_cache() -> bool:
    from hextech.catalog.precomputed_cache import has_precomputed_hextech_cache as _has

    return _has()


__all__ = [
    "CSV_ENCODING",
    "CSV_FILENAME_PATTERN",
    "CSV_MIN_VALID_ROWS",
    "CSV_REQUIRED_COLUMNS",
    "CachedDataFrameLoader",
    "DataFrameCache",
    "build_daily_csv_path",
    "build_runtime_cache_path",
    "build_runtime_debug_path",
    "build_runtime_lock_path",
    "build_runtime_persisted_path",
    "build_runtime_profile_path",
    "build_runtime_state_path",
    "build_next_synergy_snapshot_path",
    "build_raw_synergy_data_path",
    "build_synergy_cleaned_data_path",
    "build_synergy_data_path",
    "build_synergy_latest_pointer_path",
    "build_synergy_legacy_data_path",
    "build_synergy_refresh_status_path",
    "build_synergy_snapshot_path",
    "detect_hero_id_column",
    "ensure_private_runtime_dir",
    "ensure_runtime_profile_dir",
    "ensure_runtime_state_dir",
    "get_latest_csv",
    "get_latest_valid_csv",
    "get_latest_synergy_snapshot_path",
    "get_runtime_cache_dir",
    "get_runtime_debug_dir",
    "get_runtime_data_dir",
    "get_runtime_hextech_data_dir",
    "get_runtime_synergy_data_dir",
    "get_runtime_lock_dir",
    "get_runtime_persisted_dir",
    "get_runtime_profile_dir",
    "get_runtime_root_dir",
    "get_runtime_state_dir",
    "has_precomputed_hextech_cache",
    "is_synergy_snapshot_filename",
    "iter_runtime_csv_files",
    "iter_synergy_snapshot_files",
    "load_latest_runtime_df",
    "load_precomputed_champion_list",
    "load_precomputed_hextech_for_hero",
    "load_synergy_latest_pointer",
    "load_synergy_refresh_status",
    "load_runtime_csv",
    "normalize_runtime_df",
    "resolve_runtime_data_file",
    "resolve_runtime_file",
    "resolve_synergy_snapshot_from_pointer",
    "runtime_data_fallback_paths",
    "runtime_priority_paths",
    "SYNERGY_LATEST_POINTER_FILENAME",
    "SYNERGY_LEGACY_FILENAME",
    "SYNERGY_POINTER_VERSION",
    "SYNERGY_REFRESH_STATUS_FILENAME",
    "SYNERGY_SNAPSHOT_PATTERN",
    "SYNERGY_SNAPSHOT_PREFIX",
    "validate_runtime_csv_schema",
    "write_private_runtime_state_file",
]

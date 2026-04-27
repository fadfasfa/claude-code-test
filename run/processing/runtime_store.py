from __future__ import annotations

"""运行时数据定位与 DataFrame 缓存。

文件职责：
- 统一管理运行时文件定位、CSV 读取优先级和 DataFrame 缓存

核心输入：
- 运行目录 `config/`
- 打包内稳定资源目录 `RESOURCE_DIR`

核心输出：
- 标准化后的 DataFrame
- 运行时文件路径解析结果

主要依赖：
- `scraping.version_sync`
- `processing.precomputed_cache`

维护提醒：
- Web 和 UI 对 CSV 的读取都应经由这里，避免各自实现路径和缓存策略
"""

import glob
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

import pandas as pd

from scraping.version_sync import BASE_DIR, RESOURCE_DIR, STATIC_DATA_DIR

CSV_ENCODING = "utf-8-sig"
CSV_FILENAME_PATTERN = "Hextech_Data_*.csv"
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


def runtime_priority_paths(relative_name: str) -> list[str]:
    """返回稳定数据优先路径列表，先查本地 data/static，再查 bundle 内置资源。"""
    runtime_path = Path(STATIC_DATA_DIR) / relative_name
    bundled_path = Path(RESOURCE_DIR) / "data" / "static" / relative_name
    candidates = [str(runtime_path)]
    bundled = str(bundled_path)
    if bundled not in candidates:
        candidates.append(bundled)
    return candidates


def get_runtime_root_dir() -> Path:
    """返回运行态可变数据根目录。"""
    return Path(BASE_DIR) / "data" / "runtime"


def get_runtime_state_dir() -> Path:
    """返回运行态状态文件目录。"""
    return get_runtime_root_dir() / "state"


def get_runtime_cache_dir() -> Path:
    """返回运行态缓存目录。"""
    return get_runtime_root_dir() / "cache"


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


def build_runtime_state_path(filename: str) -> str:
    """生成运行态状态文件路径。"""
    return str(_join_under_dir(get_runtime_state_dir(), filename))


def build_runtime_cache_path(filename: str) -> str:
    """生成运行态缓存文件路径。"""
    return str(_join_under_dir(get_runtime_cache_dir(), filename))


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
    """返回运行态可变数据读取路径列表，不再兼容旧 config。"""
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
    """返回高频运行数据根目录。"""
    return Path(BASE_DIR) / "data" / "raw"


def get_runtime_hextech_data_dir() -> Path:
    """返回战报 CSV 原始数据目录。"""
    return get_runtime_data_dir() / "hextech"


def get_runtime_synergy_data_dir() -> Path:
    """返回协同原始数据目录。"""
    return get_runtime_data_dir() / "synergy"


def build_synergy_data_path() -> str:
    """返回协同数据主源文件路径。"""
    return str(get_runtime_synergy_data_dir() / "Champion_Synergy.json")


def build_daily_csv_path(date_str: str) -> str:
    """按统一命名规则生成每日战报 CSV 路径。"""
    return str(get_runtime_hextech_data_dir() / f"Hextech_Data_{date_str}.csv")


def iter_runtime_csv_files() -> list[str]:
    """列出运行原始数据目录中的战报 CSV 文件。"""
    return glob.glob(str(get_runtime_hextech_data_dir() / CSV_FILENAME_PATTERN))


def get_latest_csv() -> Optional[str]:
    """返回最新战报 CSV 的路径，供 Web、UI 和预计算缓存共用。"""
    files = iter_runtime_csv_files()
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


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
    from processing.precomputed_cache import load_precomputed_champion_list as _load

    return _load()


def load_precomputed_hextech_for_hero(hero_name: str):
    from processing.precomputed_cache import load_precomputed_hextech_for_hero as _load

    return _load(hero_name)


def has_precomputed_hextech_cache() -> bool:
    from processing.precomputed_cache import has_precomputed_hextech_cache as _has

    return _has()


__all__ = [
    "CSV_ENCODING",
    "CSV_FILENAME_PATTERN",
    "CSV_REQUIRED_COLUMNS",
    "CachedDataFrameLoader",
    "DataFrameCache",
    "build_daily_csv_path",
    "build_runtime_cache_path",
    "build_runtime_lock_path",
    "build_runtime_persisted_path",
    "build_runtime_profile_path",
    "build_runtime_state_path",
    "build_synergy_data_path",
    "detect_hero_id_column",
    "get_latest_csv",
    "get_runtime_cache_dir",
    "get_runtime_data_dir",
    "get_runtime_hextech_data_dir",
    "get_runtime_synergy_data_dir",
    "get_runtime_lock_dir",
    "get_runtime_persisted_dir",
    "get_runtime_profile_dir",
    "get_runtime_root_dir",
    "get_runtime_state_dir",
    "has_precomputed_hextech_cache",
    "iter_runtime_csv_files",
    "load_latest_runtime_df",
    "load_precomputed_champion_list",
    "load_precomputed_hextech_for_hero",
    "load_runtime_csv",
    "normalize_runtime_df",
    "resolve_runtime_data_file",
    "resolve_runtime_file",
    "runtime_data_fallback_paths",
    "runtime_priority_paths",
    "validate_runtime_csv_schema",
]

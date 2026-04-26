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
from typing import Callable, Optional, Tuple

import pandas as pd

from scraping.version_sync import (
    BASE_DIR,
    CONFIG_DIR,
    DATA_INDEXES_DIR,
    DATA_RAW_DIR,
    DATA_STATIC_DIR,
    RESOURCE_DIR,
)


DATA_ROOT_DIR = Path(BASE_DIR) / "data"
RUNTIME_DATA_DIR = DATA_ROOT_DIR / "runtime"
STABLE_DATA_DIR = DATA_ROOT_DIR / "stable"
LEGACY_CONFIG_DIR = Path(CONFIG_DIR)
BUNDLED_CONFIG_DIR = Path(RESOURCE_DIR) / "config"
HEXTECH_CSV_PATTERN = "Hextech_Data_*.csv"


STABLE_FILE_LOCATIONS = {
    "hero_version.txt": (Path(DATA_STATIC_DIR),),
    "Champion_Core_Data.json": (Path(DATA_STATIC_DIR),),
    "Augment_Full_Map.json": (Path(DATA_STATIC_DIR),),
    "Augment_Icon_Map.json": (Path(DATA_STATIC_DIR),),
    "Augment_Icon_Manifest.json": (Path(DATA_STATIC_DIR),),
    "Champion_Alias_Index.json": (Path(DATA_INDEXES_DIR),),
}


RUNTIME_FILE_LOCATIONS = {
    "Champion_Synergy.json": (Path(DATA_RAW_DIR) / "synergy",),
}


def get_runtime_data_dir() -> Path:
    return RUNTIME_DATA_DIR


def get_stable_data_dir() -> Path:
    return STABLE_DATA_DIR


def get_legacy_config_dir() -> Path:
    return LEGACY_CONFIG_DIR


def get_bundled_config_dir() -> Path:
    return BUNDLED_CONFIG_DIR


def runtime_data_path(relative_name: str) -> str:
    return str(get_runtime_data_dir() / relative_name)


def stable_data_path(relative_name: str) -> str:
    return str(get_stable_data_dir() / relative_name)


def legacy_config_path(relative_name: str) -> str:
    return str(get_legacy_config_dir() / relative_name)


def bundled_config_path(relative_name: str) -> str:
    return str(get_bundled_config_dir() / relative_name)


def _unique_paths(paths: list[Path]) -> list[str]:
    return list(dict.fromkeys(str(path) for path in paths))


def runtime_priority_paths(relative_name: str) -> list[str]:
    """返回运行态优先路径列表：新 runtime → 当前 raw 分层 → 旧 config → bundle config。"""
    paths = [Path(runtime_data_path(relative_name))]
    paths.extend(directory / relative_name for directory in RUNTIME_FILE_LOCATIONS.get(relative_name, ()))
    paths.extend([Path(legacy_config_path(relative_name)), Path(bundled_config_path(relative_name))])
    return _unique_paths(paths)


def stable_priority_paths(relative_name: str) -> list[str]:
    """返回稳定资源优先路径列表：新 stable → 当前 static/indexes 分层 → 旧 config → bundle config。"""
    paths = [Path(stable_data_path(relative_name))]
    paths.extend(directory / relative_name for directory in STABLE_FILE_LOCATIONS.get(relative_name, ()))
    paths.extend([Path(legacy_config_path(relative_name)), Path(bundled_config_path(relative_name))])
    return _unique_paths(paths)


def resolve_runtime_file(relative_name: str) -> Optional[str]:
    """按运行态优先级解析一个文件的实际可用路径。"""
    for candidate in runtime_priority_paths(relative_name):
        if os.path.exists(candidate):
            return candidate
    return None


def resolve_stable_file(relative_name: str) -> Optional[str]:
    """按稳定资源优先级解析一个文件的实际可用路径。"""
    for candidate in stable_priority_paths(relative_name):
        if os.path.exists(candidate):
            return candidate
    return None


def list_runtime_file_candidates(pattern: str) -> list[str]:
    """列出运行态候选文件，顺序为新 runtime → 当前 raw 分层 → 旧 config → bundle config。"""
    matches: list[str] = []
    for directory in (get_runtime_data_dir(), Path(DATA_RAW_DIR) / "hextech", Path(DATA_RAW_DIR), get_legacy_config_dir(), get_bundled_config_dir()):
        matches.extend(glob.glob(str(directory / pattern)))
    return list(dict.fromkeys(matches))


def get_latest_csv() -> Optional[str]:
    """返回最新战报 CSV 的路径，供 Web、UI 和预计算缓存共用。"""
    files = list_runtime_file_candidates(HEXTECH_CSV_PATTERN)
    if not files:
        return None

    def _priority(path: str) -> int:
        resolved = Path(path)
        priority_roots = (
            get_runtime_data_dir(),
            Path(DATA_RAW_DIR) / "hextech",
            Path(DATA_RAW_DIR),
            get_legacy_config_dir(),
            get_bundled_config_dir(),
        )
        for index, root in enumerate(priority_roots):
            try:
                if resolved == root or root in resolved.parents:
                    return index
            except OSError:
                continue
        return len(priority_roots)

    def _sort_key(path: str) -> tuple[int, float]:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = float("-inf")
        return (_priority(path), -mtime)

    files.sort(key=_sort_key)
    return files[0]


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
                df = pd.read_csv(latest)
                self._cache = DataFrameCache(
                    path=latest,
                    mtime=current_mtime,
                    df=normalize_runtime_df(df),
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
    "CachedDataFrameLoader",
    "DataFrameCache",
    "detect_hero_id_column",
    "get_latest_csv",
    "has_precomputed_hextech_cache",
    "load_precomputed_champion_list",
    "load_precomputed_hextech_for_hero",
    "bundled_config_path",
    "get_bundled_config_dir",
    "get_legacy_config_dir",
    "get_runtime_data_dir",
    "get_stable_data_dir",
    "legacy_config_path",
    "list_runtime_file_candidates",
    "normalize_runtime_df",
    "resolve_runtime_file",
    "resolve_stable_file",
    "runtime_data_path",
    "runtime_priority_paths",
    "stable_data_path",
    "stable_priority_paths",
]

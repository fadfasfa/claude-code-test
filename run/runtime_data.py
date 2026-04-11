from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

import pandas as pd


def normalize_runtime_df(df: pd.DataFrame) -> pd.DataFrame:
    # 统一运行期 DataFrame 的列名和英雄编号格式，避免多端各自清洗。
    if df.empty:
        return df

    normalized = df.copy()
    normalized.columns = normalized.columns.astype(str).str.replace(" ", "", regex=False)
    id_column = detect_hero_id_column(normalized)
    if id_column:
        normalized[id_column] = (
            normalized[id_column]
            .astype(str)
            .str.strip()
            .str.replace(".0", "", regex=False)
        )
    return normalized


def detect_hero_id_column(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        col_name = str(col)
        if "英雄ID" in col_name or col_name == "ID":
            return col_name
    return None


@dataclass
class DataFrameCache:
    path: str = ""
    mtime: float = 0.0
    df: pd.DataFrame = field(default_factory=pd.DataFrame)


class CachedDataFrameLoader:
    def __init__(self, latest_path_getter: Callable[[], Optional[str]]):
        self._latest_path_getter = latest_path_getter
        self._lock = threading.Lock()
        self._cache = DataFrameCache()

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

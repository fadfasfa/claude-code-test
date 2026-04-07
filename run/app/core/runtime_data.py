from __future__ import annotations

import glob
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

import pandas as pd

from services.sync_hero_data import CONFIG_DIR


def get_latest_csv() -> Optional[str]:
    files = glob.glob(os.path.join(CONFIG_DIR, "Hextech_Data_*.csv"))
    if not files:
        return None
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]


def detect_hero_id_column(df: pd.DataFrame) -> Optional[str]:
    for col in df.columns:
        name = str(col)
        if "英雄ID" in name or name == "ID":
            return name
    return None


def normalize_runtime_df(df: pd.DataFrame) -> pd.DataFrame:
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

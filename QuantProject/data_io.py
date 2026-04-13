# -*- coding: utf-8 -*-
#
# 共享数据读写与校验模块。
#
from __future__ import annotations

import hashlib
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd

from config import ASSETS_MAPPING, DATA_DIR, MAX_REASONABLE_PRICE

CANONICAL_COLUMNS = ["Data", "Zamkniecie", "Open", "High", "Low", "Volume"]
DATE_COLUMN_ALIASES = {"date", "data", "index"}
CLOSE_COLUMN_ALIASES = {"close", "zamkniecie"}


def asset_file_path(asset_name: str) -> Path:
    return Path(DATA_DIR) / ASSETS_MAPPING[asset_name]["file"]


def sha256_for_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_csv_checksum(path: Path) -> bool:
    checksum_path = Path(f"{path}.sha256")
    if not checksum_path.exists():
        return True
    try:
        expected = checksum_path.read_text(encoding="utf-8").strip()
        return expected == sha256_for_file(path)
    except OSError:
        return False


def write_csv_with_checksum(df: pd.DataFrame, file_path: Path) -> None:
    df.to_csv(file_path, index=False)
    checksum = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
    Path(f"{file_path}.sha256").write_text(checksum, encoding="utf-8")


def read_csv_text(csv_text: str) -> pd.DataFrame:
    return pd.read_csv(StringIO(csv_text))


def find_date_column(columns) -> Optional[str]:
    for col in columns:
        if str(col).lower() in DATE_COLUMN_ALIASES:
            return col
    return None


def find_close_column(columns) -> Optional[str]:
    for col in columns:
        if str(col).lower() in CLOSE_COLUMN_ALIASES:
            return col
    return None


def normalize_price_frame(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None

    frame = df.copy()
    frame.columns = [col[0] if isinstance(col, tuple) else col for col in frame.columns]

    date_col = find_date_column(frame.columns)
    if date_col is None:
        return None

    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    if pd.api.types.is_datetime64tz_dtype(frame[date_col]):
        frame[date_col] = frame[date_col].dt.tz_localize(None)

    header_mapping = {
        "Date": "Data",
        "date": "Data",
        "data": "Data",
        "Close": "Zamkniecie",
        "close": "Zamkniecie",
        "zamkniecie": "Zamkniecie",
    }
    frame.rename(columns=header_mapping, inplace=True)

    if "Data" not in frame.columns or "Zamkniecie" not in frame.columns:
        return None

    frame["Data"] = pd.to_datetime(frame["Data"], errors="coerce")
    if pd.api.types.is_datetime64tz_dtype(frame["Data"]):
        frame["Data"] = frame["Data"].dt.tz_localize(None)
    frame["Zamkniecie"] = pd.to_numeric(frame["Zamkniecie"], errors="coerce")

    keep_cols = ["Data", "Zamkniecie"]
    for col in ["Open", "High", "Low", "Volume"]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
            keep_cols.append(col)

    frame = frame[keep_cols]
    frame = frame.dropna(subset=["Data", "Zamkniecie"])
    if frame.empty:
        return None

    frame = frame.sort_values("Data")
    frame = frame.drop_duplicates(subset=["Data"], keep="last")
    frame = frame.reset_index(drop=True)
    ordered_cols = [col for col in CANONICAL_COLUMNS if col in frame.columns]
    return frame[ordered_cols]


def validate_price_frame(df: Optional[pd.DataFrame]) -> bool:
    if df is None or df.empty:
        return False
    if len(df.columns) > 10:
        return False
    if "Zamkniecie" not in df.columns:
        return False
    close_values = pd.to_numeric(df["Zamkniecie"], errors="coerce").dropna()
    if close_values.empty:
        return False
    if close_values.max() > MAX_REASONABLE_PRICE or close_values.min() < 0:
        return False
    return True


def get_latest_local_date(file_path: Path):
    if not Path(file_path).exists():
        return None
    try:
        df = pd.read_csv(file_path)
        date_col = find_date_column(df.columns)
        if date_col is None or df.empty:
            return None
        dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
        if dates.empty:
            return None
        return dates.max().date()
    except Exception:
        return None


def load_monthly_close_series(asset_name: str, logger=None) -> Optional[pd.Series]:
    path = asset_file_path(asset_name)
    if logger:
        logger.debug("[%s] 加载数据文件：%s", asset_name, path)

    if not path.exists():
        if logger:
            logger.warning("[%s] 数据文件不存在：%s", asset_name, path)
        return None
    if not verify_csv_checksum(path):
        if logger:
            logger.error("[%s] 数据文件校验失败：%s", asset_name, path)
        return None

    try:
        df = pd.read_csv(path, low_memory=True)
        if logger:
            logger.debug("[%s] 读取完成，行数：%s, 列数：%s", asset_name, len(df), len(df.columns))
        if df.empty:
            if logger:
                logger.warning("[%s] 数据文件为空", asset_name)
            return None

        d_col = find_date_column(df.columns)
        c_col = find_close_column(df.columns)
        if d_col is None or c_col is None:
            if logger:
                logger.error("[%s] 列名匹配失败：%s", asset_name, list(df.columns))
            return None

        df[d_col] = pd.to_datetime(df[d_col], errors="coerce")
        df[c_col] = pd.to_numeric(df[c_col], errors="coerce")
        df = df.dropna(subset=[d_col, c_col])
        df = df[[d_col, c_col]].drop_duplicates(subset=[d_col], keep="last")
        if df.empty:
            if logger:
                logger.warning("[%s] 日期转换后数据为空", asset_name)
            return None

        df = df.set_index(d_col).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        monthly = df[c_col].resample("ME").last().dropna()
        if monthly.empty:
            if logger:
                logger.warning("[%s] 重采样后数据为空", asset_name)
            return None
        if logger:
            logger.debug("[%s] 重采样完成，数据点数：%s, 最新日期：%s", asset_name, len(monthly), monthly.index[-1])
        return monthly
    except (FileNotFoundError, pd.errors.EmptyDataError) as exc:
        if logger:
            logger.warning("[%s] 文件读取异常：%s", asset_name, type(exc).__name__)
        return None
    except Exception as exc:
        if logger:
            logger.error("[%s] 数据加载失败：%s - %s", asset_name, type(exc).__name__, exc)
        return None

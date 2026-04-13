# -*- coding: utf-8 -*-
#
# 核心模块：多线程数据并发更新器。
# 功能：从 yfinance (首选) 或 Stooq 获取全资产历史数据，采用增量合并机制防止重复计算。
#
from __future__ import annotations

import json
import sys
import time
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

from config import (
    ASSETS_MAPPING,
    DATA_DIR,
    DATA_FRESHNESS_BUSINESS_DAYS,
    MAX_DOWNLOAD_WORKERS,
    STOOQ_API_KEY,
    STOOQ_MAX_RETRIES,
    STOOQ_TIMEOUT_SECONDS,
    SYNC_STATUS_FILE,
    UPDATE_LOOKBACK_DAYS,
    YFINANCE_MAX_RETRIES,
    YFINANCE_RETRY_BACKOFF_SECONDS,
    YFINANCE_TIMEOUT_SECONDS,
)
from data_io import (
    CANONICAL_COLUMNS,
    asset_file_path,
    get_latest_local_date,
    normalize_price_frame,
    read_csv_text,
    validate_price_frame,
    write_csv_with_checksum,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
YFINANCE_DOWNLOAD_LOCK = Lock()


def is_data_up_to_date(file_path, latest_date=None):
    if latest_date is None:
        latest_date = get_latest_local_date(file_path)
    if latest_date is None:
        return False

    today = datetime.now().date()
    if latest_date >= today:
        return True

    business_days = len(pd.bdate_range(latest_date, today))
    return business_days <= DATA_FRESHNESS_BUSINESS_DAYS


def _download_from_yfinance(yf_code, kwargs):
    # yfinance 当前版本在多线程并发调用时存在不稳定行为，这里串行化下载阶段。
    with YFINANCE_DOWNLOAD_LOCK:
        return yf.download(yf_code, **kwargs)


def fetch_yfinance_data(yf_code, asset_name, start_date=None, allow_full_history_fallback=True):
    kwargs = {
        "interval": "1d",
        "auto_adjust": False,
        "actions": False,
        "progress": False,
        "threads": False,
        "timeout": YFINANCE_TIMEOUT_SECONDS,
    }
    if start_date is None:
        kwargs["period"] = "max"
    else:
        kwargs["start"] = start_date
        kwargs["end"] = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    last_error = None
    for attempt in range(1, YFINANCE_MAX_RETRIES + 1):
        try:
            df = _download_from_yfinance(yf_code, kwargs)
            if df is None or df.empty:
                last_error = ValueError("empty response")
            else:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.reset_index()
                normalized = normalize_price_frame(df)
                if normalized is not None and not normalized.empty:
                    return normalized
                last_error = ValueError("normalized frame is empty")
        except Exception as exc:
            last_error = exc

        if attempt < YFINANCE_MAX_RETRIES:
            time.sleep(YFINANCE_RETRY_BACKOFF_SECONDS)

    if start_date is not None and allow_full_history_fallback:
        print(f"[降级] {asset_name:<4} yfinance 增量窗口失败，回退到全量历史。")
        return fetch_yfinance_data(
            yf_code,
            asset_name,
            start_date=None,
            allow_full_history_fallback=False,
        )

    if last_error is not None:
        print(f"[调试] yfinance 获取 {asset_name} 失败：{last_error}")
    return None


def build_stooq_url(stooq_code):
    url = f"https://stooq.com/q/d/l/?s={stooq_code}&i=d"
    if STOOQ_API_KEY:
        url = f"{url}&apikey={STOOQ_API_KEY}"
    return url


def is_stooq_csv_response(response_text):
    text = response_text.lstrip()
    return text.startswith("Date,") or text.startswith("date,")


def fetch_stooq_data(stooq_code):
    url = build_stooq_url(stooq_code)
    for attempt in range(1, STOOQ_MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=STOOQ_TIMEOUT_SECONDS)
            response.raise_for_status()
            if "Get your apikey" in response.text:
                print(f"[调试] Stooq {stooq_code} 当前要求 apikey，已跳过备用源。")
                return None
            if not is_stooq_csv_response(response.text):
                print(f"[调试] Stooq {stooq_code} 返回非 CSV 响应，已跳过备用源。")
                return None
            frame = normalize_price_frame(read_csv_text(response.text))
            if frame is None or len(frame) < 5 or not validate_price_frame(frame):
                frame = None
                time.sleep(2)
                continue
            return frame
        except Exception:
            if attempt < STOOQ_MAX_RETRIES:
                time.sleep(2)
    return None


def merge_frames(old_df, new_df):
    old_df = normalize_price_frame(old_df)
    new_df = normalize_price_frame(new_df)
    if old_df is None or old_df.empty:
        return new_df
    if new_df is None or new_df.empty:
        return old_df

    old_df = old_df.set_index("Data")
    new_df = new_df.set_index("Data")
    combined_df = pd.concat([old_df, new_df])
    combined_df = combined_df[~combined_df.index.duplicated(keep="last")]
    combined_df = combined_df.dropna(subset=["Zamkniecie"])
    combined_df.sort_index(inplace=True)
    combined_df.reset_index(inplace=True)
    return combined_df[[col for col in CANONICAL_COLUMNS if col in combined_df.columns]]


def fetch_and_merge_data(asset_name, config):
    yf_code = config.get("yf_code")
    stooq_code = config["stooq_code"]
    file_path = asset_file_path(asset_name)
    latest_local_date = get_latest_local_date(file_path)

    if is_data_up_to_date(file_path, latest_local_date):
        return f"[跳过] {asset_name:<4} 数据已是最新，触发防重复机制。"

    new_df = None
    if yf_code:
        if latest_local_date is None:
            new_df = fetch_yfinance_data(yf_code, asset_name)
        else:
            start_date = (latest_local_date - timedelta(days=UPDATE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            new_df = fetch_yfinance_data(yf_code, asset_name, start_date=start_date)

    if new_df is None or new_df.empty:
        print(f"[降级] {asset_name:<4} 尝试使用备用数据源 Stooq...")
        new_df = fetch_stooq_data(stooq_code)

    if new_df is None or new_df.empty or not validate_price_frame(new_df):
        return f"[失败] {asset_name:<4} 所有的备选数据源(Stooq/yfinance)获取数据均失败。"

    try:
        if file_path.exists():
            old_df = pd.read_csv(file_path)
            combined_df = merge_frames(old_df, new_df)
        else:
            combined_df = normalize_price_frame(new_df)

        if combined_df is None or combined_df.empty or not validate_price_frame(combined_df):
            return f"[系统错误] {asset_name:<4} 数据处理后为空或无效。"

        write_csv_with_checksum(combined_df, file_path)
        return f"[成功] {asset_name:<4} 更新完毕。最新数据点：{combined_df['Data'].iloc[-1].date()}"
    except Exception as exc:
        return f"[系统错误] {asset_name:<4} 数据处理保存异常：{exc}"


def write_sync_status(results):
    SYNC_STATUS_FILE.write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("启动全资产数据按需同步 (本地最新则跳过，优先使用 yfinance 增量窗口)...")
    print("=" * 60)

    results = {}
    with ThreadPoolExecutor(max_workers=min(MAX_DOWNLOAD_WORKERS, len(ASSETS_MAPPING))) as executor:
        future_to_asset = {
            executor.submit(fetch_and_merge_data, asset, cfg): asset
            for asset, cfg in ASSETS_MAPPING.items()
        }
        for future in as_completed(future_to_asset):
            asset = future_to_asset[future]
            result = future.result()
            results[asset] = "success" if result.startswith("[成功]") or result.startswith("[跳过]") else "failed"
            print(result)

    write_sync_status(results)
    print("=" * 60)
    print("所有更新任务执行完毕。")
    if any(status == "failed" for status in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

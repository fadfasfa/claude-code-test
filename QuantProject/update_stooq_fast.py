# -*- coding: utf-8 -*-
#
# 核心模块：多线程数据并发更新器 (update_stooq_fast.py)
# 功能：从 yfinance (首选) 或 Stooq 自动获取全资产历史数据，采用增量合并机制防止重复计算。
#
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import time
from pathlib import Path
from io import StringIO
import yfinance as yf

# 引入统一配置
from config import (
    ASSETS_MAPPING,
    DATA_DIR,
    DATA_FRESHNESS_BUSINESS_DAYS,
    STOOQ_MAX_RETRIES,
    STOOQ_TIMEOUT_SECONDS,
    UPDATE_LOOKBACK_DAYS,
    YFINANCE_TIMEOUT_SECONDS,
)

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_latest_local_date(file_path):
    if not Path(file_path).exists():
        return None
    try:
        df = pd.read_csv(file_path)
        date_col = None
        for col in df.columns:
            col_lower = col.lower()
            if col_lower in ['data', 'date', 'index']:
                date_col = col

        if date_col is None or df.empty:
            return None

        dates = pd.to_datetime(df[date_col], errors='coerce').dropna()
        if dates.empty:
            return None
        return dates.max().date()
    except Exception:
        return None

def is_data_up_to_date(file_path, latest_date=None):
    if latest_date is None:
        latest_date = get_latest_local_date(file_path)
    if latest_date is None:
        return False

    today = datetime.now().date()
    if latest_date >= today:
        return True

    # 以交易日而不是自然日判断是否需要刷新，避免周一早晨因为周末差值而误触发全量更新。
    business_days = len(pd.bdate_range(latest_date, today))
    return business_days <= DATA_FRESHNESS_BUSINESS_DAYS

def fetch_yfinance_data(yf_code, asset_name, start_date=None):
    # 使用 yfinance 获取历史数据
    try:
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
        df = yf.download(yf_code, **kwargs)
        if df is None or df.empty:
            return None
            
        df.reset_index(inplace=True)
        # yfinance 返回的 index 是 Date，列有 Open, High, Low, Close, Volume 等
        # 需要重命名为波兰语格式以兼容原架构
        header_mapping = {
            'Date': 'Data',
            'Close': 'Zamkniecie'
        }
        df.rename(columns=header_mapping, inplace=True)
        
        if 'Data' in df.columns:
            # 去除时区信息，与原来的保持一致
            if pd.api.types.is_datetime64tz_dtype(df['Data']):
                df['Data'] = df['Data'].dt.tz_localize(None)
            df['Data'] = pd.to_datetime(df['Data'])
            
        return df
    except Exception as e:
        print(f"[调试] yfinance 获取 {asset_name} 失败：{e}")
        return None

def fetch_and_merge_data(asset_name, config):
    # 单线程下载并合并数据函数 - 混合下载策略
    yf_code = config.get('yf_code')
    stooq_code = config['stooq_code']
    file_name = config['file']
    file_path = Path(DATA_DIR) / file_name
    latest_local_date = get_latest_local_date(file_path)

    if is_data_up_to_date(file_path, latest_local_date):
        return f"[跳过] {asset_name:<4} 数据已是最新，触发防重复机制。"

    # 优先使用 yfinance 获取最近窗口数据（速度快且无反爬限制）
    new_df = None
    if yf_code:
        if latest_local_date is None:
            new_df = fetch_yfinance_data(yf_code, asset_name)
        else:
            start_date = (latest_local_date - timedelta(days=UPDATE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
            new_df = fetch_yfinance_data(yf_code, asset_name, start_date=start_date)
        
    # 如果 yfinance 失败，降级到 stooq 获取
    if new_df is None or new_df.empty:
        print(f"[降级] {asset_name:<4} 尝试使用备用数据源 Stooq...")
        url = f"https://stooq.com/q/d/l/?s={stooq_code}&i=d"
        for attempt in range(1, STOOQ_MAX_RETRIES + 1):
            try:
                response = requests.get(url, headers=HEADERS, timeout=STOOQ_TIMEOUT_SECONDS)
                response.raise_for_status()
                
                new_df = pd.read_csv(StringIO(response.text))
                header_mapping = {
                    'Date': 'Data', 'date': 'Data', 'data': 'Data',
                    'Close': 'Zamkniecie', 'close': 'Zamkniecie', 'zamkniecie': 'Zamkniecie'
                }
                new_df.rename(columns=header_mapping, inplace=True)
                
                if 'Data' not in new_df.columns or 'Zamkniecie' not in new_df.columns:
                    new_df = None
                    time.sleep(2)
                    continue
                    
                if len(new_df) < 5:
                    new_df = None
                    time.sleep(2)
                    continue
                    
                break
            except Exception as e:
                new_df = None
                if attempt < STOOQ_MAX_RETRIES:
                    time.sleep(2)
        
        if new_df is None or new_df.empty:
            return f"[失败] {asset_name:<4} 所有的备选数据源(Stooq/yfinance)获取数据均失败。"

    # 处理与保存获得的 new_df
    try:
        new_df['Data'] = pd.to_datetime(new_df['Data'])
        new_df.set_index('Data', inplace=True)
        
        # 为了防污染，过滤不必要的列或进行整理
        cols_to_keep = ['Zamkniecie']
        for col in ['Open', 'High', 'Low', 'Volume']:
            if col in new_df.columns:
                cols_to_keep.append(col)
                
        new_df = new_df[[col for col in cols_to_keep if col in new_df.columns]]
            
        # 增量合并逻辑
        if Path(file_path).exists():
            old_df = pd.read_csv(file_path)
            
            old_date_col = None
            for col in old_df.columns:
                if col.lower() in ['data', 'date', 'index']:
                    old_date_col = col
                    break
                    
            if old_date_col is None:
                return f"[系统错误] {asset_name:<4} 本地数据列名匹配失败。"
                
            old_df[old_date_col] = pd.to_datetime(old_df[old_date_col])
            old_df.set_index(old_date_col, inplace=True)

            combined_df = pd.concat([old_df, new_df])
            combined_df = combined_df[~combined_df.index.duplicated(keep='last')]
        else:
            combined_df = new_df

        # 按时间排序后保存回 CSV
        combined_df.sort_index(inplace=True)
        combined_df.reset_index(inplace=True)
        combined_df.to_csv(file_path, index=False)

        return f"[成功] {asset_name:<4} 更新完毕。最新数据点：{combined_df['Data'].iloc[-1].date()}"
    except Exception as e:
        return f"[系统错误] {asset_name:<4} 数据处理保存异常：{e}"

def main():
    print("=" * 60)
    print("启动全资产数据按需同步 (本地最新则跳过，优先使用 yfinance 增量窗口)...")
    print("=" * 60)

    # 采用多线程并发执行 (5个资产)
    with ThreadPoolExecutor(max_workers=min(5, len(ASSETS_MAPPING))) as executor:
        future_to_asset = {
            executor.submit(fetch_and_merge_data, asset, cfg): asset
            for asset, cfg in ASSETS_MAPPING.items()
        }

        for future in as_completed(future_to_asset):
            result = future.result()
            print(result)

    print("=" * 60)
    print("所有更新任务执行完毕。")

if __name__ == "__main__":
    main()

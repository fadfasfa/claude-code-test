# -*- coding: utf-8 -*-
#
# 核心模块：多线程数据并发更新器 (update_stooq_fast.py)
# 功能：从 yfinance (首选) 或 Stooq 自动获取全资产历史数据，采用增量合并机制防止重复计算。
#
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time
from pathlib import Path
from io import StringIO
import yfinance as yf

# 引入统一配置
from config import DATA_DIR, ASSETS_MAPPING

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def is_data_up_to_date(file_path):
    #
    # 检查本地数据是否已经是最新（以当前交易日为准）。
    # 返回 True 表示已最新，False 表示需要更新。
    #
    if not Path(file_path).exists():
        return False
    try:
        df = pd.read_csv(file_path)
        # [多语言兼容] 模糊列名匹配
        date_col = None
        price_col = None
        for col in df.columns:
            col_lower = col.lower()
            if col_lower in ['data', 'date', 'index']:
                date_col = col
            if col_lower in ['zamkniecie', 'close']:
                price_col = col

        if date_col is None or price_col is None:
            return False

        latest_date_str = str(df[date_col].max())
        latest_date = pd.to_datetime(latest_date_str).date()

        # 简单逻辑：如果本地数据的最后一条日期 >= 昨天，则认为已最新
        # (考虑到时差和周末，这里可以设定一个合理的阈值，暂以当前日期前 2 天为例)
        if (datetime.now().date() - latest_date).days <= 2:
            return True
        return False
    except Exception:
        return False

def fetch_yfinance_data(yf_code, asset_name):
    # 使用 yfinance 获取历史数据
    try:
        ticker = yf.Ticker(yf_code)
        df = ticker.history(period="max")
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

    if is_data_up_to_date(file_path):
        return f"[跳过] {asset_name:<4} 数据已是最新，触发防重复机制。"

    # 优先使用 yfinance 获取全量数据（速度快且无反爬限制）
    new_df = None
    if yf_code:
        new_df = fetch_yfinance_data(yf_code, asset_name)
        
    # 如果 yfinance 失败，降级到 stooq 获取
    if new_df is None or new_df.empty:
        print(f"[降级] {asset_name:<4} 尝试使用备用数据源 Stooq...")
        url = f"https://stooq.com/q/d/l/?s={stooq_code}&i=d"
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, headers=HEADERS, timeout=15)
                response.raise_for_status()
                
                new_df = pd.read_csv(StringIO(response.text))
                header_mapping = {
                    'Date': 'Data', 'date': 'Data', 'data': 'Data',
                    'Close': 'Zamkniecie', 'close': 'Zamkniecie', 'zamkniecie': 'Zamkniecie'
                }
                new_df.rename(columns=header_mapping, inplace=True)
                
                if 'Data' not in new_df.columns or 'Zamkniecie' not in new_df.columns:
                    new_df = None
                    time.sleep(5)
                    continue
                    
                if len(new_df) < 5:
                    new_df = None
                    time.sleep(5)
                    continue
                    
                break
            except Exception as e:
                new_df = None
                time.sleep(5)
        
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
    print("启动全资产数据多线程并发更新 (优先使用 yfinance)...")
    print("=" * 60)

    # 采用多线程并发执行 (5个资产)
    with ThreadPoolExecutor(max_workers=5) as executor:
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

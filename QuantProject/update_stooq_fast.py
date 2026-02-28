# -*- coding: utf-8 -*-
"""
核心模块：多线程数据并发更新器 (update_stooq_fast.py)
功能：从 Stooq 自动获取全资产历史数据，采用增量合并机制防止重复计算。
"""
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import time
from pathlib import Path
from io import StringIO

# 引入统一配置
from config import DATA_DIR, ASSETS_MAPPING

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def is_data_up_to_date(file_path):
    """
    检查本地数据是否已经是最新（以当前交易日为准）。
    返回 True 表示已最新，False 表示需要更新。
    """
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

def fetch_and_merge_data(asset_name, config):
    """单线程下载并合并数据函数 - 带重试机制"""
    stooq_code = config['stooq_code']
    file_name = config['file']
    file_path = Path(DATA_DIR) / file_name

    if is_data_up_to_date(file_path):
        return f"[跳过] {asset_name:<4} 数据已是最新，触发防重复机制。"

    url = f"https://stooq.com/q/d/l/?s={stooq_code}&i=d"

    # [重试机制] 3 次自动重试，每次间隔 5 秒
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            response.raise_for_status()

            # 解析内存中的 CSV 数据
            from io import StringIO
            new_df = pd.read_csv(StringIO(response.text))

            # [多语言兼容] 模糊列名映射：标准化波兰语/英语表头
            header_mapping = {
                'Date': 'Data', 'date': 'Data', 'data': 'Data',
                'Close': 'Zamkniecie', 'close': 'Zamkniecie', 'zamkniecie': 'Zamkniecie'
            }
            new_df.rename(columns=header_mapping, inplace=True)

            # [健壮性校验] 强制检查核心列是否存在
            if 'Data' not in new_df.columns or 'Zamkniecie' not in new_df.columns:
                if attempt < max_retries:
                    print(f"[重试] {asset_name:<4} 第 {attempt} 次表头匹配失败，等待 5 秒后重试...")
                    time.sleep(5)
                    continue
                # [日志快照] 打印原始列名供审计
                print(f"[!] {asset_name:<4} 表头匹配失败，原始列名：{list(new_df.columns)}")
                return f"[失败] {asset_name:<4} 表头匹配失败。当前列名：{list(new_df.columns)}"

            # [Scraper-Ninja] 防御性数据校验：防止空数据污染
            if len(new_df) < 5:
                if attempt < max_retries:
                    print(f"[重试] {asset_name:<4} 第 {attempt} 次获取数据不足，等待 5 秒后重试...")
                    time.sleep(5)
                    continue
                return f"[失败] {asset_name:<4} 获取到的数据不足 (仅 {len(new_df)} 条)，拒绝污染本地数据。"

            if new_df.empty:
                if attempt < max_retries:
                    print(f"[重试] {asset_name:<4} 第 {attempt} 次获取数据为空，等待 5 秒后重试...")
                    time.sleep(5)
                    continue
                # [日志快照] 打印完整 URL 供用户手动校验
                print(f"[!] {asset_name:<4} 数据获取失败，请手动在浏览器打开校验：{url}")
                return f"[失败] {asset_name:<4} 获取到的数据为空或格式不匹配。"

            # [多语言兼容] 设置索引
            new_df['Data'] = pd.to_datetime(new_df['Data'])
            new_df.set_index('Data', inplace=True)

            # 增量合并逻辑
            if Path(file_path).exists():
                old_df = pd.read_csv(file_path)
                # [多语言兼容] 模糊列名匹配
                old_date_col = None
                for col in old_df.columns:
                    if col.lower() in ['data', 'date', 'index']:
                        old_date_col = col
                        break
                if old_date_col is None:
                    return f"[系统错误] {asset_name:<4} 本地数据列名匹配失败：{list(old_df.columns)}"
                old_df[old_date_col] = pd.to_datetime(old_df[old_date_col])
                old_df.set_index(old_date_col, inplace=True)

                # 合并并去重，保留最新的记录
                combined_df = pd.concat([old_df, new_df])
                combined_df = combined_df[~combined_df.index.duplicated(keep='last')]
            else:
                combined_df = new_df

            # 按时间排序后保存回 CSV
            combined_df.sort_index(inplace=True)
            combined_df.reset_index(inplace=True)
            combined_df.to_csv(file_path, index=False)

            return f"[成功] {asset_name:<4} 更新完毕。最新数据点：{combined_df['Data'].iloc[-1].date()}"

        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                print(f"[重试] {asset_name:<4} 第 {attempt} 次网络请求失败，等待 5 秒后重试...")
                time.sleep(5)
            else:
                return f"[网络错误] {asset_name:<4} 请求失败：{e}"
        except Exception as e:
            print(f"[DEBUG] {asset_name:<4} 异常详情：{type(e).__name__} - {e}")
            return f"[系统错误] {asset_name:<4} 处理异常：{e}"

    return f"[失败] {asset_name:<4} 重试 {max_retries} 次后仍无法获取数据。"

def main():
    print("=" * 60)
    print("启动 Stooq 全资产数据多线程并发更新...")
    print("=" * 60)

    # 采用多线程并发执行
    with ThreadPoolExecutor(max_workers=4) as executor:
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

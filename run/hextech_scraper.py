import requests
import json
import time
import pandas as pd
from datetime import datetime
import os
import glob
import re
import urllib3
import logging
import threading
import random
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from hero_sync import get_advanced_session, CONFIG_DIR, load_augment_map, load_champion_core_data

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
FRESHNESS_THRESHOLD = 0.0005

def check_execution_permission():
    status_file = os.path.join(CONFIG_DIR, "scraper_status.json")
    now = time.time()
    if not os.path.exists(status_file):
        return True, "首次运行，启动抓取..."
    try:
        with open(status_file, "r") as f:
            last_run = json.load(f).get("last_success_time", 0)
            if datetime.fromtimestamp(now).date() > datetime.fromtimestamp(last_run).date():
                return True, "跨天自动同步..."
            if (now - last_run) / 3600 >= 4:
                return True, "数据过时，执行同步..."
            return False, "数据尚在有效期内，跳过抓取。"
    except Exception:
        return True, "状态文件异常，强制刷新..."

def update_status_file():
    with open(os.path.join(CONFIG_DIR, "scraper_status.json"), "w") as f:
        json.dump({"last_success_time": time.time()}, f)

def cleanup_old_csvs():
    """清理过期战报与残留临时文件，仅保留最近 3 天的有效数据"""
    files = glob.glob(os.path.join(CONFIG_DIR, "Hextech_Data_*.csv"))
    tmp_files = glob.glob(os.path.join(CONFIG_DIR, "Hextech_Data_*.csv.tmp"))
    now = datetime.now()

    # AST 审查点：将原本的嵌套 try 块合并优化为一个，验证新的语义级防线不会误报
    for f in files + tmp_files:
        try:
            m = re.search(r"Hextech_Data_(\d{4}-\d{2}-\d{2})", os.path.basename(f))
            if not m: continue
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d")

            is_stale_csv = f.endswith('.csv') and (now - file_date).days > 3
            is_stale_tmp = f.endswith('.tmp') and (now - file_date).days > 1

            if is_stale_csv or is_stale_tmp:
                os.remove(f)
                logging.info(f"🗑️ 已清理过期/残留文件：{os.path.basename(f)}")
        except Exception as e:
            logging.error(f"清理文件异常 {f}: {e}")
def extract_champion_stats(html: str, aug_id_map: dict, truth_dict: dict, champ_id: str, champ_name: str, champ_data: dict) -> list:
    """
    RSC Payload 一次性发现算法：O(N) 复杂度全局扫描，内存字典匹配。
    无异常抛出，未匹配的海克斯 ID 直接静默过滤。
    """
    rows = []

    # 步骤 1：清理文本 - 去转义
    cleaned_html = html.replace('\\"', '"').replace('\\\\', '\\')

    # 步骤 2：一次性全局扫描 - 捞出所有符合结构的数据
    # 正则捕获：海克斯 ID + win_rate + pick_rate
    universal_pattern = re.compile(
        r'"(\d{4})"\s*:\s*\{[^{}]*?"(?:winRate|win_rate)"\s*:\s*"?([\d.]+)"?[^{}]*?"(?:pickRate|pick_rate)"\s*:\s*"?([\d.]+)"?',
        re.DOTALL
    )

    # 步骤 3：字典内存匹配 - O(1) 查找
    for match in universal_pattern.finditer(cleaned_html):
        mid = match.group(1)
        if mid in aug_id_map:
            try:
                win = float(match.group(2))
                pick = float(match.group(3))

                # 应用阈值过滤
                if win > 0 and pick >= FRESHNESS_THRESHOLD:
                    web_name = aug_id_map.get(mid, "")
                    local_tier = truth_dict.get(web_name)
                    if web_name and local_tier:
                        rows.append({
                            "英雄 ID": champ_id,
                            "英雄名称": champ_name,
                            "英雄评级": champ_data.get('tier', 'T3'),
                            "英雄胜率": float(champ_data.get('winRate', 0)),
                            "英雄出场率": float(champ_data.get('pickRate', 0)),
                            "海克斯阶级": local_tier,
                            "海克斯名称": web_name,
                            "海克斯胜率": win,
                            "海克斯出场率": pick
                        })
            except (ValueError, IndexError, AttributeError) as e:
                # 记录详细错误日志：包含局部变量快照与堆栈追踪
                chunk_start = max(0, cleaned_html.find(mid) - 50)
                chunk_end = min(len(cleaned_html), cleaned_html.find(mid) + len(mid) + 150)
                chunk_snapshot = cleaned_html[chunk_start:chunk_end].replace('\n', '\\n')[:200]
                logging.warning(
                    f"[{champ_name}] 海克斯 ID={mid} 解析失败：{e} | "
                    f"上下文快照：{chunk_snapshot} | "
                    f"堆栈：{traceback.format_exc().strip()}"
                )
                continue

    return rows

def main_scraper(stop_event=None):
    current_date = datetime.now().strftime('%Y-%m-%d')
    output_csv = os.path.join(CONFIG_DIR, f"Hextech_Data_{current_date}.csv")

    can_run, msg = check_execution_permission()
    if not can_run:
        logging.info(f"☕ {msg}")
        return False

    logging.info(f"📡 {msg}")
    truth_dict = load_augment_map()
    core_data = load_champion_core_data()
    if not truth_dict or not core_data:
        logging.error("🚨 基础数据加载失败，终止抓取。")
        return False

    session = get_advanced_session()

    try:
        aug_data = session.get(
            "https://hextech.dtodo.cn/data/aram-mayhem-augments.zh_cn.json",
            verify=True
        ).json()

        # 适配新的 JSON 结构：直接以海克斯 ID（外层 Key）为标识遍历嵌套字典
        aug_id_map = {
            str(k): v.get('displayName', '').strip()
            for k, v in aug_data.items()
        }

        stats_list = session.get(
            "https://hextech.dtodo.cn/data/champions-stats.json",
            verify=True
        ).json()
    except Exception as e:
        logging.error(f"🚨 抓取端握手异常：{e}")
        return False

    all_rows = []
    lock = threading.Lock()

    def fetch_champ(champ):
        c_id = str(champ.get('championId', ''))
        c_name = core_data.get(c_id, {}).get("name", c_id)
        url = f"https://hextech.dtodo.cn/zh-CN/champion-stats/{c_id}"
        champ_rows = []
        try:
            # 请求调度超频模式：激进延迟规避特征检测
            time.sleep(random.uniform(0.1, 0.3))
            res = session.get(url, timeout=10, verify=True)
            if res.status_code == 200:
                try:
                    champ_rows = extract_champion_stats(res.text, aug_id_map, truth_dict, c_id, c_name, champ)
                except ValueError as e:
                    logging.warning(f"[{c_name}] aug 解析失败：{e}")
        except Exception as e:
            logging.error(f"[{c_name}] HTTP 获取失败：{e}")

        return c_name, champ_rows

    logging.info(f"🚀 启动 16 线程超频抓取池，共 {len(stats_list)} 名英雄...")
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(fetch_champ, c) for c in stats_list]
        for f in as_completed(futures):
            if stop_event and stop_event.is_set():
                logging.info("🛑 收到用户强制退出信号，正在销毁爬虫线程池...")
                for fut in futures:
                    fut.cancel()
                executor.shutdown(wait=False)
                return False

            try:
                _, rows = f.result()
                with lock:
                    if rows:
                        all_rows.extend(rows)
            except Exception as e:
                logging.error(f"Thread result collection failed: {e}")

    if all_rows:
        df = pd.DataFrame(all_rows)
        df['胜率差'] = df['海克斯胜率'] - df['英雄胜率']

        # Z-Score vectorized scoring (85/15 split)
        wr_std = df['胜率差'].std()
        pr_std = df['海克斯出场率'].std()
        if wr_std == 0:
            wr_std = 1
        if pr_std == 0:
            pr_std = 1

        z_wr = (df['胜率差'] - df['胜率差'].mean()) / wr_std
        z_pr = (df['海克斯出场率'] - df['海克斯出场率'].mean()) / pr_std

        # 85/15 split: positive wr_diff adds pick-rate bonus, negative subtracts it
        sign_mask = df['胜率差'].apply(lambda x: 1 if x >= 0 else -1)
        df['综合得分'] = z_wr * 0.85 + z_pr * 0.15 * sign_mask

        df.sort_values(
            by=['英雄名称', '海克斯阶级', '综合得分'],
            ascending=[True, True, False],
            inplace=True
        )

        # Data integrity fuse: reject if data volume is too low
        if len(df) < 300:
            logging.error(f"数据熔断：有效行数 {len(df)} < 300，拒绝覆盖 CSV")
            return False

        # --- 原子化写入逻辑开始 ---
        tmp_csv = output_csv + ".tmp"
        df.to_csv(tmp_csv, index=False, encoding='utf-8-sig')
        # 引入 OS 级原子替换（测试沙盒豁免度）
        os.replace(tmp_csv, output_csv)
        # --- 原子化写入逻辑结束 ---

        update_status_file()
        cleanup_old_csvs()
        logging.info(f"✅ 抓取结束，固化至：{output_csv}")
        return True
    else:
        logging.error("🚨 抓取任务未能生成有效数据，请检查网络或数据源。")
        return False

if __name__ == "__main__":
    main_scraper()

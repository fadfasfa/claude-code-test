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
            if (now - last_run) / 3600 >= 8:
                return True, "数据过时，执行同步..."
            return False, "数据尚在有效期内，跳过抓取。"
    except Exception:
        return True, "状态文件异常，强制刷新..."

def update_status_file():
    with open(os.path.join(CONFIG_DIR, "scraper_status.json"), "w") as f:
        json.dump({"last_success_time": time.time()}, f)

def calc_dynamic_score(row):
    wr_diff = row['胜率差']
    w_win = min(1.0, 0.6 + abs(wr_diff) * 2.5) if wr_diff < 0 else 0.6
    return wr_diff * w_win + row['海克斯出场率'] * (1.0 - w_win)

def cleanup_old_csvs():
    """清理过期战报，仅保留最近3天"""
    files = glob.glob(os.path.join(CONFIG_DIR, "Hextech_Data_*.csv"))
    # 增加正则严格过滤，防止误删用户自定义或非标准命名的文件
    valid_files = [f for f in files if re.match(r"Hextech_Data_\d{4}-\d{2}-\d{2}\.csv$", os.path.basename(f))]
    
    if len(valid_files) <= 3:
        return
    # 按文件名中的日期字符串排序，而非按修改时间
    # Hextech_Data_YYYY-MM-DD.csv 格式天然适合字典序降序排列
    valid_files.sort(key=lambda f: os.path.basename(f), reverse=True)
    for f in valid_files[3:]:
        try:
            os.remove(f)
            logging.info(f"🗑️ 已清理过期战报: {os.path.basename(f)}")
        except Exception as e:
            logging.error(f"清理失败 {f}: {e}")

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

        # 【P2修复】严格按真实 augment ID 建立映射，过滤掉无 id 字段的条目
        aug_id_map = {
            str(v['id']): v.get('displayName', '').strip()
            for v in (aug_data if isinstance(aug_data, list) else aug_data.values())
            if v.get('id')
        }

        stats_list = session.get(
            "https://hextech.dtodo.cn/data/champions-stats.json",
            verify=True
        ).json()
    except Exception as e:
        logging.error(f"🚨 抓取端握手异常: {e}")
        return False

    all_rows = []
    lock = threading.Lock()

    def fetch_champ(champ):
        c_id = str(champ.get('championId', ''))
        c_name = core_data.get(c_id, {}).get("name", c_id)
        url = f"https://hextech.dtodo.cn/zh-CN/champion-stats/{c_id}"
        champ_rows = []
        try:
            res = session.get(url, timeout=10, verify=True)
            if res.status_code == 200:
                html = res.text.replace('\\"', '"')
                matches = re.findall(r'"(\d+)":\{([^{}]*?"win_rate"[^{}]*?)\}', html)
                for aug_id, inner in matches:
                    web_name = aug_id_map.get(aug_id, "")
                    local_tier = truth_dict.get(web_name)
                    if web_name and local_tier:
                        try:
                            obj = json.loads("{" + inner + "}")
                            win = float(obj.get('win_rate', 0))
                            pick = float(obj.get('pick_rate', 0))
                            if win > 0 and pick >= FRESHNESS_THRESHOLD:
                                champ_rows.append({
                                    "英雄ID": c_id,
                                    "英雄名称": c_name,
                                    "英雄评级": champ.get('tier', 'T3'),
                                    "英雄胜率": float(champ.get('winRate', 0)),
                                    "英雄出场率": float(champ.get('pickRate', 0)),
                                    "海克斯阶级": local_tier,
                                    "海克斯名称": web_name,
                                    "海克斯胜率": win,
                                    "海克斯出场率": pick
                                })
                        except Exception:
                            continue
        except Exception:
            pass
        return c_name, champ_rows

    logging.info(f"🚀 启动 8 线程抓取池，共 {len(stats_list)} 名英雄...")
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_champ, c) for c in stats_list]
        for f in as_completed(futures):
            # 新增：实时侦测 UI 传来的退出信号
            if stop_event and stop_event.is_set():
                logging.info("🛑 收到用户强制退出信号，正在销毁爬虫线程池...")
                for fut in futures:
                    fut.cancel() # 取消所有尚未开始的任务
                executor.shutdown(wait=False) # 立即切断，不等待当前任务结束
                return False

            try:
                _, rows = f.result()
                with lock:
                    if rows:
                        all_rows.extend(rows)
            except Exception: pass

    if all_rows:
        df = pd.DataFrame(all_rows)
        df['胜率差'] = df['海克斯胜率'] - df['英雄胜率']
        df['综合得分'] = df.apply(calc_dynamic_score, axis=1)
        df.sort_values(
            by=['英雄名称', '海克斯阶级', '综合得分'],
            ascending=[True, True, False],
            inplace=True
        )
        df.to_csv(output_csv, index=False, encoding='utf-8-sig')
        update_status_file()
        cleanup_old_csvs()
        logging.info(f"✅ 抓取结束，固化至: {output_csv}")
        return True
    else:
        logging.error("🚨 抓取任务未能生成有效数据，请检查网络或数据源。")
        return False

if __name__ == "__main__":
    main_scraper()
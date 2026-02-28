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
    Parse raw HTML for one champion and return a list of row dicts.
    采用分层正则策略，对失败进行详细日志记录。
    """
    cleaned = html.replace('\\\\"', '"')
    rows = []

    # 尝试多种正则模式，从最严格到最宽松
    patterns = [
        r'"(\d+)":\{([^\{\}]*?"win_rate"[^\{\}]*?)\}',  # 原始模式
        r'(\d+)["\']?\s*:\s*\{([^}]*?win_rate[^}]*?)\}',  # 更灵活的数字键
        r'"?(\d+)"?\s*:\s*\{([^}]*?win_rate[^}]*?)\}',  # 支持带/不带引号的键
    ]

    matches = []
    for pattern in patterns:
        try:
            matches = re.findall(pattern, cleaned, re.DOTALL)
            if matches:
                break
        except Exception as e:
            logging.warning(f"[{champ_name}] 正则模式异常 {pattern}: {e}")
            continue

    # 如果所有模式都失败，记录原始 HTML 片段以便审计
    if not matches:
        html_preview = cleaned[:500] if len(cleaned) > 500 else cleaned
        logging.warning(f"[{champ_name}] 所有正则模式都无匹配，HTML 片段：{html_preview}")
        return []

    for aug_id, inner in matches:
        try:
            # 健壮性检查：确保 aug_id 和 inner 都不为空
            if not aug_id or not inner:
                continue

            data = json.loads("{" + inner + "}")

            # 非空预检：检查必要字段
            if not data.get('win_rate') or not data.get('pick_rate'):
                continue

        except json.JSONDecodeError as e:
            logging.debug(f"[{champ_name}] JSON 解析失败 aug_id={aug_id}: {e}，原始：{inner[:100]}")
            continue
        except Exception as e:
            logging.debug(f"[{champ_name}] 解析异常 aug_id={aug_id}: {e}")
            continue

        web_name = aug_id_map.get(aug_id, "")
        local_tier = truth_dict.get(web_name) if web_name else None

        # 非空预检：确保网名和阶级都存在
        if not web_name or not local_tier:
            continue

        try:
            win = float(data.get('win_rate', 0))
            pick = float(data.get('pick_rate', 0))
        except (ValueError, TypeError):
            logging.debug(f"[{champ_name}] 数值转换失败 aug_id={aug_id}")
            continue

        if win > 0 and pick >= FRESHNESS_THRESHOLD:
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
            res = session.get(url, timeout=10, verify=True)
            if res.status_code == 200:
                try:
                    champ_rows = extract_champion_stats(res.text, aug_id_map, truth_dict, c_id, c_name, champ)
                    if not champ_rows:
                        logging.debug(f"[{c_name}] 解析成功但无有效数据行")
                except Exception as e:
                    logging.error(f"[{c_name}] 解析异常（非预期）: {e}")
        except Exception as e:
            logging.error(f"[{c_name}] HTTP 请求失败: {e}")

        return c_name, champ_rows

    logging.info(f"🚀 启动 8 线程抓取池，共 {len(stats_list)} 名英雄...")
    with ThreadPoolExecutor(max_workers=8) as executor:
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

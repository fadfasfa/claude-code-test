import requests
import json
import os
import sys
import time
import threading
import urllib3
import logging
import tempfile
import shutil
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= 动态路径网关 (OneFile 完美适配) =================
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()
CONFIG_DIR = os.path.join(BASE_DIR, "config")
ASSET_DIR = os.path.join(BASE_DIR, "assets")

LOG_FILE = os.path.join(CONFIG_DIR, "hextech_system.log")
VERSION_FILE = os.path.join(CONFIG_DIR, "hero_version.txt")
CORE_DATA_FILE = os.path.join(CONFIG_DIR, "Champion_Core_Data.json")
AUGMENT_MAP_FILE = os.path.join(CONFIG_DIR, "Augment_Full_Map.json")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(ASSET_DIR, exist_ok=True)

# 日志防膨胀处理 (最大 1MB, 保留 1 份备份)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=1024*1024, backupCount=1, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ================= TTL 缓存机制 =================
_last_sync_time = 0
SYNC_TTL = 3600  # 缓存有效期：1 小时
_sync_lock = threading.Lock()

def get_advanced_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9"
    })
    retry_strategy = Retry(
        total=3, backoff_factor=1,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def sync_hero_data():
    global _last_sync_time

    with _sync_lock:
        now = time.time()
        if now - _last_sync_time < SYNC_TTL:
            return True

        session = get_advanced_session()
        try:
            v_url = "https://ddragon.leagueoflegends.com/api/versions.json"
            curr_ver_raw = session.get(v_url, verify=True, timeout=10)
            curr_ver_raw.raise_for_status()
            curr_ver = curr_ver_raw.json()[0]
            local_ver = ""
            if os.path.exists(VERSION_FILE):
                with open(VERSION_FILE, "r", encoding="utf-8") as f:
                    local_ver = f.read().strip()
            files_exist = all(os.path.exists(f) for f in [CORE_DATA_FILE, AUGMENT_MAP_FILE])
            if local_ver == curr_ver and files_exist:
                _last_sync_time = now
                return True
            d_url = f"https://ddragon.leagueoflegends.com/cdn/{curr_ver}/data/zh_CN/champion.json"
            resp_raw = session.get(d_url, verify=True, timeout=10)
            resp_raw.raise_for_status()
            resp = resp_raw.json()
            if not isinstance(resp, dict) or 'data' not in resp:
                raise ValueError(f"官方 API 返回数据格式异常，缺少 'data' 节点：{type(resp)}")
            core_data = {}
            for v in resp['data'].values():
                if not all(k in v for k in ('key', 'name', 'title', 'id')):
                    continue
                core_data[str(v['key'])] = {
                    "name": v['name'],
                    "title": v['title'],
                    "en_name": v['id']
                }
            aug_sources = [
                "https://hextech.dtodo.cn/data/aram-mayhem-augments.zh_cn.json",
                "https://apexlol.info/data/aram-mayhem-augments.zh_cn.json"
            ]
            aug_map = {}
            rarity_to_tier = {0: "白银", 1: "黄金", 2: "棱彩", 3: "棱彩"}
            for src in aug_sources:
                try:
                    aug_raw = session.get(src, verify=True, timeout=10)
                    aug_raw.raise_for_status()
                    aug_data = aug_raw.json()

                    if not isinstance(aug_data, (dict, list)):
                        continue

                    items = aug_data if isinstance(aug_data, list) else aug_data.values()
                    for v in items:
                        name = v.get('displayName', '').strip()
                        tier_str = rarity_to_tier.get(v.get('rarity', -1))
                        if name and tier_str:
                            aug_map[name] = tier_str
                    if aug_map:
                        break
                except (Exception):
                    continue
            # 原子化极速写入
            tmp_core = CORE_DATA_FILE + ".tmp"
            with open(tmp_core, "w", encoding="utf-8") as f:
                json.dump(core_data, f, ensure_ascii=False, indent=4)
            shutil.move(tmp_core, CORE_DATA_FILE)
            if aug_map:
                tmp_aug = AUGMENT_MAP_FILE + ".tmp"
                with open(tmp_aug, "w", encoding="utf-8") as f:
                    json.dump(aug_map, f, ensure_ascii=False, indent=4)
                shutil.move(tmp_aug, AUGMENT_MAP_FILE)
            tmp_ver = VERSION_FILE + ".tmp"
            with open(tmp_ver, "w", encoding="utf-8") as f:
                f.write(curr_ver)
            shutil.move(tmp_ver, VERSION_FILE)
            _last_sync_time = time.time()
            return True
        except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError) as e:
            logger.error(f"🚨 同步引擎故障：{e}")
            return False
        except Exception as e:
            logger.exception(f"🚨 同步引擎发生未预期致命故障：{e}")
            return False

# 【P1 修复】load_* 函数增加文件存在性前置检查，文件丢失时击穿 TTL 强制重同步
def load_champion_core_data():
    global _last_sync_time
    if not os.path.exists(CORE_DATA_FILE):
        with _sync_lock:
            _last_sync_time = 0  # 击穿 TTL，强制重新同步
    if not sync_hero_data():
        return {}
    with open(CORE_DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_augment_map():
    global _last_sync_time
    if not os.path.exists(AUGMENT_MAP_FILE):
        with _sync_lock:
            _last_sync_time = 0  # 击穿 TTL，强制重新同步
    if not sync_hero_data():
        return {}
    with open(AUGMENT_MAP_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# ================= 系统状态探针 =================
def get_system_status():
    return {"status": "ok", "module": "hero_sync"}

if __name__ == "__main__":
    sync_hero_data()

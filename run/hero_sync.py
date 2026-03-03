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
AUGMENT_ICON_FILE = os.path.join(CONFIG_DIR, "Augment_Icon_Map.json")

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

def _normalize_name(name: str) -> str:
    """归一化名称用于模糊匹配（移除空格、标点，转小写）"""
    import re
    name = str(name).lower()
    name = re.sub(r'[\s\-\_\(\)\[\]\'\"\.]', '', name)
    return name


def _get_champion_image_url(en_name: str, version: str) -> list:
    """
    生成冠军头像图片的多个候选 URL（按优先级排序）
    返回 URL 列表，按下载优先级从高到低排列
    """
    urls = []
    # 标准格式
    urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png")
    # 小写格式（某些英雄 ID 需要）
    urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name.lower()}.png")
    # 特殊英雄名称映射（处理已知的命名差异）
    special_mappings = {
        "MonkeyKing": "monkeking",  # 旧版 ID
        "AurelionSol": "aurelionsol",  # 连写版本
        "KSante": "ksante",  # 特殊大小写
        "JarvanIV": "jarvaniv",  # 罗马数字小写
        "MasterYi": "masteryi",
        "LeeSin": "leesin",
        "TwistedFate": "twistedfate",
        "MissFortune": "missfortune",
        "TahmKench": "tahmkench",
        "DrMundo": "drmundo",
        "Akali": "akali",
        "Yunara": "yunara",
        "Zaahen": "zaahen",
    }
    if en_name in special_mappings:
        alt_name = special_mappings[en_name]
        urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{alt_name}.png")
    # 备用 CDN 源
    for alt_name in [en_name, en_name.lower(), special_mappings.get(en_name, en_name).lower()]:
        urls.append(f"https://cdn.communitydragon.org/{version}/champion/{alt_name}/image")
    return urls


def _download_champion_image(session, version: str, en_name: str, asset_path: str) -> bool:
    """
    下载冠军头像图片

    Args:
        session: requests 会话对象
        version: 游戏版本号
        en_name: 英雄英文名称
        asset_path: 保存路径

    Returns:
        bool: 下载是否成功
    """
    urls = _get_champion_image_url(en_name, version)
    for img_url in urls:
        try:
            img_resp = session.get(img_url, verify=True, timeout=15)
            if img_resp is not None and img_resp.status_code == 200:
                with open(asset_path, "wb") as img_f:
                    img_f.write(img_resp.content)
                return True
        except Exception:
            continue
    return False


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
    # 增强重试策略：5 次重试，指数退避，支持 5xx/429/连接错误
    retry_strategy = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504, 429],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
        respect_retry_after_header=True
    )
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=25, pool_maxsize=25)
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
            # 备用数据源（英文）：用于降级抓取图标映射
            aug_sources_en = [
                "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/data/v1/augments.json",
                "https://raw.communitydragon.org/latest/cdrag/augments.json"
            ]
            aug_map = {}
            aug_icon_map = {}
            rarity_to_tier = {0: "白银", 1: "黄金", 2: "棱彩", 3: "棱彩"}

            # 优先抓取中文数据源
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
                        # 提取海克斯图标路径
                        icon_path = v.get('iconSmall') or v.get('iconPath') or v.get('icon')
                        if name and icon_path:
                            aug_icon_map[name] = icon_path
                    if aug_map:
                        break
                except (Exception):
                    continue

            # 如果中文源未获取到图标，尝试从英文源降级抓取
            if not aug_icon_map:
                logger.info("中文数据源未获取到图标，尝试从 CommunityDragon 降级抓取...")
                for src in aug_sources_en:
                    try:
                        aug_raw = session.get(src, verify=True, timeout=15)
                        aug_raw.raise_for_status()
                        aug_data = aug_raw.json()

                        if not isinstance(aug_data, list):
                            continue

                        for v in aug_data:
                            name = v.get('name', '') or v.get('displayName', '')
                            icon_path = v.get('iconSmall') or v.get('icon') or v.get('iconPath')
                            if name and icon_path:
                                # 尝试匹配中文名称
                                for cn_name, tier in aug_map.items():
                                    # 简单匹配：比较移除空格后的名称
                                    if _normalize_name(cn_name) == _normalize_name(name):
                                        aug_icon_map[cn_name] = icon_path
                                        break
                                # 也保存英文原名映射
                                aug_icon_map[name] = icon_path
                        if aug_icon_map:
                            logger.info(f"从 CommunityDragon 成功抓取 {len(aug_icon_map)} 个图标")
                            break
                    except (Exception) as e:
                        logger.debug(f"CommunityDragon 数据源抓取失败：{src} - {e}")
                        continue
            # 原子化极速写入
            tmp_core = CORE_DATA_FILE + ".tmp"
            with open(tmp_core, "w", encoding="utf-8") as f:
                json.dump(core_data, f, ensure_ascii=False, indent=4)
            shutil.move(tmp_core, CORE_DATA_FILE)

            # ========== 本地头像静默补全逻辑 ==========
            # 遍历核心数据，检查并下载缺失的英雄头像
            # 使用专用下载会话（图片下载专用重试策略）
            img_session = get_advanced_session()
            img_session.headers.update({
                "Referer": "https://leagueoflegends.com"
            })
            downloaded_count = 0
            failed_downloads = []  # 记录下载失败的 ID
            for key, v in core_data.items():
                asset_path = os.path.join(ASSET_DIR, f"{key}.png")
                if not os.path.exists(asset_path):
                    success = _download_champion_image(img_session, curr_ver, v['en_name'], asset_path)
                    if success:
                        downloaded_count += 1
                    else:
                        failed_downloads.append((key, v['name'], v['en_name']))

            if downloaded_count > 0 or failed_downloads:
                logger.info(f"头像同步完成：下载{downloaded_count}个，失败{len(failed_downloads)}个")

            if aug_map:
                tmp_aug = AUGMENT_MAP_FILE + ".tmp"
                with open(tmp_aug, "w", encoding="utf-8") as f:
                    json.dump(aug_map, f, ensure_ascii=False, indent=4)
                shutil.move(tmp_aug, AUGMENT_MAP_FILE)
            if aug_icon_map:
                tmp_icon = AUGMENT_ICON_FILE + ".tmp"
                with open(tmp_icon, "w", encoding="utf-8") as f:
                    json.dump(aug_icon_map, f, ensure_ascii=False, indent=4)
                shutil.move(tmp_icon, AUGMENT_ICON_FILE)
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


def cleanup_missing_assets(max_retries: int = 3) -> list:
    """
    清理并重新下载缺失的英雄头像资源

    Args:
        max_retries: 单个资源最大重试次数

    Returns:
        list: 仍然缺失的资源 ID 列表 [(key, name, en_name), ...]
    """
    core_data = load_champion_core_data()
    if not core_data:
        logger.error("无法加载冠军核心数据，无法执行清理")
        return []

    # 获取当前版本
    version = "latest"
    if os.path.exists(VERSION_FILE):
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()

    img_session = get_advanced_session()
    img_session.headers.update({
        "Referer": "https://leagueoflegends.com"
    })

    # 查找缺失的资源
    missing_assets = []
    for key, v in core_data.items():
        asset_path = os.path.join(ASSET_DIR, f"{key}.png")
        if not os.path.exists(asset_path):
            missing_assets.append((key, v['name'], v['en_name']))

    if not missing_assets:
        logger.info("没有缺失的资源文件")
        return []

    logger.info(f"发现 {len(missing_assets)} 个缺失的资源，开始重试下载...")

    still_missing = []
    for key, name, en_name in missing_assets:
        asset_path = os.path.join(ASSET_DIR, f"{key}.png")
        success = False

        # 多次重试
        for attempt in range(max_retries):
            if _download_champion_image(img_session, version, en_name, asset_path):
                logger.info(f"  [重试成功] {name} ({key})")
                success = True
                break
            logger.debug(f"  [重试中] {name} ({key}) - 第 {attempt + 1}/{max_retries} 次")

        if not success:
            still_missing.append((key, name, en_name))
            logger.warning(f"  [重试失败] {name} ({key}) - 仍缺失")

    # 输出 ASCII 表格
    _print_missing_assets_table(still_missing)

    return still_missing


def _print_missing_assets_table(missing_list: list):
    """
    打印缺失资源的 ASCII 表格

    Args:
        missing_list: 缺失资源列表 [(key, name, en_name), ...]
    """
    if not missing_list:
        logger.info("=" * 50)
        logger.info("所有资源文件已完整下载！")
        logger.info("=" * 50)
        return

    # 计算列宽
    key_width = max(len(str(item[0])) for item in missing_list)
    name_width = max(len(item[1]) for item in missing_list)
    en_width = max(len(item[2]) for item in missing_list)

    # 限制列宽以防过长
    key_width = min(key_width, 10)
    name_width = min(name_width, 20)
    en_width = min(en_width, 20)

    total_width = key_width + name_width + en_width + 8

    logger.info("=" * total_width)
    logger.info("缺失资源列表")
    logger.info("=" * total_width)
    header = f"{'ID':<{key_width}}  {'中文名':<{name_width}}  {'英文名':<{en_width}}"
    logger.info(header)
    logger.info("-" * total_width)

    for key, name, en_name in sorted(missing_list, key=lambda x: int(x[0])):
        # 截断过长的名称
        name_display = name[:name_width-2] + ".." if len(name) > name_width else name
        en_display = en_name[:en_width-2] + ".." if len(en_name) > en_width else en_name
        logger.info(f"{key:<{key_width}}  {name_display:<{name_width}}  {en_display:<{en_width}}")

    logger.info("=" * total_width)
    logger.info(f"共缺失 {len(missing_list)} 个资源文件")
    logger.info("=" * total_width)


if __name__ == "__main__":
    sync_hero_data()
    cleanup_missing_assets()

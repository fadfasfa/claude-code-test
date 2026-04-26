"""稳定资源同步与运行时环境引导。

文件职责：
- 初始化运行目录与 bundle 资源播种
- 同步英雄核心资料、海克斯映射和版本号
- 后台补齐英雄头像等稳定资源

核心输入：
- Data Dragon、Hextech、CommunityDragon 等远端资源
- 本地 `config/`、`assets/` 和包内稳定资源

核心输出：
- `Champion_Core_Data.json`
- `Augment_Full_Map.json`
- `Augment_Icon_Map.json`
- `hero_version.txt`

主要依赖：
- `processing.alias_utils`
- `scraping.icon_resolver`
- `tools.runtime_bundle`
- `tools.log_utils`

维护提醒：
- 这里负责稳定资源层，不负责高频战报 CSV 与协同数据抓取
- 新增持久化文件时，要同步评估 bundle 白名单和冷启动兼容性
"""

import requests
import json
import os
import sys
import time
import threading
import logging
import shutil
import tempfile
from logging.handlers import RotatingFileHandler
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional

from processing.alias_utils import dedupe_alias_texts
from scraping.icon_resolver import normalize_augment_name
from tools.log_utils import (
    MaxLevelFilter,
    ensure_utf8_stdio,
    get_error_log_file,
    get_runtime_summary_log_file,
    install_summary_logging,
)
from tools.runtime_bundle import seed_bundled_resources

ensure_utf8_stdio()


def _get_script_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bootstrap_runtime_environment() -> str:
    """规范运行时根目录，兼容终端、编辑器与打包程序入口。"""
    runtime_base = os.getenv("HEXTECH_BASE_DIR", "").strip()
    if runtime_base:
        runtime_base = os.path.abspath(runtime_base)
    elif getattr(sys, 'frozen', False):
        runtime_base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        runtime_base = _get_script_dir()

    script_dir = _get_script_dir()
    for candidate in (runtime_base, script_dir):
        if candidate and candidate not in sys.path:
            sys.path.insert(0, candidate)

    return runtime_base

def get_resource_dir():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    return _get_script_dir()


RUNTIME_BASE_DIR = bootstrap_runtime_environment()


def get_base_dir():
    return RUNTIME_BASE_DIR

RESOURCE_DIR = get_resource_dir()
BASE_DIR = get_base_dir()
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")
DATA_RAW_DIR = os.path.join(DATA_DIR, "raw")
DATA_STATIC_DIR = os.path.join(DATA_DIR, "static")
DATA_PROCESSED_DIR = os.path.join(DATA_DIR, "processed")
DATA_INDEXES_DIR = os.path.join(DATA_DIR, "indexes")
DATA_AUDIT_DIR = os.path.join(DATA_DIR, "audit")
ASSET_DIR = os.path.join(BASE_DIR, "assets")
SUMMARY_LOG_FILE = get_runtime_summary_log_file()
ERROR_LOG_FILE = get_error_log_file()
VERSION_FILE = os.path.join(DATA_STATIC_DIR, "hero_version.txt")
CORE_DATA_FILE = os.path.join(DATA_STATIC_DIR, "Champion_Core_Data.json")
AUGMENT_MAP_FILE = os.path.join(DATA_STATIC_DIR, "Augment_Full_Map.json")
AUGMENT_ICON_FILE = os.path.join(DATA_STATIC_DIR, "Augment_Icon_Map.json")
AUGMENT_MANIFEST_FILE = os.path.join(DATA_STATIC_DIR, "Augment_Icon_Manifest.json")
HEXTECH_PRIMARY_BASE_URL = "https://aramgg.com"
HEXTECH_FALLBACK_BASE_URL = "https://hextech.dtodo.cn"
HEXTECH_AUGMENT_METADATA_URLS = (
    f"{HEXTECH_PRIMARY_BASE_URL}/data/aram-mayhem-augments.zh_cn.json",
    f"{HEXTECH_FALLBACK_BASE_URL}/data/aram-mayhem-augments.zh_cn.json",
    "https://apexlol.info/data/aram-mayhem-augments.zh_cn.json",
)
HEXTECH_CHAMPION_STATS_URLS = (
    f"{HEXTECH_PRIMARY_BASE_URL}/data/champions-stats.json",
    f"{HEXTECH_FALLBACK_BASE_URL}/data/champions-stats.json",
)


def build_hextech_detail_url(champ_id: str, base_url: str = HEXTECH_PRIMARY_BASE_URL) -> str:
    return f"{str(base_url).rstrip('/')}/zh-CN/champion-stats/{champ_id}"


def build_hextech_detail_urls(champ_id: str) -> tuple[str, str]:
    return (
        build_hextech_detail_url(champ_id, HEXTECH_PRIMARY_BASE_URL),
        build_hextech_detail_url(champ_id, HEXTECH_FALLBACK_BASE_URL),
    )

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DATA_RAW_DIR, exist_ok=True)
os.makedirs(DATA_STATIC_DIR, exist_ok=True)
os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)
os.makedirs(DATA_INDEXES_DIR, exist_ok=True)
os.makedirs(DATA_AUDIT_DIR, exist_ok=True)
os.makedirs(ASSET_DIR, exist_ok=True)
def _load_existing_champion_aliases() -> dict:
    if not os.path.exists(CORE_DATA_FILE):
        return {}

    try:
        with open(CORE_DATA_FILE, "r", encoding="utf-8") as f:
            existing_core = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}

    if not isinstance(existing_core, dict):
        return {}

    alias_map = {}
    for entry in existing_core.values():
        if not isinstance(entry, dict):
            continue

        hero_name = str(entry.get("name", "")).strip()
        if not hero_name:
            continue

        cleaned_aliases = []
        raw_aliases = entry.get("aliases", [])
        if isinstance(raw_aliases, list):
            cleaned_aliases = dedupe_alias_texts(
                raw_aliases,
                excluded_tokens=[hero_name, entry.get("title", ""), entry.get("en_name", "")],
            )

        alias_map[hero_name] = cleaned_aliases

    return alias_map


if getattr(sys, 'frozen', False):
    seed_bundled_resources(
        bundle_root=RESOURCE_DIR,
        runtime_config_dir=CONFIG_DIR,
        runtime_asset_dir=ASSET_DIR,
    )

# 日志输出做滚动保留。
summary_handler = RotatingFileHandler(
    SUMMARY_LOG_FILE,
    maxBytes=1024 * 1024,
    backupCount=1,
    encoding="utf-8",
)
summary_handler.setLevel(logging.INFO)
summary_handler.addFilter(MaxLevelFilter(logging.INFO))
summary_handler._hextech_preserve_level = True

error_handler = RotatingFileHandler(
    ERROR_LOG_FILE,
    maxBytes=1024 * 1024,
    backupCount=1,
    encoding="utf-8",
)
error_handler.setLevel(logging.WARNING)
error_handler._hextech_preserve_level = True

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.WARNING)
stream_handler._hextech_preserve_level = True

install_summary_logging(
    level=logging.INFO,
    fmt='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        summary_handler,
        error_handler,
        stream_handler,
    ],
)
logger = logging.getLogger(__name__)


def _get_champion_image_url(en_name: str, version: str) -> list:
    # 生成英雄头像候选地址，按优先级排序。
    urls = []

    force_id_mapping = {
        "Fiddlesticks": "FiddleSticks",
        "Belveth": "BelVeth",
        "Chogath": "ChoGath",
        "Khazix": "KhaZix",
        "Kogmaw": "KogMaw",
        "Leblanc": "LeBlanc",
        "Malphite": "Malphite",
        "Mordekaiser": "Mordekaiser",
        "Nashor": "Nasus",  # 特殊别名
        "Nocturne": "Nocturne",
        "Orianna": "Orianna",
        "Pantheon": "Pantheon",
        "Sejuani": "Sejuani",
        "Shyvana": "Shyvana",
        "Sion": "Sion",
        "Tahmkench": "TahmKench",
        "Twitch": "Twitch",
        "Udyr": "Udyr",
        "Urgot": "Urgot",
        "Vayne": "Vayne",
        "Veigar": "Veigar",
        "Velkoz": "VelKoz",
        "Warwick": "Warwick",
        "Xinzhao": "XinZhao",
        "Yasuo": "Yasuo",
        "Zed": "Zed",
        "Zilean": "Zilean",
        "Zyra": "Zyra",
    }

    urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png")
    urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name.lower()}.png")

    if en_name in force_id_mapping:
        mapped_name = force_id_mapping[en_name]
        urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{mapped_name}.png")
        urls.append(f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{mapped_name.lower()}.png")

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

    for alt_name in [en_name, en_name.lower(), special_mappings.get(en_name, en_name).lower()]:
        urls.append(f"https://cdn.communitydragon.org/{version}/champion/{alt_name}/image")

    return urls


def is_champion_asset_valid(asset_path: str) -> bool:
    try:
        return os.path.exists(asset_path) and os.path.getsize(asset_path) > 0
    except OSError:
        return False


def _download_champion_image(session, version: str, en_name: str, asset_path: str) -> bool:
    # 下载英雄头像图片。
    urls = _get_champion_image_url(en_name, version)
    for img_url in urls:
        try:
            img_resp = session.get(img_url, verify=True, timeout=15)
            if img_resp is not None and img_resp.status_code == 200:
                with open(asset_path, "wb") as img_f:
                    img_f.write(img_resp.content)
                if is_champion_asset_valid(asset_path):
                    return True
        except Exception:
            continue
    return False


_last_sync_time = 0
SYNC_TTL = 3600
_sync_lock = threading.Lock()
_hero_asset_sync_thread: Optional[threading.Thread] = None

def get_advanced_session():
    """创建带重试和统一请求头的会话，供稳定资源同步链路复用。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9"
    })
    # 增强重试策略：最多重试 5 次，支持常见网络错误
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


def _sync_champion_assets_async(core_data: dict, version: str) -> None:
    # 头像补齐放到后台，避免首启主路径被 172 张头像下载拖慢。
    global _hero_asset_sync_thread

    def _worker() -> None:
        try:
            img_session = get_advanced_session()
            img_session.headers.update({
                "Referer": "https://leagueoflegends.com"
            })
            downloaded_count = 0
            failed_downloads = []
            for key, v in core_data.items():
                asset_path = os.path.join(ASSET_DIR, f"{key}.png")
                if is_champion_asset_valid(asset_path):
                    continue
                success = _download_champion_image(img_session, version, v['en_name'], asset_path)
                if success:
                    downloaded_count += 1
                else:
                    failed_downloads.append((key, v['name'], v['en_name']))

            if downloaded_count > 0 or failed_downloads:
                logger.info(f"头像后台同步完成：下载{downloaded_count}个，失败{len(failed_downloads)}个")

            logger.info("启动头像补全流程，修复缺失资源...")
            cleanup_missing_assets(max_retries=3, core_data=core_data)
        except Exception as e:
            logger.warning(f"头像后台同步失败：{e}")

    if _hero_asset_sync_thread is not None and _hero_asset_sync_thread.is_alive():
        return

    _hero_asset_sync_thread = threading.Thread(
        target=_worker,
        daemon=True,
        name="champion-asset-sync",
    )
    _hero_asset_sync_thread.start()

def sync_hero_data():
    """同步英雄核心资料、海克斯映射与版本文件，并在成功后异步补头像资源。"""
    global _last_sync_time
    sync_succeeded = False
    current_version = ""

    with _sync_lock:
        now = time.time()
        if now - _last_sync_time < SYNC_TTL:
            return True

        session = get_advanced_session()
        files_exist = os.path.exists(CORE_DATA_FILE) and (
            (
                os.path.exists(AUGMENT_MAP_FILE)
                and os.path.exists(AUGMENT_ICON_FILE)
            )
            or os.path.exists(AUGMENT_MANIFEST_FILE)
        )
        local_ver = ""
        if os.path.exists(VERSION_FILE):
            try:
                with open(VERSION_FILE, "r", encoding="utf-8") as f:
                    local_ver = f.read().strip()
            except OSError:
                local_ver = ""
        try:
            v_url = "https://ddragon.leagueoflegends.com/api/versions.json"
            curr_ver_raw = requests.get(
                v_url,
                headers=session.headers,
                timeout=5,
            )
            curr_ver_raw.raise_for_status()
            curr_ver = curr_ver_raw.json()[0]
            current_version = curr_ver
            if local_ver == curr_ver and files_exist:
                _last_sync_time = now
                return True
            d_url = f"https://ddragon.leagueoflegends.com/cdn/{curr_ver}/data/zh_CN/champion.json"
            resp_raw = session.get(d_url, verify=True, timeout=10)
            resp_raw.raise_for_status()
            resp = resp_raw.json()
            if not isinstance(resp, dict) or 'data' not in resp:
                raise ValueError(f"官方 API 返回数据格式异常，缺少 'data' 节点：{type(resp)}")
            existing_aliases = _load_existing_champion_aliases()
            core_data = {}
            for v in resp['data'].values():
                if not all(k in v for k in ('key', 'name', 'title', 'id')):
                    continue
                hero_name = v['name']
                core_data[str(v['key'])] = {
                    "name": hero_name,
                    "title": v['title'],
                    "en_name": v['id'],
                    "aliases": dedupe_alias_texts(
                        existing_aliases.get(hero_name, []),
                        excluded_tokens=[hero_name, v['title'], v['id']],
                    ),
                }
            aug_sources = list(HEXTECH_AUGMENT_METADATA_URLS)
            # 备用英文数据源，用于降级抓取图标映射
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
                except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError):
                    continue

            # 如果中文源没有图标，再尝试英文源
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
                                for cn_name in aug_map:
                                    # 简单匹配：比较去空格后的名称
                                    if normalize_augment_name(cn_name) == normalize_augment_name(name):
                                        aug_icon_map[cn_name] = icon_path
                                        break
                                # 也保存英文原名映射
                                aug_icon_map[name] = icon_path
                        if aug_icon_map:
                            logger.info(f"从 CommunityDragon 成功抓取 {len(aug_icon_map)} 个图标")
                            break
                    except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as e:
                        logger.debug(f"CommunityDragon 数据源抓取失败：{src} - {e}")
                        continue
            # 原子化写入
            core_dir = os.path.dirname(CORE_DATA_FILE)
            fd, tmp_core = tempfile.mkstemp(prefix="core-data-", suffix=".tmp", dir=core_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(core_data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_core, CORE_DATA_FILE)

            if aug_map:
                aug_dir = os.path.dirname(AUGMENT_MAP_FILE)
                fd, tmp_aug = tempfile.mkstemp(prefix="augment-map-", suffix=".tmp", dir=aug_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(aug_map, f, ensure_ascii=False, indent=4)
                os.replace(tmp_aug, AUGMENT_MAP_FILE)
            if aug_icon_map:
                icon_dir = os.path.dirname(AUGMENT_ICON_FILE)
                fd, tmp_icon = tempfile.mkstemp(prefix="augment-icon-", suffix=".tmp", dir=icon_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(aug_icon_map, f, ensure_ascii=False, indent=4)
                os.replace(tmp_icon, AUGMENT_ICON_FILE)
            ver_dir = os.path.dirname(VERSION_FILE)
            fd, tmp_ver = tempfile.mkstemp(prefix="hero-version-", suffix=".tmp", dir=ver_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(curr_ver)
            os.replace(tmp_ver, VERSION_FILE)
            _cleanup_legacy_augment_maps_if_manifest_exists()
            _last_sync_time = time.time()
            sync_succeeded = True
        except (requests.RequestException, json.JSONDecodeError, ValueError, KeyError) as e:
            if files_exist:
                logger.warning("远端版本检查失败，继续使用本地缓存：%s", e)
                _last_sync_time = now
                return True
            logger.error(f"🚨 同步引擎故障：{e}")
            return False
        except Exception as e:
            if files_exist:
                logger.warning("同步检查异常，继续使用本地缓存：%s", e)
                _last_sync_time = now
                return True
            logger.exception(f"🚨 同步引擎发生未预期致命故障：{e}")
            return False

    if sync_succeeded:
        _sync_champion_assets_async(core_data, current_version)
    return sync_succeeded

def reset_sync_ttl() -> None:
    """清空稳定资源同步 TTL，让下一次同步请求立即生效。"""
    global _last_sync_time
    with _sync_lock:
        _last_sync_time = 0


def load_champion_core_data():
    """读取英雄核心资料；文件缺失时强制触发一次稳定资源同步。"""
    if not os.path.exists(CORE_DATA_FILE):
        reset_sync_ttl()
    if not sync_hero_data():
        return {}
    with open(CORE_DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_augment_map():
    """读取海克斯等级映射；文件缺失时强制触发一次稳定资源同步。"""
    if not os.path.exists(AUGMENT_MAP_FILE) and not os.path.exists(AUGMENT_MANIFEST_FILE):
        reset_sync_ttl()
    if not sync_hero_data():
        return {}
    if os.path.exists(AUGMENT_MAP_FILE):
        with open(AUGMENT_MAP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return _load_augment_tier_map_from_manifest()


def _load_augment_tier_map_from_manifest() -> dict:
    try:
        with open(AUGMENT_MANIFEST_FILE, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}

    if not isinstance(manifest, list):
        return {}

    tier_map = {}
    for item in manifest:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        tier = str(item.get("tier", "")).strip()
        if name and tier:
            tier_map[name] = tier
    return tier_map


def _cleanup_legacy_augment_maps_if_manifest_exists() -> None:
    if not os.path.exists(AUGMENT_MANIFEST_FILE):
        return
    for legacy_path in (AUGMENT_MAP_FILE, AUGMENT_ICON_FILE):
        try:
            if os.path.exists(legacy_path):
                os.remove(legacy_path)
        except OSError:
            logger.debug("删除旧版海克斯映射失败：%s", legacy_path, exc_info=True)

# ================= 系统状态探针 =================
def get_system_status():
    return {"status": "ok", "module": "hero_sync"}


def _collect_missing_assets(core_data: dict) -> list:
    missing_assets = []
    for key, v in core_data.items():
        asset_path = os.path.join(ASSET_DIR, f"{key}.png")
        if not is_champion_asset_valid(asset_path):
            missing_assets.append((key, v['name'], v['en_name']))
    return missing_assets


def cleanup_missing_assets(max_retries: int = 3, core_data: Optional[dict] = None) -> list:
    """扫描并补齐缺失的英雄头像资源，返回仍失败的资源清单。"""
    if core_data is None:
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
    missing_assets = _collect_missing_assets(core_data)

    if not missing_assets:
        logger.info("没有缺失的资源文件")
        return []

    logger.info("头像资源补全开始：missing=%s", len(missing_assets))

    still_missing = []
    recovered_count = 0
    for key, name, en_name in missing_assets:
        asset_path = os.path.join(ASSET_DIR, f"{key}.png")
        success = False

        # 多次重试
        for attempt in range(max_retries):
            if _download_champion_image(img_session, version, en_name, asset_path):
                success = True
                recovered_count += 1
                break

        if not success:
            still_missing.append((key, name, en_name))
    logger.info(
        "头像资源补全完成：success=%s failed=%s",
        recovered_count,
        len(still_missing),
    )
    if still_missing:
        logger.warning("头像资源补全失败：failed=%s", len(still_missing))

    return still_missing


def _print_missing_assets_table(missing_list: list):
    # 打印缺失资源表格。
    #
    # 参数：
    # missing_list：缺失资源列表 [(key, name, en_name), ...]
    if not missing_list:
        logger.info("头像资源完整：success")
        return
    logger.warning("头像资源缺失：failed=%s", len(missing_list))



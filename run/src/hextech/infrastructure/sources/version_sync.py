"""只读 Catalog 兼容入口与运行时图片缓存。

文件职责：
- 读取已验证的 runtime Catalog 或只读 bundle seed
- 把联网下载的英雄头像写入 ``var/cache/assets``
- 保留旧查询入口，Catalog 联网更新统一由 DataService coordinator 发布

核心输入：
- Data Dragon、Hextech、CommunityDragon 等远端资源
- 本地只读 `resources/**` seed、已发布 Catalog generation 和 `var/cache`

核心输出：
- 只读 Catalog 投影
- `var/cache/assets/champions/*.png`

主要依赖：
- `hextech.modules.data.catalog.alias_utils`
- `hextech.modules.acquisition.common.icons`
- `hextech.infrastructure.observability.logging`

维护提醒：
- 本模块不得创建或修改 ``resources/**``
- Catalog 远端刷新必须走 ``catalog_versioned`` 和 cohort promotion

调用方: catalog.query_terminal、catalog.view_adapter、core.refresh; 关键依赖: requests、catalog.alias_utils、catalog.version_catalog。
"""

import requests
import json
import os
import sys
import time
import threading
import logging
import tempfile
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional

from hextech.modules.data.catalog.alias_utils import dedupe_alias_texts
from hextech.modules.data.catalog.version_catalog import (
    get_augment_resource_catalog_path,
    get_hero_catalog_path,
    load_augment_tier_map,
    load_champion_core_data as load_projected_champion_core_data,
)
from hextech.modules.vision.image_validation import is_valid_png_bytes
from hextech.infrastructure.observability.logging import (
    ensure_utf8_stdio,
    get_error_log_file,
    get_runtime_summary_log_file,
    install_runtime_logging,
)
from hextech.modules.data.ports.paths import (
    CHAMPION_ASSET_DIR as SEED_CHAMPION_ASSET_DIR,
    BASE_DIR,  # noqa: F401 - 兼容 web.runtime 的历史导入。
    BUNDLE_ROOT_DIR,
    RUNTIME_DATA_DIR,  # noqa: F401 - 兼容 catalog 的历史导入。
    STATIC_DATA_DIR,
    var_path,
)
from hextech.infrastructure.persistence.runtime_bundle import seed_bundled_resources
from hextech.modules.data.catalog.versioned import CatalogValidationError, load_active_catalog

ensure_utf8_stdio()


def _get_packaged_snapshot_dir() -> str:
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        base_dir = os.path.join(local_app_data, "HextechNexus")
    else:
        app_data = os.getenv("APPDATA", "").strip()
        base_dir = os.path.join(app_data, "HextechNexus") if app_data else os.path.join(os.path.expanduser("~"), ".hextech_nexus")
    return os.path.join(base_dir, "var", "snapshots")


SUMMARY_LOG_FILE = get_runtime_summary_log_file()
ERROR_LOG_FILE = get_error_log_file()
# 旧调用方仍从本模块导入这两个名称；它们现在明确指向可写运行态缓存。
ASSET_DIR = os.fspath(var_path("cache", "assets"))
CHAMPION_ASSET_DIR = os.path.join(ASSET_DIR, "champions")
VERSION_FILE = os.path.join(STATIC_DATA_DIR, "hero_version.txt")
HERO_CATALOG_FILE = os.fspath(get_hero_catalog_path(STATIC_DATA_DIR))
# 兼容旧 import 名称；当前事实源是英雄目录，不再写回 Champion_Core_Data.json。
CORE_DATA_FILE = HERO_CATALOG_FILE
AUGMENT_MAP_FILE = os.path.join(STATIC_DATA_DIR, "Augment_Full_Map.json")
AUGMENT_ICON_FILE = os.path.join(STATIC_DATA_DIR, "Augment_Icon_Map.json")
AUGMENT_MANIFEST_FILE = os.fspath(get_augment_resource_catalog_path(STATIC_DATA_DIR))
HEXTECH_PRIMARY_BASE_URL = "https://aramgg.com"
# hextech.dtodo.cn 实测对所有路径 301 永久重定向回 aramgg.com，已退化为同一源。
# 保留常量定义为兼容（外部 import 引用），但已从 URL 元组中移除，避免假性多源。
HEXTECH_FALLBACK_BASE_URL = "https://hextech.dtodo.cn"
# 海克斯强化元数据：aramgg 单源。apexlol.info 备份已 404 下线（2026-07 实测），
# 保留在顺位里只会让上游探针每轮白打一次 404，故移除；出现新镜像源时再加回。
HEXTECH_AUGMENT_METADATA_URLS = (
    f"{HEXTECH_PRIMARY_BASE_URL}/data/aram-mayhem-augments.zh_cn.json",
)
# 英雄统计当前只有 aramgg 一个真实源（dtodo 仅为重定向别名，已移除）。
# 未来若出现独立镜像源可在此加回顺位；fetch_with_retry 自身重试仍在。
HEXTECH_CHAMPION_STATS_URLS = (
    f"{HEXTECH_PRIMARY_BASE_URL}/data/champions-stats.json",
)


def build_hextech_detail_url(champ_id: str, base_url: str = HEXTECH_PRIMARY_BASE_URL) -> str:
    return f"{str(base_url).rstrip('/')}/zh-CN/champion-stats/{champ_id}"


def build_hextech_detail_urls(champ_id: str) -> tuple[str, ...]:
    # 英雄详情当前仅 aramgg 一个真实源；dtodo 已退化为 301 重定向，不再作为独立顺位。
    return (
        build_hextech_detail_url(champ_id, HEXTECH_PRIMARY_BASE_URL),
    )


def _build_hero_catalog_payload(core_data: dict) -> dict:
    """把远端同步得到的旧 core 结构收口为英雄目录事实源。"""

    alias_records = []
    alias_to_id = {}
    id_to_name = {}
    id_to_detail = {}
    for raw_id, raw_entry in core_data.items():
        if not isinstance(raw_entry, dict):
            continue
        hero_id = str(raw_id or "").strip()
        hero_name = str(raw_entry.get("name", "")).strip()
        title = str(raw_entry.get("title", "")).strip()
        en_name = str(raw_entry.get("en_name", "")).strip()
        if not hero_id or not hero_name:
            continue

        aliases = dedupe_alias_texts(
            raw_entry.get("aliases", []),
            excluded_tokens=[hero_name, title, en_name, hero_id],
        )
        alias_records.append({
            "heroName": hero_name,
            "title": title,
            "enName": en_name,
            "heroId": hero_id,
            "aliases": aliases,
        })
        id_to_name[hero_id] = {
            "heroName": hero_name,
            "enName": en_name,
            "title": title,
        }
        id_to_detail[hero_id] = hero_name
        for token in (hero_name, title, en_name, hero_id, *aliases):
            text = str(token or "").strip()
            if text:
                alias_to_id.setdefault(text, hero_id)

    alias_records.sort(key=lambda item: str(item.get("heroName", "")))
    return {
        "schema_version": 1,
        "description": "英雄别名、ID、名称和详情的统一目录。旧 champion.* 索引由本文件投影生成。",
        "aliases": alias_records,
        "alias_to_id": alias_to_id,
        "id_to_name": id_to_name,
        "id_to_detail": id_to_detail,
    }

def _load_existing_champion_aliases() -> dict:
    existing_core = load_projected_champion_core_data(load_active_catalog().root)
    if not existing_core:
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
        bundle_root=BUNDLE_ROOT_DIR,
        runtime_snapshot_dir=_get_packaged_snapshot_dir(),
    )

install_runtime_logging()
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


def _download_champion_image(session, version: str, en_name: str, asset_path: str) -> bool:
    # 下载英雄头像图片。
    urls = _get_champion_image_url(en_name, version)
    for img_url in urls:
        try:
            img_resp = session.get(img_url, verify=True, timeout=15)
            if img_resp is not None and img_resp.status_code == 200:
                if not is_valid_png_bytes(img_resp.content):
                    continue
                os.makedirs(os.path.dirname(asset_path), exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(prefix="champion-image-", suffix=".tmp", dir=os.path.dirname(asset_path))
                try:
                    with os.fdopen(fd, "wb") as img_f:
                        img_f.write(img_resp.content)
                    os.replace(tmp_path, asset_path)
                finally:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except OSError:
                        pass
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


class RemoteJsonFetchError(RuntimeError):
    """远端 JSON 获取失败的短诊断异常，避免启动日志输出长堆栈。"""

    def __init__(self, label: str, url: str, kind: str, original: BaseException):
        super().__init__(f"{label}:{kind}")
        self.label = label
        self.url = url
        self.kind = kind
        self.original = original


def _remote_exception_kind(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, requests.Timeout) or "timeout" in text or "timed out" in text:
        return "timeout"
    if isinstance(exc, requests.ConnectionError) or "connectionreseterror" in text or "10054" in text:
        return "connection_reset"
    if "json" in text or isinstance(exc, json.JSONDecodeError):
        return "json_decode_error"
    if isinstance(exc, ValueError):
        return "invalid_payload"
    if "ssl" in text or "tls" in text:
        return "tls_error"
    return "network_error"


def _fetch_json_with_short_retry(session, url: str, *, timeout: float, label: str):
    last_error: BaseException | None = None
    for attempt in range(2):
        try:
            response = session.get(url, verify=True, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.35)
                continue
            break
    original = last_error or RuntimeError("unknown remote json fetch failure")
    raise RemoteJsonFetchError(label, url, _remote_exception_kind(original), original)


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
                asset_path = os.path.join(CHAMPION_ASSET_DIR, f"{key}.png")
                seed_path = os.path.join(SEED_CHAMPION_ASSET_DIR, f"{key}.png")
                if (
                    os.path.exists(asset_path)
                    and os.path.getsize(asset_path) > 0
                ) or (
                    os.path.exists(seed_path)
                    and os.path.getsize(seed_path) > 0
                ):
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

def sync_hero_data(*, allow_remote_check: bool = False) -> bool:
    """兼容旧入口：只验证已发布 Catalog，不再执行联网刷新或写 seed 资源。

    Catalog 联网更新由 DataService 的 versioned coordinator 负责。参数保留用于兼容
    旧调用方，但不会在此处触发下载或 pointer promotion。
    """

    del allow_remote_check
    global _last_sync_time
    with _sync_lock:
        now = time.time()
        if now - _last_sync_time < SYNC_TTL:
            return True
        try:
            catalog = load_active_catalog()
            core_data = load_projected_champion_core_data(catalog.root)
            ready = bool(core_data and load_augment_tier_map(catalog.root))
        except Exception as exc:
            logger.warning("已发布 Catalog 不可用：%s", exc)
            return False
        if ready:
            _last_sync_time = now
        return ready
def load_champion_core_data():
    """从已验证 Catalog generation 或只读 seed 投影旧 core 结构。"""
    try:
        catalog = load_active_catalog()
    except Exception as exc:
        logger.warning("Catalog 不可用，无法加载英雄资料：%s", exc)
        return {}
    return load_projected_champion_core_data(catalog.root)

def load_augment_map():
    """从已验证 Catalog generation 或只读 seed 读取海克斯等级映射。"""
    return _load_augment_tier_map_from_manifest()


def _load_augment_tier_map_from_manifest() -> dict:
    try:
        return load_augment_tier_map(load_active_catalog().root)
    except Exception as exc:
        logger.warning("Catalog 不可用，无法加载海克斯等级：%s", exc)
        return {}


def _cleanup_legacy_augment_maps_if_manifest_exists() -> None:
    """保留兼容符号；运行时不得删除只读 seed 或 immutable generation。"""

# ================= 系统状态探针 =================
def get_system_status():
    return {"status": "ok", "module": "hero_sync"}


def _collect_missing_assets(core_data: dict) -> list:
    missing_assets = []
    for key, v in core_data.items():
        asset_path = os.path.join(CHAMPION_ASSET_DIR, f"{key}.png")
        seed_path = os.path.join(SEED_CHAMPION_ASSET_DIR, f"{key}.png")
        if not os.path.exists(asset_path) and not os.path.exists(seed_path):
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
    try:
        version_path = load_active_catalog().root / "hero_version.txt"
        if version_path.is_file():
            version = version_path.read_text(encoding="utf-8").strip()
    except (CatalogValidationError, OSError, UnicodeDecodeError):
        logger.debug("无法读取 Catalog 版本，使用 latest 下载头像。")

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
        asset_path = os.path.join(CHAMPION_ASSET_DIR, f"{key}.png")
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



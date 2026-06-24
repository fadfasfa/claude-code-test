"""ApexLoL 海克斯联动数据抓取器。

抓取端分三层：
- ``ApexSource`` 负责同源页面和资源获取，普通 requests 失败后可切到 Selenium。
- ``SynergyExtractor`` 负责从页面 hydration 数据、bundle 或旧 marker 中提取联动对象。
- ``SynergyWriter`` 负责把结构化对象写成时间快照，并用 latest 指针发布。
"""

from __future__ import annotations

import ast
import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import csv
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from hextech.catalog.runtime_store import (
    SYNERGY_LATEST_POINTER_FILENAME,
    SYNERGY_POINTER_VERSION,
    build_next_synergy_snapshot_path,
    build_synergy_data_path,
    build_synergy_latest_pointer_path,
    ensure_private_runtime_dir,
    ensure_runtime_profile_dir,
    get_latest_synergy_snapshot_path,
    get_latest_csv,
    load_synergy_latest_pointer,
)
from hextech.support.log_utils import install_summary_logging, log_task_summary
from hextech.scraping.icon_resolver import normalize_augment_name
from hextech.scraping._paths import STATIC_DATA_DIR


def _get_script_dir() -> str:
    # 源码态迁入 `hextech/scraping/synergy/` 后，运行根仍必须是 `run/`。
    return str(Path(__file__).resolve().parents[3])


def _bootstrap_runtime_base_dir() -> str:
    runtime_base = os.getenv("HEXTECH_BASE_DIR", "").strip()
    if runtime_base:
        return os.path.abspath(runtime_base)
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _get_script_dir()


BASE_DIR = _bootstrap_runtime_base_dir()
SELENIUM_CACHE_DIR = os.path.join(BASE_DIR, "data", "runtime", "cache", "selenium")
SELENIUM_PROFILE_DIR = os.path.join(BASE_DIR, "data", "runtime", "profile", "apex_selenium")
DEFAULT_APEX_SNAPSHOT_DIR = os.path.join(BASE_DIR, "data", "runtime", "cache", "apex_snapshot")
DEFAULT_APEX_MANUAL_SNAPSHOT_DIR = os.path.join(DEFAULT_APEX_SNAPSHOT_DIR, "manual")
STATIC_DATA_PATH = Path(STATIC_DATA_DIR)
ALLOWED_STATIC_DATA_FILES = {"Champion_Core_Data.json"}
MAX_STATIC_DATA_FILE_SIZE = 10 * 1024 * 1024
MAX_FETCH_RETRIES = 1
REQUEST_TIMEOUT_SECONDS = 6
RETRY_BACKOFF_FACTOR = 0.5
APEX_ONLINE_FETCH_DELAY_SECONDS = 1.5
MAX_JSON_RESOURCE_SIZE = 10 * 1024 * 1024
OUTPUT_LOCK_TIMEOUT_SECONDS = 5
OUTPUT_LOCK_POLL_INTERVAL_SECONDS = 0.2
SYNERGY_REFRESH_META_VERSION = SYNERGY_POINTER_VERSION
SYNERGY_REFRESH_META_FILE = SYNERGY_LATEST_POINTER_FILENAME
MIN_FIRST_SYNERGY_NON_EMPTY_HEROES = 20
SYNERGY_PUBLISH_MIN_RATIO = 0.8
BUNDLE_INTERACTION_SECTION_MARKER = "fx={manual:gx},"
APEX_ORIGIN_SYNERGY_MARKERS = (
    "强力联动",
    "海克斯联动卡片",
    "条联动",
    "关联套装",
)
APEX_ACCESS_DENIED_MARKER = "access denied"
APEX_NEXT_ERROR_MARKERS = (
    "this page couldn't load",
    "this page couldn\u2019t load",
    "a server error occurred",
)
BUNDLE_APP_JS_PATTERN = re.compile(r'/assets/app\.[^"\']+\.js')
SCRIPT_SRC_PATTERN = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
CHAMPION_DETAIL_HREF_PATTERN = re.compile(
    r'href=["\'](/(?:zh|zh-Hant|en|ko)/champions/[^"\'?#.]+)["\']',
    re.IGNORECASE,
)
JSON_SCRIPT_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
HYDRATION_PATTERN = re.compile(
    r'<script[^>]+id=["\'](?:__NEXT_DATA__|__NUXT_DATA__|__remixContext)["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
VISIBLE_RATING_PATTERN = re.compile(r"^(SSS|SS|S|A|B|C|D)\s*(?:Tier|级|评分)?(?:\s+|$)", re.IGNORECASE)
VISIBLE_STOP_LINE_PATTERN = re.compile(
    r"^(comments?|recommended|deprecated|edit|delete|reply|show more|login|sign in|"
    r"评论|推荐|已弃用|编辑|删除|回复|加载更多|登录|登入)$",
    re.IGNORECASE,
)
SYNERGY_TAG_LABELS = {
    "Synergy": "强力联动",
    "Trap": "陷阱",
    "Fun": "娱乐",
    "Bug": "缺陷",
    "强力联动": "强力联动",
    "陷阱": "陷阱",
    "娱乐": "娱乐",
    "缺陷": "缺陷",
}
TIER_LABELS = {
    "Prismatic": "棱彩",
    "Gold": "黄金",
    "Silver": "白银",
    "棱彩": "棱彩",
    "彩色": "棱彩",
    "黄金": "黄金",
    "金色": "黄金",
    "白银": "白银",
    "银色": "白银",
}

install_summary_logging(level=logging.INFO, fmt="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
DEFAULT_REQUEST_USER_AGENT = "HextechApexSnapshot/1.0 (manual offline sync)"


@dataclass
class FetchedResource:
    url: str
    text: str
    source: str
    status_code: int = 200
    cloakbrowser_version: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ChampionInfo:
    id: str
    name: str
    title: str
    en_name: str
    aliases: list[str] = field(default_factory=list)
    slug: str = ""


@dataclass
class SynergyEntry:
    champion_slug: str
    augment_names: list[str]
    tier: str
    rating: str
    tag: str
    author: str
    is_original: bool
    content: str
    upvotes: int = 0
    downvotes: int = 0

    def to_compat_string(self) -> str:
        augment_text = ", ".join(dict.fromkeys(self.augment_names))
        originality = "原创" if self.is_original else "非原创"
        return " | ".join(
            [
                augment_text,
                self.tier or "未知",
                f"评分 {self.rating or '未知'}",
                self.tag or "强力联动",
                str(max(0, int(self.upvotes or 0))),
                str(max(0, int(self.downvotes or 0))),
                f"作者：{self.author or 'ApexLoL'}",
                originality,
                self.content,
            ]
        )


def get_request_user_agent() -> str:
    return os.getenv("APEX_USER_AGENT", DEFAULT_REQUEST_USER_AGENT).strip() or DEFAULT_REQUEST_USER_AGENT


def env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def normalize_name(name_str: str) -> str:
    if not name_str:
        return ""
    return "".join(ch for ch in str(name_str).lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def normalize_slug(value: str) -> str:
    return normalize_augment_name(str(value or "").replace(" ", "").replace("_", "").replace("-", ""))


def _sanitize_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:200]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _safe_exception_label(exc: Exception) -> str:
    return exc.__class__.__name__


def _clean_text(value: Any) -> str:
    text = unescape(str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_tier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in TIER_LABELS:
        return TIER_LABELS[text]
    lowered = text.lower()
    if "prismatic" in lowered or "棱彩" in text or "彩色" in text:
        return "棱彩"
    if "gold" in lowered or "黄金" in text or "金色" in text:
        return "黄金"
    if "silver" in lowered or "白银" in text or "银色" in text:
        return "白银"
    return text


def normalize_tag(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            tag = normalize_tag(item)
            if tag:
                return tag
        return "强力联动"
    text = str(value or "").strip()
    if not text:
        return "强力联动"
    return SYNERGY_TAG_LABELS.get(text, text)


def _resolve_static_data_path(filename: str) -> Path:
    base_name = os.path.basename(filename)
    if base_name != filename or filename not in ALLOWED_STATIC_DATA_FILES:
        raise ValueError(f"不允许访问的配置文件：{filename}")

    resolved = (STATIC_DATA_PATH / filename).resolve()
    static_root = STATIC_DATA_PATH.resolve()
    if resolved.parent != static_root:
        raise ValueError(f"配置文件路径越界：{filename}")
    return resolved


def _load_json_file(filename: str, expected_kind: str) -> dict:
    file_path = _resolve_static_data_path(filename)
    if not file_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{filename}")
    if file_path.stat().st_size > MAX_STATIC_DATA_FILE_SIZE:
        raise ValueError(f"配置文件过大：{filename}")

    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(f"{expected_kind} 配置格式错误：{filename}")
    if expected_kind == "core_data":
        for champ_id, champ_info in data.items():
            if not isinstance(champ_id, str) or not isinstance(champ_info, dict):
                raise ValueError(f"{expected_kind} 配置内容格式错误：{filename}")
    return data


@contextmanager
def _output_file_lock(lock_path: Path, timeout_seconds: int = OUTPUT_LOCK_TIMEOUT_SECONDS):
    deadline = time.monotonic() + timeout_seconds
    lock_fd = None
    try:
        while True:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                break
            except FileExistsError:
                try:
                    stale_age = time.time() - lock_path.stat().st_mtime
                    if stale_age > timeout_seconds * 4:
                        lock_path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待输出锁超时：{lock_path.name}")
                time.sleep(OUTPUT_LOCK_POLL_INTERVAL_SECONDS)
        yield
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(output_path: Path, payload: dict) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(output_path.parent),
            delete=False,
            suffix=".tmp",
        ) as f:
            temp_path = Path(f.name)
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, output_path)
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def summarize_synergy_payload(payload: dict) -> dict[str, int]:
    """统计协同 payload 的有效规模，用于发布熔断和 latest 指针。"""
    heroes = len(payload) if isinstance(payload, dict) else 0
    non_empty_heroes = 0
    synergy_entries = 0
    if not isinstance(payload, dict):
        return {"heroes": 0, "non_empty_heroes": 0, "synergy_entries": 0}

    for item in payload.values():
        if not isinstance(item, dict):
            continue
        items = item.get("synergy_items")
        if isinstance(items, list) and items:
            count = len(items)
        else:
            synergies = item.get("synergies")
            count = len(synergies) if isinstance(synergies, list) else 0
        if count > 0:
            non_empty_heroes += 1
            synergy_entries += count
    return {
        "heroes": heroes,
        "non_empty_heroes": non_empty_heroes,
        "synergy_entries": synergy_entries,
    }


def _load_existing_synergy_stats() -> dict[str, int]:
    current_path = Path(build_synergy_data_path())
    if not current_path.exists():
        return {"heroes": 0, "non_empty_heroes": 0, "synergy_entries": 0}
    try:
        with current_path.open("r", encoding="utf-8") as f:
            return summarize_synergy_payload(json.load(f))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"heroes": 0, "non_empty_heroes": 0, "synergy_entries": 0}


def _validate_publish_size(new_stats: dict[str, int], old_stats: dict[str, int]) -> None:
    """拒绝把明显不完整的新结果发布为 latest。"""
    new_count = int(new_stats.get("non_empty_heroes") or 0)
    old_count = int(old_stats.get("non_empty_heroes") or 0)
    if old_count > 0:
        minimum = max(1, int(old_count * SYNERGY_PUBLISH_MIN_RATIO))
        if new_count < minimum:
            raise ValueError(f"协同数据熔断：非空英雄 {new_count} < 旧快照 {old_count} 的 {SYNERGY_PUBLISH_MIN_RATIO:.0%}")
        return
    if new_count < MIN_FIRST_SYNERGY_NON_EMPTY_HEROES:
        raise ValueError(f"协同数据熔断：首次快照非空英雄 {new_count} < {MIN_FIRST_SYNERGY_NON_EMPTY_HEROES}")


def _hero_has_synergy_items(hero_payload: object) -> bool:
    if not isinstance(hero_payload, dict):
        return False
    items = hero_payload.get("synergy_items")
    if isinstance(items, list) and items:
        return True
    synergies = hero_payload.get("synergies")
    return isinstance(synergies, list) and bool(synergies)


def _load_latest_synergy_payload_for_merge() -> tuple[Optional[dict], dict]:
    pointer_path = Path(build_synergy_latest_pointer_path())
    pointer = load_synergy_latest_pointer()
    snapshot_path = get_latest_synergy_snapshot_path()
    meta = {
        "pointer_path": str(pointer_path),
        "pointer_loaded": bool(pointer),
        "snapshot_path": str(snapshot_path or ""),
        "fallback": False,
        "reason": "",
    }
    if not snapshot_path:
        meta["reason"] = "latest pointer and snapshot scan returned empty"
        return None, meta
    try:
        with Path(snapshot_path).open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        meta["reason"] = f"old latest unreadable: {_safe_exception_label(exc)}"
        return None, meta
    if not isinstance(payload, dict) or not all(isinstance(value, dict) for value in payload.values()):
        meta["reason"] = "old latest schema mismatch"
        return None, meta
    return payload, meta


def merge_payload_with_latest_snapshot(payload: dict) -> tuple[dict, dict]:
    """发布前合并旧 latest：本轮非空英雄用新数据，本轮空/缺失英雄沿用旧非空数据。"""
    old_payload, merge_meta = _load_latest_synergy_payload_for_merge()
    if old_payload is None:
        merge_meta.update(
            {
                "merged": False,
                "new_non_empty_heroes": summarize_synergy_payload(payload).get("non_empty_heroes", 0),
                "old_non_empty_heroes": 0,
                "merged_non_empty_heroes": summarize_synergy_payload(payload).get("non_empty_heroes", 0),
                "updated_heroes": summarize_synergy_payload(payload).get("non_empty_heroes", 0),
                "retained_old_heroes": 0,
                "net_regression": False,
            }
        )
        logger.warning("旧 latest 协同快照不可用于增量 merge，按全新 payload 发布：%s", merge_meta["reason"])
        return payload, merge_meta

    merged = dict(payload)
    updated_heroes = 0
    retained_old_heroes = 0
    all_hero_ids = set(old_payload) | set(payload)
    for hero_id in sorted(all_hero_ids):
        new_item = payload.get(hero_id)
        old_item = old_payload.get(hero_id)
        if _hero_has_synergy_items(new_item):
            updated_heroes += 1
            continue
        if _hero_has_synergy_items(old_item):
            # ApexLoL 全量抓取会被 IP 级反爬打断；本轮空结果不覆盖旧 latest 的有效联动。
            merged[hero_id] = old_item
            retained_old_heroes += 1

    old_stats = summarize_synergy_payload(old_payload)
    new_stats = summarize_synergy_payload(payload)
    merged_stats = summarize_synergy_payload(merged)
    merge_meta.update(
        {
            "merged": True,
            "old_non_empty_heroes": old_stats["non_empty_heroes"],
            "new_non_empty_heroes": new_stats["non_empty_heroes"],
            "merged_non_empty_heroes": merged_stats["non_empty_heroes"],
            "updated_heroes": updated_heroes,
            "retained_old_heroes": retained_old_heroes,
            "net_regression": merged_stats["non_empty_heroes"] < old_stats["non_empty_heroes"],
        }
    )
    logger.info(
        "协同 payload 已与旧 latest 增量合并：new_non_empty=%s retained_old=%s merged_non_empty=%s",
        merge_meta["new_non_empty_heroes"],
        retained_old_heroes,
        merge_meta["merged_non_empty_heroes"],
    )
    return merged, merge_meta


def _new_synergy_snapshot_path() -> Path:
    timestamp_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(build_next_synergy_snapshot_path(timestamp_label))


def write_synergy_refresh_meta(
    *,
    target_path: Path,
    base_url: str,
    resources: int,
    mapped: int,
    stats: Optional[dict[str, int]] = None,
) -> None:
    """写入 latest 指针，固定文件名不再作为刷新成功依据。"""
    meta_path = Path(build_synergy_latest_pointer_path())
    stat_payload = stats or {}
    _atomic_write_json(
        meta_path,
        {
            "version": SYNERGY_REFRESH_META_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "filename": target_path.name,
            "base_url": base_url,
            "source": "apex",
            "resources": resources,
            "mapped": mapped,
            "heroes": int(stat_payload.get("heroes") or 0),
            "non_empty_heroes": int(stat_payload.get("non_empty_heroes") or 0),
            "synergy_entries": int(stat_payload.get("synergy_entries") or 0),
        },
    )


def build_core_info(core_data: dict) -> dict[str, ChampionInfo]:
    result = {}
    for champ_id, champ_info in core_data.items():
        name = str(champ_info.get("name") or "").strip()
        if not name:
            continue
        en_name = str(champ_info.get("en_name") or "").strip()
        title = str(champ_info.get("title") or "").strip()
        aliases = champ_info.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        result[str(champ_id)] = ChampionInfo(
            id=str(champ_id),
            name=name,
            title=title,
            en_name=en_name,
            aliases=[str(item).strip() for item in aliases if str(item).strip()],
            slug=normalize_slug(en_name or title or name),
        )
    return result


def build_champion_lookup(core_info: dict[str, ChampionInfo]) -> dict[str, ChampionInfo]:
    lookup = {}
    for champ in core_info.values():
        values = [champ.id, champ.name, champ.title, champ.en_name, champ.slug, *champ.aliases]
        for value in values:
            normalized = normalize_name(value)
            if normalized:
                lookup.setdefault(normalized, champ)
            slug = normalize_slug(value)
            if slug:
                lookup.setdefault(slug, champ)
    return lookup


class ApexSource:
    """同源页面/资源获取层。"""

    def __init__(self):
        self.base_url = os.environ.get("APEX_BASE_URL", "https://apexlol.info/zh").rstrip("/")
        parsed_base = urlparse(self.base_url)
        if parsed_base.scheme != "https" or not parsed_base.netloc:
            raise ValueError("APEX_BASE_URL 必须是有效的 https URL")
        self.allowed_netloc = parsed_base.netloc
        self.allowed_json_netlocs = self._build_allowed_json_netlocs()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": get_request_user_agent()})
        self._browser_driver = None
        self.blocked = False
        self.last_fetch_error = ""
        self._browser_timeout = int(os.getenv("APEX_BROWSER_TIMEOUT_SECONDS", "18") or "18")
        self._profile_root = os.path.join(SELENIUM_PROFILE_DIR, f"session-{os.getpid()}-{int(time.time())}")
        logger.info("ApexSource 初始化完成：base=%s", _sanitize_url_for_log(self.base_url))

    def close(self) -> None:
        if self._browser_driver is not None:
            try:
                self._browser_driver.quit()
            except Exception:
                pass
            self._browser_driver = None

    def is_allowed_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc == self.allowed_netloc

    def is_allowed_json_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() in self.allowed_json_netlocs

    def _build_allowed_json_netlocs(self) -> set[str]:
        extra_hosts = {
            host.strip().lower()
            for host in os.getenv("APEX_JSON_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        }
        return {self.allowed_netloc.lower(), *extra_hosts}

    def build_allowed_url(self, href: str) -> Optional[str]:
        candidate = urljoin(f"{self.base_url}/", str(href or "").strip())
        if not self.is_allowed_url(candidate):
            logger.warning("跳过非白名单链接：%s", _sanitize_url_for_log(candidate))
            return None
        parsed = urlparse(candidate)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    def fetch_requests(self, url: str) -> Optional[FetchedResource]:
        if not self.is_allowed_url(url):
            logger.warning("拒绝非白名单请求：%s", _sanitize_url_for_log(url))
            return None

        retryable_status_codes = {429, 500, 502, 503, 504}
        for attempt in range(MAX_FETCH_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
                response.encoding = "utf-8"
                if response.status_code == 200 and not self._is_cloudflare_block(response.text):
                    return FetchedResource(url=url, text=response.text, source="requests", status_code=200)
                if response.status_code == 403 or self._is_cloudflare_block(response.text):
                    self.blocked = True
                    logger.warning("Apex 普通请求被拒绝：url=%s status=%s", _sanitize_url_for_log(url), response.status_code)
                    return None
                if response.status_code in retryable_status_codes and attempt < MAX_FETCH_RETRIES:
                    time.sleep(RETRY_BACKOFF_FACTOR * (2 ** attempt))
                    continue
                logger.error("页面状态异常：url=%s status=%s", _sanitize_url_for_log(url), response.status_code)
                return None
            except requests.RequestException as exc:
                if attempt < MAX_FETCH_RETRIES:
                    time.sleep(RETRY_BACKOFF_FACTOR * (2 ** attempt))
                    continue
                logger.error("页面加载失败：url=%s error=%s", _sanitize_url_for_log(url), _safe_exception_label(exc))
                return None
        return None

    def fetch(
        self,
        url: str,
        *,
        allow_browser: bool = False,
        allow_cloakbrowser: bool = False,
        cloakbrowser_wait_until: Optional[str] = None,
        cloakbrowser_post_wait_ms: Optional[int] = None,
    ) -> Optional[FetchedResource]:
        is_detail = "/champions/" in urlparse(url).path
        # 1) 普通请求：拿到 origin 页直接用；但英雄详情页若是 HTTP 200 的非 origin 壳页
        #    （Access Denied / Next 错误页 / 缺联动 hydration），不能当成功，继续走后端回退。
        requests_resource = self.fetch_requests(url)
        if requests_resource is not None:
            if not is_detail or not self._origin_failure_reason(requests_resource.text):
                return requests_resource

        # 2) CloakBrowser：穿 CF 并取最终 HTML；fetch_cloakbrowser 内部已对详情页做 origin 校验，
        #    无 error 即视为拿到可用 origin 页。
        cloak_resource = None
        if allow_cloakbrowser or env_flag("APEX_ALLOW_CLOAKBROWSER"):
            cloak_resource = self.fetch_cloakbrowser(
                url,
                wait_until=cloakbrowser_wait_until,
                post_wait_ms=cloakbrowser_post_wait_ms,
            )
            if cloak_resource is not None and not cloak_resource.error:
                return cloak_resource

        # 3) Selenium：CloakBrowser 不可用/超时/被拦时仍尝试旧浏览器回退。
        if allow_browser and env_flag("APEX_ALLOW_BROWSER"):
            selenium_resource = self.fetch_browser(url)
            if selenium_resource is not None:
                return selenium_resource
        elif allow_browser:
            logger.info("跳过 Apex 浏览器 fallback：APEX_ALLOW_BROWSER 未启用")

        # 4) 没有任何后端拿到 origin：返回信息量最大的尝试结果（CloakBrowser 的 error 详情优先，
        #    其次普通请求拿到的非 origin 200 页）供上层判定 failed；都没有则返回 None。
        return cloak_resource or requests_resource

    def fetch_configured_json_resource(self) -> Optional[FetchedResource]:
        raw_url = os.getenv("APEX_SYNERGY_JSON_URL", "").strip()
        if not raw_url:
            return None
        if not self.is_allowed_json_url(raw_url):
            logger.error("APEX_SYNERGY_JSON_URL 不在允许的 https host 内：%s", _sanitize_url_for_log(raw_url))
            return None
        try:
            response = self.session.get(raw_url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            if len(response.content) > MAX_JSON_RESOURCE_SIZE:
                logger.error("APEX_SYNERGY_JSON_URL 响应过大，已拒绝：%s", _sanitize_url_for_log(raw_url))
                return None
            response.encoding = "utf-8"
            payload = response.json()
            if not isinstance(payload, (dict, list)):
                logger.error("APEX_SYNERGY_JSON_URL 不是 JSON object/list：%s", _sanitize_url_for_log(raw_url))
                return None
            return FetchedResource(
                url=raw_url,
                text=json.dumps(payload, ensure_ascii=False),
                source="json-url",
                status_code=response.status_code,
            )
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) in {401, 403, 429}:
                self.blocked = True
            logger.warning("APEX_SYNERGY_JSON_URL 读取失败：url=%s error=%s", _sanitize_url_for_log(raw_url), _safe_exception_label(exc))
            return None

    def fetch_cloakbrowser(
        self,
        url: str,
        *,
        wait_until: Optional[str] = None,
        post_wait_ms: Optional[int] = None,
    ) -> Optional[FetchedResource]:
        if not self.is_allowed_url(url):
            logger.warning("CloakBrowser 拒绝非白名单请求：%s", _sanitize_url_for_log(url))
            return None
        try:
            from hextech.scraping.transport.cloakbrowser_client import fetch_page
        except ImportError as exc:
            self.last_fetch_error = str(exc)
            logger.error("CloakBrowser 后端不可用：%s", str(exc)[:240])
            return None

        timeout_ms = int(os.getenv("APEX_CLOAKBROWSER_TIMEOUT_MS", "30000") or "30000")
        headless = os.getenv("APEX_CLOAKBROWSER_HEADLESS", "1").strip() != "0"
        humanize = env_flag("APEX_CLOAKBROWSER_HUMANIZE")
        wait_until = wait_until or os.getenv("APEX_CLOAKBROWSER_WAIT_UNTIL", "networkidle").strip() or "networkidle"
        if post_wait_ms is None:
            post_wait_ms = int(os.getenv("APEX_CLOAKBROWSER_POST_WAIT_MS", "0") or "0")
        max_attempts = max(1, int(os.getenv("APEX_CLOAKBROWSER_ATTEMPTS", "2") or "2"))
        result = None
        for attempt in range(1, max_attempts + 1):
            result = fetch_page(
                url,
                timeout_ms=timeout_ms,
                headless=headless,
                humanize=humanize,
                wait_until=wait_until,
                post_wait_ms=post_wait_ms,
            )
            if not result.error:
                break
            if attempt < max_attempts:
                sleep_seconds = max(2.0, RETRY_BACKOFF_FACTOR * (2 ** attempt))
                logger.warning(
                    "CloakBrowser 访问失败后重试：url=%s attempt=%s/%s wait=%.1fs error=%s",
                    _sanitize_url_for_log(url),
                    attempt,
                    max_attempts,
                    sleep_seconds,
                    result.error[:160],
                )
                time.sleep(sleep_seconds)
        if result is None:
            return None
        html = result.html or ""
        self.last_fetch_error = result.error or ""
        if result.error:
            logger.error("CloakBrowser 访问失败：url=%s error=%s", _sanitize_url_for_log(url), result.error[:240])
            return FetchedResource(
                url=url,
                text=html,
                source="cloakbrowser",
                status_code=result.status_code or 0,
                cloakbrowser_version=result.cloakbrowser_version,
                error=result.error,
            )
        if not html or self._is_cloudflare_block(html):
            self.blocked = True
            logger.error("CloakBrowser 未取得可解析 Apex 页面：url=%s status=%s", _sanitize_url_for_log(url), result.status_code)
            return FetchedResource(
                url=url,
                text=html,
                source="cloakbrowser",
                status_code=result.status_code or 0,
                cloakbrowser_version=result.cloakbrowser_version,
                error="cloudflare_block_or_empty_html",
            )
        if "/champions/" in urlparse(url).path:
            origin_error = self._origin_failure_reason(html)
            if origin_error:
                if origin_error in {"cloudflare_block", "access_denied"}:
                    self.blocked = True
                logger.error(
                    "CloakBrowser 终态不是 Apex 英雄联动 origin 页面：url=%s status=%s reason=%s",
                    _sanitize_url_for_log(url),
                    result.status_code,
                    origin_error,
                )
                return FetchedResource(
                    url=url,
                    text=html,
                    source="cloakbrowser",
                    status_code=result.status_code or 0,
                    cloakbrowser_version=result.cloakbrowser_version,
                    error=origin_error,
                )
        return FetchedResource(
            url=url,
            text=html,
            source="cloakbrowser",
            status_code=result.status_code or 200,
            cloakbrowser_version=result.cloakbrowser_version,
        )

    def fetch_browser(self, url: str) -> Optional[FetchedResource]:
        if not self.is_allowed_url(url):
            logger.warning("浏览器拒绝非白名单请求：%s", _sanitize_url_for_log(url))
            return None
        try:
            driver = self._get_browser_driver()
            driver.get(url)
            self._wait_for_browser_ready(driver, url)
            parsed_path = urlparse(url).path.lower()
            if parsed_path.endswith((".js", ".json", ".txt")):
                text = driver.execute_script("return document.body ? document.body.innerText : document.documentElement.innerText") or ""
                if text and not self._is_cloudflare_block(text):
                    return FetchedResource(url=url, text=text, source="selenium-text", status_code=200)
            html = driver.page_source or ""
            if not html or self._is_cloudflare_block(html):
                self.blocked = True
                logger.error("浏览器未取得可解析 Apex 页面：url=%s", _sanitize_url_for_log(url))
                return None
            return FetchedResource(url=url, text=html, source="selenium", status_code=200)
        except Exception as exc:
            logger.error(
                "浏览器访问失败：url=%s error=%s detail=%s",
                _sanitize_url_for_log(url),
                _safe_exception_label(exc),
                str(exc)[:240],
            )
            return None

    def discover_resources(self) -> list[FetchedResource]:
        snapshot_resources = self._load_snapshot_resources()
        if snapshot_resources:
            logger.info("使用 Apex 本地 snapshot 资源：count=%s", len(snapshot_resources))
            return snapshot_resources

        if not env_flag("APEX_ALLOW_ONLINE_FETCH"):
            logger.error("未找到 Apex snapshot，且 APEX_ALLOW_ONLINE_FETCH 未启用；保留旧协同快照")
            return []

        allow_browser = env_flag("APEX_ALLOW_BROWSER")
        json_resource = self.fetch_configured_json_resource()
        seeds = [self.base_url, f"{self.base_url}/champions", f"{self.base_url}/hextech"]
        resources: list[FetchedResource] = []
        seen_urls = set()
        script_urls = []
        online_delay = float(os.getenv("APEX_ONLINE_FETCH_DELAY_SECONDS", str(APEX_ONLINE_FETCH_DELAY_SECONDS)) or "0")

        for url in seeds:
            resource = self.fetch(url, allow_browser=allow_browser)
            if not resource or resource.url in seen_urls:
                continue
            seen_urls.add(resource.url)
            resources.append(resource)
            script_urls.extend(self._extract_script_urls(resource.text))
            if online_delay > 0:
                time.sleep(online_delay)

        detail_urls = self._extract_champion_detail_urls(resources)
        for detail_url in detail_urls:
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)
            resource = self.fetch(detail_url, allow_browser=allow_browser)
            if resource:
                resources.append(resource)
            if online_delay > 0:
                time.sleep(online_delay)

        if not detail_urls or os.getenv("APEX_FETCH_JS_CHUNKS", "0").strip() == "1":
            for script_url in script_urls:
                if script_url in seen_urls:
                    continue
                seen_urls.add(script_url)
                script = self.fetch(script_url, allow_browser=allow_browser)
                if script:
                    resources.append(script)
                if online_delay > 0:
                    time.sleep(online_delay)

        if json_resource and json_resource.url not in seen_urls:
            resources.append(json_resource)

        return resources

    def _extract_champion_detail_urls(self, resources: Iterable[FetchedResource]) -> list[str]:
        max_pages = int(os.getenv("APEX_MAX_CHAMPION_DETAIL_PAGES", "0") or "0")
        urls = []
        for resource in resources:
            for raw_href in CHAMPION_DETAIL_HREF_PATTERN.findall(resource.text or ""):
                candidate = self.build_allowed_url(raw_href)
                if candidate and candidate not in urls:
                    urls.append(candidate)
                    if max_pages > 0 and len(urls) >= max_pages:
                        return urls
        return urls

    def _load_snapshot_resources(self) -> list[FetchedResource]:
        raw_snapshot_dir = os.getenv("APEX_SNAPSHOT_DIR", "").strip()
        snapshot_dir = (
            Path(raw_snapshot_dir).expanduser().resolve()
            if raw_snapshot_dir
            else Path(DEFAULT_APEX_MANUAL_SNAPSHOT_DIR).resolve()
        )

        allowed_root = Path(DEFAULT_APEX_SNAPSHOT_DIR).resolve()
        try:
            snapshot_dir.relative_to(allowed_root)
        except ValueError:
            logger.error("APEX_SNAPSHOT_DIR 必须位于 %s 下：%s", allowed_root, snapshot_dir)
            return []
        if not snapshot_dir.exists() or not snapshot_dir.is_dir():
            logger.warning("Apex snapshot 目录不存在或不是目录：%s", snapshot_dir)
            return []

        resources: list[FetchedResource] = []
        allowed_suffixes = {".html", ".htm", ".js", ".json", ".txt"}
        for path in sorted(snapshot_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            try:
                path.relative_to(snapshot_dir)
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                logger.warning("跳过 Apex snapshot 文件：file=%s error=%s", path.name, _safe_exception_label(exc))
                continue
            if not text.strip():
                continue
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.relative_to(snapshot_dir).as_posix())
            resources.append(FetchedResource(
                url=f"{self.base_url}/snapshot/{safe_name}",
                text=text,
                source="snapshot",
                status_code=200,
            ))
        return resources

    def _extract_script_urls(self, html: str) -> list[str]:
        urls = []
        for raw_src in SCRIPT_SRC_PATTERN.findall(html or ""):
            candidate = self.build_allowed_url(raw_src)
            if candidate:
                urls.append(candidate)
        for match in BUNDLE_APP_JS_PATTERN.findall(html or ""):
            candidate = self.build_allowed_url(match)
            if candidate:
                urls.append(candidate)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _is_cloudflare_block(text: str) -> bool:
        lowered = (text or "")[:8000].lower()
        legacy_block = "attention required" in lowered and "cloudflare" in lowered
        strong_challenge = "challenges.cloudflare.com" in lowered or "_cf_chl_opt" in lowered
        managed_challenge = "just a moment" in lowered or "请稍候" in lowered
        if strong_challenge:
            return True
        return legacy_block or managed_challenge

    @classmethod
    def _origin_failure_reason(cls, text: str) -> str:
        """识别 CloakBrowser 终态是否真是 ApexLoL 英雄联动 origin 页面。"""
        html = text or ""
        lowered = html[:200_000].lower()
        stripped_text = re.sub(r"\s+", " ", html).strip().lower()
        if not html:
            return "empty_html"
        if cls._is_cloudflare_block(html):
            return "cloudflare_block"
        if len(html.encode("utf-8", errors="ignore")) <= 2048 and APEX_ACCESS_DENIED_MARKER in stripped_text:
            return "access_denied"
        if any(marker in lowered for marker in APEX_NEXT_ERROR_MARKERS):
            return "origin_5xx_error_page"
        if not any(marker in html for marker in APEX_ORIGIN_SYNERGY_MARKERS):
            return "missing_origin_synergy_hydration"
        return ""

    def _get_browser_driver(self):
        if self._browser_driver is not None:
            return self._browser_driver

        browser = os.getenv("APEX_BROWSER", "auto").strip().lower() or "auto"
        headless = os.getenv("APEX_HEADLESS", "1").strip() != "0"
        errors = []
        os.makedirs(SELENIUM_CACHE_DIR, exist_ok=True)
        ensure_runtime_profile_dir()
        ensure_private_runtime_dir(self._profile_root)
        os.environ.setdefault("SE_CACHE_PATH", SELENIUM_CACHE_DIR)

        if browser in {"auto", "edge"}:
            try:
                from selenium import webdriver
                from selenium.webdriver.edge.options import Options as EdgeOptions

                options = EdgeOptions()
                edge_binary = self._find_browser_binary("edge")
                if edge_binary:
                    options.binary_location = edge_binary
                if headless:
                    options.add_argument("--headless=new")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--disable-crash-reporter")
                options.add_argument("--disable-crashpad")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--remote-debugging-port=0")
                options.add_argument("--window-size=1365,900")
                options.add_argument(f"--user-data-dir={os.path.join(self._profile_root, 'edge')}")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                driver = webdriver.Edge(options=options)
                driver.set_page_load_timeout(self._browser_timeout)
                self._browser_driver = driver
                logger.info("Apex Selenium 使用 Edge 启动")
                return driver
            except Exception as exc:
                errors.append(f"edge={_safe_exception_label(exc)}:{str(exc)[:160]}")

        if browser in {"auto", "chrome"}:
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options as ChromeOptions

                options = ChromeOptions()
                chrome_binary = self._find_browser_binary("chrome")
                if chrome_binary:
                    options.binary_location = chrome_binary
                if headless:
                    options.add_argument("--headless=new")
                options.add_argument("--disable-gpu")
                options.add_argument("--disable-extensions")
                options.add_argument("--disable-crash-reporter")
                options.add_argument("--disable-crashpad")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--remote-debugging-port=0")
                options.add_argument("--window-size=1365,900")
                options.add_argument(f"--user-data-dir={os.path.join(self._profile_root, 'chrome')}")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                driver = webdriver.Chrome(options=options)
                driver.set_page_load_timeout(self._browser_timeout)
                self._browser_driver = driver
                logger.info("Apex Selenium 使用 Chrome 启动")
                return driver
            except Exception as exc:
                errors.append(f"chrome={_safe_exception_label(exc)}:{str(exc)[:160]}")

        raise RuntimeError("无法启动 Selenium 浏览器：" + ", ".join(errors))

    @staticmethod
    def _find_browser_binary(kind: str) -> str:
        if kind == "edge":
            env_value = os.getenv("APEX_EDGE_BINARY", "").strip()
            candidates = [
                env_value,
                os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
            ]
        else:
            env_value = os.getenv("APEX_CHROME_BINARY", "").strip()
            candidates = [
                env_value,
                os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ]
        return next((path for path in candidates if path and os.path.exists(path)), "")

    def _wait_for_browser_ready(self, driver, url: str = "") -> None:
        deadline = time.monotonic() + self._browser_timeout
        while time.monotonic() < deadline:
            try:
                state = driver.execute_script("return document.readyState")
                if state == "complete":
                    break
            except Exception:
                return
            time.sleep(0.25)

        parsed_path = urlparse(url).path.lower()
        if "/champions/" not in parsed_path:
            time.sleep(1.0)
            return

        # Next.js 详情页会先进入 readyState=complete，随后才把联动列表灌入页面。
        # 这里等到可见文本出现联动标记，或 HTML 长度稳定，避免抓到只有 shell 的页面。
        last_length = 0
        stable_ticks = 0
        while time.monotonic() < deadline:
            try:
                body_text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
                html = driver.page_source or ""
            except Exception:
                return
            if any(marker in body_text for marker in ("评分", "作者", "强力联动", "推荐出装")):
                return
            html_length = len(html)
            stable_ticks = stable_ticks + 1 if html_length == last_length and html_length > 20000 else 0
            if stable_ticks >= 3:
                return
            last_length = html_length
            time.sleep(0.5)


class SynergyExtractor:
    """从 HTML/JS/JSON 资源中提取结构化联动对象。"""

    def __init__(self, champion_lookup: dict[str, ChampionInfo], augment_name_map: dict[str, str]):
        self.champion_lookup = champion_lookup
        self.augment_name_map = augment_name_map

    def extract(self, resources: Iterable[FetchedResource]) -> dict[str, list[SynergyEntry]]:
        results: dict[str, list[SynergyEntry]] = {}
        errors = []
        for resource in resources:
            try:
                for entry in self._extract_from_resource(resource):
                    results.setdefault(entry.champion_slug, []).append(entry)
            except Exception as exc:
                errors.append(f"{Path(urlparse(resource.url).path).name or resource.url}:{_safe_exception_label(exc)}")
                logger.debug("资源解析失败：%s", _sanitize_url_for_log(resource.url), exc_info=True)

        if results:
            return self._dedupe_entries(results)

        raise ValueError("联动解析结果为空" + (f"；errors={';'.join(errors[:6])}" if errors else ""))

    def _extract_from_resource(self, resource: FetchedResource) -> list[SynergyEntry]:
        text = resource.text or ""
        entries = []
        if "<html" in text[:1000].lower():
            entries.extend(self._extract_from_html(text, resource.url))
        if text.strip().startswith(("{", "[")):
            try:
                entries.extend(self._extract_from_json_payload(json.loads(text), fallback_slug=""))
            except json.JSONDecodeError:
                pass
        entries.extend(self._extract_old_bundle(text))
        entries.extend(self._extract_generic_js_objects(text))
        return entries

    def _extract_from_html(self, html: str, url: str) -> list[SynergyEntry]:
        entries = []
        for match in HYDRATION_PATTERN.findall(html) + JSON_SCRIPT_PATTERN.findall(html):
            try:
                payload = json.loads(unescape(match).strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            entries.extend(self._extract_from_json_payload(payload, fallback_slug=self._slug_from_url(url)))
        entries.extend(self._extract_from_visible_html_text(html, url))
        return entries

    def _extract_from_visible_html_text(self, html: str, url: str) -> list[SynergyEntry]:
        soup = BeautifulSoup(html or "", "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        lines = []
        for raw_line in soup.get_text("\n").splitlines():
            line = _clean_text(raw_line)
            if line and line not in lines[-3:]:
                lines.append(line)
        if not lines:
            return []

        url_slug = self._slug_from_url(url)
        fallback_slug = url_slug if url_slug in self.champion_lookup else (self._slug_from_visible_lines(lines) or url_slug)
        entries = []
        for index, line in enumerate(lines):
            rating_match = VISIBLE_RATING_PATTERN.match(line)
            if not rating_match:
                continue

            augment_names = []
            tier = ""
            cursor = index - 1
            while cursor >= 0 and len(augment_names) < 4:
                current = lines[cursor]
                if current == "关联套装":
                    cursor -= 1
                    continue
                if re.match(r"^关联\s*\d+\s*个海克斯$", current):
                    if augment_names:
                        break
                    cursor -= 1
                    continue
                normalized_tier = normalize_tier(current)
                if normalized_tier != current or current in TIER_LABELS:
                    tier = tier or normalized_tier
                    cursor -= 1
                    continue
                resolved_names = self._resolve_known_augment_names(current)
                if resolved_names and self._looks_like_augment_name(current, resolved_names):
                    augment_names = resolved_names + augment_names
                    cursor -= 1
                    continue
                if self._looks_like_visible_augment_name(current):
                    augment_names = [current] + augment_names
                    cursor -= 1
                    continue
                break

            if not augment_names:
                continue

            rating, tag = self._parse_visible_rating_tag(line, lines[index + 1:index + 6])
            author, content, is_original, upvotes, downvotes = self._parse_visible_author_content(lines, index + 1)
            if not content:
                continue
            entries.append(SynergyEntry(
                champion_slug=fallback_slug,
                augment_names=list(dict.fromkeys(augment_names)),
                tier=tier or "黄金",
                rating=rating,
                tag=tag,
                author=author,
                is_original=is_original or "原创" in line.lower() or "original" in line.lower(),
                content=content,
                upvotes=upvotes,
                downvotes=downvotes,
            ))
        return [entry for entry in entries if entry.champion_slug]

    def _parse_visible_rating_tag(self, line: str, following_lines: Optional[list[str]] = None) -> tuple[str, str]:
        rating_match = VISIBLE_RATING_PATTERN.match(line or "")
        rating = rating_match.group(1).upper() if rating_match else "未知"
        lowered = (line or "").lower()
        if "trap" in lowered or "陷阱" in line:
            tag = "陷阱"
        elif "fun" in lowered or "娱乐" in line:
            tag = "娱乐"
        elif "bug" in lowered or "缺陷" in line:
            tag = "缺陷"
        else:
            tag = "强力联动"
        for candidate in following_lines or []:
            if candidate in SYNERGY_TAG_LABELS:
                tag = normalize_tag(candidate)
                break
            if candidate.isdigit() or candidate == "作者" or candidate.startswith(("作者：", "作者:")):
                break
        return rating, tag

    def _parse_visible_author_content(self, lines: list[str], start_index: int) -> tuple[str, str, bool, int, int]:
        author = "ApexLoL"
        is_original = False
        votes = []
        content_lines = []
        cursor = start_index
        while cursor < len(lines) and cursor < start_index + 12:
            candidate = lines[cursor]
            if candidate in SYNERGY_TAG_LABELS or candidate in {"原创", "非原创"}:
                is_original = is_original or candidate == "原创"
                cursor += 1
                continue
            if candidate.isdigit():
                votes.append(self._int_value(candidate))
                cursor += 1
                continue
            if candidate == "作者":
                next_line = lines[cursor + 1] if cursor + 1 < len(lines) else ""
                if next_line and not self._is_visible_noise_line(next_line):
                    author = next_line
                    cursor += 2
                    break
            if candidate.startswith(("作者：", "作者:")):
                author = candidate.split(":", 1)[-1].split("：", 1)[-1].strip() or author
                cursor += 1
                break
            cursor += 1

        while cursor < len(lines) and len(content_lines) < 12:
            candidate = lines[cursor]
            if (
                VISIBLE_RATING_PATTERN.match(candidate)
                or VISIBLE_STOP_LINE_PATTERN.match(candidate)
                or self._is_visible_noise_line(candidate)
            ):
                break
            if self._looks_like_augment_name(candidate, self._resolve_known_augment_names(candidate)):
                next_line = lines[cursor + 1] if cursor + 1 < len(lines) else ""
                if normalize_tier(next_line) != next_line or next_line in TIER_LABELS:
                    break
            if not candidate.startswith(("作者：", "作者:")):
                content_lines.append(candidate)
            cursor += 1

        upvotes = votes[0] if votes else 0
        downvotes = votes[1] if len(votes) > 1 else 0
        return author, _clean_text(" ".join(content_lines)), is_original, upvotes, downvotes

    @staticmethod
    def _is_visible_noise_line(candidate: str) -> bool:
        if not candidate:
            return True
        if candidate in {"+", "-", "推荐出装", "推荐召唤师技能"}:
            return True
        if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", candidate):
            return True
        if re.match(r"^关联\s*\d+\s*个海克斯$", candidate):
            return True
        return False

    def _looks_like_augment_name(self, raw_name: str, resolved_names: list[str]) -> bool:
        text = str(raw_name or "").strip()
        if not text or not resolved_names:
            return False
        normalized = normalize_augment_name(text)
        resolved_tokens = {normalize_augment_name(name) for name in resolved_names}
        if normalized in resolved_tokens:
            return True
        return normalized in self.augment_name_map or text in self.augment_name_map

    def _looks_like_visible_augment_name(self, raw_name: str) -> bool:
        text = str(raw_name or "").strip()
        if not text or self._is_visible_noise_line(text):
            return False
        if text in SYNERGY_TAG_LABELS or text in TIER_LABELS or normalize_tier(text) != text:
            return False
        if VISIBLE_RATING_PATTERN.match(text) or text in {"作者", "原创", "非原创", "关联套装"}:
            return False
        if re.match(r"^关联\s*\d+\s*个海克斯$", text):
            return False
        return 1 < len(text) <= 40

    def _resolve_known_augment_names(self, raw_name: str) -> list[str]:
        key = str(raw_name or "").strip()
        if not key:
            return []
        candidates = [key, normalize_augment_name(key), Path(key).stem, normalize_augment_name(Path(key).stem)]
        for candidate in candidates:
            resolved = self.augment_name_map.get(candidate)
            if resolved:
                return [resolved]
        return []

    def _slug_from_visible_lines(self, lines: list[str]) -> str:
        head_text = " ".join(lines[:80])
        normalized_head = normalize_name(head_text)
        for key, champion in sorted(self.champion_lookup.items(), key=lambda item: len(item[0]), reverse=True):
            if not key or key.isdigit() or len(key) <= 1:
                continue
            if key.isascii() and len(key) < 3:
                continue
            if key in normalized_head:
                return champion.slug or normalize_slug(champion.en_name or champion.name)
        return ""

    def _extract_from_json_payload(self, payload: Any, fallback_slug: str) -> list[SynergyEntry]:
        entries = []
        for item, path in self._walk_json(payload):
            if not isinstance(item, dict):
                continue
            entry = self._entry_from_dict(item, fallback_slug=fallback_slug or self._slug_from_path(path))
            if entry:
                entries.append(entry)
        return entries

    def _walk_json(self, value: Any, path: tuple[str, ...] = ()):
        yield value, path
        if isinstance(value, dict):
            for key, child in value.items():
                yield from self._walk_json(child, (*path, str(key)))
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                yield from self._walk_json(child, (*path, str(idx)))

    def _entry_from_dict(self, item: dict, fallback_slug: str) -> Optional[SynergyEntry]:
        augment_names = self._resolve_augment_names(item)
        if not augment_names:
            return None
        content = self._resolve_content(item)
        if not content:
            return None
        champion_slug = self._resolve_champion_slug(item, fallback_slug=fallback_slug)
        if not champion_slug:
            return None
        return SynergyEntry(
            champion_slug=champion_slug,
            augment_names=augment_names,
            tier=normalize_tier(
                item.get("tier")
                or item.get("rarity")
                or item.get("rank")
                or item.get("augmentTier")
                or item.get("hextechTier")
            ),
            rating=self._resolve_rating(item),
            tag=normalize_tag(item.get("tags") or item.get("tag") or item.get("type")),
            author=str(item.get("author") or item.get("contributor") or item.get("user") or "ApexLoL").strip() or "ApexLoL",
            is_original=self._resolve_original_flag(item),
            content=content,
            upvotes=self._int_value(item.get("upvotes") or item.get("upVotes") or item.get("likes")),
            downvotes=self._int_value(item.get("downvotes") or item.get("downVotes") or item.get("dislikes")),
        )

    def _resolve_champion_slug(self, item: dict, fallback_slug: str) -> str:
        raw_values = [
            item.get("championSlug"),
            item.get("champion"),
            item.get("championName"),
            item.get("championId"),
            item.get("hero"),
            item.get("heroName"),
            item.get("champion_id"),
            item.get("champion_slug"),
            item.get("champion_name"),
            item.get("hero_id"),
            item.get("hero_name"),
            fallback_slug,
        ]
        for raw in raw_values:
            if raw is None:
                continue
            normalized = normalize_name(raw)
            slug = normalize_slug(raw)
            champion = self.champion_lookup.get(normalized) or self.champion_lookup.get(slug)
            if champion:
                return champion.slug or normalize_slug(champion.en_name or champion.name)
        return ""

    def _resolve_content(self, item: dict) -> str:
        note = (
            item.get("note")
            or item.get("content")
            or item.get("comment")
            or item.get("body")
            or item.get("guide")
            or item.get("description")
            or item.get("text")
            or item.get("tips")
        )
        if isinstance(note, dict):
            note = note.get("zh") or note.get("zh_CN") or note.get("cn") or note.get("en") or next(iter(note.values()), "")
        return _clean_text(note)

    def _resolve_augment_names(self, item: dict) -> list[str]:
        raw_values = []

        def append_raw(value: Any) -> None:
            if isinstance(value, list):
                for child in value:
                    append_raw(child)
            elif value is not None:
                raw_values.append(value)

        for key in (
            "hextechId",
            "hextechIds",
            "hextech",
            "hextechs",
            "hextechName",
            "hextechNames",
            "hextechSlug",
            "hextechSlugs",
            "augmentId",
            "augmentIds",
            "augment",
            "augments",
            "augmentName",
            "augmentNames",
            "augmentSlug",
            "augmentSlugs",
            "augment_name",
            "augment_names",
            "name",
        ):
            value = item.get(key)
            append_raw(value)

        names = []
        for raw in raw_values:
            if isinstance(raw, dict):
                raw = (
                    raw.get("name")
                    or raw.get("displayName")
                    or raw.get("display_name")
                    or raw.get("label")
                    or raw.get("id")
                    or raw.get("slug")
                )
            key = str(raw or "").strip()
            if not key:
                continue
            resolved = (
                self.augment_name_map.get(key)
                or self.augment_name_map.get(normalize_augment_name(key))
                or self.augment_name_map.get(Path(key).stem)
                or self.augment_name_map.get(normalize_augment_name(Path(key).stem))
            )
            if resolved:
                names.append(resolved)
            elif not key.isdigit() and len(key) > 1:
                names.append(key)
        return [name for name in dict.fromkeys(names) if name]

    def _resolve_rating(self, item: dict) -> str:
        value = item.get("rating") or item.get("grade") or item.get("score") or item.get("tierScore")
        if isinstance(value, dict):
            value = value.get("label") or value.get("grade") or value.get("rating") or value.get("value")
        text = str(value or "").strip()
        return text or "未知"

    @staticmethod
    def _resolve_original_flag(item: dict) -> bool:
        value = item.get("isOriginal")
        if value is None:
            value = item.get("original")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "原创", "original"}
        return bool(value)

    def _extract_old_bundle(self, bundle_text: str) -> list[SynergyEntry]:
        if BUNDLE_INTERACTION_SECTION_MARKER not in bundle_text:
            return []
        payload = self._extract_interaction_payload(bundle_text)
        arrays = payload["arrays"]
        entries = []
        for mapping_name in ("manual_map", "community_map"):
            champion_map = payload.get(mapping_name) or {}
            for champion_slug, short_key in champion_map.items():
                array_literal = arrays.get(str(short_key))
                if not array_literal:
                    continue
                try:
                    items = self._parse_js_array_literal(array_literal)
                except Exception as exc:
                    logger.warning("解析旧 bundle 数组失败：champion=%s error=%s", champion_slug, _safe_exception_label(exc))
                    continue
                for item in items:
                    entry = self._entry_from_dict(item, fallback_slug=str(champion_slug))
                    if entry:
                        entries.append(entry)
        return entries

    def _extract_generic_js_objects(self, text: str) -> list[SynergyEntry]:
        entries = []
        if "hextech" not in text and "augment" not in text and "rating" not in text:
            return entries
        object_pattern = re.compile(r"\{[^{}]{0,1200}(?:hextechId|hextechIds|augmentId|augmentIds|rating|isOriginal)[^{}]{0,1200}\}")
        for match in object_pattern.finditer(text):
            literal = match.group(0)
            try:
                item = ast.literal_eval(self._convert_js_literal_to_python(literal))
            except Exception:
                continue
            if isinstance(item, dict):
                entry = self._entry_from_dict(item, fallback_slug="")
                if entry:
                    entries.append(entry)
        return entries

    def _extract_interaction_payload(self, bundle_text: str) -> dict:
        section_index = bundle_text.find(BUNDLE_INTERACTION_SECTION_MARKER)
        if section_index == -1:
            raise ValueError("未找到联动数据起始标记")
        section_index += len(BUNDLE_INTERACTION_SECTION_MARKER)

        manual_index = bundle_text.find("Tk={", section_index)
        community_index = bundle_text.find("RA={", section_index)
        if manual_index == -1 or community_index == -1:
            raise ValueError("未找到英雄映射对象")

        manual_literal, manual_object_end = self._extract_js_object_literal(bundle_text, manual_index + len("Tk="))
        community_literal, _ = self._extract_js_object_literal(bundle_text, community_index + len("RA="))

        stop_index = bundle_text.rfind("],RA={", manual_object_end, community_index)
        stop_index = stop_index + 1 if stop_index != -1 else community_index
        short_key_arrays = {}
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, section_index, manual_index))
        short_key_arrays.update(self._extract_named_array_assignments(bundle_text, manual_object_end, stop_index))
        if not short_key_arrays:
            raise ValueError("未找到联动数组定义")

        return {
            "arrays": short_key_arrays,
            "manual_map": self._parse_js_identifier_map(manual_literal),
            "community_map": self._parse_js_identifier_map(community_literal),
        }

    def _extract_js_object_literal(self, text: str, start_index: int) -> tuple[str, int]:
        return self._extract_balanced_literal(text, start_index, "{", "}")

    def _extract_js_array_literal(self, text: str, start_index: int) -> tuple[str, int]:
        return self._extract_balanced_literal(text, start_index, "[", "]")

    def _extract_balanced_literal(self, text: str, start_index: int, opener: str, closer: str) -> tuple[str, int]:
        if start_index < 0 or start_index >= len(text) or text[start_index] != opener:
            raise ValueError("字面量起始位置无效")
        depth = 0
        quote = None
        escaped = False
        i = start_index
        while i < len(text):
            char = text[i]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            else:
                if char in ('"', "'", "`"):
                    quote = char
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        return text[start_index : i + 1], i + 1
            i += 1
        raise ValueError("字面量未闭合")

    def _extract_named_array_assignments(self, text: str, start_index: int, stop_index: int) -> dict:
        assignments = {}
        pattern = re.compile(r"([A-Za-z_$][A-Za-z0-9_$]*)=\[")
        cursor = start_index
        while cursor < stop_index:
            match = pattern.search(text, cursor, stop_index)
            if not match:
                break
            literal, cursor = self._extract_js_array_literal(text, match.end() - 1)
            assignments[match.group(1)] = literal
            if cursor < stop_index and text[cursor : cursor + 1] == ",":
                cursor += 1
        return assignments

    def _parse_js_identifier_map(self, literal: str) -> dict:
        body = literal.strip()
        if not body.startswith("{") or not body.endswith("}"):
            raise ValueError("英雄映射对象格式无效")
        mapping = {}
        pair_pattern = re.compile(r'''(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][A-Za-z0-9_$]*))\s*:\s*([A-Za-z_$][A-Za-z0-9_$]*)''')
        for match in pair_pattern.finditer(body[1:-1]):
            key = match.group(1) or match.group(2) or match.group(3)
            value = match.group(4)
            if key:
                mapping[key] = value
        if not mapping:
            raise ValueError("英雄映射对象解析为空")
        return mapping

    def _parse_js_array_literal(self, literal: str) -> list:
        return ast.literal_eval(self._convert_js_literal_to_python(literal))

    def _convert_js_literal_to_python(self, literal: str) -> str:
        result = []
        i = 0
        simple_escapes = {"n": "\\n", "r": "\\r", "t": "\\t", "b": "\\b", "f": "\\f", "\\": "\\", '"': '"', "'": "'", "`": "`", "/": "/"}
        while i < len(literal):
            char = literal[i]
            if char in ('"', "'", "`"):
                quote = char
                i += 1
                chunks = []
                while i < len(literal):
                    current = literal[i]
                    if current == "\\":
                        i += 1
                        if i >= len(literal):
                            chunks.append("\\")
                            break
                        escaped = literal[i]
                        if escaped == "u" and i + 4 < len(literal):
                            hex_part = literal[i + 1 : i + 5]
                            if all(c in "0123456789abcdefABCDEF" for c in hex_part):
                                chunks.append(chr(int(hex_part, 16)))
                                i += 5
                                continue
                        chunks.append(simple_escapes.get(escaped, escaped))
                        i += 1
                        continue
                    if current == quote:
                        i += 1
                        break
                    chunks.append(current)
                    i += 1
                result.append(json.dumps("".join(chunks), ensure_ascii=False))
                continue
            if char == "!" and i + 1 < len(literal) and literal[i + 1] in "01":
                result.append("True" if literal[i + 1] == "0" else "False")
                i += 2
                continue
            if char.isalpha() or char in "_$":
                j = i + 1
                while j < len(literal) and (literal[j].isalnum() or literal[j] in "_$"):
                    j += 1
                token = literal[i:j]
                k = j
                while k < len(literal) and literal[k].isspace():
                    k += 1
                if k < len(literal) and literal[k] == ":":
                    result.append(json.dumps(token, ensure_ascii=False))
                    result.append(literal[j : k + 1])
                    i = k + 1
                    continue
                result.append({"null": "None", "true": "True", "false": "False", "undefined": "None"}.get(token, token))
                i = j
                continue
            result.append(char)
            i += 1
        return "".join(result)

    def _slug_from_url(self, url: str) -> str:
        path = urlparse(url).path.rstrip("/")
        tail = path.rsplit("/", 1)[-1]
        return normalize_slug(Path(tail).stem or tail)

    def _slug_from_path(self, path: tuple[str, ...]) -> str:
        for part in reversed(path):
            normalized = normalize_name(part)
            slug = normalize_slug(part)
            if normalized in self.champion_lookup:
                return normalized
            if slug in self.champion_lookup:
                return slug
        return ""

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _dedupe_entries(entries_by_slug: dict[str, list[SynergyEntry]]) -> dict[str, list[SynergyEntry]]:
        result = {}
        for slug, entries in entries_by_slug.items():
            seen = set()
            unique = []
            for entry in entries:
                key = (tuple(entry.augment_names), entry.rating, entry.tag, entry.author, entry.content)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(entry)
            result[slug] = unique
        return result


class SynergyWriter:
    def __init__(self, core_info: dict[str, ChampionInfo]):
        self.core_info = core_info
        self.champion_lookup = build_champion_lookup(core_info)

    def build_payload(self, synergy_map: dict[str, list[SynergyEntry]]) -> dict:
        final_data = {}
        missing_synergy = []
        for champ_id, champ_info in self.core_info.items():
            synergies = self._find_synergies_for_champion(champ_info, synergy_map)
            if not synergies:
                missing_synergy.append(champ_info.name)
            final_data[champ_id] = {
                "id": champ_id,
                "name": champ_info.name,
                "title": champ_info.title,
                "en_name": champ_info.en_name,
                "aliases": champ_info.aliases,
                "synergies": [entry.to_compat_string() for entry in synergies],
                "synergy_items": [
                    {
                        "augment_names": entry.augment_names,
                        "tier": entry.tier,
                        "rating": entry.rating,
                        "tag": entry.tag,
                        "author": entry.author,
                        "is_original": entry.is_original,
                        "content": entry.content,
                        "upvotes": entry.upvotes,
                        "downvotes": entry.downvotes,
                    }
                    for entry in synergies
                ],
            }
        if missing_synergy:
            logger.warning("部分英雄暂无联动：count=%s", len(missing_synergy))
        return final_data

    def write(self, output_path: Path, payload: dict) -> None:
        lock_path = output_path.with_suffix(output_path.suffix + ".lock")
        with _output_file_lock(lock_path):
            _atomic_write_json(output_path, payload)

    def _lookup_synergies_by_key(self, key: str, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
        for candidate in (normalize_slug(key), normalize_name(key)):
            if candidate and candidate in synergy_map:
                return synergy_map[candidate]
        return []

    def _alias_belongs_to_champion(self, alias: str, champ_info: ChampionInfo) -> bool:
        for candidate in (normalize_name(alias), normalize_slug(alias)):
            if not candidate:
                continue
            matched = self.champion_lookup.get(candidate)
            if matched is not None and matched.id != champ_info.id:
                return False
        return True

    def _find_synergies_for_champion(self, champ_info: ChampionInfo, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
        primary_keys = [champ_info.id, champ_info.slug, champ_info.en_name, champ_info.name, champ_info.title]
        for key in primary_keys:
            synergies = self._lookup_synergies_by_key(key, synergy_map)
            if synergies:
                return synergies

        keys = [alias for alias in champ_info.aliases if self._alias_belongs_to_champion(alias, champ_info)]
        for key in keys:
            synergies = self._lookup_synergies_by_key(key, synergy_map)
            if synergies:
                return synergies
        return []


def build_augment_name_map_from_static() -> dict:
    name_map = {}

    def add_mapping(raw_key, raw_name):
        key = str(raw_key or "").strip()
        name = str(raw_name or "").strip()
        if not key or not name:
            return
        candidates = {key, normalize_augment_name(key), Path(key).stem, normalize_augment_name(Path(key).stem)}
        for candidate in candidates:
            if candidate:
                name_map.setdefault(candidate, name)

    for filename in ("Augment_Apexlol_Map.json", "Augment_Icon_Manifest.json"):
        path = STATIC_DATA_PATH / filename
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if filename == "Augment_Apexlol_Map.json" and isinstance(payload, dict):
            for raw_name, raw_slug in payload.items():
                add_mapping(raw_slug, raw_name)
                add_mapping(raw_name, raw_name)
        elif filename == "Augment_Icon_Manifest.json" and isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                filename_stem = Path(str(item.get("filename") or "")).stem
                add_mapping(name, name)
                add_mapping(filename_stem, name)

    if name_map:
        return name_map

    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        logger.warning("最新 runtime CSV 不存在，无法构建海克斯名称映射")
        return {}

    try:
        import pandas as pd

        df = pd.read_csv(latest_csv, usecols=["海克斯名称"])
        for _, row in df.iterrows():
            name = str(row.get("海克斯名称") or "").strip()
            if name:
                add_mapping(name, name)
    except Exception as exc:
        logger.warning("读取最新 CSV 构建海克斯名称映射失败：%s", _safe_exception_label(exc))
    return name_map


def _entry_to_report_item(entry: SynergyEntry) -> dict:
    return {
        "champion_slug": entry.champion_slug,
        "augment_names": entry.augment_names,
        "tier": entry.tier,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "is_original": entry.is_original,
        "content": entry.content,
        "upvotes": entry.upvotes,
        "downvotes": entry.downvotes,
    }


def _default_single_champion_report_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(BASE_DIR) / "data" / "runtime" / "reports" / "synergy_vi_cloakbrowser" / timestamp


def _default_full_validate_report_dir() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(BASE_DIR) / "data" / "runtime" / "reports" / "synergy_full_validate" / timestamp


def _write_html_report_sample(output_path: Path, html: str, limit_bytes: int = 200 * 1024) -> None:
    encoded = (html or "").encode("utf-8")[:limit_bytes]
    output_path.write_bytes(encoded.decode("utf-8", errors="ignore").encode("utf-8"))


def _build_single_champion_core_info(champion_slug: str) -> dict[str, ChampionInfo]:
    try:
        return build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
    except FileNotFoundError:
        # 单英雄 smoke 不能为了补静态资料触发稳定资源同步；缺文件时只补当前英雄的解析锚点。
        slug = str(champion_slug or "").strip()
        return {
            slug: ChampionInfo(
                id=slug,
                name=slug,
                title=slug,
                en_name=slug,
                aliases=[slug],
                slug=normalize_slug(slug),
            )
        }


def _champion_detail_url(source: ApexSource, champion: ChampionInfo) -> str:
    slug = champion.en_name or champion.slug or champion.name or champion.id
    detail_url = source.build_allowed_url(f"/zh/champions/{slug}")
    if not detail_url:
        raise ValueError(f"英雄 URL 不在 Apex 白名单内：{slug}")
    return detail_url


def _find_entries_for_champion(champion: ChampionInfo, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
    keys = [champion.id, champion.slug, champion.en_name, champion.name, champion.title, *champion.aliases]
    for key in keys:
        for candidate in (normalize_slug(key), normalize_name(key)):
            if candidate and candidate in synergy_map:
                return synergy_map[candidate]
    return []


def _source_check_record(champion: ChampionInfo, entry: SynergyEntry, html: str) -> dict:
    content_prefix = entry.content[: min(16, len(entry.content))]
    first_augment = entry.augment_names[0] if entry.augment_names else ""
    return {
        "champion_id": champion.id,
        "champion_slug": champion.slug,
        "champion_name": champion.name,
        "url_slug": champion.en_name,
        "augment": first_augment,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "content_prefix": content_prefix,
        "augment_in_html": bool(first_augment and first_augment in html),
        "author_in_html": bool(entry.author and entry.author in html),
        "content_prefix_in_html": bool(content_prefix and content_prefix in html),
    }


def _write_per_champion_csv(output_path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "champion_id",
        "champion_slug",
        "champion_name",
        "url",
        "backend",
        "status_code",
        "entry_count",
        "status",
        "cf_blocked",
        "error",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _extract_champion_entries(
    extractor: SynergyExtractor,
    champion: ChampionInfo,
    resource: FetchedResource,
) -> tuple[list[SynergyEntry], dict[str, list[SynergyEntry]]]:
    synergy_map = extractor.extract([resource])
    entries = _find_entries_for_champion(champion, synergy_map)
    if not entries:
        entries = [entry for values in synergy_map.values() for entry in values]
    return entries, synergy_map


def run_full_validation(
    *,
    max_champions: Optional[int] = None,
    report_dir: Optional[str] = None,
    delay_seconds: Optional[float] = None,
    champion_slugs: Optional[list[str]] = None,
) -> dict:
    """全量 dry-run 验证 ApexLoL 详情页抓取与解析，不写正式 synergy 快照。"""
    started_at = time.time()
    out_dir = Path(report_dir).resolve() if report_dir else _default_full_validate_report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = out_dir / "stderr.log"
    file_handler = logging.FileHandler(stderr_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(file_handler)

    source = ApexSource()
    per_champion: list[dict] = []
    failures: list[dict] = []
    source_checks: list[dict] = []
    combined_synergy_map: dict[str, list[SynergyEntry]] = {}
    cf_blocked_count = 0
    try:
        core_info = build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
        champions = list(core_info.values())
        if champion_slugs:
            wanted = {normalize_slug(slug) for slug in champion_slugs if str(slug or "").strip()}
            champions = [
                champion
                for champion in champions
                if normalize_slug(champion.en_name or champion.slug or champion.name) in wanted
                or normalize_slug(champion.slug) in wanted
            ]
        if max_champions and max_champions > 0:
            champions = champions[:max_champions]
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(core_info),
            augment_name_map=build_augment_name_map_from_static(),
        )
        delay = delay_seconds
        if delay is None:
            delay = float(os.getenv("APEX_VALIDATE_DELAY_SECONDS", "0") or "0")
        max_attempts = max(1, int(os.getenv("APEX_VALIDATE_FETCH_ATTEMPTS", "2") or "2"))
        primary_wait_until = os.getenv("APEX_CLOAKBROWSER_WAIT_UNTIL", "networkidle").strip() or "networkidle"
        primary_post_wait_ms = int(os.getenv("APEX_CLOAKBROWSER_POST_WAIT_MS", "0") or "0")

        for index, champion in enumerate(champions, start=1):
            detail_url = _champion_detail_url(source, champion)
            resource = None
            for attempt in range(1, max_attempts + 1):
                resource = source.fetch(
                    detail_url,
                    allow_cloakbrowser=True,
                    cloakbrowser_wait_until=primary_wait_until,
                    cloakbrowser_post_wait_ms=primary_post_wait_ms,
                )
                html_for_attempt = resource.text if resource else ""
                if resource and html_for_attempt and not resource.error and not source._origin_failure_reason(html_for_attempt):
                    break
                if attempt < max_attempts:
                    logger.warning(
                        "Apex 全量验证重试：champion=%s attempt=%s error=%s",
                        champion.en_name or champion.slug,
                        attempt,
                        (resource.error if resource else source.last_fetch_error)
                        or source._origin_failure_reason(html_for_attempt)
                        or "empty_html",
                    )
                    time.sleep(max(3.0, delay * attempt))
            html = resource.text if resource else ""
            cf_blocked = source._is_cloudflare_block(html)
            entry_count = 0
            status = "failed"
            error = (resource.error if resource else source.last_fetch_error) or ""
            entries: list[SynergyEntry] = []
            synergy_map: dict[str, list[SynergyEntry]] = {}
            origin_error = source._origin_failure_reason(html)
            if origin_error:
                status = "failed"
                error = error or origin_error
                if origin_error in {"cloudflare_block", "access_denied"}:
                    cf_blocked = True
            elif resource and html and not cf_blocked and not error:
                try:
                    entries, synergy_map = _extract_champion_entries(extractor, champion, resource)
                    entry_count = len(entries)
                    status = "success" if entry_count else "empty"
                    if not entries:
                        error = "synergy_parse_empty"
                    for key, values in synergy_map.items():
                        combined_synergy_map.setdefault(key, []).extend(values)
                    if entries and champion.slug != "vi" and len(source_checks) < 3:
                        source_checks.append(_source_check_record(champion, entries[0], html))
                except Exception as exc:
                    status = "empty"
                    error = str(exc)
            elif cf_blocked:
                error = error or "cloudflare_block"
            elif not html:
                error = error or "empty_html"

            if status != "success" and primary_wait_until != "networkidle":
                fallback = source.fetch_cloakbrowser(detail_url, wait_until="networkidle", post_wait_ms=0)
                fallback_html = fallback.text if fallback else ""
                fallback_cf_blocked = source._is_cloudflare_block(fallback_html)
                fallback_error = (fallback.error if fallback else source.last_fetch_error) or ""
                fallback_origin_error = source._origin_failure_reason(fallback_html)
                if fallback and fallback_html and not fallback_cf_blocked and not fallback_error and not fallback_origin_error:
                    try:
                        fallback_entries, fallback_map = _extract_champion_entries(extractor, champion, fallback)
                        if fallback_entries:
                            resource = fallback
                            html = fallback_html
                            cf_blocked = False
                            entries = fallback_entries
                            synergy_map = fallback_map
                            entry_count = len(entries)
                            status = "success"
                            error = ""
                            origin_error = ""
                            for key, values in synergy_map.items():
                                combined_synergy_map.setdefault(key, []).extend(values)
                            if entries and champion.slug != "vi" and len(source_checks) < 3:
                                source_checks.append(_source_check_record(champion, entries[0], html))
                    except Exception as exc:
                        error = error or str(exc)
            if cf_blocked:
                cf_blocked_count += 1

            row = {
                "champion_id": champion.id,
                "champion_slug": champion.slug,
                "champion_name": champion.name,
                "url": detail_url,
                "backend": resource.source if resource else "cloakbrowser",
                "status_code": resource.status_code if resource else None,
                "entry_count": entry_count,
                "status": status,
                "cf_blocked": cf_blocked,
                "error": error,
                "origin_check": "ok" if not origin_error else origin_error,
            }
            per_champion.append(row)
            if status != "success":
                failures.append(row)
            logger.info(
                "Apex 全量验证进度：%s/%s champion=%s status=%s entries=%s cf=%s",
                index,
                len(champions),
                champion.en_name or champion.slug,
                status,
                entry_count,
                cf_blocked,
            )
            if delay > 0 and index < len(champions):
                time.sleep(delay)

        unique_synergy_map = SynergyExtractor._dedupe_entries(combined_synergy_map)
        payload = SynergyWriter(core_info).build_payload(unique_synergy_map)
        stats = summarize_synergy_payload(payload)
        old_stats = _load_existing_synergy_stats()
        publishable = True
        publish_error = ""
        try:
            _validate_publish_size(stats, old_stats)
        except ValueError as exc:
            publishable = False
            publish_error = str(exc)
        failed_count = sum(1 for row in per_champion if row["status"] == "failed")
        if failed_count:
            publishable = False
            failed_error = f"存在未通过 origin 校验的英雄：failed={failed_count}"
            publish_error = f"{publish_error}；{failed_error}" if publish_error else failed_error
        elapsed = round(time.time() - started_at, 3)
        summary = {
            "total_champions": len(core_info),
            "processed": len(per_champion),
            "non_empty": sum(1 for row in per_champion if row["status"] == "success"),
            "empty": sum(1 for row in per_champion if row["status"] == "empty"),
            "real_empty": sum(1 for row in per_champion if row["status"] == "empty"),
            "failed": failed_count,
            "failed_blocked": failed_count,
            "synergy_entries_total": sum(int(row["entry_count"] or 0) for row in per_champion),
            "cf_blocked_count": cf_blocked_count,
            "elapsed": elapsed,
            "stats": stats,
            "publishable": publishable,
            "publish_error": publish_error,
            "dry_run": True,
        }
        _atomic_write_json(out_dir / "summary.json", summary)
        _atomic_write_json(out_dir / "per_champion.json", {"items": per_champion})
        _write_per_champion_csv(out_dir / "per_champion.csv", per_champion)
        _atomic_write_json(out_dir / "failures.json", {"items": failures})
        _atomic_write_json(out_dir / "source_checks.json", {"items": source_checks})
        print(
            "full_validate processed={processed} non_empty={non_empty} empty={empty} failed={failed} "
            "synergy_entries_total={entries} cf_blocked_count={cf} publishable={publishable} out_dir={out_dir}".format(
                processed=summary["processed"],
                non_empty=summary["non_empty"],
                empty=summary["empty"],
                failed=summary["failed"],
                entries=summary["synergy_entries_total"],
                cf=summary["cf_blocked_count"],
                publishable=str(summary["publishable"]).lower(),
                out_dir=out_dir,
            )
        )
        return {"out_dir": str(out_dir), **summary}
    finally:
        source.close()
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


def run_single_champion_probe(champion_slug: str = "Vi", report_dir: Optional[str] = None) -> dict:
    """只抓单个 ApexLoL 英雄详情页，并把抓取和解析证据写入 runtime reports。"""
    started_at = time.time()
    out_dir = Path(report_dir).resolve() if report_dir else _default_single_champion_report_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = out_dir / "stderr.log"
    file_handler = logging.FileHandler(stderr_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(file_handler)

    source = ApexSource()
    resource: Optional[FetchedResource] = None
    result = {
        "url": "",
        "backend": "cloakbrowser",
        "status_code": None,
        "cloakbrowser_version": None,
        "cf_blocked": True,
        "synergy_entry_count": 0,
        "error": "",
    }
    try:
        detail_url = source.build_allowed_url(f"/zh/champions/{champion_slug}")
        if not detail_url:
            raise ValueError(f"英雄 URL 不在 Apex 白名单内：{champion_slug}")
        result["url"] = detail_url
        resource = source.fetch(detail_url, allow_cloakbrowser=True)
        html = resource.text if resource else ""
        cf_blocked = source._is_cloudflare_block(html)
        result.update(
            {
                "backend": resource.source if resource else "cloakbrowser",
                "status_code": resource.status_code if resource else None,
                "cloakbrowser_version": resource.cloakbrowser_version if resource else None,
                "cf_blocked": cf_blocked,
                "error": (resource.error if resource else source.last_fetch_error) or "",
            }
        )
        _write_html_report_sample(out_dir / "page.html.txt", html)

        entries: list[SynergyEntry] = []
        origin_error = source._origin_failure_reason(html)
        if origin_error and not result["error"]:
            result["error"] = origin_error
            result["cf_blocked"] = origin_error in {"cloudflare_block", "access_denied"}
        if resource and html and not result["cf_blocked"] and not result["error"]:
            core_info = _build_single_champion_core_info(champion_slug)
            extractor = SynergyExtractor(
                champion_lookup=build_champion_lookup(core_info),
                augment_name_map=build_augment_name_map_from_static(),
            )
            synergy_map = extractor.extract([resource])
            for candidate in (normalize_slug(champion_slug), normalize_name(champion_slug)):
                if candidate and candidate in synergy_map:
                    entries = synergy_map[candidate]
                    break
            if not entries:
                entries = [entry for values in synergy_map.values() for entry in values]
            result["synergy_entry_count"] = len(entries)

        synergy_payload = [_entry_to_report_item(entry) for entry in entries]
        _atomic_write_json(out_dir / "synergy_vi.json", synergy_payload)
        if result["synergy_entry_count"] == 0 and not result["error"]:
            if result["cf_blocked"]:
                result["error"] = "cloudflare_block"
            elif not html:
                result["error"] = "empty_html"
            else:
                result["error"] = "synergy_parse_empty"
        _atomic_write_json(out_dir / "result.json", result)
        print(
            "backend={backend} cf_blocked={cf_blocked} synergy_entry_count={count} out_dir={out_dir}".format(
                backend=result["backend"],
                cf_blocked=str(result["cf_blocked"]).lower(),
                count=result["synergy_entry_count"],
                out_dir=out_dir,
            )
        )
        log_task_summary(
            logger,
            task="ApexLoL Vi 单英雄 CloakBrowser 验证",
            started_at=started_at,
            success=bool(result["synergy_entry_count"]),
            detail=f"backend={result['backend']} cf_blocked={result['cf_blocked']} items={result['synergy_entry_count']} out_dir={out_dir}",
        )
        return {"out_dir": str(out_dir), **result}
    except Exception as exc:
        result["error"] = str(exc)
        if resource is not None:
            result["status_code"] = resource.status_code
            result["cloakbrowser_version"] = resource.cloakbrowser_version
        _atomic_write_json(out_dir / "synergy_vi.json", [])
        _atomic_write_json(out_dir / "result.json", result)
        print(
            "backend={backend} cf_blocked={cf_blocked} synergy_entry_count=0 out_dir={out_dir}".format(
                backend=result["backend"],
                cf_blocked=str(result["cf_blocked"]).lower(),
                out_dir=out_dir,
            )
        )
        logger.exception("ApexLoL Vi 单英雄 CloakBrowser 验证失败")
        return {"out_dir": str(out_dir), **result}
    finally:
        source.close()
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()


def main(*, dry_run: Optional[bool] = None, output_path: Optional[str] = None):
    started_at = time.time()
    dry_run = (os.getenv("APEX_DRY_RUN", "0").strip() == "1") if dry_run is None else bool(dry_run)
    logger.info("ApexLoL 协同抓取开始：dry_run=%s", dry_run)

    try:
        core_data = _load_json_file("Champion_Core_Data.json", "core_data")
        core_info = build_core_info(core_data)
        logger.info("核心数据加载成功：count=%s", len(core_info))
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        log_task_summary(logger, task="ApexLoL 协同抓取", started_at=started_at, success=False, detail=f"stage=core_data error={_safe_exception_label(exc)}")
        return None

    source = ApexSource()
    try:
        resources = source.discover_resources()
        if not resources:
            raise ValueError("Apex 页面和资源均不可用")
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(core_info),
            augment_name_map=build_augment_name_map_from_static(),
        )
        try:
            synergy_map = extractor.extract(resources)
        except ValueError as exc:
            if not str(exc).startswith("联动解析结果为空"):
                raise
            # 在线入口被 ApexLoL 反爬整体拦截时，继续构造空 payload，让 latest merge 保住旧有效英雄。
            logger.warning("本轮联动解析为空，进入 latest merge 兜底：%s", exc)
            synergy_map = {}
        payload = SynergyWriter(core_info).build_payload(synergy_map)
        # 本轮真实抓取规模（merge 前）：用于判定是否值得发布，避免 latest merge 把失败轮次兜底成假发布。
        fresh_stats = summarize_synergy_payload(payload)
        payload, merge_meta = merge_payload_with_latest_snapshot(payload)
        stats = summarize_synergy_payload(payload)
        old_stats = _load_existing_synergy_stats()
        publishable = True
        if not synergy_map or fresh_stats.get("non_empty_heroes", 0) == 0:
            # 本轮在线整体被反爬拦截、无任何有效新增：不能靠 latest merge 兜底写出 mapped=0 的
            # “假发布”，保持旧 latest 不动（非 dry-run 抛出后由外层 except 记录并保留旧快照）。
            publishable = False
            logger.error(
                "本轮无有效联动新增（mapped=%s non_empty=%s），保持旧 latest 不变",
                len(synergy_map),
                fresh_stats.get("non_empty_heroes", 0),
            )
            if not dry_run:
                raise ValueError("本轮抓取无有效新增，跳过发布以保留旧 latest")
        else:
            try:
                _validate_publish_size(stats, old_stats)
            except ValueError as exc:
                publishable = False
                logger.error("%s", exc)
                if not dry_run:
                    raise
        target_path = Path(output_path) if output_path else _new_synergy_snapshot_path()
        if dry_run:
            log_task_summary(
                logger,
                task="ApexLoL 协同抓取",
                started_at=started_at,
                success=publishable,
                detail=(
                    f"dry_run=1 heroes={len(payload)} mapped={len(synergy_map)} "
                    f"resources={len(resources)} non_empty={stats['non_empty_heroes']} "
                    f"items={stats['synergy_entries']} publishable={int(publishable)} "
                    f"retained_old={merge_meta.get('retained_old_heroes', 0)}"
                ),
            )
        else:
            SynergyWriter(core_info).write(target_path, payload)
            if output_path is None:
                write_synergy_refresh_meta(
                    target_path=target_path,
                    base_url=source.base_url,
                    resources=len(resources),
                    mapped=len(synergy_map),
                    stats=stats,
                )
            log_task_summary(
                logger,
                task="ApexLoL 协同抓取",
                started_at=started_at,
                success=True,
                detail=(
                    f"heroes={len(payload)} mapped={len(synergy_map)} "
                    f"non_empty={stats['non_empty_heroes']} items={stats['synergy_entries']} "
                    f"retained_old={merge_meta.get('retained_old_heroes', 0)} output={target_path.name}"
                ),
            )
        return {
            "resources": len(resources),
            "synergy_data": payload,
            "dry_run": dry_run,
            "published": bool(not dry_run and publishable and output_path is None),
            "publishable": publishable,
            "output_path": str(target_path),
            "stats": stats,
            "merge": merge_meta,
        }
    except Exception as exc:
        log_task_summary(
            logger,
            task="ApexLoL 协同抓取",
            started_at=started_at,
            success=False,
            detail=f"stage=synergy_extract error={_safe_exception_label(exc)}",
        )
        logger.warning("ApexLoL 协同抓取失败，旧 latest 协同快照保持不变：%s", exc)
        return {
            "dry_run": dry_run,
            "published": False,
            "blocked": bool(getattr(source, "blocked", False)),
            "error": str(exc),
        }
    finally:
        source.close()


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ApexLoL 协同数据抓取器")
    parser.add_argument(
        "--single-champion",
        metavar="SLUG",
        help="只抓单个 ApexLoL 英雄详情页并写入 runtime reports，不发布正式 synergy 快照",
    )
    parser.add_argument(
        "--report-dir",
        help="单英雄验证产物目录；未指定时写入 data/runtime/reports/synergy_vi_cloakbrowser/<timestamp>/",
    )
    parser.add_argument(
        "--validate-full",
        action="store_true",
        help="全量 dry-run 验证 172 英雄详情页抓取和解析，并写入 runtime reports，不发布正式快照",
    )
    parser.add_argument(
        "--max-champions",
        type=int,
        default=0,
        help="配合 --validate-full 使用；只验证前 N 个英雄，用于小批量 smoke",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=None,
        help="配合 --validate-full 使用；英雄之间等待秒数，默认读取 APEX_VALIDATE_DELAY_SECONDS 或 0",
    )
    parser.add_argument(
        "--champions",
        help="配合 --validate-full 使用；逗号分隔的英雄英文 slug 清单，用于定向复核",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.validate_full:
        run_full_validation(
            max_champions=args.max_champions or None,
            report_dir=args.report_dir,
            delay_seconds=args.delay_seconds,
            champion_slugs=[item.strip() for item in (args.champions or "").split(",") if item.strip()] or None,
        )
    elif args.single_champion:
        run_single_champion_probe(args.single_champion, report_dir=args.report_dir)
    else:
        main()

# ruff: noqa: F401
"""ApexLoL 海克斯联动数据抓取器。

抓取端分层：
- ``ApexSource`` 负责同源页面和资源获取，静态 HTTP 优先，必要时普通 browser fallback。
- ``SynergyExtractor`` 负责从页面 hydration 数据、bundle 或旧 marker 中提取联动对象。
- ``SynergyWriter`` 负责把结构化对象写成时间快照，并用 latest 指针发布。

资源来源记录为 ``snapshot``、``json-url``、``scrapling-get`` 或
``scrapling-browser``，方便按日志 source 反查具体后端。

调用方: core.refresh、tests.test_runtime_logging、dev_checks; 关键依赖: catalog.runtime_store、scraping._paths、catalog.version_catalog。
"""

from __future__ import annotations

import ast
import argparse
import json
import logging
import os
import re
import tempfile
import time
import csv
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from hextech.modules.data.catalog.runtime_store import (
    APEX_CURRENT_POINTER_FILENAME,
    SOURCE_POINTER_VERSION,
    build_next_synergy_snapshot_path,
    build_raw_synergy_data_path,
    build_synergy_latest_pointer_path,
    get_latest_synergy_snapshot_path,
    get_latest_csv,
    load_synergy_latest_pointer,
)
from hextech.modules.data.ports.paths import RUNTIME_DATA_DIR, STATIC_DATA_DIR
from hextech.modules.data.catalog.version_catalog import load_apexlol_slug_map, load_augment_manifest_entries, load_champion_core_data
from hextech.modules.acquisition.common.icons import normalize_augment_name
from hextech.infrastructure.transport.scrapling_client import (
    ScraplingFetchResult,
    fetch_page as fetch_browser_page,
    fetch_text,
)
from hextech.infrastructure.observability.logging import RedactingTextFormatter, install_runtime_logging, log_task_summary
from hextech.contracts import SourceHealth
from hextech.modules.data.source_runs import write_run_diagnostics
from hextech.modules.acquisition.common.contracts import SourceRunManifest, utc_now_iso
from hextech.modules.acquisition.apex.diagnostics import item_outcome
from hextech.modules.acquisition.apex.parser import ApexPageState, classify_apex_page
from hextech.infrastructure.sources.apex.publisher import publish_apex_run
from hextech.infrastructure.sources.apex.source import build_champion_slug_map, champion_detail_url


BASE_DIR = str(Path(RUNTIME_DATA_DIR).parents[1])
DEFAULT_APEX_SNAPSHOT_DIR = os.path.join(RUNTIME_DATA_DIR, "cache", "apex_snapshot")
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
SYNERGY_REFRESH_META_VERSION = SOURCE_POINTER_VERSION
SYNERGY_REFRESH_META_FILE = APEX_CURRENT_POINTER_FILENAME
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
ARCHIVED_SYNERGY_MARKERS = (
    "已弃用归档",
    "已弃用",
    "deprecated",
    "archived",
    "retired",
)
ARCHIVED_STATUS_KEYS = {
    "status",
    "state",
    "lifecycle",
    "section",
    "category",
    "categories",
    "tag",
    "tags",
    "type",
    "badge",
    "badges",
    "label",
    "labels",
    "flags",
}
ARCHIVED_TEXT_KEYS = {
    "body",
    "content",
    "description",
    "desc",
    "summary",
    "text",
    "title",
}
ARCHIVED_BOOL_KEYS = {
    "deprecated",
    "isdeprecated",
    "archived",
    "isarchived",
    "retired",
    "isretired",
}
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

install_runtime_logging()
logger = logging.getLogger(__name__)
DEFAULT_REQUEST_USER_AGENT = "HextechApexSnapshot/1.0 (manual offline sync)"


@dataclass
class FetchedResource:
    url: str
    text: str
    source: str
    status_code: int = 200
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

    def to_display_string(self) -> str:
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


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


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
    if filename == "Champion_Core_Data.json":
        data = load_champion_core_data(STATIC_DATA_PATH)
    elif not file_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{filename}")
    else:
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
    current_path = Path(build_raw_synergy_data_path())
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



__all__ = [name for name in globals() if not name.startswith("__")]

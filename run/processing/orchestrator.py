from __future__ import annotations

"""运行时编排层。

文件职责：
- 收敛抓取、自愈、预计算缓存重建和启动状态判断等后台编排入口

核心输入：
- 当前运行目录中的核心配置、CSV 和自愈状态

核心输出：
- 统一的后台刷新入口与就绪状态判断结果

主要依赖：
- `scraping.*`
- `processing.precomputed_cache`

维护提醒：
- 上层只应调用这里暴露的编排入口，不应在 UI 或 Web 中直接拼装多段抓取流程
"""

import os
import time

from processing.runtime_store import get_latest_csv, resolve_runtime_file, runtime_data_path
from scraping.full_hextech_scraper import main_scraper
from scraping.full_synergy_scraper import main as run_apex_spider
from scraping.heal_worker import heal_missing_artifacts
from processing.precomputed_cache import (
    has_precomputed_hextech_cache,
    load_precomputed_champion_list,
    rebuild_precomputed_api_cache_from_latest_csv,
)
from processing.processed_writer import (
    AUGMENTS_LIST_VIEW_FILE,
    AUGMENT_ICON_LOOKUP_FILE,
    BUNDLE_META_FILE,
    CHAMPIONS_LIST_VIEW_FILE,
    CHAMPION_ALIAS_INDEX_FILE,
    CHAMPION_ALIAS_LOOKUP_FILE,
    CHAMPION_DETAIL_ROUTE_FILE,
    CHAMPION_DETAIL_VIEW_FILE,
    CHAMPION_DETAILS_DIR,
    CHAMPION_ID_NAME_FILE,
    CHAMPION_LIST_CACHE_FILE,
    HEXTECH_DETAIL_CACHE_FILE,
    SYNERGY_BY_CHAMPION_FILE,
)
from scraping.augment_catalog import (
    is_augment_icon_prefetch_ready,
    manifest_has_incomplete_entries,
    run_augment_icon_prefetch,
)
from scraping.version_sync import (
    AUGMENT_ICON_FILE,
    AUGMENT_MANIFEST_FILE,
    AUGMENT_MAP_FILE,
    CORE_DATA_FILE,
    sync_hero_data,
)


SYNERGY_FILE_NAME = "Champion_Synergy.json"
SYNERGY_FILE = runtime_data_path(SYNERGY_FILE_NAME)
HIGH_FREQUENCY_REFRESH_TTL_SECONDS = 4 * 60 * 60
MTIME_COMPARE_TOLERANCE_SECONDS = 5


CSV_DERIVED_ARTIFACTS = (
    CHAMPIONS_LIST_VIEW_FILE,
    CHAMPION_DETAIL_VIEW_FILE,
    CHAMPION_LIST_CACHE_FILE,
    HEXTECH_DETAIL_CACHE_FILE,
    BUNDLE_META_FILE,
    CHAMPION_ALIAS_INDEX_FILE,
    CHAMPION_ALIAS_LOOKUP_FILE,
    CHAMPION_DETAIL_ROUTE_FILE,
    CHAMPION_ID_NAME_FILE,
)

AUGMENT_DERIVED_ARTIFACTS = (
    AUGMENTS_LIST_VIEW_FILE,
    AUGMENT_ICON_LOOKUP_FILE,
)


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _latest_detail_shard_mtime() -> float:
    if not os.path.isdir(CHAMPION_DETAILS_DIR):
        return 0.0
    mtimes = [
        _safe_mtime(os.path.join(CHAMPION_DETAILS_DIR, filename))
        for filename in os.listdir(CHAMPION_DETAILS_DIR)
        if filename.endswith(".json")
    ]
    return max(mtimes, default=0.0)


def _artifact_is_missing_or_stale(
    path: str,
    source_mtime: float,
    ttl_seconds: int,
    now: float | None = None,
) -> bool:
    artifact_mtime = _safe_mtime(path)
    if not artifact_mtime:
        return True
    if source_mtime and artifact_mtime + MTIME_COMPARE_TOLERANCE_SECONDS < source_mtime:
        return True
    if ttl_seconds > 0:
        current_time = time.time() if now is None else now
        return artifact_mtime + ttl_seconds < current_time
    return False


def _any_artifact_stale(paths: tuple[str, ...], source_mtime: float, ttl_seconds: int) -> bool:
    now = time.time()
    return any(
        _artifact_is_missing_or_stale(path, source_mtime, ttl_seconds, now=now)
        for path in paths
    )


def _source_artifact_stale(path: str, source_mtime: float, ttl_seconds: int) -> bool:
    if source_mtime:
        return _artifact_is_missing_or_stale(path, source_mtime, ttl_seconds)
    return not bool(_safe_mtime(path))


def _any_source_artifact_stale(paths: tuple[str, ...], source_mtime: float, ttl_seconds: int) -> bool:
    return any(_source_artifact_stale(path, source_mtime, ttl_seconds) for path in paths)


def processed_outputs_stale(ttl_seconds: int = HIGH_FREQUENCY_REFRESH_TTL_SECONDS) -> bool:
    latest_csv = get_latest_csv()
    latest_csv_mtime = _safe_mtime(latest_csv) if latest_csv else 0.0
    if not latest_csv_mtime:
        return False

    if _any_artifact_stale(CSV_DERIVED_ARTIFACTS, latest_csv_mtime, ttl_seconds):
        return True
    if _latest_detail_shard_mtime() + MTIME_COMPARE_TOLERANCE_SECONDS < latest_csv_mtime:
        return True

    synergy_file = resolve_synergy_file()
    synergy_mtime = _safe_mtime(synergy_file) if synergy_file else 0.0
    if _source_artifact_stale(SYNERGY_BY_CHAMPION_FILE, synergy_mtime, ttl_seconds):
        return True

    augment_source_mtime = max(
        _safe_mtime(AUGMENT_MANIFEST_FILE),
        _safe_mtime(AUGMENT_MAP_FILE),
        _safe_mtime(AUGMENT_ICON_FILE),
    )
    return _any_source_artifact_stale(AUGMENT_DERIVED_ARTIFACTS, augment_source_mtime, ttl_seconds)


def get_synergy_file() -> str:
    return runtime_data_path(SYNERGY_FILE_NAME)


def resolve_synergy_file() -> str | None:
    return resolve_runtime_file(SYNERGY_FILE_NAME)


def is_first_run(force: bool = False) -> bool:
    if force:
        return True
    augment_data_ready = (
        os.path.exists(AUGMENT_MANIFEST_FILE)
        or (os.path.exists(AUGMENT_MAP_FILE) and os.path.exists(AUGMENT_ICON_FILE))
    )
    synergy_file = resolve_synergy_file()
    core_files_ready = os.path.exists(CORE_DATA_FILE) and bool(synergy_file)
    latest_csv = get_latest_csv()
    if not core_files_ready or not augment_data_ready or not latest_csv or not os.path.exists(latest_csv):
        return True
    return processed_outputs_stale()


def should_refresh_synergy(force: bool, stale_after_seconds: int) -> bool:
    synergy_file = resolve_synergy_file()
    if force or not synergy_file:
        return True
    try:
        return (os.path.getmtime(synergy_file) + stale_after_seconds) < time.time()
    except OSError:
        return True


def run_hero_sync() -> bool:
    return bool(sync_hero_data())


def run_hextech_refresh(stop_event=None) -> bool:
    return bool(main_scraper(stop_event))


def run_synergy_refresh() -> bool:
    run_apex_spider()
    return resolve_synergy_file() is not None


def run_augment_refresh(force_refresh: bool, stop_event=None) -> dict:
    return run_augment_icon_prefetch(
        force_refresh=force_refresh,
        stop_event=stop_event,
        max_workers=8,
    )


def current_api_cache_ready() -> bool:
    return bool(load_precomputed_champion_list()) and has_precomputed_hextech_cache()


def api_cache_rebuild_needed(force: bool = False) -> bool:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return False
    return force or not current_api_cache_ready() or processed_outputs_stale()


def rebuild_api_cache_if_needed(force: bool = False) -> bool:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return current_api_cache_ready()
    if api_cache_rebuild_needed(force=force):
        return bool(rebuild_precomputed_api_cache_from_latest_csv())
    return True


def refresh_backend_data(force: bool = False, stop_event=None) -> bool:
    """执行一次运行时自愈与后台刷新。

    这个入口用于 Web 启动、自检和桌面后台线程；它本身不直接拼接多段抓取逻辑，
    而是委托 `heal_missing_artifacts` 按缺失产物清单执行最小修复。
    """
    report = heal_missing_artifacts(force=force, stop_event=stop_event)
    repaired = set(report.get("repaired", []))
    requested = bool(report.get("requested"))
    if stop_event is not None and stop_event.is_set():
        return force or bool(repaired) or requested

    should_rebuild = api_cache_rebuild_needed(force=force)
    rebuilt = rebuild_api_cache_if_needed(force=force) if should_rebuild else False
    return force or bool(repaired) or requested or rebuilt


def heal_runtime_artifacts(force: bool = False, stop_event=None) -> dict:
    return heal_missing_artifacts(force=force, stop_event=stop_event)


def get_startup_status_file() -> str:
    return runtime_data_path("startup_status.json")


__all__ = [
    "SYNERGY_FILE",
    "api_cache_rebuild_needed",
    "current_api_cache_ready",
    "get_startup_status_file",
    "get_synergy_file",
    "heal_runtime_artifacts",
    "is_augment_icon_prefetch_ready",
    "is_first_run",
    "manifest_has_incomplete_entries",
    "processed_outputs_stale",
    "refresh_backend_data",
    "rebuild_api_cache_if_needed",
    "resolve_synergy_file",
    "run_augment_refresh",
    "run_hero_sync",
    "run_hextech_refresh",
    "run_synergy_refresh",
    "should_refresh_synergy",
]

from __future__ import annotations

import os
from typing import Optional

from services.runtime_precomputed_cache import (
    has_precomputed_hextech_cache,
    load_precomputed_champion_list,
    rebuild_precomputed_api_cache_from_latest_csv,
)
from services.scrape_augments import (
    is_augment_icon_prefetch_ready,
    manifest_has_incomplete_entries,
    run_augment_icon_prefetch,
)
from services.scrape_hextech import main_scraper
from services.scrape_synergy import main as run_apex_spider
from services.sync_hero_data import (
    AUGMENT_ICON_FILE,
    AUGMENT_MAP_FILE,
    CONFIG_DIR,
    CORE_DATA_FILE,
    sync_hero_data,
)
from app.core.runtime_data import get_latest_csv

SYNERGY_FILE = os.path.join(CONFIG_DIR, "Champion_Synergy.json")


def is_first_run(force: bool = False) -> bool:
    if force:
        return True
    core_files_ready = all(
        os.path.exists(path)
        for path in (CORE_DATA_FILE, AUGMENT_MAP_FILE, AUGMENT_ICON_FILE, SYNERGY_FILE)
    )
    latest_csv = get_latest_csv()
    return not core_files_ready or not latest_csv or not os.path.exists(latest_csv)


def should_refresh_synergy(force: bool, stale_after_seconds: int) -> bool:
    if force or not os.path.exists(SYNERGY_FILE):
        return True
    try:
        return (os.path.getmtime(SYNERGY_FILE) + stale_after_seconds) < __import__("time").time()
    except OSError:
        return True


def run_hero_sync() -> bool:
    return bool(sync_hero_data())


def run_hextech_refresh(stop_event=None) -> bool:
    return bool(main_scraper(stop_event))


def run_synergy_refresh() -> bool:
    run_apex_spider()
    return os.path.exists(SYNERGY_FILE)


def run_augment_refresh(force_refresh: bool, stop_event=None) -> dict:
    return run_augment_icon_prefetch(
        force_refresh=force_refresh,
        stop_event=stop_event,
        max_workers=8,
    )


def current_api_cache_ready() -> bool:
    return bool(load_precomputed_champion_list()) and has_precomputed_hextech_cache()


def rebuild_api_cache_if_needed(force: bool = False) -> bool:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return current_api_cache_ready()
    if force or not current_api_cache_ready():
        return bool(rebuild_precomputed_api_cache_from_latest_csv())
    return True


__all__ = [
    "SYNERGY_FILE",
    "current_api_cache_ready",
    "is_augment_icon_prefetch_ready",
    "is_first_run",
    "manifest_has_incomplete_entries",
    "rebuild_api_cache_if_needed",
    "run_augment_refresh",
    "run_hero_sync",
    "run_hextech_refresh",
    "run_synergy_refresh",
    "should_refresh_synergy",
]

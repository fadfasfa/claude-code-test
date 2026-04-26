"""运行时自愈调度器。

在恢复 `run/scraping/` 包路径时提供最小自愈能力，供当前 `processing/` 与 `display/`
层安全调用，不要求一次性回滚全部历史目录结构。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock, Timeout

from processing.runtime_store import get_latest_csv, resolve_runtime_file, runtime_data_path
import scraping.version_sync as version_sync
from scraping.augment_catalog import (
    build_augment_icon_manifest,
    is_augment_icon_prefetch_ready,
    load_augment_icon_manifest,
    manifest_has_incomplete_entries,
    run_augment_icon_prefetch,
)
from scraping.full_hextech_scraper import main_scraper
from scraping.full_synergy_scraper import main as run_synergy_scraper
from scraping.version_sync import (
    ASSET_DIR,
    AUGMENT_ICON_FILE,
    AUGMENT_MANIFEST_FILE,
    AUGMENT_MAP_FILE,
    CORE_DATA_FILE,
    VERSION_FILE,
    cleanup_missing_assets,
    is_champion_asset_valid,
    load_champion_core_data,
    reset_sync_ttl,
    sync_hero_data,
)

logger = logging.getLogger(__name__)
LOCK_FILE = Path(runtime_data_path("heal_worker.lock"))


@dataclass
class HealReport:
    requested: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)

    def mark_requested(self, artifact: str) -> None:
        self.requested.append(artifact)
        self.statuses[artifact] = "requested"

    def mark_repaired(self, artifact: str) -> None:
        self.repaired.append(artifact)
        self.statuses[artifact] = "repaired"

    def mark_failed(self, artifact: str) -> None:
        self.failed.append(artifact)
        self.statuses[artifact] = "failed"

    def mark_skipped(self, artifact: str, reason: str = "skipped") -> None:
        self.skipped.append(artifact)
        self.statuses[artifact] = reason

    def as_dict(self) -> dict:
        return {
            "requested": list(self.requested),
            "repaired": list(self.repaired),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
            "statuses": dict(self.statuses),
        }


def _latest_csv_ready() -> bool:
    latest_csv = get_latest_csv()
    return bool(latest_csv and os.path.exists(latest_csv))


def _core_data_ready() -> bool:
    augment_data_ready = (
        os.path.exists(AUGMENT_MANIFEST_FILE)
        or (os.path.exists(AUGMENT_MAP_FILE) and os.path.exists(AUGMENT_ICON_FILE))
    )
    return os.path.exists(CORE_DATA_FILE) and os.path.exists(VERSION_FILE) and augment_data_ready


def _augment_manifest_ready() -> bool:
    manifest = load_augment_icon_manifest()
    return bool(manifest) and not manifest_has_incomplete_entries()


def _image_assets_ready() -> bool:
    core_data = load_champion_core_data()
    if not core_data:
        return False
    for key in core_data.keys():
        asset_path = Path(ASSET_DIR) / f"{key}.png"
        if not is_champion_asset_valid(str(asset_path)):
            return False
    return True


def detect_missing_artifacts() -> dict:
    latest_csv = get_latest_csv()
    return {
        "hextech_rankings": not _latest_csv_ready(),
        "synergy_data": resolve_runtime_file("Champion_Synergy.json") is None,
        "augment_catalog": not _core_data_ready() or not _augment_manifest_ready(),
        "champion_core": not os.path.exists(CORE_DATA_FILE),
        "images": not _image_assets_ready(),
        "latest_csv": latest_csv or "",
        "augment_icons_prefetched": is_augment_icon_prefetch_ready(),
    }


def _heal_hero_rankings(stop_event=None) -> bool:
    if stop_event is not None and stop_event.is_set():
        return False
    return bool(main_scraper(stop_event))


def _heal_synergy_data() -> bool:
    if not run_synergy_scraper():
        return False
    return resolve_runtime_file("Champion_Synergy.json") is not None


def _heal_augment_catalog(force: bool = False, stop_event=None) -> bool:
    # Always rebuild the manifest from current source maps during healing.
    # This avoids preserving an older manifest that still looks complete while
    # the underlying augment map files were just recreated.
    manifest = build_augment_icon_manifest(force_refresh=force)
    if not manifest:
        return False
    result = run_augment_icon_prefetch(force_refresh=force, stop_event=stop_event, max_workers=8)
    return bool(result.get("ready"))


def _heal_champion_core() -> bool:
    if not _core_data_ready():
        reset_sync_ttl()
    return bool(sync_hero_data() and os.path.exists(CORE_DATA_FILE))


def _heal_images() -> bool:
    core_data = load_champion_core_data()
    if not core_data:
        return False
    missing = cleanup_missing_assets(max_retries=3, core_data=core_data)
    return not missing


def heal_missing_artifacts(*, force: bool = False, stop_event=None, include_alias_index: bool = False) -> dict:
    del include_alias_index
    report = HealReport()
    try:
        with FileLock(str(LOCK_FILE), timeout=1):
            missing = detect_missing_artifacts()
            requested_repairs = {
                "champion_core": bool(force or missing.get("champion_core") or not _core_data_ready()),
                "hextech_rankings": bool(missing.get("hextech_rankings")),
                "synergy_data": bool(missing.get("synergy_data")),
                "augment_catalog": bool(missing.get("augment_catalog")),
                "images": bool(missing.get("images")),
            }
            repair_actions = {
                "champion_core": lambda: _heal_champion_core(),
                "hextech_rankings": lambda: _heal_hero_rankings(stop_event=stop_event),
                "synergy_data": _heal_synergy_data,
                "augment_catalog": lambda: _heal_augment_catalog(force=False, stop_event=stop_event),
                "images": _heal_images,
            }

            for artifact, should_repair in requested_repairs.items():
                if not should_repair:
                    report.mark_skipped(artifact, "not_needed")
                    continue
                report.mark_requested(artifact)
                if repair_actions[artifact]():
                    report.mark_repaired(artifact)
                else:
                    report.mark_failed(artifact)
    except Timeout:
        report.mark_skipped("heal_worker", "lock_busy")
        payload = report.as_dict()
        logger.info("heal_worker skipped: another repair is already running: %s", json.dumps(payload, ensure_ascii=False))
        return payload

    payload = report.as_dict()
    message = "heal_worker completed: %s"
    if report.failed:
        logger.error(message, json.dumps(payload, ensure_ascii=False))
    elif report.repaired or report.requested:
        logger.warning(message, json.dumps(payload, ensure_ascii=False))
    else:
        logger.info(message, json.dumps(payload, ensure_ascii=False))
    return payload


def heal_once(force: bool = False, stop_event=None) -> dict:
    return heal_missing_artifacts(force=force, stop_event=stop_event)

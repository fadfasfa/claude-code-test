"""运行时自愈调度器。

在恢复 `run/scraping/` 包路径时提供最小自愈能力，供当前 `processing/` 与 `display/`
层安全调用，不要求一次性回滚全部历史目录结构。
"""

from __future__ import annotations

import json

from tools.atomic_io import atomic_write_json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from filelock import FileLock, Timeout

from processing.runtime_store import (
    build_runtime_lock_path,
    build_runtime_state_path,
    build_synergy_data_path,
    get_latest_csv,
)
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
    load_champion_core_data,
    sync_hero_data,
)

logger = logging.getLogger(__name__)
LOCK_FILE = Path(build_runtime_lock_path("heal_worker.lock"))
HIGH_FREQUENCY_STALE_SECONDS = 4 * 60 * 60


@dataclass
class HealReport:
    requested: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "requested": list(self.requested),
            "repaired": list(self.repaired),
            "failed": list(self.failed),
        }


def _latest_csv_ready() -> bool:
    latest_csv = get_latest_csv()
    return bool(latest_csv and os.path.exists(latest_csv))


def _file_is_fresh(path: str, stale_after_seconds: int = HIGH_FREQUENCY_STALE_SECONDS) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        return (os.path.getmtime(path) + stale_after_seconds) >= time.time()
    except OSError:
        return False


def _latest_csv_fresh() -> bool:
    latest_csv = get_latest_csv()
    return _file_is_fresh(latest_csv or "")


def _synergy_data_fresh() -> bool:
    return _file_is_fresh(build_synergy_data_path())


def _write_startup_status(**updates) -> None:
    status_file = build_runtime_state_path("startup_status.json")
    payload = {
        "hero_ready": os.path.exists(CORE_DATA_FILE),
        "hextech_ready": _latest_csv_ready(),
        "synergy_ready": os.path.exists(build_synergy_data_path()),
        "augment_icons_prefetched": is_augment_icon_prefetch_ready(),
        "in_progress_tasks": [],
        "last_error": "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    payload.update(updates)
    atomic_write_json(status_file, payload, ensure_ascii=False, indent=2)


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
        if not asset_path.exists():
            return False
    return True


def detect_missing_artifacts() -> dict:
    latest_csv = get_latest_csv()
    return {
        "hextech_rankings": not _latest_csv_fresh(),
        "synergy_data": not _synergy_data_fresh(),
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
    result = run_synergy_scraper()
    if not result:
        return False
    if not os.path.exists(build_synergy_data_path()):
        return False
    try:
        with open(build_synergy_data_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.exception("读取协同数据结果失败")
        return False
    if not isinstance(data, dict) or not data:
        return False
    return any(bool((item or {}).get("synergies")) for item in data.values() if isinstance(item, dict))


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
        with version_sync._sync_lock:
            version_sync._last_sync_time = 0
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
    _write_startup_status(in_progress_tasks=["detect_missing_artifacts"])
    try:
        with FileLock(str(LOCK_FILE), timeout=1):
            missing = detect_missing_artifacts()
            requested = []
            if force or missing.get("hextech_rankings"):
                requested.append("hextech_rankings")
            if force or missing.get("synergy_data"):
                requested.append("synergy_data")
            _write_startup_status(in_progress_tasks=requested, last_error="")

            if force or missing.get("champion_core"):
                report.requested.append("champion_core")
                if _heal_champion_core():
                    report.repaired.append("champion_core")
                else:
                    report.failed.append("champion_core")
                    _write_startup_status(in_progress_tasks=requested, last_error="champion_core refresh failed")

            high_frequency_tasks = []
            if force or missing.get("hextech_rankings"):
                high_frequency_tasks.append(("hextech_rankings", lambda: _heal_hero_rankings(stop_event=stop_event)))
            if force or missing.get("synergy_data"):
                high_frequency_tasks.append(("synergy_data", _heal_synergy_data))

            if high_frequency_tasks:
                for task_name, _ in high_frequency_tasks:
                    report.requested.append(task_name)
                _write_startup_status(in_progress_tasks=[task_name for task_name, _ in high_frequency_tasks])

                with ThreadPoolExecutor(max_workers=len(high_frequency_tasks)) as executor:
                    future_to_task = {
                        executor.submit(task_func): task_name
                        for task_name, task_func in high_frequency_tasks
                    }
                    for future in as_completed(future_to_task):
                        task_name = future_to_task[future]
                        try:
                            success = bool(future.result())
                        except Exception:
                            logger.exception("高频自愈任务执行失败：%s", task_name)
                            success = False
                        if success:
                            report.repaired.append(task_name)
                        else:
                            report.failed.append(task_name)

            if force:
                if missing.get("augment_catalog"):
                    report.requested.append("augment_catalog")
                    if _heal_augment_catalog(force=force, stop_event=stop_event):
                        report.repaired.append("augment_catalog")
                    else:
                        report.failed.append("augment_catalog")

                if missing.get("images"):
                    report.requested.append("images")
                    if _heal_images():
                        report.repaired.append("images")
                    else:
                        report.failed.append("images")
    except Timeout:
        payload = report.as_dict()
        _write_startup_status(in_progress_tasks=[], last_error="another repair is already running")
        logger.info("heal_worker skipped: another repair is already running: %s", json.dumps(payload, ensure_ascii=False))
        return payload

    payload = report.as_dict()
    _write_startup_status(
        in_progress_tasks=[],
        last_error=", ".join(report.failed),
        hextech_ready=_latest_csv_ready(),
        synergy_ready=os.path.exists(build_synergy_data_path()),
    )
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

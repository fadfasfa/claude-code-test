"""海克斯图标并发预取编排。"""

from __future__ import annotations

import os
import time
from typing import Optional

from hextech.modules.acquisition.common.icons import batch_prefetch_augment_icons
from hextech.modules.data.ports.paths import ASSET_DIR, STATIC_DATA_DIR
from hextech.infrastructure.sources.catalog import load_augment_icon_manifest, list_missing_augment_icons
def run_augment_icon_prefetch(
    force_refresh: bool = False,
    stop_event=None,
    config_dir: Optional[str] = None,
    asset_dir: Optional[str] = None,
    max_workers: int = 8,
) -> dict:
    config_dir = config_dir or STATIC_DATA_DIR
    asset_dir = asset_dir or ASSET_DIR
    start_time = time.time()
    manifest = load_augment_icon_manifest(config_dir=config_dir, force_refresh=force_refresh)
    missing = [
        item["filename"]
        for item in manifest
        if item.get("filename")
        and (not os.path.exists(os.path.join(asset_dir, item["filename"])) or os.path.getsize(os.path.join(asset_dir, item["filename"])) <= 0)
    ]
    if not missing:
        return {
            "kind": "augment_icon_prefetch",
            "mode": "startup_prefetch" if force_refresh else "runtime_on_demand_repair",
            "total": 0,
            "success": 0,
            "failed": 0,
            "failed_files": [],
            "ready": True,
            "duration_ms": round((time.time() - start_time) * 1000, 2),
        }

    result = batch_prefetch_augment_icons(
        missing,
        asset_dir=asset_dir,
        force_refresh=force_refresh,
        max_workers=max_workers,
        stop_event=stop_event,
    )
    ready = not list_missing_augment_icons(config_dir=config_dir, asset_dir=asset_dir)
    result.update({
        "kind": "augment_icon_prefetch",
        "mode": "startup_prefetch" if force_refresh else "runtime_on_demand_repair",
        "ready": ready,
        "duration_ms": round((time.time() - start_time) * 1000, 2),
    })
    return result

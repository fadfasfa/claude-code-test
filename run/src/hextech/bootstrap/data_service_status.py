"""DataService generation 的公共启动状态投影。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json


def sync_startup_snapshot_status(publisher: DataSnapshotPublisher, result: Mapping[str, Any]) -> None:
    """generation 切换后同步公共启动状态，避免 Web 与 runtime status 跨代。"""

    if publisher.root.name != "snapshots":
        return
    try:
        client = DataSnapshotClient(publisher.root)
        snapshot_status = client.status()
        if snapshot_status.get("state") not in {"ready", "degraded"}:
            return
        manifest = client.load_manifest()
        status_path = publisher.root.parent / "state" / "startup_status.json"
        payload: dict[str, Any] = {}
        if status_path.is_file():
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        synergy_ready = any(
            Path(str(item.get("name") or "")).name == "synergy.json"
            for item in manifest.source_files
            if isinstance(item, Mapping)
        )
        payload.update(
            {
                "first_run": False,
                "hero_ready": manifest.champion_count > 0,
                "hextech_ready": manifest.stat_record_count > 0,
                "synergy_ready": synergy_ready,
                "in_progress_tasks": [],
                "last_error": "" if result.get("state") == "ready" else str(result.get("reason_code") or ""),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "data_snapshot": {
                    **snapshot_status,
                    "source": str(result.get("source") or "runtime_current"),
                    "champion_count": manifest.champion_count,
                    "augment_count": manifest.augment_count,
                    "stat_record_count": manifest.stat_record_count,
                },
            }
        )
        atomic_write_json(status_path, payload, ensure_ascii=False, indent=2)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # status 是诊断投影，写入失败不能回滚已经原子发布的 generation。
        return


__all__ = ["sync_startup_snapshot_status"]

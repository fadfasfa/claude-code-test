"""Mayhem 结构化 combo 来源 run 发布器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hextech.contracts import SourceHealth
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import publish_source_run, source_run_artifact_path
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso


def publish_mayhem_run(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    started_at: str,
    report: Mapping[str, Any],
) -> tuple[str, SourceRunManifest]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        raise ValueError("Mayhem combo 为空，拒绝发布")
    artifact = source_run_artifact_path("mayhem", run_id, "combos.json")
    atomic_write_json(artifact, dict(payload), indent=2)
    outcomes = tuple(
        ItemOutcome(
            item_id=str(item.get("id") or item.get("source_url") or index),
            state="success",
            stage="normalize",
            record_count=1,
        )
        for index, item in enumerate(items)
        if isinstance(item, Mapping)
    )
    manifest = SourceRunManifest(
        source="mayhem",
        run_id=run_id,
        health=SourceHealth.HEALTHY,
        started_at=started_at,
        finished_at=utc_now_iso(),
        expected_items=len(outcomes),
        successful_items=len(outcomes),
        confirmed_empty_items=0,
        failed_items=0,
        record_count=len(items),
        artifact="combos.json",
        outcomes=outcomes,
    )
    publish_source_run(manifest, report=dict(report))
    return str(artifact), manifest


__all__ = ["publish_mayhem_run"]

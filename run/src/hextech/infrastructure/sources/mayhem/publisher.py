"""Mayhem 结构化 combo 来源 run 发布器。"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from hextech.contracts import SourceHealth
from hextech.modules.data.catalog.versioned import load_active_catalog
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import build_artifact_descriptor, publish_source_run, source_run_artifact_path
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso
from hextech.modules.acquisition.mayhem.validation import validate_mayhem_run
from hextech.modules.data.source_runs import resolve_current_artifact


def publish_mayhem_run(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    started_at: str,
    report: Mapping[str, Any],
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
) -> tuple[str, SourceRunManifest]:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if not items:
        raise ValueError("Mayhem combo 为空，拒绝发布")
    previous_payload: Mapping[str, Any] | None = None
    previous_path = resolve_current_artifact("mayhem")
    if previous_path is not None:
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_payload = previous if isinstance(previous, Mapping) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            previous_payload = None
    validation = validate_mayhem_run(payload, report, previous_payload=previous_payload)
    artifact = source_run_artifact_path("mayhem", run_id, "combos.json")
    atomic_write_json(artifact, dict(payload), indent=2)
    catalog = load_active_catalog()
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
        catalog_generation_id=catalog.generation_id,
        catalog_sha256=catalog.content_sha256,
        health=SourceHealth.HEALTHY,
        started_at=started_at,
        completed_at=utc_now_iso(),
        expected_items=len(outcomes),
        successful_items=len(outcomes),
        confirmed_empty_items=0,
        failed_items=0,
        artifact=build_artifact_descriptor(
            artifact,
            role="combos",
            relative_path="combos.json",
            record_count=len(items),
        ),
        outcomes=outcomes,
    )
    publish_source_run(
        manifest,
        report={**dict(report), **validation},
        promote_current=promote_current,
        pointer_output=pointer_output,
    )
    return str(artifact), manifest


__all__ = ["publish_mayhem_run"]

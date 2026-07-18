"""Apex 完整来源 run 发布器。"""

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
from hextech.modules.acquisition.apex.validation import validate_apex_run
from hextech.modules.data.source_runs import resolve_current_artifact


def publish_apex_run(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    outcomes: tuple[ItemOutcome, ...],
    record_count: int,
    started_at: str,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
) -> tuple[str, SourceRunManifest]:
    previous_payload: Mapping[str, Any] | None = None
    previous_path = resolve_current_artifact("apex")
    if previous_path is not None:
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_payload = previous if isinstance(previous, Mapping) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            previous_payload = None
    validation = validate_apex_run(
        payload,
        outcomes,
        expected_champion_ids=[outcome.item_id for outcome in outcomes],
        previous_payload=previous_payload,
    )
    failed = sum(outcome.state == "failed" for outcome in outcomes)
    confirmed_empty = sum(outcome.state == "confirmed_empty" for outcome in outcomes)
    successful = sum(outcome.state == "success" for outcome in outcomes)
    if failed or successful + confirmed_empty != len(outcomes) or record_count != validation["record_count"]:
        raise ValueError("Apex 来源未通过覆盖率或非空联动门禁")
    artifact = source_run_artifact_path("apex", run_id, "synergy.json")
    atomic_write_json(artifact, dict(payload), indent=2)
    catalog = load_active_catalog()
    manifest = SourceRunManifest(
        source="apex",
        run_id=run_id,
        catalog_generation_id=catalog.generation_id,
        catalog_sha256=catalog.content_sha256,
        health=SourceHealth.HEALTHY,
        started_at=started_at,
        completed_at=utc_now_iso(),
        expected_items=len(outcomes),
        successful_items=successful,
        confirmed_empty_items=confirmed_empty,
        failed_items=0,
        artifact=build_artifact_descriptor(
            artifact,
            role="synergy",
            relative_path="synergy.json",
            record_count=record_count,
        ),
        outcomes=outcomes,
    )
    publish_source_run(
        manifest,
        report={"failed_samples": [], **validation},
        promote_current=promote_current,
        pointer_output=pointer_output,
    )
    return str(artifact), manifest


__all__ = ["publish_apex_run"]

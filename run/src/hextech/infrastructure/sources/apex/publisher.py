"""Apex 完整来源 run 发布器。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hextech.contracts import SourceHealth
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import publish_source_run, source_run_artifact_path
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso


def publish_apex_run(
    payload: Mapping[str, Any],
    *,
    run_id: str,
    outcomes: tuple[ItemOutcome, ...],
    record_count: int,
    started_at: str,
) -> tuple[str, SourceRunManifest]:
    failed = sum(outcome.state == "failed" for outcome in outcomes)
    confirmed_empty = sum(outcome.state == "confirmed_empty" for outcome in outcomes)
    successful = sum(outcome.state == "success" for outcome in outcomes)
    if failed or successful + confirmed_empty != len(outcomes) or record_count <= 0:
        raise ValueError("Apex 来源未通过覆盖率或非空联动门禁")
    artifact = source_run_artifact_path("apex", run_id, "synergy.json")
    atomic_write_json(artifact, dict(payload), indent=2)
    manifest = SourceRunManifest(
        source="apex",
        run_id=run_id,
        health=SourceHealth.HEALTHY,
        started_at=started_at,
        finished_at=utc_now_iso(),
        expected_items=len(outcomes),
        successful_items=successful,
        confirmed_empty_items=confirmed_empty,
        failed_items=0,
        record_count=record_count,
        artifact="synergy.json",
        outcomes=outcomes,
    )
    publish_source_run(manifest, report={"failed_samples": []})
    return str(artifact), manifest


__all__ = ["publish_apex_run"]

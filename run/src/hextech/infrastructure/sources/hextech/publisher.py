"""Hextech 完整来源 run 发布器。"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from hextech.contracts import SourceHealth
from hextech.modules.data.ports.atomic import atomic_write_csv
from hextech.modules.data.source_runs import publish_source_run, source_run_artifact_path
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso
from hextech.modules.acquisition.hextech.parser import validate_hextech_frame
from hextech.modules.data.catalog.runtime_store import CSV_ENCODING


def publish_hextech_run(
    frame: pd.DataFrame,
    *,
    run_id: str,
    expected_hero_ids: Iterable[str],
    outcomes: tuple[ItemOutcome, ...],
    started_at: str,
) -> tuple[str, SourceRunManifest]:
    expected = tuple(str(value) for value in expected_hero_ids)
    validate_hextech_frame(frame, expected)
    artifact = source_run_artifact_path("hextech", run_id, "stats.csv")
    atomic_write_csv(artifact, frame, index=False, encoding=CSV_ENCODING)
    manifest = SourceRunManifest(
        source="hextech",
        run_id=run_id,
        health=SourceHealth.HEALTHY,
        started_at=started_at,
        finished_at=utc_now_iso(),
        expected_items=len(expected),
        successful_items=len(expected),
        confirmed_empty_items=0,
        failed_items=0,
        record_count=len(frame),
        artifact="stats.csv",
        outcomes=outcomes,
    )
    publish_source_run(manifest, report={"failed_samples": []})
    return str(artifact), manifest


__all__ = ["publish_hextech_run"]

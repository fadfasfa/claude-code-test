"""Hextech 完整来源 run 发布器。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from hextech.contracts import SourceHealth
from hextech.modules.data.catalog.versioned import load_active_catalog
from hextech.modules.data.ports.atomic import atomic_write_csv
from hextech.modules.data.source_runs import build_artifact_descriptor, publish_source_run, source_run_artifact_path
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso
from hextech.modules.acquisition.hextech.validation import validate_hextech_frame
from hextech.modules.data.catalog.runtime_store import CSV_ENCODING


def publish_hextech_run(
    frame: pd.DataFrame,
    *,
    run_id: str,
    expected_hero_ids: Iterable[str],
    outcomes: tuple[ItemOutcome, ...],
    started_at: str,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
) -> tuple[str, SourceRunManifest]:
    expected = tuple(str(value) for value in expected_hero_ids)
    validate_hextech_frame(frame, expected)
    artifact = source_run_artifact_path("hextech", run_id, "stats.csv")
    atomic_write_csv(artifact, frame, index=False, encoding=CSV_ENCODING)
    catalog = load_active_catalog()
    manifest = SourceRunManifest(
        source="hextech",
        run_id=run_id,
        catalog_generation_id=catalog.generation_id,
        catalog_sha256=catalog.content_sha256,
        health=SourceHealth.HEALTHY,
        started_at=started_at,
        completed_at=utc_now_iso(),
        expected_items=len(expected),
        successful_items=len(expected),
        confirmed_empty_items=0,
        failed_items=0,
        artifact=build_artifact_descriptor(
            artifact,
            role="stats",
            relative_path="stats.csv",
            record_count=len(frame),
        ),
        outcomes=outcomes,
    )
    publish_source_run(
        manifest,
        report={"failed_samples": []},
        promote_current=promote_current,
        pointer_output=pointer_output,
    )
    return str(artifact), manifest


__all__ = ["publish_hextech_run"]

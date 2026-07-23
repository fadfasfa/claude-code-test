"""Hextech 完整来源 run 发布器。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from hextech.contracts import SourceHealth
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries
from hextech.modules.data.catalog.versioned import load_active_catalog
from hextech.modules.data.ports.atomic import atomic_write_csv
from hextech.modules.data.source_runs import (
    build_artifact_descriptor,
    load_source_run_manifest,
    publish_source_run,
    source_run_artifact_path,
)
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso
from hextech.modules.acquisition.hextech.coverage import (
    HextechCoverageError,
    build_hextech_coverage_report,
    validate_hextech_coverage_report,
)
from hextech.modules.acquisition.hextech.validation import validate_hextech_frame
from hextech.modules.data.catalog.runtime_store import CSV_ENCODING


def publish_hextech_run(
    frame: pd.DataFrame,
    *,
    run_id: str,
    expected_hero_ids: Iterable[str],
    outcomes: tuple[ItemOutcome, ...],
    started_at: str,
    metadata_ids: Iterable[object] | None = None,
    upstream_version: str = "",
    upstream_date: str = "",
    catalog_entries: Sequence[Mapping[str, Any]] | None = None,
    last_good_coverage: Mapping[str, Any] | None = None,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
) -> tuple[str, SourceRunManifest]:
    expected = tuple(str(value) for value in expected_hero_ids)
    validate_hextech_frame(frame, expected)
    catalog = load_active_catalog()
    # 生产抓取必须传入 metadata；保留 frame IDs 仅用于旧调用方和历史测试的兼容，
    # 这样不会把未经 metadata 校验的候选误当作真实上游覆盖。
    source_ids = (
        tuple(metadata_ids)
        if metadata_ids is not None
        else tuple(frame.get("海克斯ID", pd.Series(dtype=object)).tolist())
    )
    if last_good_coverage is None:
        try:
            current_manifest = load_source_run_manifest("hextech")
        except ValueError:
            current_manifest = None
        candidate = current_manifest.metadata.get("coverage") if current_manifest is not None else None
        last_good_coverage = candidate if isinstance(candidate, Mapping) else None
    coverage = build_hextech_coverage_report(
        frame,
        metadata_ids=source_ids,
        catalog_entries=(
            tuple(catalog_entries)
            if catalog_entries is not None
            else tuple(load_augment_manifest_entries(catalog.root))
        ),
        upstream_version=upstream_version,
        upstream_date=upstream_date,
        last_good=last_good_coverage,
    )
    try:
        validate_hextech_coverage_report(coverage, expected_hero_ids=expected, outcomes=outcomes)
    except HextechCoverageError as exc:
        exc.coverage = coverage
        raise
    artifact = source_run_artifact_path("hextech", run_id, "stats.csv")
    atomic_write_csv(artifact, frame, index=False, encoding=CSV_ENCODING)
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
        metadata={"coverage": coverage},
    )
    publish_source_run(
        manifest,
        report={"failed_samples": [], "coverage": coverage},
        promote_current=promote_current,
        pointer_output=pointer_output,
    )
    return str(artifact), manifest


__all__ = ["publish_hextech_run"]

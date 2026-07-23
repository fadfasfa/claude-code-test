"""验证 Hextech 候选代覆盖报告和发布门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
from hextech.modules.acquisition.common.contracts import ItemOutcome
from hextech.modules.acquisition.hextech.coverage import (
    HextechCoverageError,
    build_hextech_coverage_report,
    validate_hextech_coverage_report,
)
from hextech.modules.data.generation import DataSnapshotPublisher


METADATA_IDS = tuple(f"augment-{index}" for index in range(10))
HERO_IDS = ("1", "2")


def _frame(
    source_ids: tuple[str, ...] = METADATA_IDS,
    *,
    records: dict[str, int] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    for hero_id, count in (records or {"1": 70, "2": 70}).items():
        for index in range(count):
            rows.append({"英雄ID": hero_id, "海克斯ID": source_ids[index % len(source_ids)]})
    return pd.DataFrame(rows)


def _outcomes(*, failed: bool = False) -> tuple[ItemOutcome, ...]:
    return tuple(
        ItemOutcome(
            item_id=hero_id,
            state="failed" if failed and hero_id == "2" else "success",
            stage="fixture",
            record_count=70,
        )
        for hero_id in HERO_IDS
    )


def _report(
    frame: pd.DataFrame,
    *,
    last_good: dict | None = None,
    catalog_entries: tuple[dict[str, str], ...] | None = None,
) -> dict:
    return build_hextech_coverage_report(
        frame,
        metadata_ids=METADATA_IDS,
        catalog_entries=(
            tuple({"source_id": value} for value in METADATA_IDS)
            if catalog_entries is None
            else catalog_entries
        ),
        upstream_version="15.14",
        upstream_date="2026-07-22",
        last_good=last_good,
    )


def test_complete_candidate_records_upstream_and_catalog_projection_coverage() -> None:
    report = _report(_frame())

    validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())

    assert report["upstream"] == {"version": "15.14", "date": "2026-07-22"}
    assert report["metadata"]["coverage_ratio"] == 1.0
    assert report["catalog_projection"]["coverage_ratio"] == 1.0
    assert {hero["record_count"] for hero in report["heroes"].values()} == {70}


def test_metadata_gap_rejects_candidate_even_when_each_hero_has_enough_rows() -> None:
    report = _report(_frame(METADATA_IDS[:8]))

    with pytest.raises(HextechCoverageError, match="metadata 覆盖不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_low_per_hero_record_count_rejects_candidate() -> None:
    report = _report(_frame(records={"1": 69, "2": 70}))

    with pytest.raises(HextechCoverageError, match="英雄有效统计不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_unsuccessful_hero_rejects_candidate_before_publication() -> None:
    report = _report(_frame())

    with pytest.raises(HextechCoverageError, match="未成功英雄"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes(failed=True))


def test_last_good_coverage_regression_rejects_candidate() -> None:
    last_good = {
        "metadata": {"coverage_ratio": 1.0},
        "catalog_projection": {"coverage_ratio": 1.0},
        "heroes": {
            hero_id: {"metadata_coverage_ratio": 1.0}
            for hero_id in HERO_IDS
        },
    }
    report = _report(_frame(METADATA_IDS[:9]), last_good=last_good)

    with pytest.raises(HextechCoverageError, match="相对 last-good 下降"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_catalog_bridge_disappearance_is_a_regression_not_an_ignored_empty_set() -> None:
    last_good = {
        "metadata": {"coverage_ratio": 1.0},
        "catalog_projection": {"coverage_ratio": 1.0},
        "heroes": {},
    }
    report = _report(_frame(), last_good=last_good, catalog_entries=())

    with pytest.raises(HextechCoverageError, match="Catalog 投影覆盖相对 last-good 下降"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_upstream_marker_change_forces_next_hextech_candidate(tmp_path: Path) -> None:
    run_root = tmp_path / "sources" / "hextech" / "runs" / "run-old"
    run_root.mkdir(parents=True)
    (run_root / "manifest.json").write_text(
        json.dumps({"metadata": {"coverage": {"upstream": {"version": "15.13", "date": "old"}}}}),
        encoding="utf-8",
    )
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: None,
        root=tmp_path,
        upstream_marker_probe=lambda: {"version": "15.14", "date": "2026-07-22"},
    )

    changed, marker = coordinator._probe_hextech_upstream_change({"run_id": "run-old"})

    assert changed is True
    assert marker == {"version": "15.14", "date": "2026-07-22"}

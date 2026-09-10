"""验证 Hextech 候选代覆盖报告和发布门禁。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
from hextech.contracts import RefreshScheduleV1, RefreshSourceState
from hextech.infrastructure.persistence.refresh_schedule import SCHEDULE_SOURCES
from hextech.modules.acquisition.common.contracts import ItemOutcome
from hextech.modules.acquisition.hextech.coverage import (
    AVAILABILITY_POLICY,
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
    for hero_offset, (hero_id, count) in enumerate((records or {"1": 4_500, "2": 4_500}).items()):
        for index in range(count):
            rows.append({"英雄ID": hero_id, "海克斯ID": source_ids[(index + hero_offset) % len(source_ids)]})
    return pd.DataFrame(rows)


def _outcomes(*, failed: bool = False) -> tuple[ItemOutcome, ...]:
    return tuple(
        ItemOutcome(
            item_id=hero_id,
            state="failed" if failed and hero_id == "2" else "success",
            stage="fixture",
            record_count=4_500,
        )
        for hero_id in HERO_IDS
    )


def _report(
    frame: pd.DataFrame,
    *,
    last_good: dict | None = None,
    catalog_entries: tuple[dict[str, str], ...] | None = None,
    metadata_ids: tuple[str, ...] = METADATA_IDS,
    observed_source_ids: tuple[str, ...] = (),
) -> dict:
    return build_hextech_coverage_report(
        frame,
        metadata_ids=metadata_ids,
        catalog_entries=(
            tuple({"source_id": value} for value in METADATA_IDS)
            if catalog_entries is None
            else catalog_entries
        ),
        upstream_version="15.14",
        upstream_date="2026-07-22",
        observed_source_ids=observed_source_ids,
        last_good=last_good,
    )


def test_complete_candidate_records_upstream_and_catalog_projection_coverage() -> None:
    report = _report(_frame())

    validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())

    assert report["upstream"] == {"version": "15.14", "date": "2026-07-22", "marker_sha256": ""}
    assert report["metadata"]["coverage_ratio"] == 1.0
    assert report["catalog_projection"]["coverage_ratio"] == 1.0
    assert report["availability_policy"] == AVAILABILITY_POLICY
    assert report["availability"] == {
        "hero_count": 2,
        "record_count": 9_000,
        "per_hero_min": 4_500,
        "per_hero_median": 4_500.0,
        "per_hero_max": 4_500,
    }


def test_metadata_gap_rejects_candidate_even_when_each_hero_has_enough_rows() -> None:
    report = _report(_frame(METADATA_IDS[:7]))

    with pytest.raises(HextechCoverageError, match="metadata 覆盖不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_low_per_hero_record_count_rejects_candidate() -> None:
    report = _report(_frame(records={"1": 9, "2": 8_991}))

    with pytest.raises(HextechCoverageError, match="英雄有效统计不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_unsuccessful_hero_rejects_candidate_before_publication() -> None:
    report = _report(_frame())

    with pytest.raises(HextechCoverageError, match="未成功英雄"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes(failed=True))


def test_last_good_coverage_regression_rejects_candidate() -> None:
    last_good = {
        "availability_policy": AVAILABILITY_POLICY,
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
        "availability_policy": AVAILABILITY_POLICY,
        "metadata": {"coverage_ratio": 1.0},
        "catalog_projection": {"coverage_ratio": 1.0},
        "heroes": {},
    }
    report = _report(_frame(), last_good=last_good, catalog_entries=())

    with pytest.raises(HextechCoverageError, match="Catalog 投影覆盖不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_policy_migration_skips_legacy_regression_but_keeps_absolute_gates() -> None:
    legacy = {
        "metadata": {"coverage_ratio": 1.0},
        "catalog_projection": {"coverage_ratio": 1.0},
    }
    report = _report(_frame(METADATA_IDS[:9]), last_good=legacy)

    validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())

    assert report["last_good_comparison"]["state"] == "skipped_policy_change"
    assert report["last_good_delta"] == {
        "metadata_coverage_ratio": None,
        "catalog_projection_coverage_ratio": None,
        "catalog_identity_mapping_coverage_ratio": None,
    }


def test_total_record_gate_rejects_candidate() -> None:
    report = _report(_frame(records={"1": 4_499, "2": 4_500}))

    with pytest.raises(HextechCoverageError, match="有效统计总量不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())


def test_median_record_gate_rejects_candidate() -> None:
    hero_ids = ("1", "2", "3")
    report = _report(_frame(records={"1": 49, "2": 49, "3": 9_000}))
    outcomes = tuple(ItemOutcome(item_id=value, state="success", stage="fixture", record_count=50) for value in hero_ids)

    with pytest.raises(HextechCoverageError, match="中位数不足"):
        validate_hextech_coverage_report(report, expected_hero_ids=hero_ids, outcomes=outcomes)


def test_current_public_non_null_shape_keeps_236_identity_pool() -> None:
    enabled_ids = tuple(str(1_000 + index) for index in range(236))
    disabled_ids = tuple(str(9_000 + index) for index in range(7))
    metadata_entries = {
        value: {"displayName": f"海克斯 {value}", "name": f"Augment_{value}", "enabled": True}
        for value in enabled_ids
    }
    metadata_entries.update(
        {
            value: {"displayName": f"禁用海克斯 {value}", "name": f"Disabled_{value}", "enabled": False}
            for value in disabled_ids
        }
    )
    actual_ids = enabled_ids[:198]
    catalog_entries = tuple(
        {
            "source_id": value,
            "name": metadata_entries[value]["displayName"],
            "augment_name_id": metadata_entries[value]["name"],
            "filename": f"{value}.png",
            "local_path": f"assets/augments/{value}.png",
            "icon_sha256": f"{index:064x}",
        }
        for index, value in enumerate(enabled_ids, start=1)
    )
    records = {str(index): 57 for index in range(1, 174)}
    records.update({"1": 10, "2": 132, "3": 97})
    frame = _frame(actual_ids, records=records)
    report = build_hextech_coverage_report(
        frame,
        metadata_ids=tuple(metadata_entries),
        metadata_entries=metadata_entries,
        catalog_entries=catalog_entries,
        upstream_marker_sha256="a" * 64,
        observed_source_ids=enabled_ids,
    )
    outcomes = tuple(
        ItemOutcome(item_id=hero_id, state="success", stage="fixture", record_count=count)
        for hero_id, count in records.items()
    )

    validate_hextech_coverage_report(report, expected_hero_ids=records, outcomes=outcomes)

    assert report["availability"]["record_count"] == 9_929
    assert report["availability"]["per_hero_min"] == 10
    assert report["availability"]["per_hero_median"] == 57.0
    assert report["availability"]["per_hero_max"] == 132
    assert report["metadata"]["coverage_ratio"] == 0.838983
    assert report["catalog_projection"]["coverage_ratio"] == 1.0
    assert report["identity_coverage"] == {
        "metadata_total": 243,
        "enabled_pool": 236,
        "disabled_pool": 7,
        "non_null_statistics": 198,
        "catalog_identity_mapping": 236,
    }
    assert report["production_augment_pool"]["state"] == "ready"
    assert len(report["production_augment_pool"]["canonical_ids"]) == 236


def test_unresolved_enabled_identity_rejects_production_pool() -> None:
    metadata_ids = tuple(str(1_000 + index) for index in range(236))
    actual_ids = metadata_ids[:198]
    catalog_entries = tuple({"source_id": value, "name": value} for value in metadata_ids[:228])
    records = {str(index): 57 for index in range(1, 174)}
    records.update({"1": 10, "2": 132, "3": 97})
    report = _report(
        _frame(actual_ids, records=records),
        metadata_ids=metadata_ids,
        catalog_entries=catalog_entries,
    )
    outcomes = tuple(
        ItemOutcome(item_id=hero_id, state="success", stage="fixture", record_count=count)
        for hero_id, count in records.items()
    )

    with pytest.raises(HextechCoverageError) as raised:
        validate_hextech_coverage_report(report, expected_hero_ids=records, outcomes=outcomes)

    assert raised.value.reason_code == "production_pool_unavailable"


def test_observed_detail_id_missing_from_catalog_has_specific_reason_code() -> None:
    report = _report(_frame(), observed_source_ids=("augment-new",))

    with pytest.raises(HextechCoverageError, match="Catalog 未登记") as raised:
        validate_hextech_coverage_report(report, expected_hero_ids=HERO_IDS, outcomes=_outcomes())

    assert raised.value.reason_code == "active_catalog_mismatch"


def _aramkit_marker(data_path: str) -> dict[str, object]:
    return {
        "version": "16.15",
        "dataPath": data_path,
        "buildTimeUnixMs": 1234,
        "allMatches": 999,
    }


def test_upstream_version_change_forces_next_aramkit_candidate(tmp_path: Path) -> None:
    run_root = tmp_path / "sources" / "aramkit" / "runs" / "run-old"
    run_root.mkdir(parents=True)
    (run_root / "manifest.json").write_text(
        json.dumps({"metadata": {"marker": _aramkit_marker("data/old")}}),
        encoding="utf-8",
    )
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: None,
        root=tmp_path,
        upstream_marker_probe=lambda: _aramkit_marker("data/new"),
    )

    changed, marker = coordinator._probe_aramkit_upstream_change({"run_id": "run-old"})

    assert changed is True
    assert marker == _aramkit_marker("data/new")


def test_same_aramkit_marker_does_not_force_next_candidate(tmp_path: Path) -> None:
    run_root = tmp_path / "sources" / "aramkit" / "runs" / "run-old"
    run_root.mkdir(parents=True)
    (run_root / "manifest.json").write_text(
        json.dumps({"metadata": {"marker": _aramkit_marker("data/same")}}),
        encoding="utf-8",
    )
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: None,
        root=tmp_path,
        upstream_marker_probe=lambda: _aramkit_marker("data/same"),
    )

    changed, marker = coordinator._probe_aramkit_upstream_change({"run_id": "run-old"})

    assert changed is False
    assert marker == _aramkit_marker("data/same")


def test_dead_apexlol_backup_metadata_url_removed() -> None:
    """回归：apexlol.info 备份已 404 下线，留在顺位里每 15 分钟白打一次探针。"""

    from hextech.infrastructure.sources.version_sync import HEXTECH_AUGMENT_METADATA_URLS

    assert all("apexlol.info" not in url for url in HEXTECH_AUGMENT_METADATA_URLS)
    assert HEXTECH_AUGMENT_METADATA_URLS


def test_probe_returns_content_marker_when_version_and_date_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：aramgg 无 version/date 时旧探针返回空并 fall through 打 404。"""

    from hextech.infrastructure.sources.hextech import service as hextech_service

    payload = {"1001": {"displayName": "回归基本功", "rarity": 2}}

    class FakeResponse:
        headers: dict[str, str] = {}

        def json(self) -> dict:
            return payload

    calls: list[str] = []

    def fake_fetch(url: str, **_kwargs) -> FakeResponse:
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(hextech_service, "fetch_with_retry", fake_fetch)

    marker = hextech_service.probe_hextech_upstream_marker()

    assert marker["version"] == ""
    assert len(marker["marker_sha256"]) == 64
    # 首个 URL 已给出有效 marker，不得继续遍历后续顺位（旧行为的 404 来源）。
    assert len(calls) == 1


def _marker_coordinator(tmp_path: Path, *, previous_marker: str, probe_marker: str) -> CohortRefreshCoordinator:
    run_root = tmp_path / "sources" / "aramkit" / "runs" / "run-old"
    run_root.mkdir(parents=True)
    metadata = {"marker": _aramkit_marker(previous_marker)} if previous_marker else {}
    (run_root / "manifest.json").write_text(
        json.dumps({"metadata": metadata}),
        encoding="utf-8",
    )
    return CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: None,
        root=tmp_path,
        upstream_marker_probe=lambda: _aramkit_marker(probe_marker),
    )


def test_content_marker_change_forces_next_aramkit_candidate(tmp_path: Path) -> None:
    coordinator = _marker_coordinator(tmp_path, previous_marker="data/a", probe_marker="data/b")

    changed, marker = coordinator._probe_aramkit_upstream_change({"run_id": "run-old"})

    assert changed is True
    assert marker["dataPath"] == "data/b"


def test_same_content_marker_does_not_force_candidate(tmp_path: Path) -> None:
    coordinator = _marker_coordinator(tmp_path, previous_marker="data/a", probe_marker="data/a")

    changed, _marker = coordinator._probe_aramkit_upstream_change({"run_id": "run-old"})

    assert changed is False


def test_missing_previous_marker_stays_conservative(tmp_path: Path) -> None:
    """升级后首轮：上一 run 无哈希时不加速，避免虚假强刷。"""

    coordinator = _marker_coordinator(tmp_path, previous_marker="", probe_marker="data/a")

    changed, _marker = coordinator._probe_aramkit_upstream_change({"run_id": "run-old"})

    assert changed is False


def test_aramkit_backoff_does_not_probe_upstream_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    probe_calls = 0

    def marker_probe() -> dict[str, str]:
        nonlocal probe_calls
        probe_calls += 1
        return _aramkit_marker("data/new")

    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: None,
        root=tmp_path,
        now=lambda: now,
        upstream_marker_probe=marker_probe,
    )
    monkeypatch.setattr(
        coordinator,
        "_current_pointer",
        lambda source: {"run_id": "run-old"} if source == "aramkit" else {},
    )
    coordinator.schedule_store.save(
        RefreshScheduleV1(
            updated_at=now.isoformat(),
            sources={
                source: RefreshSourceState(
                    next_due_at=(now + timedelta(hours=1)).isoformat(),
                    state="backoff" if source == "aramkit" else "ready",
                )
                for source in SCHEDULE_SOURCES
            },
        )
    )

    result = coordinator.refresh()

    assert result["reason_code"] == "not_stale"
    assert probe_calls == 0

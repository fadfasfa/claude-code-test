from __future__ import annotations

import pandas as pd
import pytest

from hextech.contracts import (
    ArtifactDescriptor,
    BaselineContributionV2,
    CatalogManifestV2,
    DataContractError,
    FailureKind,
    FetchAttempt,
    ItemOutcome,
    SnapshotFileDescriptor,
    SourcePointerV2,
    SourceProvenance,
    SourceRunManifestV2,
    SourceStatusV2,
)
from hextech.modules.acquisition.apex.validation import ApexValidationError, validate_apex_run
from hextech.modules.acquisition.hextech.validation import HextechSchemaChanged, validate_hextech_frame
from hextech.modules.acquisition.mayhem.validation import MayhemValidationError, validate_mayhem_run


@pytest.mark.parametrize("failure_kind", list(FailureKind))
def test_fetch_attempt_round_trips_every_stable_failure_kind(failure_kind: FailureKind) -> None:
    attempt = FetchAttempt(
        url="https://example.test/data",
        backend="static_http",
        status_code=403 if failure_kind is FailureKind.HTTP_403 else None,
        elapsed_ms=125,
        attempts=2,
        failure_kind=failure_kind,
        retryable=failure_kind
        in {FailureKind.TIMEOUT, FailureKind.TLS_ERROR, FailureKind.NETWORK_ERROR, FailureKind.HTTP_5XX},
        error="fixture",
    )

    restored = FetchAttempt.from_mapping(attempt.to_dict())

    assert restored == attempt
    assert restored.ok is False


def test_v1_source_pointer_and_catalog_manifest_are_rejected() -> None:
    artifact = ArtifactDescriptor(
        role="stats",
        relative_path="stats.csv",
        sha256="a" * 64,
        record_count=1,
        content_schema_version=2,
        size=1,
    )
    pointer = {
        "schema_version": 1,
        "source": "hextech",
        "run_id": "run-test",
        "catalog_generation_id": "catalog-test",
        "catalog_sha256": "b" * 64,
        "manifest_sha256": "c" * 64,
        "artifact": artifact.to_dict(),
        "completed_at": "2026-07-17T00:00:00+00:00",
        "last_success_at": "2026-07-17T00:00:00+00:00",
    }
    with pytest.raises(DataContractError, match="source pointer schema"):
        SourcePointerV2.from_mapping(pointer)

    catalog = {
        "schema_version": 1,
        "catalog_generation_id": "catalog-test",
        "created_at": "2026-07-17T00:00:00+00:00",
        "content_sha256": "d" * 64,
        "files": [
            {**artifact.to_dict(), "role": role, "relative_path": f"{role}.json"}
            for role in ("champions", "augments", "versions")
        ],
    }
    with pytest.raises(DataContractError, match="catalog schema"):
        CatalogManifestV2.from_mapping(catalog)


def test_baseline_contribution_binds_origin_generation_without_fake_artifact_path() -> None:
    provenance = SourceProvenance(
        source="hextech",
        run_id="run-hextech",
        catalog_generation_id="catalog-test",
        artifact_role="stats",
        artifact_sha256="a" * 64,
        record_count=10,
        manifest_sha256="b" * 64,
        content_schema_version=2,
    )
    files = tuple(
        SnapshotFileDescriptor(
            role=role,
            relative_path=f"{role}.json",
            size=10,
            sha256=str(index) * 64,
        )
        for index, role in enumerate(("champions", "champion_hextech", "overlay_hints", "identities"), start=1)
    )
    baseline = BaselineContributionV2(
        source="hextech",
        origin_generation_id="20260718T000000-0123456789",
        catalog_generation_id="catalog-test",
        catalog_sha256="c" * 64,
        created_at="2026-07-18T00:00:00+00:00",
        provenance=provenance,
        snapshot_files=files,
    )

    payload = baseline.to_dict()

    assert "artifact" not in payload
    assert BaselineContributionV2.from_mapping(payload) == baseline


def test_source_status_accepts_old_partial_generation_fields_as_unknown() -> None:
    status = SourceStatusV2.from_mapping({"run_id": "legacy-run"})

    assert status.run_id == "legacy-run"
    assert status.freshness == "unknown"
    assert status.record_count == 0


def test_v2_contracts_reject_invalid_outcomes_and_duplicate_catalog_roles() -> None:
    with pytest.raises(DataContractError, match="state 无效"):
        ItemOutcome(item_id="1", state="unknown", stage="fixture")  # type: ignore[arg-type]

    manifest = {
        "schema_version": 2,
        "source": "hextech",
        "run_id": "run-test",
        "catalog_generation_id": "catalog-test",
        "catalog_sha256": "a" * 64,
        "health": "failed",
        "started_at": "2026-07-18T00:00:00+00:00",
        "completed_at": "2026-07-18T00:00:01+00:00",
        "expected_items": 1,
        "successful_items": 0,
        "confirmed_empty_items": 0,
        "failed_items": 1,
        "artifact": None,
        "outcomes": {"item_id": "1"},
    }
    with pytest.raises(DataContractError, match="outcomes 必须是对象数组"):
        SourceRunManifestV2.from_mapping(manifest)
    manifest["outcomes"] = ["not-an-object"]
    with pytest.raises(DataContractError, match="outcomes 必须是对象数组"):
        SourceRunManifestV2.from_mapping(manifest)

    duplicate_files = tuple(
        ArtifactDescriptor(
            role=role,
            relative_path=f"{index}.json",
            sha256=str(index) * 64,
            record_count=1,
            content_schema_version=2,
            size=1,
        )
        for index, role in enumerate(("champions", "champions", "augments", "versions"), start=1)
    )
    with pytest.raises(DataContractError, match="三个角色"):
        CatalogManifestV2(
            catalog_generation_id="catalog-test",
            created_at="fixture",
            files=duplicate_files,
            content_sha256="f" * 64,
        )


def _hextech_frame() -> pd.DataFrame:
    rows = []
    for champion_id, champion_name in (("1", "英雄一"), ("2", "英雄二")):
        rows.append(
            {
                "英雄ID": champion_id,
                "英雄名称": champion_name,
                "英雄评级": "A",
                "英雄胜率": 0.51,
                "英雄出场率": 0.12,
                "海克斯ID": "10",
                "海克斯阶级": "Gold",
                "海克斯名称": "测试海克斯",
                "海克斯胜率": 0.52,
                "海克斯出场率": 0.2,
                "胜率差": 0.01,
                "综合得分": 1.0,
            }
        )
    return pd.DataFrame(rows)


def test_hextech_validator_rejects_shifted_identity_column_and_duplicate_pair() -> None:
    frame = _hextech_frame()
    shifted = frame.rename(columns={"英雄ID": "英雄 ID"})
    with pytest.raises(HextechSchemaChanged, match="缺少字段"):
        validate_hextech_frame(shifted, ("1", "2"), min_rows=2)

    duplicated = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    with pytest.raises(HextechSchemaChanged, match="记录重复"):
        validate_hextech_frame(duplicated, ("1", "2"), min_rows=2)


def _apex_item(name: str) -> dict[str, object]:
    return {"augment_names": [name, "共同海克斯"], "tag": "fixture", "content": name}


def test_apex_requires_explicit_empty_evidence_and_verified_removal_evidence() -> None:
    payload = {
        "1": {"synergy_items": [_apex_item("新组合")]},
        "2": {"synergy_items": []},
    }
    invalid_empty = (
        ItemOutcome(item_id="1", state="success", stage="visible_cards", record_count=1),
        ItemOutcome(item_id="2", state="confirmed_empty", stage="visible_cards"),
    )
    with pytest.raises(ApexValidationError, match="明确空态"):
        validate_apex_run(payload, invalid_empty, expected_champion_ids=("1", "2"))

    previous = {"1": {"synergy_items": [_apex_item("旧组合")]}}
    valid_empty = ItemOutcome(
        item_id="2",
        state="confirmed_empty",
        stage="visible_cards",
        details={"page_identity_verified": True, "evidence": "explicit_empty_state"},
    )
    missing_removal_identity = (
        ItemOutcome(item_id="1", state="success", stage="visible_cards", record_count=1),
        valid_empty,
    )
    with pytest.raises(ApexValidationError, match="removal evidence"):
        validate_apex_run(
            payload,
            missing_removal_identity,
            expected_champion_ids=("1", "2"),
            previous_payload=previous,
        )

    verified = (
        ItemOutcome(
            item_id="1",
            state="success",
            stage="visible_cards",
            record_count=1,
            details={"page_identity_verified": True, "evidence": "parsed_entries"},
        ),
        valid_empty,
    )
    result = validate_apex_run(
        payload,
        verified,
        expected_champion_ids=("1", "2"),
        previous_payload=previous,
    )
    assert result["successful_champions"] == 1
    assert result["confirmed_empty_champions"] == 1
    assert len(result["removals"]) == 1


def _mayhem_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "raw_items": 1,
        "valid_items": 1,
        "duplicate_items": 0,
        "rejected_items": 0,
        "max_pages": 0,
        "total": 1,
        "selected": 1,
        "pagination_complete": True,
        "rejects": [],
    }
    report.update(overrides)
    return report


def test_mayhem_requires_complete_pagination_classification_and_bounded_rejects() -> None:
    payload = {"items": [{"champion_id": "1", "augment_names": ["新组合", "共同海克斯"]}]}
    with pytest.raises(MayhemValidationError, match="分页不完整"):
        validate_mayhem_run(payload, _mayhem_report(pagination_complete=False))
    with pytest.raises(MayhemValidationError, match="分类不守恒"):
        validate_mayhem_run(payload, _mayhem_report(raw_items=2))
    with pytest.raises(MayhemValidationError, match="reject 比例越界"):
        validate_mayhem_run(
            payload,
            _mayhem_report(
                raw_items=2,
                rejected_items=1,
                rejects=[{"reason_code": "unknown_augment"}],
            ),
        )

    previous = {"items": [{"champion_id": "1", "augment_names": ["旧组合", "共同海克斯"]}]}
    result = validate_mayhem_run(payload, _mayhem_report(), previous_payload=previous)
    assert result["valid_items"] == 1
    assert len(result["removals"]) == 1

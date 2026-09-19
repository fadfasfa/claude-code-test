from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from hextech.infrastructure.persistence.cohort_recovery import due_schedule, validate_generation_cohort
from hextech.infrastructure.persistence.retention import protected_references
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.contracts import SourcePointerV2, SourceProvenance
from tooling.build import deploy
from tooling.build.cohort_seed import collect_cohort_seed
from test_cohort_v3 import _v3_runtime


def _v3_with_optional(tmp_path: Path, source: str):
    runtime, _, _ = _v3_runtime(tmp_path, details=True)
    pointer = SourcePointerV2.from_mapping(json.loads(
        (runtime / f"sources/{source}/current.v2.json").read_text(encoding="utf-8")))
    view = DataSnapshotClient(runtime / "snapshots").open_view()
    provenance = SourceProvenance(source=source, run_id=pointer.run_id,
        catalog_generation_id=pointer.catalog_generation_id, artifact_role=pointer.artifact.role,
        artifact_sha256=pointer.artifact.sha256, record_count=pointer.artifact.record_count,
        manifest_sha256=pointer.manifest_sha256, content_schema_version=pointer.artifact.content_schema_version)
    manifest = DataSnapshotPublisher(runtime / "snapshots").publish(
        deepcopy(view._payloads), source_files=[*view.manifest.source_files, provenance],
        components=view.manifest.components, require_complete_provenance=True)
    candidate = validate_generation_cohort(runtime, manifest.generation_id)
    metadata = collect_cohort_seed(runtime / "snapshots").metadata
    schedule = due_schedule(candidate).to_dict()
    schedule["sources"][source].update(state="backoff", failure_kind="validation", check_status="failed")
    atomic_write_json(runtime / "state/data-service/refresh_schedule.v1.json", schedule)
    return runtime, metadata, schedule


@pytest.mark.parametrize("source", ["apex", "mayhem", "blitz"])
def test_verified_v3_optional_last_good_backoff_is_not_install_failure(tmp_path, source):
    runtime, metadata, _ = _v3_with_optional(tmp_path, source)
    assert deploy._runtime_cohort_errors(runtime, metadata) == []


@pytest.mark.parametrize("damage", ["core_backoff", "wrong_run", "wrong_catalog", "bad_artifact", "empty_failure"])
def test_optional_backoff_does_not_relax_cohort_or_core_guards(tmp_path, damage):
    runtime, metadata, schedule = _v3_with_optional(tmp_path, "apex")
    pointer_path = runtime / "sources/apex/current.v2.json"
    pointer = json.loads(pointer_path.read_text())
    if damage == "core_backoff":
        schedule["sources"]["aramkit"].update(state="backoff", failure_kind="timeout")
    elif damage == "wrong_run":
        schedule["sources"]["apex"]["current_run_id"] = "unverified-run"
    elif damage == "empty_failure":
        schedule["sources"]["apex"]["failure_kind"] = ""
    elif damage == "wrong_catalog":
        pointer["catalog_generation_id"] = "other-catalog"
        atomic_write_json(pointer_path, pointer)
    else:
        artifact = runtime / "sources/apex/runs" / pointer["run_id"] / pointer["artifact"]["relative_path"]
        artifact.write_bytes(b"tampered")
    atomic_write_json(runtime / "state/data-service/refresh_schedule.v1.json", schedule)
    assert deploy._runtime_cohort_errors(runtime, metadata)


@pytest.mark.parametrize("details", [False, True])
def test_deploy_v3_checks_only_present_optional_pointers_and_full_units(tmp_path: Path, details: bool) -> None:
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=details)
    metadata = collect_cohort_seed(runtime / "snapshots").metadata
    candidate = validate_generation_cohort(runtime, generation_id)
    atomic_write_json(runtime / "state/data-service/refresh_schedule.v1.json", due_schedule(candidate).to_dict())
    assert deploy._runtime_cohort_errors(runtime, metadata) == []
    incomplete = deepcopy(metadata)
    incomplete["units"].pop(next(iter(incomplete["units"])))
    assert any("unit closure" in error for error in deploy._runtime_cohort_errors(runtime, incomplete))
    legacy = {**metadata, "schema_version": 1}
    assert deploy._runtime_cohort_errors(runtime, legacy)


def test_deploy_v3_rechecks_nonprimary_child_hash(tmp_path: Path) -> None:
    runtime, generation_id, child = _v3_runtime(tmp_path, details=True)
    metadata = collect_cohort_seed(runtime / "snapshots").metadata
    candidate = validate_generation_cohort(runtime, generation_id)
    atomic_write_json(runtime / "state/data-service/refresh_schedule.v1.json", due_schedule(candidate).to_dict())
    assert child is not None
    child.write_text("tampered", encoding="utf-8")
    assert any("unit closure 无效" in error for error in deploy._runtime_cohort_errors(runtime, metadata))


def test_deploy_v3_treats_retired_refresh_checkpoint_as_history_only(tmp_path: Path) -> None:
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=True)
    metadata = collect_cohort_seed(runtime / "snapshots").metadata
    candidate = validate_generation_cohort(runtime, generation_id)
    schedule_path = runtime / "state/data-service/refresh_schedule.v1.json"
    atomic_write_json(schedule_path, due_schedule(candidate).to_dict())
    (runtime / "state/data-service/refresh_checkpoint.v1.json").write_text(
        "retired-v1-checkpoint-is-not-an-incremental-authority",
        encoding="utf-8",
    )

    assert deploy._runtime_cohort_errors(runtime, metadata) == []

    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["generation_id"] = "generation-outside-unit-closure"
    atomic_write_json(schedule_path, schedule)
    assert any("cohort schedule generation 不一致" in error
               for error in deploy._runtime_cohort_errors(runtime, metadata))


def test_deploy_resolves_newer_v3_partial_cohort_without_four_source_requirement(tmp_path: Path) -> None:
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=False)
    previous_id = json.loads((runtime / "snapshots/previous.v2.json").read_text())["generation_id"]
    baseline = validate_generation_cohort(runtime, previous_id)
    expected = {"schema_version": 1, "generation_id": previous_id,
                "catalog_generation_id": baseline.pointers["catalog"]["catalog_generation_id"],
                "production_pool_id": baseline.production_pool_id, "production_pool_count": baseline.production_pool_count,
                "source_run_ids": {source: pointer["run_id"] for source, pointer in baseline.pointers.items() if source != "catalog"}}
    resolved, errors = deploy._resolve_runtime_cohort_expected(runtime, expected)
    assert errors == []
    assert resolved["generation_id"] == generation_id
    assert resolved["schema_version"] == 2
    assert resolved["snapshot_schema_version"] == 3
    assert resolved["source_run_ids"] == {"aramkit": "rank-v3"}
    assert set(resolved["units"]) == {"aramkit/rank-v3"}


def test_retention_protects_all_v3_current_previous_recovery_and_journal_units(tmp_path: Path) -> None:
    def write(relative: str, payload: dict) -> None:
        atomic_write_json(tmp_path / relative, payload)

    def unit(run: str) -> dict:
        return {"source": "aramkit", "run_id": run, "catalog_generation_id": "catalog-" + run}

    for kind in ("current", "previous"):
        field = "current_generation_id" if kind == "current" else "generation_id"
        write(f"snapshots/{kind}.v2.json", {field: "gen-" + kind})
        write(f"snapshots/generations/gen-{kind}/manifest.json", {
            "schema_version": 3,
            "source_files": [{"source": "aramkit", "run_id": f"{kind}-hero-{n}",
                              "catalog_generation_id": "catalog-" + kind} for n in (1, 2)],
            "components": {"ranking": {"run_id": kind + "-rank", "catalog_id": "catalog-" + kind},
                           "champions": {"1": {"run_id": kind + "-hero-1", "catalog_id": "catalog-" + kind}}},
        })
    # Retention must preserve direct unit roots even if recovery generation is missing.
    write("state/data-service/cohort_recovery_point.v1.json", {
        "schema_version": 2, "snapshot_schema_version": 3, "generation_id": "missing-recovery-generation",
        "units": {"aramkit/recovery-hero": unit("recovery-hero")}, "pointers": {},
    })
    write("state/data-service/promotion_journal.v1.json", {
        "target_pointers": {"generation": {"recovery_point": {
            "schema_version": 2, "units": {"aramkit/journal-hero": unit("journal-hero")}}}},
    })
    protected = protected_references(tmp_path)
    assert protected["source_runs"] == {
        "aramkit:current-rank", "aramkit:current-hero-1", "aramkit:current-hero-2",
        "aramkit:previous-rank", "aramkit:previous-hero-1", "aramkit:previous-hero-2",
        "aramkit:recovery-hero", "aramkit:journal-hero",
    }
    assert {"catalog-current", "catalog-previous", "catalog-recovery-hero", "catalog-journal-hero"} <= protected["catalog_generations"]
    assert "missing-recovery-generation" in protected["generations"]

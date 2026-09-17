from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from hextech.infrastructure.persistence.cohort_recovery import due_schedule, validate_generation_cohort
from hextech.infrastructure.persistence.retention import protected_references
from hextech.modules.data.ports.atomic import atomic_write_json
from tooling.build import deploy
from tooling.build.cohort_seed import collect_cohort_seed
from test_cohort_v3 import _v3_runtime


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

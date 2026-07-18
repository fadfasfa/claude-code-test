from __future__ import annotations

import json

import pytest

from hextech.contracts import ArtifactDescriptor, SourceHealth
from hextech.modules.data import source_runs
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso


def _manifest(
    run_id: str,
    artifact: ArtifactDescriptor | None,
    *,
    health: SourceHealth = SourceHealth.HEALTHY,
) -> SourceRunManifest:
    return SourceRunManifest(
        source="apex",
        run_id=run_id,
        catalog_generation_id="catalog-test",
        catalog_sha256="c" * 64,
        health=health,
        started_at=utc_now_iso(),
        completed_at=utc_now_iso(),
        expected_items=1,
        successful_items=1 if health is SourceHealth.HEALTHY else 0,
        confirmed_empty_items=0,
        failed_items=0 if health is SourceHealth.HEALTHY else 1,
        artifact=artifact,
        outcomes=(
            ItemOutcome(
                item_id="1",
                state="success" if health is SourceHealth.HEALTHY else "failed",
                stage="test",
                record_count=1 if health is SourceHealth.HEALTHY else 0,
            ),
        ),
    )


def _install_formal_pointer(manifest: SourceRunManifest) -> None:
    """模拟已经由 cohort store 提升的正式 pointer；publisher 本身不得直写。"""

    pointer = source_runs.publish_source_run(manifest)
    atomic_write_json(source_runs.source_current_path(manifest.source), pointer)


def test_failed_source_run_never_replaces_last_good(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    artifact = source_runs.source_run_artifact_path("apex", "run-good", "synergy.json")
    atomic_write_json(artifact, {"1": {"items": [1]}})
    descriptor = source_runs.build_artifact_descriptor(
        artifact,
        role="synergy",
        relative_path="synergy.json",
        record_count=1,
    )
    first = _manifest("run-good", descriptor)
    _install_formal_pointer(first)
    previous = source_runs.load_source_current("apex")

    failed = _manifest("run-failed", None, health=SourceHealth.FAILED)
    source_runs.write_run_diagnostics(failed, report={"reason": "schema_changed"})
    with pytest.raises(source_runs.SourceRunValidationError):
        source_runs.publish_source_run(failed)
    assert source_runs.load_source_current("apex") == previous


def test_current_artifact_hash_is_verified(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    artifact = source_runs.source_run_artifact_path("apex", "run-good", "synergy.json")
    atomic_write_json(artifact, {"1": {"items": [1]}})
    descriptor = source_runs.build_artifact_descriptor(
        artifact,
        role="synergy",
        relative_path="synergy.json",
        record_count=1,
    )
    manifest = _manifest("run-good", descriptor)
    _install_formal_pointer(manifest)
    assert source_runs.resolve_current_artifact("apex") == artifact

    artifact.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    assert source_runs.resolve_current_artifact("apex") is None


def test_candidate_pointer_does_not_replace_current(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    artifact = source_runs.source_run_artifact_path("apex", "run-candidate", "synergy.json")
    atomic_write_json(artifact, {"1": {"items": [1]}})
    descriptor = source_runs.build_artifact_descriptor(
        artifact,
        role="synergy",
        relative_path="synergy.json",
        record_count=1,
    )
    pointer_output = tmp_path / "state" / "candidate.v2.json"

    source_runs.publish_source_run(
        _manifest("run-candidate", descriptor),
        promote_current=False,
        pointer_output=pointer_output,
    )

    assert pointer_output.is_file()
    assert source_runs.load_source_current("apex") == {}


def test_source_publisher_rejects_direct_current_promotion(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    artifact = source_runs.source_run_artifact_path("apex", "run-direct", "synergy.json")
    atomic_write_json(artifact, {"1": {"items": [1]}})
    descriptor = source_runs.build_artifact_descriptor(
        artifact,
        role="synergy",
        relative_path="synergy.json",
        record_count=1,
    )

    with pytest.raises(source_runs.SourceRunValidationError, match="cohort promotion"):
        source_runs.publish_source_run(_manifest("run-direct", descriptor), promote_current=True)

    assert source_runs.load_source_current("apex") == {}

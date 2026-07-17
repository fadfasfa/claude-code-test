from __future__ import annotations

import json

import pytest

from hextech.contracts import SourceHealth
from hextech.modules.data import source_runs
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.acquisition.common.contracts import ItemOutcome, SourceRunManifest, utc_now_iso


def _manifest(run_id: str, *, health: SourceHealth = SourceHealth.HEALTHY) -> SourceRunManifest:
    return SourceRunManifest(
        source="apex",
        run_id=run_id,
        health=health,
        started_at=utc_now_iso(),
        finished_at=utc_now_iso(),
        expected_items=1,
        successful_items=1 if health is SourceHealth.HEALTHY else 0,
        confirmed_empty_items=0,
        failed_items=0 if health is SourceHealth.HEALTHY else 1,
        record_count=1,
        artifact="synergy.json",
        outcomes=(ItemOutcome(item_id="1", state="success" if health is SourceHealth.HEALTHY else "failed", stage="test"),),
    )


def test_failed_source_run_never_replaces_last_good(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    first = _manifest("run-good")
    atomic_write_json(source_runs.source_run_artifact_path("apex", first.run_id, first.artifact), {"1": {"items": [1]}})
    source_runs.publish_source_run(first)
    previous = source_runs.load_source_current("apex")

    failed = _manifest("run-failed", health=SourceHealth.FAILED)
    source_runs.write_run_diagnostics(failed, report={"reason": "schema_changed"})
    with pytest.raises(source_runs.SourceRunValidationError):
        source_runs.publish_source_run(failed)
    assert source_runs.load_source_current("apex") == previous


def test_current_artifact_hash_is_verified(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    manifest = _manifest("run-good")
    artifact = source_runs.source_run_artifact_path("apex", manifest.run_id, manifest.artifact)
    atomic_write_json(artifact, {"1": {"items": [1]}})
    source_runs.publish_source_run(manifest)
    assert source_runs.resolve_current_artifact("apex") == artifact

    artifact.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    assert source_runs.resolve_current_artifact("apex") is None

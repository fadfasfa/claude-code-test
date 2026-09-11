"""部署恢复代必须有本次进程凭据和真实完整cohort，不能只看代号。"""
import json
import os
import time
from datetime import datetime
import psutil
import pytest
from test_cohort_seed import _fixture_runtime
from tooling.build.cohort_seed import collect_cohort_seed
from tooling.build.deploy_lineage import validated_launch_generation, refresh_checkpoint_errors
from hextech.infrastructure.persistence import cohort_seed


def test_real_launch_selection_receipt_requires_owner_and_complete_artifacts(tmp_path, monkeypatch):
    runtime, data = _fixture_runtime(tmp_path)
    expected = collect_cohort_seed(runtime / "snapshots").metadata
    expected["_source_fingerprint"] = "source-a"
    monkeypatch.setattr(cohort_seed, "current_build_id", lambda: "build-a")
    monkeypatch.setattr(cohort_seed, "get_build_identity", lambda: {"source_fingerprint": "source-a"})
    written_after = time.time()
    cohort_seed._write_selection_status(runtime,state="already_current",selected_source="current",
        selected_generation_id=str(data["generation_id"]),bundle_generation_id=str(data["generation_id"]),candidates=[])
    selection = json.loads((runtime / "state/data-service/cohort_selection.v1.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(selection["updated_at"]).timestamp() >= written_after - 0.000002
    args = dict(build_id="build-a",launch_started_at=psutil.Process().create_time()-1,
                desktop_pid=os.getpid(),current_generation_id=str(data["generation_id"]))
    identity, errors = validated_launch_generation(runtime,expected,**args)
    assert identity == data["generation_id"] and errors == []
    assert validated_launch_generation(runtime,expected,**{**args,"desktop_pid":os.getpid()+100000})[1]
    assert validated_launch_generation(runtime,expected,**{**args,"build_id":"wrong-build"})[1]
    manifest = runtime / "snapshots/generations" / str(data["generation_id"]) / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["content_fingerprint"] = "0" * 64
    manifest.write_text(json.dumps(payload),encoding="utf-8")
    assert validated_launch_generation(runtime,expected,**args)[1]


def test_historical_checkpoint_is_not_install_receipt_and_fake_completion_rejected():
    complete = {
        "schema_version": 1,
        "state": "complete",
        "cycle_id": "cycle-historical",
        "created_at": "2026-09-07T10:00:00+00:00",
        "updated_at": "2026-09-07T10:01:00+00:00",
        "scope": "due",
        "refresh_phase": "complete",
        "catalog": {"catalog_generation_id": "catalog-old"},
        "completed_sources": {},
        "pending_sources": [],
        "core_generation_id": "generation-old",
        "failures": {},
    }
    assert refresh_checkpoint_errors(complete, "catalog-current") == []
    assert refresh_checkpoint_errors(
        {"state":"complete","core_generation_id":"old","pending_sources":[]},
        "catalog",
    )
    assert refresh_checkpoint_errors({**complete,"failures":{"blitz":{}}},"catalog-current")
    assert refresh_checkpoint_errors({"state":"complete","pending_sources":[{}]},"catalog")


@pytest.mark.parametrize("field,value", [("gameflow", "false"), ("frozen", "false"), ("desync", True), ("identity_desync", True)])
def test_stats_pin_rejects_untrusted_active_or_identity_flags(tmp_path, field, value):
    from test_cohort_seed import _rewrite_generation_identity
    from tooling.build.deploy_lineage import validated_game_stats_generation

    runtime, data = _fixture_runtime(tmp_path)
    original = str(data["generation_id"])
    current = "20990101T000000-123456789a"
    _rewrite_generation_identity(runtime, original, current, "2099-01-01T00:00:00+00:00")
    visibility = {"stats_generation_id": original, "host": {"gameflow": True},
                  "window": {"game_instance_id": "game-a", "desync": False, "identity_desync": False}}
    report = {"session_id": "game-a", "stats_generation_id": original,
              "stats_scope": {"frozen": True, "stats_generation_id": original,
                              "stage_context": {"game_instance_id": "game-a"}}}
    assert validated_game_stats_generation(runtime, visibility, report, current) == (original, [])
    if field == "gameflow":
        visibility["host"][field] = value
    elif field == "frozen":
        report["stats_scope"][field] = value
    else:
        visibility["window"][field] = value
    assert validated_game_stats_generation(runtime, visibility, report, current)[1]

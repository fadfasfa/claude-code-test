"""Deployment lineage validates immutable v4 closure and active game pins."""
from dataclasses import replace
import pytest
from tests.support.v4_release import make_release
from hextech.modules.data.ports.atomic import atomic_write_json
from tooling.build.cohort_seed import collect_cohort_seed
from tooling.build.deploy_lineage import validated_launch_generation, validated_game_stats_generation


def test_launch_validates_both_current_and_bundle_release_closures(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    manifest = make_release(runtime, monkeypatch)
    expected = collect_cohort_seed(runtime / "snapshots").metadata
    args = dict(build_id="test", launch_started_at=0, desktop_pid=None, current_generation_id=manifest.generation_id)
    assert validated_launch_generation(runtime, expected, **args) == ("", [])
    path = runtime / "snapshots/generations" / manifest.generation_id / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    assert validated_launch_generation(runtime, expected, **args)[1]


@pytest.mark.parametrize("field,value", [("gameflow", "false"), ("frozen", "false"), ("desync", True), ("identity_desync", True)])
def test_stats_pin_rejects_untrusted_active_or_identity_flags(tmp_path, monkeypatch, field, value):
    runtime = tmp_path / "runtime"
    manifest = make_release(runtime, monkeypatch)
    original = manifest.generation_id
    current = "20990101T000000-new-release"
    newer = replace(manifest, generation_id=current, created_at="2099-01-01T00:00:00+00:00")
    atomic_write_json(runtime / "snapshots/generations" / current / "manifest.json", newer.to_dict())
    atomic_write_json(runtime / "snapshots/current.v4.json", {"schema_version": 4, "current_generation_id": current, "previous_generation_id": original})
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

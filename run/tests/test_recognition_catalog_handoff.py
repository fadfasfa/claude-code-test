import json
from types import SimpleNamespace

import pytest

from hextech.infrastructure.vision import data_source as vision_source
from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager


def test_catalog_names_are_usable_without_statistics(monkeypatch, tmp_path):
    catalog = SimpleNamespace(root=tmp_path, generation_id="catalog-new", manifest_sha256="a" * 64)
    monkeypatch.setattr(vision_source, "load_runtime_catalog", lambda: catalog)
    monkeypatch.setattr(vision_source, "_catalog_production_pool", lambda _: {
        "identities": [{"canonical_id": "123", "name": "新增名字", "tier": "gold"}],
        "canonical_ids": ["123"], "catalog_generation_id": "catalog-new"})
    monkeypatch.setattr(vision_source, "validate_production_pool_assets", lambda *_: None)
    monkeypatch.setattr(vision_source.SharedOverlayDataSource, "read_hint_cache",
                        lambda _: pytest.fail("Vision must not depend on statistics"))
    data = vision_source.CatalogVisionDataSource().read_hint_cache()
    assert data["vision"]["catalog_generation_id"] == "catalog-new"
    assert data["hints"]["123"]["name"] == "新增名字"
    assert data["source"]["private_policy_stats_enabled"] is False


def test_explicit_invalid_catalog_does_not_fall_back():
    with pytest.raises(ValueError):
        vision_source.CatalogVisionDataSource(catalog_id="../../wrong").read_hint_cache()


def test_catalog_change_is_observed_without_statistics_change(monkeypatch, tmp_path):
    from hextech.modules.data import generation
    from hextech.modules.data.catalog import versioned

    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    (snapshot_root / "current.v2.json").write_text(json.dumps({"current_generation_id": "same-stats"}))
    pointer = tmp_path / "catalog.json"
    pointer.write_text("catalog-a")
    monkeypatch.setattr(generation, "default_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr(versioned, "catalog_current_path", lambda: pointer)
    def prepare():
        return {"snapshot": {"generation_id": "same-stats"},
                "vision": {"catalog_generation_id": pointer.read_text()}}
    runtime = OverlayRuntimeManager(prepare_data_func=prepare,
        vision_pool_fingerprint_func=lambda hint: hint["vision"]["catalog_generation_id"],
        game_active_probe=lambda: False, start_context_poller_func=None)
    runtime.active_vision_pool_fingerprint = "catalog-a"
    runtime.observe_data_generation()
    pointer.write_text("catalog-b")
    result = runtime.observe_data_generation()
    assert result["changed"]
    assert result["state"] == "ready"
    assert runtime.pending_recognition_catalog_id == "catalog-b"
    assert runtime.observed_data_generation_id == "same-stats"
    pointer.write_text("catalog-a")
    runtime.observe_data_generation()
    assert runtime.pending_recognition_catalog_id == ""
    assert runtime.pending_vision_pool_fingerprint == ""
    assert runtime._pending_vision_hint_cache is None
    (snapshot_root / "current.v2.json").write_text("{broken")
    pointer.write_text("catalog-c")
    assert runtime.observe_data_generation()["recognition_catalog_id"] == "catalog-c"


def test_handoff_blocked_for_entire_game_even_outside_selection():
    runtime = OverlayRuntimeManager(game_active_probe=lambda: True, start_context_poller_func=None)
    runtime.desired_enabled = True
    runtime.pending_vision_pool_fingerprint = "new"
    assert not runtime.prepare_vision_handoff()


def test_pending_catalog_does_not_block_old_sidecar_recovery_during_game():
    from support.process_fakes import FakeProcess

    starts = []

    def start_sidecar(**kwargs):
        starts.append(dict(kwargs))
        return FakeProcess(2234)

    old = FakeProcess(1234)
    runtime = OverlayRuntimeManager(
        prepare_data_func=lambda: pytest.fail("recovery must not read the pending Catalog"),
        game_active_probe=lambda: True,
        start_context_poller_func=None,
        write_inactive_func=lambda: None,
        start_sidecar_func=start_sidecar,
    )
    runtime.host_process = FakeProcess(1235)
    runtime.sidecar_process = old
    runtime.desired_enabled = True
    runtime.status = "starting"
    runtime.phase = "sidecar_restart"
    runtime.active_vision_pool_fingerprint = "old-fingerprint"
    runtime.active_vision_origin_generation_id = "old-generation"
    runtime.active_recognition_catalog_id = "old-catalog"
    runtime._active_vision_hint_cache = {"vision": {"catalog_generation_id": "old-catalog"}}
    runtime.cache_stats = {
        "vision_pool_fingerprint": "new-fingerprint",
        "vision_pool_origin_generation_id": "new-generation",
        "recognition_catalog_id": "new-catalog",
    }
    runtime._prepared_vision_hint_cache = {"vision": {"catalog_generation_id": "new-catalog"}}
    runtime.pending_vision_pool_fingerprint = "new-fingerprint"
    runtime.pending_vision_origin_generation_id = "new-generation"
    runtime.pending_recognition_catalog_id = "new-catalog"

    result = runtime.set_enabled(True)

    assert len(starts) == 1
    assert starts[0]["target_generation_id"] == "old-generation"
    assert starts[0]["target_catalog_id"] == "old-catalog"
    assert starts[0]["expected_vision_pool_fingerprint"] == "old-fingerprint"
    assert old.stopped
    assert result["status"] == "running"
    assert result["active_recognition_catalog_id"] == "old-catalog"
    assert result["pending_recognition_catalog_id"] == "new-catalog"
    assert runtime._active_vision_hint_cache == {
        "vision": {"catalog_generation_id": "old-catalog"}
    }


@pytest.mark.parametrize("missing_field", [
    "active_vision_pool_fingerprint", "active_recognition_catalog_id",
])
def test_pending_catalog_recovery_without_old_identity_fails_closed(missing_field):
    from support.process_fakes import FakeProcess

    old = FakeProcess(1234)
    runtime = OverlayRuntimeManager(
        game_active_probe=lambda: True,
        start_context_poller_func=None,
        start_sidecar_func=lambda **_: pytest.fail("pending Catalog must not cold-start"),
    )
    runtime.host_process = FakeProcess(1235)
    runtime.sidecar_process = old
    runtime.desired_enabled = True
    runtime.status = "starting"
    runtime.phase = "sidecar_restart"
    runtime.active_vision_pool_fingerprint = "old-fingerprint"
    runtime.active_recognition_catalog_id = "old-catalog"
    runtime.active_vision_origin_generation_id = "old-generation"
    setattr(runtime, missing_field, "")
    runtime.pending_vision_pool_fingerprint = "new-fingerprint"
    runtime.pending_recognition_catalog_id = "new-catalog"

    result = runtime.set_enabled(True)

    assert result["status"] == "error"
    assert result["phase"] == "sidecar_recovery_blocked"
    assert result["last_start_failure_kind"] == "sidecar_recovery_identity_missing"
    assert result["pending_recognition_catalog_id"] == "new-catalog"
    assert not old.stopped


@pytest.mark.parametrize("missing_field", [
    "", "active_vision_pool_fingerprint", "active_recognition_catalog_id",
])
def test_catalog_only_runtime_recovers_pinned_catalog_during_game(monkeypatch, tmp_path, missing_field):
    from hextech.infrastructure.vision import template_build
    from hextech.infrastructure.vision.template_runtime import (
        load_or_build_default_template_runtime, template_runtime_resource_signature, vision_pool_fingerprint,
    )
    from hextech.modules.data.ports import paths
    from support.process_fakes import FakeProcess
    from test_cohort_seed import _write_catalog
    from test_cohort_v3 import _publish_independent_recognition_catalog

    runtime_root = tmp_path / "runtime"
    _, pointer_a, _ = _write_catalog(runtime_root)
    monkeypatch.setattr(paths, "RUNTIME_DATA_DIR", runtime_root)
    monkeypatch.setattr(template_build, "ASSET_DIR", tmp_path / "assets")
    assert not (runtime_root / "snapshots").exists()
    signature = template_runtime_resource_signature(tmp_path / "resources")

    def build(**kwargs):
        return load_or_build_default_template_runtime(
            cache_file=tmp_path / "templates.npz", resource_signature=signature, **kwargs,
        )

    starts = []
    playing = [False]

    def start_sidecar(**kwargs):
        starts.append(dict(kwargs))
        # Exercise the pinned Catalog reader and actual matrix build on restart too.
        hint_cache = vision_source.CatalogVisionDataSource(
            catalog_id=kwargs["target_catalog_id"], generation_id=kwargs["target_generation_id"],
        ).read_hint_cache()
        built = build(hint_cache=hint_cache, require_production_pool=True)
        assert built.stats["vision_pool_fingerprint"] == kwargs["expected_vision_pool_fingerprint"]
        process = FakeProcess(2234 + len(starts))
        process._hextech_recognition_catalog_id = built.stats["recognition_catalog_id"]
        process._hextech_vision_pool_fingerprint = built.stats["vision_pool_fingerprint"]
        process._hextech_vision_origin_generation_id = built.stats["vision_pool_origin_generation_id"]
        return process

    runtime = OverlayRuntimeManager(
        prepare_data_func=vision_source.prepare_catalog_vision_data,
        load_template_runtime_func=build,
        vision_pool_fingerprint_func=lambda hint: vision_pool_fingerprint(hint, resource_signature=signature),
        game_active_probe=lambda: playing[0], start_context_poller_func=None,
        write_inactive_func=lambda: None, start_sidecar_func=start_sidecar,
    )
    runtime.host_process = FakeProcess(1235)
    runtime._prewarm_templates()
    assert runtime.cache_status == "ready", runtime.last_error
    assert runtime.cache_stats["production_pool_state"] == "ready"
    assert runtime.cache_stats["rank_identity_count"] > 0
    assert runtime.cache_stats["vision_pool_origin_generation_id"] == ""
    assert runtime.set_enabled(True)["status"] == "running"
    catalog_a = pointer_a["catalog_generation_id"]
    fingerprint_a = runtime.active_vision_pool_fingerprint
    hints_a = runtime._active_vision_hint_cache
    assert runtime.active_recognition_catalog_id == catalog_a
    assert runtime.active_vision_origin_generation_id == ""
    assert fingerprint_a

    playing[0] = True
    pointer_b = _publish_independent_recognition_catalog(runtime_root)
    assert runtime.observe_data_generation()["state"] == "deferred_game_active"
    catalog_b = pointer_b["catalog_generation_id"]
    assert runtime.pending_recognition_catalog_id == catalog_b != catalog_a
    # A newer prewarm must not replace the identity recorded from the active process.
    runtime._prewarm_templates()
    assert runtime.cache_stats["recognition_catalog_id"] == catalog_b
    assert runtime.cache_stats["vision_pool_fingerprint"] != fingerprint_a
    old = runtime.sidecar_process
    runtime.status, runtime.phase = "starting", "sidecar_restart"
    if missing_field:
        setattr(runtime, missing_field, "")
    monkeypatch.setattr(runtime, "_prepare_data_func", lambda: pytest.fail("recovery read current Catalog"))
    result = runtime.set_enabled(True)
    assert result["pending_recognition_catalog_id"] == catalog_b
    assert runtime._active_vision_hint_cache == hints_a
    assert not (runtime_root / "snapshots").exists()
    if missing_field:
        assert result["last_start_failure_kind"] == "sidecar_recovery_identity_missing"
        assert result["phase"] == "sidecar_recovery_blocked"
        assert len(starts) == 1
        assert not old.stopped
    else:
        assert result["status"] == "running"
        assert result["active_recognition_catalog_id"] == catalog_a
        assert result["active_vision_pool_fingerprint"] == fingerprint_a
        assert result["active_vision_origin_generation_id"] == ""
        assert len(starts) == 2
        assert starts[-1]["target_catalog_id"] == catalog_a
        assert starts[-1]["target_generation_id"] == ""
        assert starts[-1]["expected_vision_pool_fingerprint"] == fingerprint_a
        assert old.stopped


@pytest.mark.parametrize("old_catalog", ["old-catalog", ""])
def test_supervisor_recovers_old_catalog_or_reports_missing_identity(monkeypatch, tmp_path, old_catalog):
    import threading

    from hextech.bootstrap.supervisor import RuntimeSupervisor
    from support.process_fakes import FakeProcess

    starts = []
    finished = threading.Event()
    runtime = OverlayRuntimeManager(
        prepare_data_func=lambda: pytest.fail("restart must not read pending Catalog"),
        game_active_probe=lambda: True,
        start_context_poller_func=None,
        write_inactive_func=lambda: None,
        start_sidecar_func=lambda **kwargs: (starts.append(kwargs), FakeProcess(2234))[1],
    )
    runtime.host_process = FakeProcess(1235)
    runtime.sidecar_process = FakeProcess(1234)
    runtime.desired_enabled = True
    runtime.status = "running"
    runtime.active_vision_pool_fingerprint = "old-fingerprint"
    runtime.active_vision_origin_generation_id = "old-generation"
    runtime.active_recognition_catalog_id = old_catalog
    runtime.pending_vision_pool_fingerprint = "new-fingerprint"
    runtime.pending_recognition_catalog_id = "new-catalog"
    runtime.cache_stats = {
        "vision_pool_fingerprint": "new-fingerprint", "recognition_catalog_id": "new-catalog",
    }
    runtime._prepared_vision_hint_cache = {"vision": {"catalog_generation_id": "new-catalog"}}
    monkeypatch.setattr(runtime, "_read_sidecar_liveness", lambda: {
        "status": "running" if runtime._sidecar_pid() == 2234 else "stale",
        "reason": "heartbeat_stale",
    })
    monkeypatch.setattr(runtime, "observe_data_generation", lambda: {
        "changed": False, "state": "deferred_game_active",
    })
    supervisor = RuntimeSupervisor(parent_pid=0, overlay_runtime=runtime, event_log_path=tmp_path / "events.jsonl")
    execute = supervisor._execute_game_overlay_action

    def tracked_execute(**kwargs):
        try:
            execute(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(supervisor, "_execute_game_overlay_action", tracked_execute)
    supervisor.tick()
    assert finished.wait(2)
    action = next(iter(supervisor._actions.values()))
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    event_names = [event["event"] for event in events]
    assert "game_overlay.sidecar_restart_requested" in event_names
    assert "game_overlay.sidecar_recovered" not in event_names
    assert runtime.pending_recognition_catalog_id == "new-catalog"
    assert runtime._vision_recovery_identity is None
    if old_catalog:
        assert action["status"] == "completed"
        assert "game_overlay.sidecar_restart" in event_names
        assert starts[0]["target_catalog_id"] == "old-catalog"
        assert starts[0]["expected_vision_pool_fingerprint"] == "old-fingerprint"
        assert runtime._prepared_vision_hint_cache is None  # Never retain pending hints as active.
        assert runtime.snapshot()["status"] == "running"
    else:
        assert action["status"] == "failed"
        assert "game_overlay.failed" in event_names
        assert "game_overlay.sidecar_restart" not in event_names
        assert action["result"]["last_start_failure_kind"] == "sidecar_recovery_identity_missing"
        assert not starts


@pytest.mark.parametrize("entrypoint", ["set_enabled", "_start"])
def test_reusable_sidecar_early_return_clears_recovery_identity(monkeypatch, entrypoint):
    import threading

    from support.process_fakes import FakeProcess

    runtime = OverlayRuntimeManager(
        prepare_data_func=lambda: pytest.fail("reusable sidecar does not prepare data"),
        start_context_poller_func=None,
    )
    runtime.host_process = FakeProcess(1235)
    runtime.sidecar_process = FakeProcess(1234)
    runtime._vision_recovery_identity = {
        "vision_pool_fingerprint": "old-fingerprint", "recognition_catalog_id": "old-catalog",
    }
    monkeypatch.setattr(runtime, "_read_sidecar_liveness", lambda: {"status": "running"})
    if entrypoint == "set_enabled":
        result = runtime.set_enabled(True)
    else:
        result = runtime._start(0, threading.Event())
    assert result["status"] == "running"
    assert runtime._vision_recovery_identity is None


def test_game_starts_during_prewarm_preserves_old_sidecar(monkeypatch):
    from support.process_fakes import FakeProcess

    playing = [False]
    old = FakeProcess(1234)
    runtime = OverlayRuntimeManager(prepare_data_func=lambda: {},
        game_active_probe=lambda: playing[0], start_context_poller_func=None,
        write_inactive_func=lambda: pytest.fail("handoff must not clear live slots"),
        start_sidecar_func=lambda **_: pytest.fail("new matrix started in game"))
    runtime.host_process = FakeProcess(1235)
    runtime.sidecar_process = old
    runtime.desired_enabled = True
    runtime.pending_vision_pool_fingerprint = "new"
    runtime._vision_handoff_in_progress = True
    monkeypatch.setattr(runtime, "_sidecar_is_reusable", lambda: True)
    monkeypatch.setattr(runtime, "_wait_for_template_prewarm", lambda **_: playing.__setitem__(0, True))
    result = runtime.set_enabled(True)
    assert result["vision_handoff_state"] == "deferred_game_active"
    assert runtime.sidecar_process is old
    assert not old.stopped


def test_rollback_has_recovery_budget_after_new_start_timeout(monkeypatch):
    import time
    import threading
    from support.process_fakes import FakeProcess

    runtime = OverlayRuntimeManager(start_context_poller_func=None,
        load_template_runtime_func=lambda **_: SimpleNamespace(stats={"recognition_catalog_id": "old-catalog"}))
    runtime._rollback_vision_hint_cache = {"source": {}}
    runtime._startup_hard_deadline = time.perf_counter() - 1
    def restore(*_):
        assert runtime._startup_hard_deadline > time.perf_counter()
        return FakeProcess(5432)
    monkeypatch.setattr(runtime, "_start_sidecar_with_retry", restore)
    assert runtime._rollback_vision_handoff("timeout", 0, threading.Event())
    assert runtime.active_recognition_catalog_id == "old-catalog"


def test_switch_context_requires_fresh_explicit_not_in_game(monkeypatch, tmp_path):
    from hextech.modules.data.ports import paths

    monkeypatch.setattr(paths, "get_var_dir", lambda: tmp_path)
    monkeypatch.setattr(vision_source.time, "time", lambda: 100)
    assert vision_source.recognition_switch_blocked()
    file = tmp_path / "state/data-service/download_context.v1.json"
    file.parent.mkdir(parents=True)
    for payload, blocked in [({"observed_at": 99, "in_game": False}, False),
                             ({"observed_at": 90, "in_game": False}, True),
                             ({"observed_at": 99, "in_game": True}, True)]:
        file.write_text(json.dumps(payload))
        assert vision_source.recognition_switch_blocked() is blocked


def test_retention_protects_active_recognition_catalog(tmp_path):
    from hextech.infrastructure.persistence.retention import protected_references

    status = tmp_path / "state/game_overlay_sidecar_status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"recognition_catalog_id": "catalog-active"}))
    assert "catalog-active" in protected_references(tmp_path)["catalog_generations"]


def test_restart_keeps_verified_new_catalog_with_old_statistics(tmp_path):
    import shutil
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort
    from hextech.modules.data.catalog.versioned import build_catalog_manifest, sha256_file
    from test_cohort_v3 import _v3_runtime
    from test_cohort_seed import _bundle_from_runtime

    source, _, _ = _v3_runtime(tmp_path / "source", details=True)
    bundle = _bundle_from_runtime(tmp_path, source)
    runtime = tmp_path / "runtime"
    install_bundled_cohort(bundle_root=bundle, runtime_root=runtime)
    pointer_file = runtime / "catalog/current.v2.json"
    old = json.loads(pointer_file.read_text())
    staging = tmp_path / "new-catalog"
    shutil.copytree(runtime / "catalog/generations" / old["catalog_generation_id"], staging)
    (staging / "hero_version.txt").write_text("new-version", encoding="utf-8")
    manifest = build_catalog_manifest(staging, created_at="2099-01-01T00:00:00+00:00")
    target = runtime / "catalog/generations" / manifest.catalog_generation_id
    shutil.copytree(staging, target)
    (target / "manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    current = {"schema_version": 2, "catalog_generation_id": manifest.catalog_generation_id,
               "content_sha256": manifest.content_sha256, "manifest_sha256": sha256_file(target / "manifest.json")}
    pointer_file.write_text(json.dumps(current), encoding="utf-8")
    before = (runtime / "snapshots/current.v2.json").read_bytes()
    install_bundled_cohort(bundle_root=bundle, runtime_root=runtime)
    assert json.loads(pointer_file.read_text())["catalog_generation_id"] == manifest.catalog_generation_id
    assert (runtime / "snapshots/current.v2.json").read_bytes() == before

    # Corruption cannot be legitimized by the independent publication path.
    (target / "hero_version.txt").write_text("broken", encoding="utf-8")
    install_bundled_cohort(bundle_root=bundle, runtime_root=runtime)
    assert json.loads(pointer_file.read_text())["catalog_generation_id"] == old["catalog_generation_id"]

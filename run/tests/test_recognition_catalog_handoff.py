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

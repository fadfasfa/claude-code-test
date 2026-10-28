"""Actual App composition must prewarm a real Catalog, in both launch modes."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("frozen", [False, True])
def test_default_app_owner_composes_and_prewarms_actual_catalog(tmp_path, monkeypatch, frozen):
    import sys
    from hextech.bootstrap.runtime_owner import AppRuntimeOwner
    from hextech.infrastructure.vision import data_source, template_runtime
    from hextech.modules.data.ports import paths
    from support.v4_release import make_release

    runtime_root = tmp_path / "runtime"
    make_release(runtime_root, monkeypatch)
    monkeypatch.setattr(paths, "RUNTIME_DATA_DIR", runtime_root)
    monkeypatch.setattr(template_runtime, "TEMPLATE_RUNTIME_CACHE_FILE", tmp_path / "cache" / "templates.npz")
    monkeypatch.setattr(template_runtime, "TEMPLATE_RUNTIME_CACHE_V1_FILE", tmp_path / "cache" / "legacy.npz")
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    owner = AppRuntimeOwner(event_log_path=tmp_path / "events.jsonl", monitor=False,
                            retention_worker=SimpleNamespace(request=lambda: None))
    try:
        runtime = owner._overlay_runtime
        assert runtime._template_loader() is template_runtime.load_or_build_default_template_runtime
        assert runtime._prepare_data_func is data_source.prepare_catalog_vision_data
        assert runtime._vision_pool_fingerprint_func is template_runtime.vision_pool_fingerprint
        assert runtime._game_active_probe is data_source.recognition_switch_blocked

        # Real v4 Catalog parsing, asset validation and template/matrix build; no mocked loader.
        owner.start_overlay_template_prewarm()
        runtime._prewarm_thread.join(timeout=30)
        assert not runtime._prewarm_thread.is_alive()
        assert runtime.cache_status == "ready", runtime.last_error
        assert runtime.cache_stats["production_pool_state"] == "ready"
        assert runtime.cache_stats["rank_identity_count"] == 1
        assert runtime.cache_stats["recognition_catalog_id"]
        hints = runtime._prepared_vision_hint_cache
        assert hints["vision"]["catalog_generation_id"] == runtime.cache_stats["recognition_catalog_id"]
        assert runtime.cache_stats["vision_pool_fingerprint"] == template_runtime.vision_pool_fingerprint(hints)
        assert (tmp_path / "cache" / "templates.npz").is_file()
    finally:
        owner.stop(timeout=5)

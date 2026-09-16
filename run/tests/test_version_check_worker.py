"""Production worker wiring, no live network or source publication."""
import pytest

from hextech.bootstrap import acquisition_worker as worker
from hextech.contracts import RefreshSourceState
from hextech.infrastructure.persistence.refresh_schedule import RefreshScheduleStore


@pytest.mark.parametrize("source,module,entry", [
    ("blitz", "hextech.infrastructure.sources.blitz.service", "refresh_blitz"),
    ("apex", "hextech.infrastructure.sources.apex.service", "main"),
    ("mayhem", "hextech.infrastructure.sources.mayhem.service", "run_mayhem_refresh"),
])
def test_incremental_worker_passes_check_cache_and_failed_identity(tmp_path, monkeypatch, source, module, entry):
    import importlib
    from dataclasses import replace
    from types import SimpleNamespace
    from hextech.modules.data.ports import paths

    monkeypatch.setattr(paths, "get_var_dir", lambda: tmp_path)
    store = RefreshScheduleStore(tmp_path)
    old = store.load()
    store.save(replace(old, sources={**old.sources, source: RefreshSourceState(failure_fingerprint="previous")}))
    monkeypatch.setattr(worker, "_watch_cancel", lambda *args: None)
    target = importlib.import_module(module)
    if source == "blitz":
        monkeypatch.setattr(target.CatalogBinding, "active", lambda **kwargs: object())
    if source == "apex":
        monkeypatch.setattr("hextech.modules.data.catalog.versioned.load_active_catalog",
                            lambda: SimpleNamespace(generation_id="catalog"))
    seen = {}
    def execute(**kwargs):
        seen.update(kwargs)
        return {"success": False, "reason": "validation_unchanged", "failure_fingerprint": "previous",
                "upstream_revision": "revision", "retry_after_seconds": 21600}
    monkeypatch.setattr(target, entry, execute)
    with pytest.raises(worker.SourceRefreshFailed) as caught:
        worker.run_worker(source, force=False, pointer_output=tmp_path / "pointer.json",
                          cancel_file=tmp_path / "cancel", incremental=True)
    assert seen["conditional_cache_root"] == tmp_path / "state" / "http-validators"
    assert seen["previous_failure_fingerprint"] == "previous"
    assert caught.value.payload["source_result"]["retry_after_seconds"] == 21600
    assert caught.value.payload["source_result"]["failure_fingerprint"] == "previous"


def test_check_fields_round_trip_without_authorizing_legacy_latest():
    legacy = RefreshSourceState.from_mapping({"state": "ready"})
    assert legacy.check_status == "never_checked" and not legacy.last_checked_at
    from dataclasses import asdict
    state = RefreshSourceState(check_status="up_to_date", last_checked_at="2026-09-15T00:00:00Z",
        upstream_revision="data/1", applied_revision="data/1", check_interval_seconds=14400,
        consecutive_failures=2, failure_fingerprint="x")
    assert RefreshSourceState.from_mapping(asdict(state)) == state

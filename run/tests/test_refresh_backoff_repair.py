"""Real scheduler entry must retain source backoff during automatic repair requests."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from hextech.contracts import RefreshSourceState
from test_incremental_refresh_service import _service, _units
from test_incremental_refresh_service import runtime as runtime


@pytest.mark.parametrize("scope", ["due", "core"])
@pytest.mark.parametrize("condition", ["cold", "missing_manifest", "missing_priority"])
def test_automatic_repair_preserves_backoff(runtime, monkeypatch, scope, condition):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *a, **k: pytest.fail("worker must not start"))
    ranking, hero = _units(binding, root / "units")
    if condition != "cold":
        service._ranking, service._heroes = ranking, {"1": hero}
    if condition == "missing_manifest":
        (root / "sources/aramkit/runs" / hero["run_id"] / "manifest.json").unlink()
    if condition == "missing_priority":
        service._context["champion_id"] = "2"
    future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    schedule = service.schedule_store.load()
    service.schedule_store.save(replace(schedule, sources={source: RefreshSourceState(
        state="backoff", next_due_at=future, consecutive_failures=2,
        failure_kind="transient_network", failure_fingerprint="failed-input") for source in schedule.sources}))
    before = service.schedule_store.path.read_bytes()
    service.marker_probe = lambda: pytest.fail("probe must respect source retry deadline")
    service.refresh(force=False, scope=scope)
    service.wait_optional()
    assert service.schedule_store.path.read_bytes() == before
    assert service.progress()["checked"] is False


@pytest.mark.parametrize("state,offset,force", [("backoff", -1, False), ("ready", 12, False),
                                              ("backoff", 12, True)])
def test_repair_allowed_after_backoff_or_normal_interval_or_explicit_force(runtime, monkeypatch, state, offset, force):
    service = _service(runtime, monkeypatch, lambda *a, **k: None)
    schedule = service.schedule_store.load()
    aramkit = RefreshSourceState(state=state, next_due_at=(datetime.now(timezone.utc)+timedelta(hours=offset)).isoformat())
    service.schedule_store.save(replace(schedule, sources={**schedule.sources, "aramkit": aramkit}))
    monkeypatch.setattr(service, "_due", lambda source, *a: False)
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    calls = []
    def dispatch(source, work, *, force=False):
        calls.append(source)
        raise RuntimeError("fixture ends after scheduling")
    monkeypatch.setattr(service, "_run", dispatch)
    service.refresh(force=force)
    assert calls == ["aramkit"]

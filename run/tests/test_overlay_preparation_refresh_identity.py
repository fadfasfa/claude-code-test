"""重复heartbeat不能重新准备/重绑定旧画面，时效变化仍须后台刷新。"""
from types import SimpleNamespace
import threading
import time

from hextech.interfaces.overlay import host_data_preparation as module


def event():
    return {"selection_type": "hextech", "active": True, "source": {
        "session_id": "g", "game_instance_id": "g", "window_hwnd": 1,
        "selection_epoch": 1, "selection_revision": 1, "scene_state": "active",
        "selection_window_active": True}, "slots": []}


def test_same_request_does_not_requeue_after_one_second(monkeypatch):
    worker = module.OverlayDataPreparation(SimpleNamespace())
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    worker.request(event(), {"ok": True}, completed=0, host_read_at=10.)
    first = worker._pending
    worker._pending = None
    worker._last_requested -= 2.
    worker.request(event(), {"ok": True}, completed=0, host_read_at=12.)
    assert worker._pending is None
    assert worker._latest_request == first


def test_caller_does_not_deepcopy_event(monkeypatch):
    worker = module.OverlayDataPreparation(SimpleNamespace())
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(module, "deepcopy", lambda value: (_ for _ in ()).throw(AssertionError("UI deep copy")))
    assert worker.request(event(), {"ok": True}, completed=0, host_read_at=10.)


def test_refresh_signature_ignores_age_but_tracks_expiry():
    from hextech.interfaces.overlay.host_data_preparation import source_refresh_identity
    a = {"generation_id": "g", "source_status": {"aramkit": {"state": "ready", "stale": False, "stale_age_seconds": 1}}}
    b = {"generation_id": "g", "source_status": {"aramkit": {"state": "ready", "stale": False, "stale_age_seconds": 2}}}
    assert source_refresh_identity(a) == source_refresh_identity(b)
    b["source_status"]["aramkit"]["stale"] = True
    assert source_refresh_identity(a) != source_refresh_identity(b)


def test_visual_variant_enrichment_is_a_real_preparation_change():
    payload = event()
    payload["slots"] = [{"state": "ready", "augment_id": "1", "name": "one", "slot_generation": 1}]
    before = module.preparation_key(payload, {}, 0)
    payload["slots"][0].update(tier="gold", visual_variant_id="1-gold")
    assert module.preparation_key(payload, {}, 0) != before


def test_background_rechecks_identity_without_repreparing_unchanged(monkeypatch):
    view = SimpleNamespace(status=lambda: {"generation_id": "g", "stale": False})
    worker = module.OverlayDataPreparation(SimpleNamespace(open_view=lambda: view))
    worker._bootstrap_view = view
    prepared = threading.Event()
    calls = []
    def prepare(*request):
        calls.append(request)
        worker._source_identity = module.source_refresh_identity(view.status())
        worker._result = SimpleNamespace(scope={"frozen": True})
        prepared.set()
    monkeypatch.setattr(worker, "_prepare", prepare)
    worker.request(event(), {"ok": True}, completed=0, host_read_at=10.)
    try:
        assert prepared.wait(1)
        time.sleep(1.15)
        assert len(calls) == 1
        view.status = lambda: {"generation_id": "g", "stale": True}
        deadline = time.monotonic()+2
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(calls) == 2
        assert calls[1][-3:] == calls[0][-3:]
    finally:
        worker.close()


def test_closed_or_invalidated_request_cannot_be_refreshed(monkeypatch):
    worker = module.OverlayDataPreparation(SimpleNamespace(open_view=lambda: (_ for _ in ()).throw(AssertionError("stale request"))))
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    worker.request(event(), {"ok": True}, completed=0, host_read_at=10.)
    old = worker._latest_request
    worker.invalidate()
    assert worker._latest_request is None
    assert not worker._needs_refresh(old)

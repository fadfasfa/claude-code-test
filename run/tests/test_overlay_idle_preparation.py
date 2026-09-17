"""冷启动无游戏时保持等待态，不对1x1占位做布局，也不吞掉真正的坏几何。"""
from queue import Queue
from types import SimpleNamespace
import pytest
from hextech.interfaces.overlay import host_runner
from support.host_input import PreloadedInputObserver


@pytest.fixture(autouse=True)
def preloaded_input(monkeypatch):
    monkeypatch.setattr(host_runner, "HostInputObserver", PreloadedInputObserver)


@pytest.mark.parametrize("show,session", [(False, ""), (False, "game-a"), (True, "game-a")])
def test_full_render_tick_defers_budget_until_viewport_available(monkeypatch, show, session):
    callbacks, reports = [], []
    event = {"active": show, "visible": show, "selection_type": "hextech", "slots": [],
             "source": {"session_id": session, "selection_epoch": 1, "game_instance_id": session,
                        "selection_window_active": show, "scene_state": "active" if show else "paused",
                        "transient_pause": not show}}
    class Preparation:
        invalidations = 0
        def request(self, *_args, **_kwargs):
            raise AssertionError("must not invent a viewport to prepare summaries")
        def status(self):
            return {"bootstrap_generation_id": "verified-seed", "generation": {}}
        def invalidate(self):
            self.invalidations += 1
    class Poller:
        def status(self):
            return {"last_probe_at": 1.0, "probe_status": "missing"}
    canvas = SimpleNamespace(winfo_width=lambda: 1, winfo_height=lambda: 1,
        after=lambda delay, callback: callbacks.append((delay, callback)) or "scheduled")
    gate = SimpleNamespace(evaluate=lambda *_a, **_k: SimpleNamespace(
        state="pending", reason="context_game_identity_missing", context_revision=0, held=False, payload={}))
    monkeypatch.setattr(host_runner, "ContextRenderGate", lambda: gate)
    monkeypatch.setattr(host_runner, "WindowTargetPoller", Poller)
    monkeypatch.setattr(host_runner, "_refresh_target_window", lambda *_a: None)
    monkeypatch.setattr(host_runner, "is_scoreboard_key_down", lambda: False)
    monkeypatch.setattr(host_runner, "_sync_event_visibility", lambda *_a, **_k: show)
    monkeypatch.setattr(host_runner, "_signal_overlay_ready", lambda: True)
    monkeypatch.setattr(host_runner, "_write_host_visibility_status", lambda *_a, **_k: None)
    monkeypatch.setattr(host_runner, "_write_overlay_session_report", lambda *_a, **_k: reports.append(1))
    visibility = {"window_target_poller": Poller(), "user_enabled": True}
    prep = Preparation()
    host_runner._schedule_event_render(object(), canvas, {}, visibility, Queue(),
        data_source=SimpleNamespace(read_event=lambda: event, read_context=lambda: {}), data_preparation=prep)
    assert reports and callbacks
    if show:
        assert visibility["consecutive_render_failures"] == 1
        assert not visibility.get("readiness_signaled")
    else:
        assert visibility["consecutive_render_failures"] == 0
        assert visibility["readiness_signaled"] is True
        assert prep.invalidations == (0 if session else 1)
        if not session:
            assert visibility["stats_generation_id"] == "verified-seed"

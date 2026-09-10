"""首个等待界面不等待数据准备请求或其共享锁。"""
from queue import Queue
from types import SimpleNamespace

from hextech.interfaces.overlay import host_runner


def test_shell_maps_before_preparation_and_yields_to_native_idle(monkeypatch):
    calls, callbacks = [], []
    event = {"active": True, "visible": True, "selection_type": "hextech", "slots": [],
             "source": {"session_id": "game", "game_instance_id": "game", "selection_epoch": 1,
                        "selection_window_active": True, "scene_state": "active"}}
    class Preparation:
        def request(self, *_a, **_k):
            calls.append("prepare")
        def status(self):
            calls.append("preparation-lock")
            return {}
    class Poller:
        def status(self):
            return {"last_probe_at": 1., "probe_status": "ok"}
    gate = SimpleNamespace(evaluate=lambda *_a, **_k: SimpleNamespace(
        state="confirmed", reason="", context_revision=1, held=False, payload={}))
    monkeypatch.setattr(host_runner, "ContextRenderGate", lambda: gate)
    monkeypatch.setattr(host_runner, "WindowTargetPoller", Poller)
    monkeypatch.setattr(host_runner, "_refresh_target_window", lambda *_a: None)
    monkeypatch.setattr(host_runner, "is_scoreboard_key_down", lambda: False)
    monkeypatch.setattr(host_runner, "_sync_event_visibility", lambda *_a, **k: calls.append("map") or True)
    monkeypatch.setattr(host_runner, "_signal_overlay_ready", lambda: True)
    monkeypatch.setattr(host_runner, "present_overlay_model", lambda *_a, **_k: calls.append("shell"))
    monkeypatch.setattr(host_runner, "_write_host_visibility_status", lambda *_a, **_k: None)
    monkeypatch.setattr(host_runner, "_write_overlay_session_report", lambda *_a, **_k: None)
    canvas = SimpleNamespace(after=lambda delay, callback: callbacks.append((delay, callback)) or "scheduled")
    visibility = {"render_full_overlay": True, "window_target_poller": Poller(), "user_enabled": True}
    host_runner._schedule_event_render(object(), canvas, {}, visibility, Queue(),
        data_source=SimpleNamespace(read_event=lambda: event, read_context=lambda: {}), data_preparation=Preparation())
    assert calls[-2:] == ["shell", "map"]
    assert "prepare" not in calls and "preparation-lock" not in calls
    assert callbacks[-1][0] == 16


def test_starvation_message_changes_display_key_but_not_slot_identity():
    from hextech.interfaces.overlay.host_render_state import SlotRenderCache, render_semantic_key
    from hextech.interfaces.overlay.host_data_preparation import preparation_key
    event = {"source": {"session_id": "g", "selection_epoch": 1},
             "slots": [{"state": "detecting", "slot_generation": 1} for _ in range(3)]}
    kwargs = dict(context_revision=1, generation_id="g", display_mode="compact", viewport=(1920, 1080))
    old_render, old_preparation = render_semantic_key(event, **kwargs), preparation_key(event, {}, None)
    event["slots"][0]["diagnostic"] = "evidence_starved"
    assert render_semantic_key(event, **kwargs) != old_render
    assert preparation_key(event, {}, None) != old_preparation
    shell = SlotRenderCache().build_shell(event, current_selection_key=("g", 1))
    assert shell["stats"][0]["stats_text"] == "识别未确认"
    assert shell["stats"][1]["stats_text"] == "识别中…"


def test_waiting_heartbeat_does_not_cancel_pending_mapping(monkeypatch):
    callbacks, draws, marks = [], [], []
    event = {"active": True, "visible": True, "selection_type": "hextech", "slots": [],
             "source": {"session_id": "g", "selection_epoch": 1, "scene_state": "active",
                        "selection_window_active": True, "reason": "waiting_gameflow"}}
    gate = SimpleNamespace(evaluate=lambda *_a, **_k: SimpleNamespace(
        state="pending", reason="", context_revision=0, held=False, payload={}))
    monkeypatch.setattr(host_runner, "ContextRenderGate", lambda: gate)
    monkeypatch.setattr(host_runner, "_refresh_target_window", lambda *_a: None)
    monkeypatch.setattr(host_runner, "is_scoreboard_key_down", lambda: False)
    monkeypatch.setattr(host_runner, "_sync_event_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(host_runner, "_draw_waiting_status", lambda *_a: draws.append(1))
    monkeypatch.setattr(host_runner, "mark_canvas_drawn", lambda *_a, **_k: marks.append(1))
    monkeypatch.setattr(host_runner, "_write_host_visibility_status", lambda *_a, **_k: None)
    monkeypatch.setattr(host_runner, "_write_overlay_session_report", lambda *_a, **_k: None)
    canvas = SimpleNamespace(winfo_width=lambda:1920,winfo_height=lambda:1080,
                             after=lambda delay, cb: callbacks.append(cb) or "scheduled")
    prep = SimpleNamespace(request=lambda *_a, **_k: "k", status=lambda: {})
    vis = {"render_full_overlay": False}
    host_runner._schedule_event_render(object(), canvas, {}, vis, Queue(),
        data_source=SimpleNamespace(read_event=lambda: event, read_context=lambda:{}), data_preparation=prep)
    for _ in range(10):
        callbacks.pop(0)()
    assert len(draws) == len(marks) == 1
    vis["geometry_version"] = 2
    callbacks.pop(0)()
    assert len(draws) == len(marks) == 2

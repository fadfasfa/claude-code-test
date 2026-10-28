"""Candidate projection and probe responsiveness; synthetic windows only, no real client input."""
from __future__ import annotations

from types import MethodType, SimpleNamespace
import queue
import threading
import time

import pytest

from hextech.interfaces.desktop.app_view import DesktopViewMixin
from hextech.interfaces.desktop.responsive_view import DesktopResponsiveMixin
from hextech.interfaces.desktop.runtime_window import candidate_update_loop
from hextech.interfaces.desktop.window_presentation import CandidateDisplay
from test_desktop_window_presentation import controller, observation


def bind_projection(ui, owner, snapshot):
    ui._desktop_window_presentation = owner
    ui.stop_event = threading.Event()
    ui._snapshot_generation_id = "g1"
    ui._snapshot_client = snapshot
    ui._champions_lock = threading.Lock()
    ui.champions = [{"id": "1"}]
    ui.current_candidate_groups = {"selected_champion_ids": [], "bench_champion_ids": []}
    for name in ("_candidate_groups_from_input", "_prepare_candidate_display", "update_ui"):
        setattr(ui, name, MethodType(getattr(DesktopViewMixin, name), ui))
    ui._build_candidate_display_list = lambda groups, champions, **kwargs: [
        {"id": value} for value in groups["bench_champion_ids"]]


def test_slow_snapshot_runs_off_gui_and_only_newest_request_reaches_gui():
    ui, owner = controller()
    entered, release, projected = threading.Event(), threading.Event(), threading.Event()
    opened_on, applied, publications = [], [], []
    view = SimpleNamespace(status=lambda: {"generation_id": "g1"}, get_champions=lambda: [{"id": "1"}])

    def open_view():
        opened_on.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return view

    bind_projection(ui, owner, SimpleNamespace(open_view=open_view))
    gui_thread = threading.get_ident()
    ui._apply_candidate_display = lambda display: applied.append((display, threading.get_ident()))
    publish = owner.publish_candidate_display

    def capture(display):
        result = publish(display)
        publications.append(result)
        if result:
            projected.set()
        return result

    owner.publish_candidate_display = capture
    worker = threading.Thread(target=candidate_update_loop, args=(ui,))
    worker.start()
    try:
        started = time.perf_counter()
        ui.update_ui({"bench_champion_ids": ["1"]})
        assert time.perf_counter()-started < .05
        assert entered.wait(.5)
        # Window safety and later input stay responsive throughout the blocked snapshot.
        assert owner.apply_latest(now=10.1).visible
        for champion in range(2, 102):
            ui.update_ui({"bench_champion_ids": [str(champion)]})
        release.set()
        assert projected.wait(1)
        owner.apply_latest(now=10.2)
        assert [entry[0].rows for entry in applied] == [({"id": "101"},)]
        assert applied[0][1] == gui_thread
        assert all(identity != gui_thread for identity in opened_on)
        assert publications == [False, True]
        assert len(opened_on) == 2
    finally:
        ui.stop_event.set()
        release.set()
        worker.join(2)
        owner.close()
    assert not worker.is_alive()


@pytest.mark.parametrize("invalidation", ["generation", "phase", "client", "game", "expiry", "close"])
def test_old_projection_is_rejected_at_publish_and_consume(invalidation):
    ui, owner = controller()
    ui._snapshot_generation_id = "g1"
    consumed = []
    ui._apply_candidate_display = consumed.append
    owner.publish_candidates({"bench_champion_ids": ["1"]})
    request = owner.take_candidate_request()
    display = CandidateDisplay(request, "g1", request.groups, ({"id": "1"},))
    assert owner.publish_candidate_display(display)
    if invalidation == "generation":
        ui._snapshot_generation_id = "g2"
    elif invalidation == "phase":
        owner.publish_phase({"context_phase": "lobby"}, client_hwnd=100, observed_at=11)
    elif invalidation == "client":
        owner.publish_window(observation(client_hwnd=101, observed_at=11))
    elif invalidation == "game":
        owner.publish_window(observation(game_window_hwnd=200, observed_at=11))
    elif invalidation == "expiry":
        owner._candidate_observed_at = time.monotonic()-9
    else:
        owner.close()
    assert not owner.publish_candidate_display(display)
    if invalidation != "close":
        owner._apply_candidate_display()
        owner.close()
    assert consumed == []


@pytest.mark.parametrize("view_generation", ["older", "newer"])
def test_projection_from_different_snapshot_generation_is_not_published(view_generation):
    ui, owner = controller()
    ui._snapshot_generation_id = "current"
    owner.publish_candidates({"bench_champion_ids": ["1"]})
    request = owner.take_candidate_request()
    assert not owner.publish_candidate_display(CandidateDisplay(request, view_generation, request.groups))
    owner.close()


def test_generation_refresh_keeps_newest_pending_candidate_groups():
    ui, owner = controller()
    ui._snapshot_generation_id = "g1"
    owner.publish_candidates({"bench_champion_ids": ["9"]})
    ui._snapshot_generation_id = "g2"
    owner.refresh_candidates({"bench_champion_ids": ["1"]})
    request = owner.take_candidate_request()
    assert request.groups == {"bench_champion_ids": ["9"]}
    assert request.generation == "g2"
    owner.close()


def test_shutdown_while_snapshot_is_blocked_never_publishes_to_destroyed_gui():
    ui, owner = controller()
    entered, release = threading.Event(), threading.Event()
    def open_view():
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(status=lambda: {"generation_id": "g1"}, get_champions=lambda: [{"id": "1"}])
    bind_projection(ui, owner, SimpleNamespace(open_view=open_view))
    ui._apply_candidate_display = lambda display: pytest.fail("closed GUI consumed projection")
    worker = threading.Thread(target=candidate_update_loop, args=(ui,))
    worker.start()
    try:
        ui.update_ui({"bench_champion_ids": ["1"]})
        assert entered.wait(.5)
        ui._closing = True
        ui.stop_event.set()
        owner.close()
        release.set()
        worker.join(2)
        assert owner._candidate_display is None
    finally:
        release.set()
        ui.stop_event.set()
        worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize("hard_hide", ["game", "foreground"])
def test_hard_hide_precedes_backlogged_gui_callbacks_and_drain_has_time_budget(hard_hide):
    ui, owner = controller()
    owner.apply_latest(now=10.1)
    assert ui._window_visible
    ui._ui_callbacks = queue.Queue()
    calls = []
    ui._drain_ui_callbacks = MethodType(DesktopResponsiveMixin._drain_ui_callbacks, ui)
    def work():
        assert not ui._window_visible
        calls.append(1)
        time.sleep(.003)
    for _ in range(100):
        ui._ui_callbacks.put(work)
    if hard_hide == "game":
        owner.publish_window(observation(game_visible=True, observed_at=10.2))
    else:
        ui._desktop_client_is_foreground = lambda hwnd: False
    owner.apply_latest(now=10.2)
    assert not ui._window_visible
    assert 1 <= len(calls) <= 2
    assert ui._ui_callbacks.qsize() >= 98
    assert owner.status()["gui_callbacks_elapsed_ms"] >= 3
    assert owner.status()["gui_callback_max_ms"] >= 3
    owner.close()


def test_scan_skips_own_and_known_non_game_process_without_reading_titles(monkeypatch):
    import os
    from hextech.modules.vision import window
    titles = []
    pids = {1: os.getpid(), 2: os.getpid()+1, 3: os.getpid()+2}
    monkeypatch.setattr(window.win32gui, "EnumWindows", lambda callback, data: [callback(hwnd, data) for hwnd in pids])
    monkeypatch.setattr(window.win32process, "GetWindowThreadProcessId", lambda hwnd: (1, pids[hwnd]))
    monkeypatch.setattr(window, "_window_process_name", lambda hwnd: "other.exe" if hwnd == 2 else "league of legends.exe")
    monkeypatch.setattr(window.win32gui, "GetWindowText", lambda hwnd: titles.append(hwnd))
    monkeypatch.setattr(window, "_window_client_rect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(window, "is_window_renderable", lambda hwnd: False)
    monkeypatch.setattr(window, "game_window_identity", lambda hwnd: dict(process_id=3, process_started_at=1,
        game_instance_id="game", identity_quality="process", game_window_mode_status="unknown",
        game_window_mode="unknown", game_window_mode_reason="unknown", game_window_mode_source="game_cfg",
        game_window_mode_observed_at=1))
    assert window.probe_lol_game_window(include_nonrenderable=True).hwnd == 3
    assert titles == []
    assert window.probe_lol_game_window().status == "missing"


def test_only_external_unreadable_process_uses_title_fallback(monkeypatch):
    import os
    from hextech.modules.vision import window
    monkeypatch.setattr(window.win32gui, "EnumWindows", lambda callback, data: callback(1, data))
    monkeypatch.setattr(window.win32process, "GetWindowThreadProcessId", lambda hwnd: (1, os.getpid()+1))
    monkeypatch.setattr(window, "_window_process_name", lambda hwnd: "")
    monkeypatch.setattr(window.win32gui, "GetWindowText", lambda hwnd: window.LOL_GAME_WINDOW_TITLE)
    monkeypatch.setattr(window, "_window_client_rect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(window, "is_window_renderable", lambda hwnd: True)
    monkeypatch.setattr(window, "game_window_identity", lambda hwnd: dict(process_id=3, process_started_at=1,
        game_instance_id="game", identity_quality="process", game_window_mode_status="unknown",
        game_window_mode="unknown", game_window_mode_reason="unknown", game_window_mode_source="game_cfg",
        game_window_mode_observed_at=1))
    assert window.probe_lol_game_window().hwnd == 1


def test_hidden_owned_win32_window_does_not_synchronously_query_gui_wm_gettext(monkeypatch):
    import os
    import win32api
    import win32con
    import win32gui
    from hextech.modules.vision import window
    messages = []
    def proc(hwnd, message, wparam, lparam):
        if message == win32con.WM_GETTEXT:
            messages.append(message)
            time.sleep(.35)
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)
    instance = win32api.GetModuleHandle(None)
    cls = win32gui.WNDCLASS()
    cls.hInstance = instance
    cls.lpszClassName = f"HextechProbeNoGetText-{os.getpid()}-{time.monotonic_ns()}"
    cls.lpfnWndProc = proc
    atom = win32gui.RegisterClass(cls)
    hwnd = win32gui.CreateWindowEx(0, atom, "owned probe fixture", 0, -30000, -30000, 32, 32, 0, 0, instance, None)
    foreground = win32gui.GetForegroundWindow()
    monkeypatch.setattr(window.win32gui, "EnumWindows", lambda callback, data: callback(hwnd, data))
    result = []
    scanner = threading.Thread(target=lambda: result.append(window.probe_lol_game_window(include_nonrenderable=True)))
    try:
        scanner.start()
        scanner.join(.15)
        completed_without_gui_pump = not scanner.is_alive()
        # Drain only our own pending messages if a regression sent synchronous WM_GETTEXT.
        if not completed_without_gui_pump:
            win32gui.PumpWaitingMessages()
            scanner.join(1)
        assert completed_without_gui_pump
        assert result[0].status == "missing"
        assert messages == []
        assert win32gui.GetForegroundWindow() == foreground
    finally:
        win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(atom, instance)

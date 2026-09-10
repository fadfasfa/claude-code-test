"""备战席显示、右侧空间与手动关闭的行为合同，不操作真实客户端。"""

from dataclasses import replace
from types import SimpleNamespace
import threading

import pytest

from hextech.interfaces.desktop.window_presentation import (
    DesktopMonitor,
    DesktopWindowObservation,
    DesktopWindowPresentation,
    resolve_desktop_window,
    right_dock_rect,
)


def observation(**changes):
    return replace(DesktopWindowObservation(
        client_hwnd=100, client_rect=(300, 100, 1580, 820),
        workarea=(0, 0, 1920, 1080), client_visible=True, client_active=True,
        observed_at=10.0, gameflow_known=True, gameflow_observed_at=10.0,
        gameflow_client_hwnd=100,
    ), **changes)


def decide(obs=None, **changes):
    args = dict(phase="champ_select", connection="connected", phase_hwnd=100,
                phase_at=10.0, user_hidden=False, manual_open=False,
                already_visible=False, mode="champ_select_only", now=10.1)
    args.update(changes)
    return resolve_desktop_window(obs or observation(), **args)


def test_right_dock_never_clamps_into_client_or_crosses_bottom():
    assert right_dock_rect((300, 100, 1580, 820), (0, 0, 1920, 1080)) == (1580, 100, 1900, 820)
    assert right_dock_rect((500, 100, 1780, 820), (0, 0, 1920, 1080)) is None
    assert right_dock_rect((-2200, 100, -920, 900), (-2560, 0, 0, 1440)) == (-920, 100, -600, 840)


def test_auto_window_only_opens_in_champion_select():
    assert decide().visible and not decide().topmost
    for phase in ("", "not_in_champ_select", "lobby", "end_of_game", "loading"):
        assert not decide(phase=phase).visible
    assert decide(connection="degraded").reason == "context_unconfirmed"
    assert decide(connection="degraded", already_visible=True).visible


def test_user_close_survives_foreground_new_selection_and_fallback_mode():
    for mode in ("champ_select_only", "client_right"):
        assert decide(user_hidden=True, mode=mode).reason == "user_hidden"
        result = decide(user_hidden=False, manual_open=True, phase="not_in_champ_select", mode=mode)
        assert not result.visible and result.reason == "not_in_champ_select"
        assert decide(user_hidden=False, manual_open=True, mode=mode).visible


def test_manual_open_cannot_bypass_game_foreground_phase_or_right_space():
    assert not decide(observation(game_visible=True), manual_open=True).visible
    assert not decide(observation(gameflow_in_progress=True), manual_open=True).visible
    manual = decide(
        observation(client_rect=(500, 100, 1780, 820)),
        manual_open=True,
        position_mode="manual",
        manual_rect=(100, 100, 420, 820),
    )
    assert not manual.visible and manual.reason == "right_width_insufficient"
    docked = decide(position_mode="manual", manual_open=True, manual_rect=(100, 100, 420, 820))
    assert docked.visible and not docked.topmost
    assert docked.rect == (1580, 100, 1900, 820)
    assert not decide(observation(client_visible=False), manual_open=True).visible
    assert decide(observation(client_active=False, overlay_active=True), manual_open=True).reason == "client_not_foreground"
    assert decide(observation(gameflow_known=False), phase="not_in_champ_select", manual_open=True).reason == "not_in_champ_select"


def test_stale_or_different_window_context_cannot_reopen_window():
    assert not decide(phase_hwnd=99).visible
    assert not decide(observation(observed_at=7.0)).visible
    assert not decide(phase_at=7.0).visible


def test_legacy_client_right_mode_still_requires_selection_and_never_topmost():
    result = decide(phase="not_in_champ_select", mode="client_right")
    assert not result.visible and result.reason == "not_in_champ_select"
    result = decide(mode="client_right")
    assert result.visible and not result.topmost
    assert result.rect == (1580, 100, 1900, 820)


class FakeRoot:
    def __init__(self):
        self.rect = (30, 60, 31, 61)
        self.mapped = False
        self.after_calls = []

    def winfo_ismapped(self): return self.mapped
    def winfo_rootx(self): return self.rect[0]
    def winfo_rooty(self): return self.rect[1]
    def winfo_width(self): return self.rect[2] - self.rect[0]
    def winfo_height(self): return self.rect[3] - self.rect[1]
    def after(self, delay, callback):
        self.after_calls.append((delay, callback))
        return len(self.after_calls)
    def after_cancel(self, _token): pass


class FakeUI:
    def __init__(self):
        self.root = FakeRoot()
        self._closing = False
        self._window_visible = False
        self._window_topmost = False
        self._auto_follow_enabled = True
        self._overlay_position_initialized = False
        self.cooldown_elapsed = True
        self.calls = []
        self.marks = []
        self.startup_timing = SimpleNamespace(mark=lambda *a, **kw: self.marks.append((a, kw)))

    def _show_overlay(self, topmost):
        if not self._window_visible:
            self.calls.append("show")
        self._window_visible = self.root.mapped = True
        self._window_topmost = topmost

    def _hide_overlay(self):
        if self._window_visible:
            self.calls.append("hide")
        self._window_visible = self.root.mapped = False
        self._window_topmost = False

    def _move_overlay_to(self, x, y, height=None):
        rect = (x, y, x + 320, y + height)
        if rect != self.root.rect:
            self.calls.append("move")
            self.root.rect = rect

    def _resume_auto_follow(self):
        self._auto_follow_enabled = True
        self.calls.append("resume")

    def _manual_follow_cooldown_elapsed(self, _seconds):
        return self.cooldown_elapsed


def controller(ui=None):
    ui = ui or FakeUI()
    c = DesktopWindowPresentation(ui)
    c.publish_window(observation())
    c.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.0)
    return ui, c


@pytest.mark.parametrize("hide_fails", [False, True])
def test_presentation_failure_records_actual_hide_outcome_and_keeps_retrying(monkeypatch, hide_fails):
    ui, c = controller()
    ui.root.mapped = ui._window_visible = True
    def fail():
        raise RuntimeError("desktop_window_style_readback_mismatch")
    monkeypatch.setattr(c, "apply_latest", fail)
    if hide_fails:
        monkeypatch.setattr(ui, "_hide_overlay", fail)
    c._tick()
    state = c.status()
    assert state["reason"] == "presentation_failed"
    assert state["hide_confirmed"] is (not hide_fails)
    assert state["visible"] is hide_fails
    assert state["error_type"] == "RuntimeError"
    assert ui.root.after_calls


def test_close_with_partial_map_hides_even_without_logical_visible_flag():
    ui, c = controller()
    ui.root.mapped = True
    ui._maintain_foreground_layer = lambda *a, **k: {}
    c.close()
    assert not ui.root.mapped


@pytest.mark.parametrize("shutdown", ["close", "destroy", "closing"])
def test_ui_callback_shutdown_does_not_continue_window_operations_or_reschedule(shutdown):
    ui, c = controller()
    def drain():
        if shutdown == "close":
            c.close()
        elif shutdown == "closing":
            ui._closing = True
        else:
            ui.root.winfo_exists = lambda: False
    ui._drain_ui_callbacks = drain
    assert c.apply_latest(now=10.1).reason == "closed"
    c._tick()
    assert ui.calls == []
    assert ui.root.after_calls == []


def test_insufficient_space_exposes_existing_status_guidance():
    ui, c = controller()
    c.publish_window(observation(client_rect=(500, 100, 1780, 820)))
    decision = c.apply_latest(now=10.1)
    assert not decision.visible
    assert "请将客户端左移" in c.status()["user_message"]
    assert ui.calls == []


def test_callback_closing_then_raising_does_not_reenter_destroyed_ui():
    ui, c = controller()
    def drain():
        c.close()
        raise RuntimeError("callback shutdown")
    ui._drain_ui_callbacks = drain
    c._tick()
    assert ui.calls == []
    assert ui.root.after_calls == []


def test_first_mapping_uses_confirmed_geometry_and_does_not_remap_unchanged_state():
    ui, c = controller()
    c.apply_latest(now=10.1)
    assert ui.calls == ["resume", "move", "show"]
    assert c.status()["actual_rect"] == (1580, 100, 1900, 820)
    for _ in range(20):
        c.apply_latest(now=10.2)
    assert ui.calls.count("show") == ui.calls.count("move") == 1
    assert len(ui.marks) == 1
    assert c.status()["mapped_at"] == 10.1


def test_close_remains_hidden_across_updates_and_manual_restore_requires_fresh_geometry(monkeypatch):
    ui, c = controller()
    c.apply_latest(now=10.1)
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.time", lambda: 10.2)
    c.request_hide()
    for _ in range(10):
        c.publish_window(observation(observed_at=10.3))
        c.apply_latest(now=10.3)
    assert ui.calls.count("show") == 1 and not ui.root.mapped
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.time", lambda: 10.4)
    c.request_show(seconds=10)
    assert c.apply_latest(now=10.4).reason == "waiting_fresh_window"
    c.publish_window(observation(observed_at=10.5, gameflow_observed_at=10.5))
    assert c.apply_latest(now=10.5).visible
    assert ui.root.mapped and not ui._window_topmost


def test_manual_drag_is_inert_and_auto_dock_never_leaves_client_edge():
    ui, c = controller()
    c.apply_latest(now=10.1)
    c.begin_manual_drag((1600, 120))
    c.drag_to_cursor((50, 160))
    ui.cooldown_elapsed = True
    c.apply_latest(now=10.2)
    assert c.position_mode == "auto" and c.manual_rect is None
    assert ui._auto_follow_enabled
    assert ui.root.rect == (1580, 100, 1900, 820)
    assert "resume" not in ui.calls[3:]


def test_inert_drag_preserves_foreground_lobby_visibility():
    ui, c = controller()
    c.apply_latest(now=10.1)
    c.begin_manual_drag((1600, 120))
    c.end_manual_drag()
    c.publish_phase({"context_phase": "lobby", "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.2)

    decision = c.apply_latest(now=10.2)

    assert c.position_mode == "auto" and c.manual_rect is None
    assert decision.reason == "client_foreground"
    assert ui.root.mapped


def test_tray_show_does_not_adopt_current_position_and_allows_foreground_lobby(monkeypatch):
    ui, c = controller()
    ui.root.rect = (100, 120, 420, 840)
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.time", lambda: 10.0)
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.monotonic", lambda: 10.0)

    c.request_show(seconds=10)
    assert c.position_mode == "auto" and c.manual_rect is None
    assert c.apply_latest(now=10.0).rect == (1580, 100, 1900, 820)

    c.request_restore_auto_docking(reason="tray_restore")
    c.publish_window(observation(observed_at=10.1, gameflow_observed_at=10.1))
    c.publish_phase({"context_phase": "lobby", "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.1)
    decision = c.apply_latest(now=10.1)
    assert c.position_mode == "auto"
    assert decision.reason == "client_foreground"


def test_tray_show_ignores_unmapped_tk_geometry_and_uses_client_dock(monkeypatch):
    ui, c = controller()
    ui.root.rect = (30, 60, 31, 61)
    ui._overlay_pixel_width = 320
    ui._window_height_px = 720
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.time", lambda: 10.0)
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.monotonic", lambda: 10.0)

    c.request_show(seconds=10)

    assert c.manual_rect is None and c.position_mode == "auto"
    assert c.apply_latest(now=10.0).rect == (1580, 100, 1900, 820)
    assert ui.root.rect == (1580, 100, 1900, 820)


def test_inert_drag_cannot_preserve_position_across_game_hide_and_display_change():
    ui, c = controller()
    c.apply_latest(now=10.1)
    c.begin_manual_drag((1600, 120))
    c.drag_to_cursor((100, 200))
    c.end_manual_drag()
    c.apply_latest(now=10.2)
    assert c.manual_rect is None and ui.root.rect == (1580, 100, 1900, 820)

    c.publish_window(observation(game_visible=True, observed_at=10.3, monitor_device="other"))
    assert c.apply_latest(now=10.3).reason == "game_in_progress"
    assert c.position_mode == "auto" and c.manual_rect is None
    assert not ui.root.mapped

    c.publish_window(observation(game_visible=False, observed_at=10.4, monitor_device="other",
                                 client_rect=(200, 100, 1480, 820)))
    c.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.4)
    assert c.apply_latest(now=10.4).visible
    assert c.position_mode == "auto" and c.manual_rect is None
    assert ui.root.rect == (1480, 100, 1800, 820)


def test_adjacent_monitor_is_not_a_production_dock_target():
    ui = FakeUI()
    c = DesktopWindowPresentation(ui)
    monitors = (
        DesktopMonitor("left-150", (-2560, 0, 0, 1528), 1.5),
        DesktopMonitor("right-100", (0, 0, 2560, 1392), 1.0),
    )
    c.publish_window(observation(
        client_rect=(-1920, 100, 0, 900), workarea=(-2560, 0, 0, 1528),
        dpi_scale=1.5, monitor_device="left-150", monitors=monitors,
    ))
    c.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.0)
    decision = c.apply_latest(now=10.1)
    assert not decision.visible and decision.rect is None
    assert decision.reason == "right_width_insufficient"
    assert not ui.root.mapped
    status = c.status()
    assert status["position_mode"] == "auto"
    assert status["client_monitor_device"] == "left-150"
    assert status["dock_monitor_device"] == "left-150"
    assert status["candidate_rejections"] == ["right_width_insufficient"]


def test_actual_minimum_height_rejects_same_screen_without_adjacent_fallback():
    ui = FakeUI()
    measured = []
    def measure(layout):
        measured.append(layout.monitor_device)
        return 300
    ui._apply_desktop_layout = measure
    c = DesktopWindowPresentation(ui)
    monitors = (
        DesktopMonitor("left", (-1000, 0, 0, 250), 1.0, (-1000, 0, 0, 900)),
        DesktopMonitor("right", (0, 0, 1000, 900), 1.0, (0, 0, 1000, 900)),
    )
    c.publish_window(observation(
        client_rect=(-800, 0, -300, 800), workarea=(-1000, 0, 0, 250),
        dpi_scale=1.0, monitor_device="left", monitors=monitors,
    ))
    c.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.0)

    decision = c.apply_latest(now=10.1)

    assert not decision.visible and decision.reason == "right_height_insufficient"
    assert decision.layout is not None and decision.layout.monitor_device == "left"
    assert decision.rect == (-300, 0, -44, 250)
    assert not ui.root.mapped and "move" not in ui.calls
    assert c.status()["candidate_rejections"] == ["right_height_insufficient"]
    assert measured == ["left"]
    measured.clear()
    c.apply_latest(now=10.2)
    assert measured == []  # 同一失败布局已测量；宽限期间不反复重排。


def test_mailbox_coalesces_geometry_and_ignores_results_after_close():
    ui, c = controller()
    c.publish_window(observation(client_rect=(200, 100, 1480, 820), observed_at=10.2))
    c.publish_window(observation(client_rect=(500, 100, 1780, 820), observed_at=10.1))
    c.apply_latest(now=10.2)
    assert ui.root.rect[0] == 1480
    c.close()
    c.publish_window(observation(observed_at=10.3))
    assert c.apply_latest(now=10.3).reason == "closed"


def test_background_publication_never_calls_tk_and_render_rejects_wrong_thread():
    ui, c = controller()
    errors = []
    def publish():
        c.publish_window(observation(observed_at=10.2))
        try:
            c.apply_latest(now=10.2)
        except RuntimeError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=publish)
    worker.start()
    worker.join()
    assert len(errors) == 1 and not ui.calls and not ui.root.after_calls


@pytest.mark.parametrize("client,reason", [
    ((500, 100, 1780, 820), "right_width_insufficient"),
    ((300, 900, 1580, 1000), "right_height_insufficient"),
])
def test_insufficient_space_reuses_last_layout_until_geometry_static_for_three_seconds(client, reason):
    ui, c = controller()
    ui._apply_desktop_layout = lambda layout: 300
    original = c.apply_latest(now=10.1).rect
    for now in (10.2, 11.0, 12.0, 13.19):
        c.publish_window(observation(client_rect=client, observed_at=now))
        c.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                        client_hwnd=100, observed_at=now)
        held = c.apply_latest(now=now)
        assert held.visible and not held.topmost and held.reason == "adjustment_grace"
        assert held.rect == original == ui.root.rect
        assert c.status()["space_reason"] == reason
    # 新快照与 GUI tick 不续期，只有客户端几何变化才会重启三秒时钟。
    c.publish_window(observation(client_rect=client, observed_at=13.2))
    expired = c.apply_latest(now=13.2)
    assert not expired.visible and expired.reason == reason
    assert not ui.root.mapped and c.status()["adjustment_remaining_seconds"] == 0
    assert ui.calls.count("show") == 1 and ui.calls.count("hide") == 1
    c.close()


@pytest.mark.parametrize("changes,phase,phase_hwnd,reason", [
    ({"client_active": False, "overlay_active": True}, "champ_select", 100, "client_not_foreground"),
    ({"game_visible": True}, "champ_select", 100, "game_in_progress"),
    ({"gameflow_in_progress": True}, "champ_select", 100, "game_in_progress"),
    ({"client_visible": False}, "champ_select", 100, "client_not_visible"),
])
def test_hard_gates_hide_immediately_during_space_grace(changes, phase, phase_hwnd, reason):
    ui, c = controller()
    assert c.apply_latest(now=10.1).visible
    c.publish_window(observation(client_rect=(500, 100, 1780, 820), observed_at=10.2))
    assert c.apply_latest(now=10.2).reason == "adjustment_grace"
    c.publish_window(observation(client_rect=(500, 100, 1780, 820), observed_at=10.3, **changes))
    c.publish_phase({"context_phase": phase, "context_connection_state": "connected"},
                    client_hwnd=phase_hwnd, observed_at=10.3)
    result = c.apply_latest(now=10.3)
    assert not result.visible and result.reason == reason
    assert not ui.root.mapped and not ui._window_topmost
    c.close()


@pytest.mark.parametrize("phase", ["not_in_champ_select", "loading", "end_of_game"])
def test_phase_changes_do_not_hide_foreground_client_without_game_evidence(phase):
    ui, c = controller()
    c.apply_latest(now=10.1)
    c.publish_phase({"context_phase": phase, "context_connection_state": "connected"},
                    client_hwnd=100, observed_at=10.2)
    c.apply_latest(now=10.2)
    assert ui.root.mapped


@pytest.mark.parametrize("client,workarea,target", [
    ((9700, 10000, 10980, 10720), (9500, 9900, 11500, 11000), (10980, 10000, 11300, 10720)),
    ((-2200, -10000, -920, -9280), (-2560, -10100, 0, -9000), (-920, -10000, -600, -9280)),
])
@pytest.mark.parametrize("startup_index", range(3))
def test_native_tk_desktop_maps_at_dock_without_activation_then_stays_closed(request, monkeypatch, client, workarea, target, startup_index):
    from native_tk_runner import run_native_tk_case

    if run_native_tk_case(request.node.nodeid):
        return
    import time
    import win32gui
    from hextech.interfaces.desktop.app import HextechUI

    # HWND 100 是几何 fixture，不是真实客户端；native layer 由独立测试覆盖。
    monkeypatch.setattr(HextechUI, "_bind_client_layer", lambda self, hwnd: None)
    monkeypatch.setattr(HextechUI, "_desktop_client_is_foreground", lambda self, hwnd: hwnd == 100)
    monkeypatch.setattr(HextechUI, "_maintain_foreground_layer", lambda self, hwnd, eligible:
                        {"desired_topmost": eligible, "actual_topmost": False})
    monkeypatch.setattr(HextechUI, "_start_desktop_tray", lambda self: None)
    monkeypatch.setattr(HextechUI, "_schedule_post_visible_bootstrap", lambda self: None)
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_services.initialize_window_threads", lambda ui: None)
    foreground = win32gui.GetForegroundWindow()
    ui = HextechUI()
    assert win32gui.GetForegroundWindow() == foreground, "constructor_activated"
    for method_name in ("_apply_desktop_layout", "_move_overlay_to", "_show_overlay"):
        method = getattr(ui, method_name)
        def checked(*args, _method=method, _name=method_name, **kwargs):
            import os
            result = _method(*args, **kwargs)
            import win32process
            from hextech.modules.vision.window import root_window_hwnd
            active = win32gui.GetForegroundWindow()
            assert active == foreground, (_name, active, root_window_hwnd(ui.root.winfo_id()),
                                               win32process.GetWindowThreadProcessId(active)[1] if active else 0, os.getpid())
            return result
        monkeypatch.setattr(ui, method_name, checked)
    try:
        assert not ui.root.winfo_ismapped()
        ui.root.attributes("-alpha", 0.0)
        c = ui._desktop_window_presentation
        now = time.time()
        # 全部是本测试窗口的离屏虚拟输入，不操作真实客户端或用户显示器。
        c.publish_window(observation(client_rect=client, workarea=workarea, observed_at=now))
        c.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                        client_hwnd=100, observed_at=now)
        c.apply_latest()
        ui.root.update()
        assert c.status()["actual_rect"] == target
        assert win32gui.GetForegroundWindow() == foreground
        ui._tray_controller = SimpleNamespace(refresh=lambda: None)
        ui.hide_to_tray()
        c.apply_latest()
        c.publish_window(observation(observed_at=time.time()))
        c.apply_latest()
        assert not ui.root.winfo_ismapped()
        assert win32gui.GetForegroundWindow() == foreground
    finally:
        ui.root.destroy()
    assert ui.stop_event.is_set()
    assert c.ui is None


def test_phase_publication_precedes_slow_list_update_and_uses_one_request(monkeypatch):
    import time
    from hextech.interfaces.desktop import runtime_window

    ui, c = controller()
    ui._desktop_window_presentation = c
    ui.stop_event = threading.Event()
    ui.pause_event = threading.Event()
    c.publish_window(observation(observed_at=time.time()))
    entered, release = threading.Event(), threading.Event()
    requests = []
    def phase(_ui):
        requests.append(1)
        return {"context_phase": "champ_select", "context_connection_state": "connected"}
    def slow_list(*_args, **_kwargs):
        entered.set()
        assert release.wait(2.0)
    monkeypatch.setattr(
        runtime_window,
        "resolve_lol_client_window",
        lambda **_kwargs: SimpleNamespace(
            status="found", hwnd=100, client_rect=(0, 0, 1280, 720)
        ),
    )
    monkeypatch.setattr(runtime_window, "_fallback_live_state", phase)
    monkeypatch.setattr(runtime_window, "_sync_candidate_ids", slow_list)
    monkeypatch.setattr(runtime_window, "_drain_preload_pending", lambda ui: None)
    worker = threading.Thread(target=runtime_window.lcu_polling_loop, args=(ui,))
    list_worker = threading.Thread(target=runtime_window.candidate_update_loop, args=(ui,))
    list_worker.start()
    worker.start()
    try:
        assert entered.wait(1.0)
        assert c.apply_latest().visible
        assert ui.root.mapped and len(requests) == 1
        # 慢列表尚未返回时，后续阶段仍能继续读取，而不是只有首帧侥幸提前发布。
        deadline = time.monotonic() + .8
        while len(requests) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(requests) >= 2 and not release.is_set()
    finally:
        ui.stop_event.set()
        release.set()
        worker.join(2.0)
        list_worker.join(2.0)
        c.close()
    assert not worker.is_alive() and not list_worker.is_alive()


def test_minimized_game_still_blocks_desktop_but_is_not_a_capture_target(monkeypatch):
    from hextech.modules.vision import window

    monkeypatch.setattr(window.win32gui, "EnumWindows", lambda callback, data: callback(777, data))
    monkeypatch.setattr(window.win32gui, "GetWindowText", lambda hwnd: "League of Legends (TM) Client")
    monkeypatch.setattr(window, "is_window_renderable", lambda hwnd: False)
    monkeypatch.setattr(window, "_window_client_rect", lambda hwnd: (-32000, -32000, -31840, -31973))
    monkeypatch.setattr(window, "_window_process_name", lambda hwnd: "league of legends.exe")
    assert window.find_lol_game_window() is None
    assert window.find_lol_game_window(include_nonrenderable=True)[0] == 777


def test_foreground_transition_wakes_phase_poll_instead_of_waiting_idle_interval():
    import time

    ui, c = controller()
    c.publish_window(observation(client_active=False, observed_at=10.1))
    entered, done = threading.Event(), threading.Event()
    stopped = threading.Event()
    def wait_phase():
        entered.set()
        c.wait_for_phase_poll(stopped)
        done.set()
    worker = threading.Thread(target=wait_phase)
    worker.start()
    try:
        assert entered.wait(.5)
        started = time.perf_counter()
        c.publish_window(observation(client_active=True, observed_at=10.2))
        assert done.wait(.3)
        assert time.perf_counter() - started < .3
    finally:
        stopped.set()
        c.close()
        worker.join(2.0)

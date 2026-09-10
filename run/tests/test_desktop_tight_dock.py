"""右侧贴边、前台硬门和三秒几何宽限。"""
from test_desktop_window_presentation import controller, observation
from hextech.interfaces.desktop.responsive_layout import DesktopMonitor


def publish(owner, now, rect=(500, 100, 1780, 820), **changes):
    owner.publish_window(observation(observed_at=now, client_rect=rect, **changes))
    owner.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                        client_hwnd=changes.get("client_hwnd", 100), observed_at=now)
    return owner.apply_latest(now=now)


def test_hold_last_position_for_three_seconds_then_recover():
    ui, owner = controller()
    original = owner.apply_latest(now=10.1).rect
    assert publish(owner, 11).rect == original
    assert owner.status()["reason"] == "adjustment_grace"
    assert publish(owner, 13.99).visible
    assert not publish(owner, 14).visible
    restored = publish(owner, 14.1, rect=(300, 100, 1580, 820))
    assert restored.visible and restored.rect == original and not ui._window_topmost


def test_only_geometry_changes_extend_grace_not_updates_or_mouse():
    _, owner = controller()
    owner.apply_latest(now=10.1)
    for timestamp in (11, 13, 15, 17):
        assert publish(owner, timestamp, rect=(500+int(timestamp), 100, 1780+int(timestamp), 820)).visible
    owner.begin_manual_drag((0, 0))
    owner.drag_to_cursor((999, 999))
    assert publish(owner, 19.99, rect=(517, 100, 1797, 820)).visible
    assert not publish(owner, 20, rect=(517, 100, 1797, 820)).visible
    assert owner.position_mode == "auto" and owner.manual_rect is None


def test_hard_hides_override_grace():
    for changes in ({"client_active": False, "overlay_active": True}, {"client_visible": False},
                    {"game_visible": True}, {"gameflow_in_progress": True}):
        ui, owner = controller()
        owner.apply_latest(now=10.1)
        assert publish(owner, 11).visible
        assert not publish(owner, 11.1, **changes).visible
        assert not ui._window_visible


def test_close_and_tray_do_not_extend_grace_or_bypass_foreground(monkeypatch):
    _, owner = controller()
    owner.apply_latest(now=10.1)
    publish(owner, 11)
    monkeypatch.setattr("hextech.interfaces.desktop.window_presentation.time.time", lambda: 12)
    owner.request_hide()
    assert not publish(owner, 12).visible
    owner.request_show(seconds=60)
    assert not publish(owner, 12.1, client_active=False).visible
    assert not publish(owner, 14).visible


def test_no_history_no_adjacent_screen_and_no_origin_fallback():
    _, owner = controller()
    monitors = (DesktopMonitor("left", (-2560, 0, 0, 1528), 1.5),
                DesktopMonitor("right", (0, 0, 2560, 1392), 1))
    result = publish(owner, 11, rect=(-1920, 100, 0, 900), monitors=monitors,
                     workarea=monitors[0].workarea, monitor_device="left", dpi_scale=1.5)
    assert not result.visible and result.rect is None


def test_hwnd_change_and_monitor_disconnect_discard_old_position():
    for changes in ({"client_hwnd": 101}, {"monitor_device": "replacement", "workarea": (-1920, 0, 0, 1080)}):
        _, owner = controller()
        owner.apply_latest(now=10.1)
        result = publish(owner, 11, **changes)
        assert not result.visible
        assert owner._dock_grace.last_layout is None


def test_live_foreground_rechecked_after_layout_before_show():
    ui, owner = controller()
    answers = iter((True, False))
    ui._desktop_client_is_foreground = lambda hwnd: next(answers)
    result = owner.apply_latest(now=10.1)
    assert not result.visible and not ui._window_visible


def test_height_rejection_is_not_remeasured_every_grace_tick():
    ui, owner = controller()
    changes = []
    def measure(layout):
        if not changes or changes[-1] != layout:
            changes.append(layout)
        return 400
    ui._apply_desktop_layout = measure
    owner.apply_latest(now=10.1)
    for i in range(40):
        assert publish(owner, 11+i*.025, rect=(400, 850, 1680, 1570)).reason == "adjustment_grace"
    assert len(changes) == 3  # original -> measure failing layout once -> restore original
    ui.current_candidate_groups = {"bench": ["changed"]}
    publish(owner, 12.1, rect=(400, 850, 1680, 1570))
    assert len(changes) == 5  # content changed: one fresh measure, not a permanent stale cache

"""r11 前台租约：自有窗口原生Z-order、限频、撤销与非激活。"""
from unittest.mock import Mock
import pytest


@pytest.mark.parametrize("blocker_topmost", [False, True])
def test_native_foreground_panel_recovers_above_nonactivating_blocker(request, monkeypatch, blocker_topmost):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import tkinter as tk
    import win32gui
    import time
    from hextech.interfaces.desktop import client_layer
    from hextech.interfaces.desktop.foreground_layer import ForegroundLayerLease
    from hextech.interfaces.desktop.presentation_smoke import _above
    from hextech.interfaces.desktop.window_activation import suppress_desktop_activation
    foreground = win32gui.GetForegroundWindow()
    windows = []
    flags = 0x0010 | 0x0001 | 0x0002 | 0x0200
    try:
        with suppress_desktop_activation():
            for x in (10000, 11280, 11280):
                root = tk.Tk()
                windows.append(root)
                root.withdraw()
                root.overrideredirect(True)
                root.attributes("-alpha", 0.0)
                root.geometry(f"320x600+{x}+10000")
                root.update_idletasks()
                hwnd = client_layer.desktop_wrapper_hwnd(root)
                win32gui.SetWindowLong(hwnd, -20, win32gui.GetWindowLong(hwnd, -20) | 0x08000000)
                root.deiconify()
                root.update_idletasks()
        owner, panel, blocker = [client_layer.desktop_wrapper_hwnd(w) for w in windows]
        client_layer.bind_client_layer(panel, owner)
        owner_style = win32gui.GetWindowLong(owner, -20)
        monkeypatch.setattr("hextech.interfaces.desktop.foreground_layer.client_is_foreground", lambda h: h == owner)
        lease = ForegroundLayerLease()
        lease.maintain(panel, owner, eligible=True, now=10)
        win32gui.SetWindowPos(blocker, -1 if blocker_topmost else 0, 0, 0, 0, 0, flags)
        state = lease.maintain(panel, owner, eligible=True, now=10.2)
        assert state["actual_topmost"] and state["owner_matches"]
        assert _above(win32gui, panel, blocker)
        client_layer.bind_client_layer(panel, owner)
        assert win32gui.GetWindowLong(panel, -20) & 8, "binder must preserve the foreground lease"
        assert win32gui.GetWindowLong(owner, -20) == owner_style
        assert win32gui.GetForegroundWindow() == foreground
        started = time.perf_counter()
        monkeypatch.setattr("hextech.interfaces.desktop.foreground_layer.client_is_foreground", lambda h: False)
        lease.maintain(panel, owner, eligible=True, now=10.3)
        windows[1].withdraw()
        elapsed = time.perf_counter()-started
        print({"blocker_topmost": blocker_topmost, "revoke_hide_ms": elapsed*1000,
               "last_maintain_ms": state["last_check_ms"], "owner_unchanged": win32gui.GetWindowLong(owner, -20) == owner_style})
        assert elapsed <= .100
        assert not win32gui.IsWindowVisible(panel) and not win32gui.GetWindowLong(panel, -20) & 8
        assert win32gui.GetForegroundWindow() == foreground
    finally:
        for root in reversed(windows):
            root.destroy()


def test_correction_budget_is_three_per_rolling_second_and_revoke_is_unlimited(monkeypatch):
    from hextech.interfaces.desktop import foreground_layer as layer
    monkeypatch.setattr(layer, "require_desktop_wrapper", lambda hwnd: None)
    monkeypatch.setattr(layer, "client_is_foreground", lambda hwnd: True)
    monkeypatch.setattr(layer.win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(layer.win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(layer.win32gui, "GetWindow", lambda hwnd, flag: 0)
    monkeypatch.setattr(layer.win32gui, "GetWindowLong", lambda hwnd, flag: 8)
    monkeypatch.setattr(layer, "probe_occlusion", lambda hwnd, owner: {"occluder_hwnd": 3, "scanned": 1, "truncated": False})
    change = Mock()
    monkeypatch.setattr(layer.win32gui, "SetWindowPos", change)
    lease = layer.ForegroundLayerLease()
    for at in (10, 10.11, 10.22, 10.33):
        state = lease.maintain(1, 2, eligible=True, now=at)
    assert change.call_count == 3
    assert state["reason"] == "layer_conflict"
    assert state["corrections_in_window"] == 3
    lease.maintain(1, 2, eligible=False, now=10.34)
    assert change.call_count == 4 and change.call_args.args[1] == -2


def test_layer_maintenance_runs_even_when_geometry_and_content_are_unchanged():
    from test_desktop_window_presentation import controller
    ui, owner = controller()
    maintain = Mock(return_value={"desired_topmost": True, "actual_topmost": True})
    ui._maintain_foreground_layer = maintain
    owner.apply_latest(now=10.1)
    ui.calls.clear()
    owner.apply_latest(now=10.2)
    assert maintain.call_count == 2
    assert ui.calls == []
    assert owner.status()["topmost"] is True


def test_probe_has_strict_count_limit_and_does_not_read_titles(monkeypatch):
    from hextech.interfaces.desktop import foreground_layer as layer
    monkeypatch.setattr(layer.time, "perf_counter", lambda: 0)
    monkeypatch.setattr(layer.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 100, 100))
    monkeypatch.setattr(layer.win32gui, "GetWindow", lambda hwnd, flag: hwnd+1)
    monkeypatch.setattr(layer.win32gui, "IsWindowVisible", lambda hwnd: False)
    title = Mock(side_effect=AssertionError("do not read window content"))
    monkeypatch.setattr(layer.win32gui, "GetWindowText", title)
    result = layer.probe_occlusion(1, 2)
    assert result["scanned"] == 24 and result["truncated"]
    title.assert_not_called()


def test_closing_flag_does_not_skip_immediate_lease_revocation():
    from test_desktop_window_presentation import controller
    ui, owner = controller()
    ui._window_visible = True
    ui._closing = True
    ui._maintain_foreground_layer = Mock(return_value={"actual_topmost": False})
    owner.close()
    ui._maintain_foreground_layer.assert_called_once_with(0, eligible=False)
    assert not ui._window_visible

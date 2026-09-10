"""桌面 r10：常态前台跟随、空操作与头像代际；不启动真实服务。"""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from dataclasses import replace

from hextech.interfaces.desktop.window_presentation import resolve_desktop_window
from test_desktop_window_presentation import observation, controller


def test_default_follows_foreground_client_without_lcu_or_selection():
    ui, owner = controller()
    owner.publish_phase({}, client_hwnd=0, observed_at=11)
    assert owner.mode == "client_foreground"
    assert owner.apply_latest(now=10.1).visible
    assert ui._window_visible


def test_foreground_mode_preserves_all_hard_hides():
    for change in ({"game_visible": True}, {"client_active": False}, {"client_visible": False},
                   {"gameflow_in_progress": True}):
        result = resolve_desktop_window(observation(**change), phase="", connection="", phase_hwnd=0,
            phase_at=0, user_hidden=False, manual_open=False, already_visible=False,
            mode="client_foreground", now=10.1)
        assert not result.visible


@pytest.mark.parametrize("changes", [{"gameflow_observed_at": 1}, {"gameflow_client_hwnd": 99},
                                     {"gameflow_known": False}])
def test_stale_or_different_client_gameflow_cannot_hide_foreground_client(changes):
    result = resolve_desktop_window(observation(gameflow_in_progress=True, **changes), phase="", connection="",
        phase_hwnd=0, phase_at=0, user_hidden=False, manual_open=False, already_visible=False,
        mode="client_foreground", now=10.1)
    assert result.visible


def test_unchanged_observation_does_not_enter_layout_move_show_or_bind():
    ui, owner = controller()
    ui._apply_desktop_layout = Mock(return_value=100)
    ui._bind_client_layer = Mock()
    owner.apply_latest(now=10.1)
    ui._apply_desktop_layout.reset_mock()
    ui._bind_client_layer.reset_mock()
    ui._move_overlay_to = Mock()
    ui._show_overlay = Mock()
    for index in range(20):
        owner.apply_latest(now=10.2+index*.001)
    for operation in (ui._apply_desktop_layout, ui._bind_client_layer, ui._move_overlay_to, ui._show_overlay):
        operation.assert_not_called()


def test_translation_only_does_not_remeasure_or_reflow():
    ui, owner = controller()
    ui._apply_desktop_layout = Mock(return_value=100)
    owner.apply_latest(now=10.1)
    ui._apply_desktop_layout.reset_mock()
    owner.publish_window(replace(observation(), client_rect=(310, 100, 1590, 820), observed_at=10.2))
    result = owner.apply_latest(now=10.2)
    assert result.rect == (1590, 100, 1910, 820)
    ui._apply_desktop_layout.assert_not_called()


def test_show_noop_short_circuits_before_native_decorator(monkeypatch):
    from hextech.interfaces.desktop.app_view import DesktopViewMixin
    from hextech.interfaces.desktop import client_layer
    ui = SimpleNamespace(_window_visible=True, root=SimpleNamespace(winfo_ismapped=lambda: True))
    probe = Mock(side_effect=AssertionError("no native owner operation on unchanged show"))
    monkeypatch.setattr(client_layer.win32gui, "GetWindow", probe)
    DesktopViewMixin._show_overlay(ui)
    probe.assert_not_called()


def test_binding_rejects_child_window_before_changing_parent(monkeypatch):
    from hextech.interfaces.desktop import client_layer
    monkeypatch.setattr(client_layer.win32gui, "IsWindow", lambda _: True)
    monkeypatch.setattr(client_layer.win32gui, "GetWindowLong", lambda hwnd, field: 0x40000000 if field == -16 else 0)
    write = Mock()
    monkeypatch.setattr(client_layer.win32gui, "SetWindowLong", write)
    with pytest.raises(ValueError, match="top-level"):
        client_layer.bind_client_layer(101, 102)
    write.assert_not_called()


def test_partial_map_failure_does_not_skip_native_withdraw():
    from hextech.interfaces.desktop.app_view import DesktopViewMixin
    root = SimpleNamespace(winfo_ismapped=lambda: True, withdraw=Mock())
    ui = SimpleNamespace(_window_visible=False, root=root, _set_window_topmost=Mock())
    ui._withdraw_overlay = lambda: DesktopViewMixin._withdraw_overlay.__wrapped__(ui)
    DesktopViewMixin._hide_overlay(ui)
    root.withdraw.assert_called_once()
    assert not ui._window_visible

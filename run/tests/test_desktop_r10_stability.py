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
    mapped = [True]
    root = SimpleNamespace(winfo_ismapped=lambda: mapped[0], withdraw=Mock(side_effect=lambda: mapped.__setitem__(0, False)))
    ui = SimpleNamespace(_window_visible=False, root=root, _set_window_topmost=Mock())
    ui._withdraw_overlay = lambda: DesktopViewMixin._withdraw_overlay.__wrapped__(ui)
    DesktopViewMixin._hide_overlay(ui)
    root.withdraw.assert_called_once()
    assert not ui._window_visible


def test_native_only_mapping_does_not_skip_withdraw_even_with_unmapped_inner():
    from hextech.interfaces.desktop.app_view import DesktopViewMixin
    ui = SimpleNamespace(_window_visible=False, root=SimpleNamespace(winfo_ismapped=lambda: False),
                         _desktop_native_visible=lambda: True, _withdraw_overlay=Mock())
    DesktopViewMixin._hide_overlay(ui)
    ui._withdraw_overlay.assert_called_once()


@pytest.mark.parametrize("native_hide_succeeds", [False, True])
def test_live_tk_withdraw_error_still_attempts_native_hide(monkeypatch, native_hide_succeeds):
    import tkinter as tk
    from hextech.interfaces.desktop.app_view import DesktopViewMixin
    from hextech.interfaces.desktop import client_layer
    calls, native_visible = [], [True]
    def withdraw():
        calls.append("tk_withdraw")
        raise tk.TclError("injected live Tk failure")
    def native_hide(root):
        calls.append("native_hide")
        native_visible[0] = not native_hide_succeeds
    monkeypatch.setattr(client_layer, "hide_desktop_wrapper", native_hide)
    root = SimpleNamespace(winfo_exists=lambda: True, winfo_ismapped=lambda: False, withdraw=withdraw)
    ui = SimpleNamespace(_window_visible=False, root=root, _set_window_topmost=Mock(),
                         _desktop_native_visible=lambda: native_visible[0])
    if native_hide_succeeds:
        DesktopViewMixin._withdraw_overlay.__wrapped__(ui)
        assert not ui._window_visible
    else:
        with pytest.raises(RuntimeError, match="hide_unconfirmed"):
            DesktopViewMixin._withdraw_overlay.__wrapped__(ui)
    assert calls == ["tk_withdraw", "native_hide"]


def test_destroyed_tk_during_withdraw_never_reuses_cached_wrapper(monkeypatch):
    import tkinter as tk
    from hextech.interfaces.desktop.app_view import DesktopViewMixin
    from hextech.interfaces.desktop import client_layer
    exists = [True]
    def unavailable():
        raise tk.TclError("application has been destroyed")
    def withdraw():
        exists[0] = False
        unavailable()
    root = SimpleNamespace(winfo_exists=lambda: exists[0], winfo_ismapped=unavailable,
                           wm_frame=Mock(side_effect=AssertionError("no current wrapper")), withdraw=withdraw)
    ui = SimpleNamespace(_window_visible=True, root=root, _set_window_topmost=Mock(), _desktop_hwnd=1234,
                         _desktop_native_visible=lambda: client_layer.desktop_wrapper_is_visible(root))
    native_hide = Mock(side_effect=AssertionError("must not write stale HWND"))
    monkeypatch.setattr(client_layer, "hide_desktop_wrapper", native_hide)
    DesktopViewMixin._withdraw_overlay.__wrapped__(ui)
    assert not ui._window_visible
    root.wm_frame.assert_not_called()
    native_hide.assert_not_called()

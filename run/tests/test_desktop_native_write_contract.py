"""自有窗口写入必须同时验证错误码和回读，不以无异常冒充成功。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("previous,error,actual,expected_error", [
    (0, 0, 128, None), (0, 5, 0, OSError),
    (32, 0, 32, RuntimeError), (32, 0, 128, None),
])
def test_checked_window_write(monkeypatch, previous, error, actual, expected_error):
    from hextech.interfaces.desktop import client_layer as layer
    monkeypatch.setattr(layer, "require_desktop_wrapper", lambda hwnd: None)
    setter = Mock(return_value=previous)
    monkeypatch.setattr(layer.ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(SetWindowLongPtrW=setter))
    monkeypatch.setattr(layer.ctypes, "set_last_error", lambda code: None)
    monkeypatch.setattr(layer.ctypes, "get_last_error", lambda: error)
    monkeypatch.setattr(layer, "_native_long", lambda hwnd, index: actual)
    if expected_error:
        with pytest.raises(expected_error):
            layer.checked_set_window_long(1, -20, 128)
    else:
        layer.checked_set_window_long(1, -20, 128)
    setter.assert_called_once_with(1, -20, 128)


def test_external_owner_write_is_forbidden(monkeypatch):
    from hextech.interfaces.desktop import client_layer as layer
    monkeypatch.setattr(layer, "require_desktop_wrapper", lambda hwnd: None)
    api = Mock()
    monkeypatch.setattr(layer.ctypes, "WinDLL", api)
    with pytest.raises(ValueError, match="ownership is forbidden"):
        layer.checked_set_window_long(1, -8, 2)
    api.assert_not_called()


def test_visibility_read_validates_current_wrapper_and_ignores_cached_handle(monkeypatch):
    from hextech.interfaces.desktop import client_layer as layer
    root = SimpleNamespace(winfo_exists=lambda: True, wm_frame=lambda: "0x456")
    validate, read = Mock(), Mock(return_value=True)
    monkeypatch.setattr(layer, "require_desktop_wrapper", validate)
    monkeypatch.setattr(layer.win32gui, "IsWindowVisible", read)
    assert layer.desktop_wrapper_is_visible(root)
    validate.assert_called_once_with(0x456)
    read.assert_called_once_with(0x456)


def test_destroyed_root_does_not_read_stale_wrapper(monkeypatch):
    from hextech.interfaces.desktop import client_layer as layer
    root = SimpleNamespace(winfo_exists=lambda: False, wm_frame=Mock())
    read = Mock()
    monkeypatch.setattr(layer.win32gui, "IsWindowVisible", read)
    assert not layer.desktop_wrapper_is_visible(root)
    root.wm_frame.assert_not_called()
    read.assert_not_called()


def test_native_hide_reads_back_failure_and_uses_noactivate_flags(monkeypatch):
    from hextech.interfaces.desktop import client_layer as layer
    root = SimpleNamespace(winfo_exists=lambda: True, wm_frame=lambda: "0x456")
    monkeypatch.setattr(layer, "require_desktop_wrapper", lambda hwnd: None)
    monkeypatch.setattr(layer.win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(layer.win32gui, "GetWindowLong", lambda hwnd, index: 0x8)
    position, show = Mock(), Mock()
    monkeypatch.setattr(layer.win32gui, "SetWindowPos", position)
    monkeypatch.setattr(layer.win32gui, "ShowWindow", show)
    with pytest.raises(RuntimeError, match="native_hide_unconfirmed"):
        layer.hide_desktop_wrapper(root)
    assert position.call_args.args[-1] & 0x0010
    assert position.call_args.args[-1] & 0x0200
    show.assert_called_once_with(0x456, 0)


def test_tk_operation_destroy_does_not_probe_old_wrapper_after_method(monkeypatch):
    import tkinter as tk
    from hextech.interfaces.desktop import client_layer as layer
    exists = [True]
    def probe_exists():
        if not exists[0]:
            raise tk.TclError("application has been destroyed")
        return True
    root = SimpleNamespace(winfo_exists=probe_exists)
    ui = SimpleNamespace(root=root)
    wrapper = Mock(return_value=0x456)
    monkeypatch.setattr(layer, "desktop_wrapper_hwnd", wrapper)
    monkeypatch.setattr(layer, "_hooks", {})
    monkeypatch.setattr(layer, "_session_end_handlers", {})
    @layer.unowned_tk_operation
    def destroy(self):
        exists[0] = False
    destroy(ui)
    wrapper.assert_called_once_with(root)
    assert ui._desktop_window_operation_depth == 0

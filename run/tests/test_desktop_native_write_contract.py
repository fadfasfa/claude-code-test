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

"""独立桌面面板：只写自有窗口，客户端仅为视觉跟随目标。"""
from __future__ import annotations
from functools import wraps
import ctypes
from ctypes import wintypes
import os
import win32gui
import win32process

_hooks: dict[int, tuple[object, object]] = {}
_regions: dict[int, tuple] = {}


def require_desktop_wrapper(hwnd: int) -> None:
    if not win32gui.IsWindow(hwnd) or win32gui.GetWindowLong(hwnd, -16) & 0x40000000:
        raise ValueError("desktop top-level wrapper is not materialized")
    if win32process.GetWindowThreadProcessId(hwnd)[1] != os.getpid():
        raise ValueError("desktop wrapper belongs to another process")
    if win32gui.GetClassName(hwnd) != "TkTopLevel":
        raise ValueError("desktop wrapper is not a Tk top-level")


def desktop_wrapper_hwnd(root) -> int:
    hwnd = int(root.wm_frame(), 0)
    require_desktop_wrapper(hwnd)
    return hwnd


def _native_long(hwnd: int, index: int) -> int:
    get = ctypes.windll.user32.GetWindowLongPtrW
    get.argtypes = [wintypes.HWND, ctypes.c_int]
    get.restype = ctypes.c_ssize_t
    return int(get(hwnd, index))


def checked_set_window_long(hwnd: int, index: int, value: int) -> None:
    """零返回可能成功也可能失败；必须核对错误码和实际结果。"""
    require_desktop_wrapper(hwnd)
    if index == -8:
        raise ValueError("external window ownership is forbidden")
    api = ctypes.WinDLL("user32", use_last_error=True)
    setter = api.SetWindowLongPtrW
    setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    setter.restype = ctypes.c_ssize_t
    ctypes.set_last_error(0)
    previous = setter(hwnd, index, value)
    error = ctypes.get_last_error()
    if previous == 0 and error:
        raise ctypes.WinError(error)
    actual = _native_long(hwnd, index)
    mask = 0xFFFFFFFF if index in {-16, -20} else -1
    if actual & mask != int(value) & mask:
        raise RuntimeError("desktop_window_style_readback_mismatch")


def _install_input_hook(hwnd: int) -> None:
    require_desktop_wrapper(hwnd)
    if hwnd in _hooks:
        return
    original = _native_long(hwnd, -4)
    def callback(window, message, wparam, lparam):
        if message == 0x21:
            return 3
        result = win32gui.CallWindowProc(original, window, message, wparam, lparam)
        if message == 0x82:
            _hooks.pop(window, None)
            _regions.pop(window, None)
        return result
    ctypes.windll.kernel32.SetLastError(0)
    old = win32gui.SetWindowLong(hwnd, -4, callback)
    error = ctypes.windll.kernel32.GetLastError()
    if not old and error:
        raise ctypes.WinError(error)
    if _native_long(hwnd, -4) == original:
        raise RuntimeError("desktop_input_hook_not_installed")
    _hooks[hwnd] = (original, callback)


def unowned_tk_operation(method):
    """兼容入口；只恢复自己的Tk过程，无跨进程owner写入。"""
    @wraps(method)
    def call(self, *args, **kwargs):
        if getattr(self, "_desktop_window_operation_depth", 0):
            return method(self, *args, **kwargs)
        hwnd = desktop_wrapper_hwnd(self.root)
        hook = _hooks.pop(hwnd, None)
        if hook:
            checked_set_window_long(hwnd, -4, int(hook[0]))
        self._desktop_window_operation_depth = 1
        try:
            result = method(self, *args, **kwargs)
        finally:
            self._desktop_window_operation_depth = 0
            if self.root.winfo_exists():
                current = desktop_wrapper_hwnd(self.root)
                if current != hwnd:
                    _regions.pop(hwnd, None)
                    self._desktop_wrapper_revision = getattr(self, "_desktop_wrapper_revision", 0) + 1
                self._desktop_hwnd = current
                _install_input_hook(current)
                style = win32gui.GetWindowLong(current, -20)
                expected = style | 0x08000000 | 0x80
                if style != expected:
                    checked_set_window_long(current, -20, expected)
                win32gui.RedrawWindow(current, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100 | 0x0400)
        return result
    return call


def client_is_foreground(hwnd: int) -> bool:
    foreground = win32gui.GetForegroundWindow()
    return bool(hwnd and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
                and not win32gui.IsIconic(hwnd)
                and (foreground == hwnd or win32gui.IsChild(hwnd, foreground)))


def client_layer_matches(hwnd: int, client_hwnd: int) -> bool:
    if hwnd not in _hooks or not win32gui.IsWindow(hwnd) or not win32gui.IsWindow(client_hwnd):
        return False
    style = win32gui.GetWindowLong(hwnd, -20)
    return bool(win32gui.GetWindow(hwnd, 4) == 0 and style & 0x08000000
                and _regions.get(hwnd) == (win32gui.GetWindowRect(hwnd), win32gui.GetWindowRect(client_hwnd)))


def bind_client_layer(hwnd: int, client_hwnd: int, *, topmost: bool | None = None) -> None:
    """兼容调用名；设置自有样式与裁剪，不建立native owner。"""
    require_desktop_wrapper(hwnd)
    if hwnd == client_hwnd or not client_hwnd or not win32gui.IsWindow(client_hwnd):
        raise ValueError("client follow target unavailable")
    if win32gui.GetWindow(hwnd, 4):
        raise RuntimeError("desktop_unexpected_native_owner")
    _install_input_hook(hwnd)
    style = win32gui.GetWindowLong(hwnd, -20)
    expected = style | 0x08000000 | 0x80
    if style != expected:
        checked_set_window_long(hwnd, -20, expected)
    if topmost is False and style & 0x8:
        win32gui.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0200 | 0x4000)
    exclude_client_overlap(hwnd, client_hwnd)


def exclude_client_overlap(hwnd: int, client_hwnd: int) -> None:
    require_desktop_wrapper(hwnd)
    panel, client = win32gui.GetWindowRect(hwnd), win32gui.GetWindowRect(client_hwnd)
    key = (panel, client)
    if _regions.get(hwnd) == key:
        return
    left, top = max(panel[0], client[0]), max(panel[1], client[1])
    right, bottom = min(panel[2], client[2]), min(panel[3], client[3])
    if left >= right or top >= bottom:
        win32gui.SetWindowRgn(hwnd, 0, True)
    else:
        region = win32gui.CreateRectRgnIndirect((0, 0, panel[2]-panel[0], panel[3]-panel[1]))
        excluded = win32gui.CreateRectRgnIndirect((left-panel[0], top-panel[1], right-panel[0], bottom-panel[1]))
        try:
            win32gui.CombineRgn(region, region, excluded, 4)
            win32gui.SetWindowRgn(hwnd, region, True)
            region = None
        finally:
            win32gui.DeleteObject(excluded)
            if region is not None:
                win32gui.DeleteObject(region)
    _regions[hwnd] = key
    win32gui.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0080)

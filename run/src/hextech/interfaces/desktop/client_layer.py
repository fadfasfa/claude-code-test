"""独立桌面面板：只写自有窗口，客户端仅为视觉跟随目标。"""
from __future__ import annotations
from functools import wraps
import ctypes
from ctypes import wintypes
import os
import tkinter as tk
from collections.abc import Callable
import win32gui
import win32process

_hooks: dict[int, tuple[object, object]] = {}
_regions: dict[int, tuple] = {}
_session_end_handlers: dict[int, tuple[Callable[[], object], Callable[[bool], object]]] = {}


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


def desktop_wrapper_is_visible(root) -> bool:
    """Tk inner 的映射位不能代表独立 native wrapper 的可见性。"""
    try:
        exists = root.winfo_exists()
    except tk.TclError:
        # 正常 destroy 后没有当前 wrapper，也不能退回缓存的旧 HWND。
        return False
    if not exists:
        return False
    hwnd = desktop_wrapper_hwnd(root)
    return bool(win32gui.IsWindowVisible(hwnd))


def hide_desktop_wrapper(root) -> None:
    """只撤销当前自有 wrapper 的残余映射，并检查 native 结果。"""
    if not desktop_wrapper_is_visible(root):
        return
    hwnd = desktop_wrapper_hwnd(root)
    if win32gui.GetWindowLong(hwnd, -20) & 0x8:
        win32gui.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0200)
    win32gui.ShowWindow(hwnd, 0)  # SW_HIDE，不激活其他窗口。
    if desktop_wrapper_is_visible(root):
        raise RuntimeError("desktop_native_hide_unconfirmed")


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
        if message == 0x11:  # WM_QUERYENDSESSION
            handler = _session_end_handlers.get(int(window))
            if handler is not None:
                try:
                    return 1 if handler[0]() is not False else 0
                except Exception:
                    return 0
            return 1
        if message == 0x16:  # WM_ENDSESSION
            handler = _session_end_handlers.get(int(window))
            if handler is not None:
                try:
                    handler[1](bool(wparam))
                except Exception:
                    pass
                # The handler may synchronously destroy the Tk HWND.  Do not
                # call the original WndProc with an already-destroyed window.
                return 0
        result = win32gui.CallWindowProc(original, window, message, wparam, lparam)
        if message == 0x82:
            _hooks.pop(window, None)
            _regions.pop(window, None)
            _session_end_handlers.pop(int(window), None)
        return result
    ctypes.windll.kernel32.SetLastError(0)
    old = win32gui.SetWindowLong(hwnd, -4, callback)
    error = ctypes.windll.kernel32.GetLastError()
    if not old and error:
        raise ctypes.WinError(error)
    if _native_long(hwnd, -4) == original:
        raise RuntimeError("desktop_input_hook_not_installed")
    _hooks[hwnd] = (original, callback)


def install_session_end_handler(
    hwnd: int,
    *,
    on_query: Callable[[], object],
    on_end: Callable[[bool], object],
) -> None:
    """在自有 Tk 顶层窗口上接收 Windows session-end 消息。"""

    require_desktop_wrapper(hwnd)
    _session_end_handlers[int(hwnd)] = (on_query, on_end)
    _install_input_hook(hwnd)


def unowned_tk_operation(method):
    """兼容入口；只恢复自己的Tk过程，无跨进程owner写入。"""
    @wraps(method)
    def call(self, *args, **kwargs):
        if getattr(self, "_desktop_window_operation_depth", 0):
            return method(self, *args, **kwargs)
        hwnd = desktop_wrapper_hwnd(self.root)
        session_handler = _session_end_handlers.get(hwnd)
        hook = _hooks.pop(hwnd, None)
        # Keep the session-end subclass installed while Tk pumps messages.
        # Geometry operations may recreate the wrapper; the saved handler is
        # migrated below even if the old HWND receives WM_NCDESTROY first.
        if hook and session_handler is None:
            checked_set_window_long(hwnd, -4, int(hook[0]))
        elif hook:
            _hooks[hwnd] = hook
        self._desktop_window_operation_depth = 1
        try:
            result = method(self, *args, **kwargs)
        finally:
            self._desktop_window_operation_depth = 0
            try:
                exists = self.root.winfo_exists()
            except tk.TclError:
                exists = False
            if exists:
                current = desktop_wrapper_hwnd(self.root)
                if current != hwnd:
                    _regions.pop(hwnd, None)
                    if session_handler is not None:
                        _session_end_handlers.pop(hwnd, None)
                        _session_end_handlers[current] = session_handler
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

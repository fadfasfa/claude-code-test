"""仅在当前GUI线程的窗口重建期间禁止激活；不拦截游戏或其他进程。"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from collections.abc import Iterator


_retained_hooks: dict[int, object] = {}


@contextmanager
def suppress_desktop_activation() -> Iterator[None]:
    if not hasattr(ctypes, "WinDLL"):
        yield
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    next_hook = user32.CallNextHookEx
    next_hook.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    next_hook.restype = ctypes.c_ssize_t

    class CreateStruct(ctypes.Structure):
        _fields_ = [("params", ctypes.c_void_p), ("instance", wintypes.HINSTANCE),
                    ("menu", wintypes.HMENU), ("parent", wintypes.HWND),
                    ("cy", ctypes.c_int), ("cx", ctypes.c_int), ("y", ctypes.c_int), ("x", ctypes.c_int),
                    ("style", wintypes.LONG), ("name", ctypes.c_void_p), ("class_name", ctypes.c_void_p),
                    ("ex_style", wintypes.DWORD)]

    class CreateWindow(ctypes.Structure):
        _fields_ = [("create", ctypes.POINTER(CreateStruct)), ("insert_after", wintypes.HWND)]
    created: list[tuple[int, bool, bool]] = []
    get_style = user32.GetWindowLongPtrW
    get_style.argtypes = [wintypes.HWND, ctypes.c_int]
    get_style.restype = ctypes.c_ssize_t
    set_style = user32.SetWindowLongPtrW
    set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    set_style.restype = ctypes.c_ssize_t

    @callback_type
    def callback(code, wparam, lparam):
        if code == 3 and lparam:
            creation = ctypes.cast(lparam, ctypes.POINTER(CreateWindow)).contents.create.contents
            if not creation.style & 0x40000000:  # 非WS_CHILD：新顶层在创建时就获得保护。
                created.append((int(wparam), bool(creation.style & 0x08000000), bool(creation.ex_style & 0x08000000)))
                creation.style |= 0x08000000  # WS_DISABLED：Tk的首次SetActiveWindow不得清空原前台。
                creation.ex_style |= 0x08000000
                set_style(wparam, -16, get_style(wparam, -16) | 0x08000000)
                set_style(wparam, -20, get_style(wparam, -20) | 0x08000000)
        # 不否决已经开始的HCBT_ACTIVATE，否则Windows可能把旧前台清空为NULL。
        # 在创建阶段赋予保护即可阻止Tk首次SetActiveWindow，且不改变其他窗口。
        return next_hook(None, code, wparam, lparam)

    install = user32.SetWindowsHookExW
    install.argtypes = [ctypes.c_int, callback_type, wintypes.HINSTANCE, wintypes.DWORD]
    install.restype = wintypes.HANDLE
    remove = user32.UnhookWindowsHookEx
    remove.argtypes = [wintypes.HANDLE]
    remove.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    handle = install(5, callback, None, kernel32.GetCurrentThreadId())
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    _retained_hooks[int(handle)] = callback
    try:
        yield
    finally:
        if not remove(handle):
            # 若OS未释放hook，保留回调地址，避免悬空的native callback。
            raise ctypes.WinError(ctypes.get_last_error())
        _retained_hooks.pop(int(handle), None)
        import win32gui
        for window, disabled, no_activate in created:
            if win32gui.IsWindow(window):
                if not disabled:
                    win32gui.EnableWindow(window, True)
                if not no_activate:
                    style = win32gui.GetWindowLong(window, -20)
                    win32gui.SetWindowLong(window, -20, style & ~0x08000000)

"""显式QA只捕获当前测试创建的Tk窗口，不捕获桌面或游戏。"""
from pathlib import Path
import ctypes

from PIL import Image
import win32gui
import win32ui

from hextech.modules.vision.window import root_window_hwnd
from hextech.interfaces.desktop.window_activation import suppress_desktop_activation


def capture_test_window(root, destination: Path) -> None:
    with suppress_desktop_activation():
        root.attributes("-alpha", 1.0)
        root.update()
    hwnd = root_window_hwnd(root.winfo_id())
    width, height = root.winfo_width(), root.winfo_height()
    dc_handle = win32gui.GetWindowDC(hwnd)
    dc = win32ui.CreateDCFromHandle(dc_handle)
    memory = dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(dc, width, height)
    memory.SelectObject(bitmap)
    try:
        memory.FillSolidRect((0, 0, width, height), 0x281409)
        win32gui.RedrawWindow(hwnd, None, None, 0x0001 | 0x0004 | 0x0080 | 0x0100)
        assert ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), 2)
        image = Image.frombuffer("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
        assert any(low != high for low, high in image.getextrema()), "native capture is blank"
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, dc_handle)

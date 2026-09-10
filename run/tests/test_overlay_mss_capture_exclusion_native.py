"""用自有 Tk fixture 验证 MSS 不会再次捕获受保护的 layered window。"""

from __future__ import annotations


def test_native_mss_respects_wda_excludefromcapture(request) -> None:
    from native_tk_runner import run_native_tk_case

    if run_native_tk_case(request.node.nodeid):
        return

    import ctypes
    import time
    import tkinter as tk

    from PIL import Image

    from hextech.interfaces.overlay.host_platform import (
        WDA_EXCLUDEFROMCAPTURE,
        _root_hwnd,
        _set_dpi_awareness,
        _window_client_rect_on_screen,
    )
    from hextech.modules.vision.screen_capture import MssCaptureBackend

    marker = (255, 0, 255)

    def marker_ratio(image: Image.Image) -> float:
        pixels = list(image.convert("RGB").getdata())
        matches = sum(
            max(abs(actual - expected) for actual, expected in zip(pixel, marker, strict=True)) <= 24
            for pixel in pixels
        )
        return matches / max(1, len(pixels))

    _set_dpi_awareness()
    target = tk.Tk()
    target.title("Hextech MSS exclusion target")
    target.overrideredirect(True)
    target.geometry("240x200+80+80")
    target.configure(bg="#114477")
    target.attributes("-topmost", True)

    overlay = tk.Toplevel(target)
    overlay.title("Hextech MSS exclusion overlay")
    overlay.overrideredirect(True)
    overlay.geometry("140x120+120+110")
    overlay.configure(bg="#ff00ff")
    overlay.attributes("-alpha", 1.0)
    overlay.attributes("-topmost", True)

    backend = MssCaptureBackend()
    hwnd = 0
    try:
        target.update_idletasks()
        target.update()
        overlay.lift()
        overlay.update_idletasks()
        overlay.update()
        hwnd = _root_hwnd(overlay)
        rect = _window_client_rect_on_screen(hwnd)
        assert rect is not None
        left, top, right, bottom = rect
        assert right - left >= 80 and bottom - top >= 80
        inset = 24
        capture_rect = (left + inset, top + inset, right - inset, bottom - inset)

        def capture() -> Image.Image:
            image = backend.capture_rgb(capture_rect)
            assert image is not None
            return image

        before = capture()
        assert marker_ratio(before) >= 0.90, "MSS fixture 映射前未捕获到自有 marker"

        user32 = ctypes.windll.user32
        assert user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        applied = ctypes.c_uint(0)
        assert user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(applied))
        assert int(applied.value) == WDA_EXCLUDEFROMCAPTURE
        try:
            ctypes.windll.dwmapi.DwmFlush()
        except Exception:
            pass
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            target.update()
            time.sleep(0.01)

        after = capture()
        assert marker_ratio(after) <= 0.05, "WDA_EXCLUDEFROMCAPTURE 后 MSS 仍捕获到 overlay marker"
        assert all(abs(actual-expected) <= 24 for actual, expected in
                   zip(after.getpixel((after.width//2, after.height//2)), (17, 68, 119), strict=True)), {
                       "reason": "排除marker后必须捕获到自有底色，黑帧不得算通过",
                       "actual_rgb": after.getpixel((after.width//2, after.height//2)),
                   }
    finally:
        if hwnd:
            try:
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0)
            except Exception:
                pass
        backend.close()
        try:
            overlay.destroy()
        finally:
            target.destroy()

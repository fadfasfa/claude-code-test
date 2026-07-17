"""Vision sidecar capture 职责模块。"""
# ruff: noqa: F403, F405

from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import *
from hextech.infrastructure.vision.sidecar_scene_geometry import *

def _set_dpi_awareness() -> None:
    # Vision 坐标必须以目标窗口客户区的物理像素为准；System DPI aware 在跨缩放显示器
    # 时会把 GetClientRect 与 ImageGrab 置于不同坐标系。
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        logger.debug("设置 Per-Monitor V2 DPI 感知失败，回退 System DPI aware。", exc_info=True)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logger.debug("设置 Vision sidecar DPI 感知失败。", exc_info=True)


def _window_dpi_scale(hwnd: int) -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
        return round(max(1, dpi) / 96.0, 4)
    except Exception:
        return 1.0


def _find_lol_game_window() -> tuple[int, tuple[int, int, int, int]] | None:
    return find_lol_game_window()


def _find_lol_game_rect() -> tuple[int, int, int, int] | None:
    target = _find_lol_game_window()
    return target[1] if target is not None else None


def _is_lol_game_foreground(hwnd: int | None) -> bool:
    if win32gui is None or not hwnd:
        return False
    try:
        foreground = int(win32gui.GetForegroundWindow())
        if not foreground:
            return False
        return root_window_hwnd(foreground) == root_window_hwnd(hwnd)
    except Exception:
        return False


def _capture_lol_game_rect(rect: tuple[int, int, int, int]) -> Image.Image | None:
    try:
        return ImageGrab.grab(bbox=rect).convert("RGB")
    except OSError:
        return None


def capture_lol_game_frame() -> Image.Image | None:
    """截取 LoL 游戏窗口矩形；找不到窗口时返回 None。"""

    rect = _find_lol_game_rect()
    if rect is None:
        return None
    return _capture_lol_game_rect(rect)



__all__ = [name for name in globals() if not name.startswith("__")]

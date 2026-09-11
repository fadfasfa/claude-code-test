# pyright: reportUnsupportedDunderAll=false
"""Vision sidecar capture 职责模块。"""
from __future__ import annotations

from hextech.infrastructure.vision.capture_geometry import required_capture_bounds
from hextech.infrastructure.vision.sidecar_common import (
    Image,
    configure_process_dpi_awareness,
    ctypes,
    logger,
    probe_lol_game_window,
    root_window_hwnd,
    win32gui,
)
from hextech.modules.vision.screen_capture import MssCaptureBackend


# 保留窄兼容名，现有 facade/tests 无需知道共享后端的分层迁移。
_MssCaptureBackend = MssCaptureBackend
_DEFAULT_CAPTURE_BACKEND: MssCaptureBackend | None = None


def _default_capture_backend() -> MssCaptureBackend:
    global _DEFAULT_CAPTURE_BACKEND
    if _DEFAULT_CAPTURE_BACKEND is None:
        _DEFAULT_CAPTURE_BACKEND = MssCaptureBackend()
    return _DEFAULT_CAPTURE_BACKEND


def close_capture_backend() -> None:
    """幂等关闭 Sidecar 当前 MSS 会话；下一次捕获会重新懒建。"""

    global _DEFAULT_CAPTURE_BACKEND
    backend, _DEFAULT_CAPTURE_BACKEND = _DEFAULT_CAPTURE_BACKEND, None
    if backend is not None:
        backend.close()


def _set_dpi_awareness() -> None:
    mode = configure_process_dpi_awareness()
    if mode == "unavailable":
        logger.debug("设置 Vision sidecar DPI 感知失败。")


def _window_dpi_scale(hwnd: int) -> float:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
        return round(max(1, dpi) / 96.0, 4)
    except Exception:
        return 1.0


def _find_lol_game_window() -> tuple[int, tuple[int, int, int, int]] | None:
    return probe_lol_game_window().target


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


def _capture_lol_game_rect(
    rect: tuple[int, int, int, int],
    *,
    preset_name: str = "auto",
    backend: MssCaptureBackend | None = None,
    force_full_client: bool = False,
) -> Image.Image | None:
    """用 MSS 抓取所需联合 ROI，并保留原 client size/坐标系。"""

    left, top, right, bottom = (int(value) for value in rect)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None

    try:
        if force_full_client:
            origin_box = (0, 0, width, height)
            capture_mode = "client_full_recovery"
        else:
            origin_box = required_capture_bounds((width, height), preset_name)
            capture_mode = "roi_union"
    except ValueError:
        # 未知版式无法证明局部范围完整；仍只抓当前游戏客户区。
        origin_box = (0, 0, width, height)
        capture_mode = "client_full"

    origin_x, origin_y, roi_right, roi_bottom = origin_box
    screen_box = (
        left + origin_x,
        top + origin_y,
        left + roi_right,
        top + roi_bottom,
    )
    capture_backend = backend or _default_capture_backend()
    crop = capture_backend.capture_rgb(screen_box)
    expected_crop_size = (roi_right - origin_x, roi_bottom - origin_y)
    if crop is None or crop.size != expected_crop_size:
        return None

    if capture_mode == "roi_union":
        frame = Image.new("RGB", (width, height), "black")
        origin = (origin_x, origin_y)
        frame.paste(crop, origin)
    else:
        frame = crop
        origin = (0, 0)
    frame.info.update(
        {
            "hextech_capture_mode": capture_mode,
            "hextech_roi_origin": origin,
            "hextech_roi_size": crop.size,
            "hextech_client_size": (width, height),
            "hextech_client_rect": (left, top, right, bottom),
            "hextech_capture_screen_rect": screen_box,
        }
    )
    return frame


def capture_lol_game_frame(*, preset_name: str = "auto") -> Image.Image | None:
    """截取 LoL 游戏窗口矩形；找不到窗口时返回 None。"""

    rect = _find_lol_game_rect()
    if rect is None:
        return None
    return _capture_lol_game_rect(rect, preset_name=preset_name)



__all__ = [name for name in globals() if not name.startswith("__")]

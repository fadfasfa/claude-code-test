"""单次捕获使用的原子窗口几何，禁止混用显示器与客户区尺寸。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameWindowObservation:
    hwnd: int
    observed_at: float
    client_rect: tuple[int, int, int, int]
    capture_size: tuple[int, int]
    dpi_scale: float
    foreground: bool
    layout_transform: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        left, top, right, bottom = self.client_rect
        expected = (max(0, right - left), max(0, bottom - top))
        if self.hwnd <= 0 or expected[0] <= 0 or expected[1] <= 0:
            raise ValueError("game_window_geometry_invalid")
        if self.capture_size != expected:
            raise ValueError("capture_client_size_mismatch")
        if self.dpi_scale <= 0:
            raise ValueError("dpi_scale_invalid")

"""右侧外贴的三秒调整宽限；只有客户端几何变化才能续期。"""
from __future__ import annotations
from dataclasses import dataclass
from .responsive_layout import DesktopLayout


@dataclass
class DockingGrace:
    client_hwnd: int = 0
    client_rect: tuple | None = None
    changed_at: float = 0.0
    last_layout: DesktopLayout | None = None

    def observe(self, window, now: float) -> None:
        if self.client_hwnd != window.client_hwnd:
            self.last_layout = None
            self.client_rect = None
        if self.client_rect != window.client_rect:
            self.changed_at = now
        self.client_hwnd = window.client_hwnd
        self.client_rect = window.client_rect
        if self.last_layout is not None:
            layout = self.last_layout
            areas = {m.device: m.workarea for m in window.monitors}
            area = areas.get(layout.monitor_device) if areas else window.workarea
            rect = layout.rect
            if (area is None or rect is None or layout.monitor_device != window.monitor_device
                or layout.dpi_scale != window.dpi_scale
                or not (area[0] <= rect[0] < rect[2] <= area[2] and area[1] <= rect[1] < rect[3] <= area[3])):
                self.last_layout = None

    def remaining(self, now: float) -> float:
        return max(0.0, min(3.0, 3.0 - (now-self.changed_at)))

    def fallback(self, now: float) -> DesktopLayout | None:
        return self.last_layout if self.remaining(now) > 0 else None

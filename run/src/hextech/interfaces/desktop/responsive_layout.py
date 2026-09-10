"""备战席逻辑尺寸与右侧空间约束；纯计算，不操作窗口。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
from typing import Literal

Rect = tuple[int, int, int, int]
LayoutMode = Literal["normal", "narrow", "minimum"]


def layout_mode(width: float, previous: str = "") -> LayoutMode:
    if width < 230:
        return "minimum"
    if width < 300:
        return "minimum" if previous == "minimum" and width < 238 else "narrow"
    if previous in {"minimum", "narrow"} and width < 308:
        return "narrow"
    return "normal"


@dataclass(frozen=True)
class DesktopLayout:
    rect: Rect | None = None
    dpi_scale: float = 1.0
    client_scale: float = 1.0
    logical_width: float = 0.0
    mode: LayoutMode = "normal"
    reason: str = "waiting_client"
    monitor_device: str = ""
    candidate_rejections: tuple[str, ...] = ()

    @property
    def scale(self) -> float:
        return self.dpi_scale * self.client_scale

    def px(self, value: float) -> int:
        return max(1, int(value * self.scale + .5))

    def font(self, size: float, bold: bool = False) -> tuple:
        pixels = max(1, int(max(12, size * self.client_scale) * self.dpi_scale + .5))
        return ("Microsoft YaHei", -pixels, "bold" if bold else "normal")

    def diagnostic(self) -> dict:
        return {**asdict(self), "coordinate_space": "logical_to_physical_once",
                "minimum_width_px": math.ceil(200 * self.dpi_scale)}


def resolve_desktop_layout(
    client: Rect | None, workarea: Rect | None, *, dpi_scale: float = 1.0,
    previous_mode: str = "",
) -> DesktopLayout:
    if not math.isfinite(dpi_scale) or not .5 <= dpi_scale <= 3:
        return DesktopLayout(reason="display_context_unavailable")
    if client is None or workarea is None:
        return DesktopLayout(reason="waiting_client", dpi_scale=dpi_scale)
    left, top, right, bottom = client
    wl, wt, wr, wb = workarea
    if right <= left or bottom <= top or wr <= wl or wb <= wt or right < wl or right > wr:
        return DesktopLayout(reason="right_width_insufficient", dpi_scale=dpi_scale)
    scale = max(.8, min(1.25, (right - left) / dpi_scale / 1280))
    width = min(max(math.ceil(200 * dpi_scale), int(320 * scale * dpi_scale + .5)), wr - right)
    logical = width / dpi_scale
    mode = layout_mode(logical, previous_mode)
    y = max(top, wt)
    end = min(bottom, wb, y + int(740 * scale * dpi_scale + .5))
    reason = "" if logical >= 200 else "right_width_insufficient"
    if not reason and end <= y:
        reason = "right_height_insufficient"
    return DesktopLayout(None if reason else (right, y, right + width, end),
                         dpi_scale, scale, logical, mode, reason)


@dataclass(frozen=True)
class DesktopMonitor:
    device: str
    workarea: Rect
    dpi_scale: float
    bounds: Rect | None = None


def _valid_monitor(m: DesktopMonitor) -> bool:
    left, top, right, bottom = m.workarea
    return right > left and bottom > top and math.isfinite(m.dpi_scale) and .5 <= m.dpi_scale <= 3


def monitor_for_point(monitors: tuple[DesktopMonitor, ...], point: tuple[int, int]) -> DesktopMonitor | None:
    valid = [m for m in monitors if _valid_monitor(m)]
    if not valid:
        return None
    x, y = point
    def distance(m):
        left, top, right, bottom = m.bounds or m.workarea
        return max(left-x, 0, x-right+1)**2 + max(top-y, 0, y-bottom+1)**2
    return min(valid, key=lambda m: (distance(m), m.device))


def drag_origin(cursor: tuple[int, int], anchor: tuple[int, int], *, source_dpi_scale: float,
                target_dpi_scale: float) -> tuple[int, int]:
    if not all(math.isfinite(x) and x > 0 for x in (source_dpi_scale, target_dpi_scale)):
        raise ValueError("invalid drag DPI")
    scale = target_dpi_scale / source_dpi_scale
    return round(cursor[0] - anchor[0] * scale), round(cursor[1] - anchor[1] * scale)


def resolve_manual_desktop_layout(rect: Rect, monitors: tuple[DesktopMonitor, ...], *,
                                  source_dpi_scale: float = 1, client_scale: float = 1,
                                  cursor: tuple[int, int] | None = None, settle: bool = True,
                                  previous_mode: str = "") -> DesktopLayout:
    left, top, right, bottom = rect
    if not math.isfinite(source_dpi_scale) or source_dpi_scale <= 0 or right <= left or bottom <= top:
        return DesktopLayout(reason="manual_geometry_unavailable")
    monitor = monitor_for_point(monitors, cursor or ((left+right)//2, (top+bottom)//2))
    if monitor is None:
        return DesktopLayout(reason="display_context_unavailable")
    wl, wt, wr, wb = monitor.workarea
    dpi = monitor.dpi_scale
    width = min(wr-wl, max(math.ceil(200*dpi), round((right-left)*dpi/source_dpi_scale)))
    height = min(wb-wt, max(math.ceil(200*dpi), round((bottom-top)*dpi/source_dpi_scale)))
    if width < math.ceil(200*dpi):
        return DesktopLayout(reason="screen_width_insufficient", monitor_device=monitor.device)
    if settle:
        left, top = max(wl, min(left, wr-width)), max(wt, min(top, wb-height))
    logical = width/dpi
    return DesktopLayout((left,top,left+width,top+height), dpi, client_scale, logical,
                         layout_mode(logical, previous_mode), "", monitor.device)


def _auto_desktop_candidate_data(client: Rect | None, monitors: tuple[DesktopMonitor, ...], *,
                                 client_monitor_device: str, client_dpi_scale: float = 1,
                                 previous_mode: str = "", previous_monitor_device: str = "",
                                 ) -> tuple[tuple[DesktopLayout, ...], tuple[str, ...], str]:
    if client is None or not math.isfinite(client_dpi_scale) or client_dpi_scale <= 0:
        return (), (), "waiting_client"
    left,top,right,bottom = client
    valid = [m for m in monitors if _valid_monitor(m)]
    origin = next((m for m in valid if m.device == client_monitor_device), None)
    if origin is None:
        origin = monitor_for_point(tuple(valid), ((left+right)//2,(top+bottom)//2))
    if origin is None or right <= left or bottom <= top:
        return (), (), "display_context_unavailable"
    scale = max(.8, min(1.25, (right-left)/client_dpi_scale/1280))
    rejections, candidates = [], []
    for m in sorted(valid, key=lambda x: (x != origin, (x.bounds or x.workarea)[0], x.device)):
        wl,wt,wr,wb = m.workarea
        bounds = m.bounds or m.workarea
        edge = (origin.bounds or origin.workarea)[2]
        if m != origin and abs(bounds[0]-edge) > 1:
            rejections.append(f"{m.device}:non_adjacent_blank_gap")
            continue
        x,y = max(right,wl), max(top,wt)
        width = min(wr-x, max(math.ceil(200*m.dpi_scale), round(320*scale*m.dpi_scale)))
        end = min(bottom,wb,y+round(740*scale*m.dpi_scale))
        if (right < wl and m == origin) or width < math.ceil(200*m.dpi_scale):
            rejections.append(f"{m.device}:right_width_insufficient")
            continue
        if end-y < math.ceil(200*m.dpi_scale):
            rejections.append(f"{m.device}:vertical_intersection_insufficient")
            continue
        layout = DesktopLayout((x,y,x+width,end),m.dpi_scale,scale,width/m.dpi_scale,
                               layout_mode(width/m.dpi_scale,previous_mode),"",m.device)
        candidates.append((layout, wr-x))
    if not candidates:
        return (), tuple(rejections), "no_valid_dock_target"
    chosen, available = candidates[0]
    previous = next((layout for layout,_ in candidates if layout.monitor_device == previous_monitor_device), None)
    if previous is not None and previous.monitor_device != chosen.monitor_device and available < (320*scale+8)*chosen.dpi_scale:
        candidates = [item for item in candidates if item[0] == previous] + [
            item for item in candidates if item[0] != previous
        ]
    rejected = tuple(rejections)
    return tuple(replace(layout, candidate_rejections=rejected) for layout,_ in candidates), rejected, ""


def auto_desktop_layout_candidates(client: Rect | None, monitors: tuple[DesktopMonitor, ...], *,
                                   client_monitor_device: str, client_dpi_scale: float = 1,
                                   previous_mode: str = "", previous_monitor_device: str = "",
                                   ) -> tuple[DesktopLayout, ...]:
    """按稳定优先级返回全部几何有效候选；真实控件高度由 Tk owner 继续筛选。"""
    candidates, _rejections, _reason = _auto_desktop_candidate_data(
        client, monitors, client_monitor_device=client_monitor_device,
        client_dpi_scale=client_dpi_scale, previous_mode=previous_mode,
        previous_monitor_device=previous_monitor_device,
    )
    return candidates


def resolve_auto_desktop_layout(client: Rect | None, monitors: tuple[DesktopMonitor, ...], *,
                               client_monitor_device: str, client_dpi_scale: float = 1,
                               previous_mode: str = "", previous_monitor_device: str = "") -> DesktopLayout:
    candidates, rejections, reason = _auto_desktop_candidate_data(
        client, monitors, client_monitor_device=client_monitor_device,
        client_dpi_scale=client_dpi_scale, previous_mode=previous_mode,
        previous_monitor_device=previous_monitor_device,
    )
    return candidates[0] if candidates else DesktopLayout(reason=reason, candidate_rejections=rejections)

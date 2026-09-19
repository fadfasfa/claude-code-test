"""Overlay 布局模型与 Canvas 绘制实现。"""

from __future__ import annotations

import time
import math
from collections.abc import Sequence
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from hextech.modules.vision.layout import LayoutTransform, apply_transform
from .display_geometry import (
    display_card_panels,
    display_geometry_metadata,
    display_stat_boxes,
)
from .data_notice import DataNoticeModel
from .text_metrics import (
    canvas_text_metrics, _visual_text_width, _ellipsize_visual, _wrap_visual_text,
    fit_stats_block,
)
from .synergy_canvas import (
    draw_compact_synergy_panel as _draw_compact_synergy_panel_impl,
    draw_expanded_synergy_panel as _draw_expanded_synergy_panel_impl,
)
from .typography import (
    OverlayTypographyMetrics,
    SynergyTextLayout as SynergyTextLayout,
    resolve_overlay_typography,
)

INNER_BAR_TOP_MARGIN_RATIO = 0.829

OVERLAY_THEME: dict[str, str] = {
    "panel_bg": "#0A1428",
    "outer_gold": "#C8AA6E",
    "middle_bronze": "#785A28",
    "inner_bluegray": "#091428",
    "highlight_cyan": "#0AC8B9",
    "text_primary": "#F0E6D2",
    "text_secondary": "#A09B8C",
    "text_muted": "#5C5B57",
    "text_shadow": "#000000",
    "stat_label": "#F0E6D2",
    "stat_value": "#FFB23E",
    "stat_low_sample": "#F87171",
    "stat_aggregate": "#3FA9DC",
    "stat_separator": "#9A7B3F",
    "prismatic": "#F498F5",
    "gold": "#C8AA6E",
    "silver": "#9EACA8",
}


class CanvasLike(Protocol):
    def winfo_width(self) -> int: ...

    def winfo_height(self) -> int: ...

    def delete(self, *args: Any) -> Any: ...

    def create_polygon(self, *args: Any, **kwargs: Any) -> Any: ...

    def create_line(self, *args: Any, **kwargs: Any) -> Any: ...

    def create_rectangle(self, *args: Any, **kwargs: Any) -> Any: ...

    def create_text(self, *args: Any, **kwargs: Any) -> Any: ...


StatStatusCode = Literal[
    "READY",
    "DETECTING",
    "RECOGNITION_MISSING",
    "SNAPSHOT_UNAVAILABLE",
    "STATS_PREPARING",
    "STATS_STALE",
    "GENERATION_DEGRADED",
    "PRIVACY_OFF",
    "NO_STATS",
    "SOURCE_STAT_MISSING",
    "CHAMPION_STAT_MISSING",
    "IDENTITY_UNRESOLVED",
    "CONTEXT_MISSING",
    "CONTEXT_EXPIRED",
]
SynergyStatusCode = Literal[
    "READY",
    "SYNERGY_DEGRADED",
    "NO_MATCH",
    "CONFIRMED_EMPTY",
    "CONTEXT_MISSING",
    "SOURCE_UNAVAILABLE",
    "GENERATION_MISMATCH",
]
class StatPanelModel(TypedDict):
    slot: int
    state: str
    name: str
    tier: str
    stats_text: str
    status_code: StatStatusCode
    winrate_text: str
    pickrate_text: str
    status_text: str
    synergy_status: SynergyStatusCode
    stats_tone: NotRequired[Literal["default", "low_sample", "aggregate"]]
    low_sample_outline: NotRequired[bool]
    sample_count: NotRequired[int | None]
    stats_scope: NotRequired[str]
    fallback_reason: NotRequired[str]
    # 命中 hint 的规范 augment_id；仅供 session report 关联数据层，不参与绘制。
    hint_id: NotRequired[str]


class SynergyPanelModel(TypedDict):
    slot: int
    augment_name: str
    tier: str
    hero_name: str
    rating: str
    tag: str
    content: Any
    raw_content: NotRequired[Any]
    display_summary: NotRequired[dict[str, Any]]
    data_status: NotRequired[SynergyStatusCode]
    status_text: NotRequired[str]


class StageIndicatorModel(TypedDict):
    stage: int
    label: str
    data_notice: NotRequired[DataNoticeModel]


class OverlayRenderModel(TypedDict):
    stats: list[StatPanelModel]
    synergies: list[SynergyPanelModel]
    stage_indicator: NotRequired[StageIndicatorModel]
    data_notice: NotRequired[DataNoticeModel]


class OverlayLayout(TypedDict):
    stat_boxes: list[tuple[int, int, int, int]]
    card_boxes: list[tuple[int, int, int, int]]
    synergy_rail: tuple[int, int, int, int]
    synergy_boxes: list[tuple[int, int, int, int]]
    geometry_scale: float
    stage_indicator_box: NotRequired[tuple[int, int, int, int]]
    safe_boxes: NotRequired[list[tuple[int, int, int, int]]]


def _clean_text(value: Any, *, limit: int = 120) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _format_percent(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    percent = numeric * 100.0 if abs(numeric) <= 1.0 else numeric
    if not math.isfinite(percent) or not 0.0 <= percent <= 100.0:
        return ""
    formatted = f"{percent:.1f}%"
    return "100%" if formatted == "100.0%" else formatted


def _clamp(low: int, value: float, high: int) -> int:
    return max(low, min(high, int(value)))


def _card_panel_ratios(viewport_size: tuple[int, int]) -> tuple[tuple[float, float, float, float], ...]:
    """显示锚点独立于识别 ROI；二者变化不能互相驱动。"""

    return display_card_panels(viewport_size)


def resolve_overlay_layout(
    viewport_size: tuple[int, int],
    *,
    layout_transform: LayoutTransform | None = None,
    synergy_count: int = 0,
    synergy_heights: Sequence[int] | None = None,
    synergy_slots: Sequence[int] | None = None,
    expanded: bool = False,
) -> OverlayLayout:
    width, height = (int(value) for value in viewport_size)
    if width <= 1 or height <= 1:
        raise ValueError("overlay viewport is not ready")
    margin = max(_clamp(8, width * 0.008, 20), int(round(height * .04)))
    card_panels = _card_panel_ratios((width, height))
    # 参数仅保留调用兼容：按钮/名称 ROI 的逐帧变换不再驱动显示几何。
    transform = LayoutTransform()
    card_boxes = [apply_transform(panel, (width, height), transform) for panel in card_panels]
    card_y0 = card_boxes[0][1]
    card_y1 = card_boxes[0][3]
    card_height = card_y1 - card_y0
    card_width = max(1, card_boxes[0][2] - card_boxes[0][0])
    geometry_scale = min(card_width / 477.0, card_height / 768.0)
    geometry_scale = max(0.1, min(2.5, geometry_scale))
    stat_boxes = list(display_stat_boxes((width, height)))
    synergy_gap = max(8, int(round(16 * geometry_scale)))
    synergy_bottom = max(margin + 1, card_y0 - synergy_gap)
    rail = (card_boxes[0][0], margin, card_boxes[-1][2], synergy_bottom)
    requested_heights = (
        [max(1, int(value)) for value in list(synergy_heights)[:3]]
        if synergy_heights is not None
        else []
    )
    count = len(requested_heights) if synergy_heights is not None else max(0, min(3, int(synergy_count)))
    raw_slots = list(synergy_slots)[:count] if synergy_slots is not None else list(range(count))
    slots: list[int] = []
    for raw_slot in raw_slots:
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError):
            continue
        if 0 <= slot < len(card_boxes) and slot not in slots:
            slots.append(slot)
    if len(slots) < count:
        slots.extend(slot for slot in range(len(card_boxes)) if slot not in slots and len(slots) < count)
    boxes: list[tuple[int, int, int, int]] = []
    if slots:
        available_height = max(1, synergy_bottom - margin)
        default_height = max(64, int(round(96 * geometry_scale)))
        if not requested_heights:
            requested_heights = [default_height] * len(slots)
        for slot in slots:
            card_x0, _, card_x1, _ = card_boxes[slot]
            panel_height = available_height if expanded else min(default_height, available_height)
            boxes.append((card_x0, synergy_bottom - panel_height, card_x1, synergy_bottom))
    return {
        "stat_boxes": stat_boxes,
        "safe_boxes": list(stat_boxes),
        "card_boxes": card_boxes,
        "synergy_rail": rail,
        "synergy_boxes": boxes,
        "geometry_scale": round(geometry_scale, 4),
    }


def _tier_color(tier: Any) -> str:
    value = _clean_text(tier, limit=24).casefold()
    aliases = {
        "prismatic": "prismatic",
        "棱彩": "prismatic",
        "silver": "silver",
        "白银": "silver",
        "gold": "gold",
        "黄金": "gold",
    }
    return OVERLAY_THEME[aliases.get(value, "gold")]


def _chamfered_points(box: tuple[int, int, int, int], inset: int = 0) -> list[int]:
    x0, y0, x1, y1 = box
    x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    corner = max(5, min(14, (y1 - y0) // 5))
    return [x0 + corner, y0, x1 - corner, y0, x1, y0 + corner, x1, y1 - corner,
            x1 - corner, y1, x0 + corner, y1, x0, y1 - corner, x0, y0 + corner]


def _draw_native_panel(canvas: CanvasLike, box: tuple[int, int, int, int], *, tier: str = "") -> None:
    theme = OVERLAY_THEME
    canvas.create_polygon(
        _chamfered_points(box),
        fill=theme["panel_bg"],
        outline=theme["outer_gold"],
        width=2,
    )

    x0, y0, x1, y1 = box
    corner = max(5, min(14, (y1 - y0) // 5))

    # 原生面板的细横线质感，压暗后只作为边框层次，不抢正文可读性。
    for scan_y in range(y0 + corner, y1 - corner, 4):
        canvas.create_line(x0 + 4, scan_y, x1 - 4, scan_y, fill=theme["inner_bluegray"], width=1)

    canvas.create_polygon(
        _chamfered_points(box, 3),
        fill="",
        outline=theme["middle_bronze"],
        width=1,
    )
    canvas.create_polygon(
        _chamfered_points(box, 6),
        fill=theme["inner_bluegray"],
        outline="",
        width=0,
    )

    highlight = theme.get("highlight_cyan", "#0AC8B9")

    # 顶部高光模拟玻璃反射，帮助统计条贴近 LoL 原生 HUD。
    for i in range(1, 4):
        ly = y0 + i * 2
        lx0 = x0 + corner + i * 3
        lx1 = x1 - corner - i * 3
        if lx1 > lx0:
            canvas.create_line(lx0, ly, lx1, ly, fill=highlight, width=1)

    canvas.create_line(x0 + corner, y0 + 1, x1 - corner, y0 + 1, fill=highlight, width=1)
    canvas.create_line(x0 + 1, y0 + corner, x0 + 1, y1 - corner, fill=highlight, width=1)

    # 悬浮 HUD 角标。
    canvas.create_line(x0 - 2, y0 + corner, x0 - 2, y0 - 2, x0 + corner, y0 - 2, fill=highlight, width=2)
    canvas.create_line(x1 + 2, y1 - corner, x1 + 2, y1 + 2, x1 - corner, y1 + 2, fill=highlight, width=2)

    tier_color = _tier_color(tier)
    canvas.create_line(x0 + 16, y0 + 6, x1 - 16, y0 + 6, fill=tier_color, width=2)
    canvas.create_rectangle(x0 + 13, y0 + 5, x0 + 15, y0 + 7, fill=tier_color, outline="")
    canvas.create_rectangle(x1 - 15, y0 + 5, x1 - 13, y0 + 7, fill=tier_color, outline="")


def _draw_shadowed_text(canvas: CanvasLike, x: int, y: int, **kwargs: Any) -> Any:
    """每段可见文字只创建一个 Canvas item，避免叠层字体产生重影。"""

    return canvas.create_text(x, y, **kwargs)


def _pixel_font(family: str, size: int, *styles: str) -> tuple[Any, ...]:
    """Tk 负数字号表示像素，避免 Windows DPI 把布局字号再次按点缩放。"""

    return (family, -max(1, int(size)), *styles)


def _draw_embedded_bar(canvas: CanvasLike, box: tuple[int, int, int, int], *, tier: str = "") -> None:
    """完全透明的统计底条占位，不再绘制任何阻挡原生游戏背景的矩形或线条。"""
    pass


def _draw_low_sample_outline(canvas: CanvasLike, box: tuple[int, int, int, int]) -> None:
    """综合回退时用细红内框保留低样本风险，不覆盖范围蓝字。"""

    canvas.create_polygon(
        _chamfered_points(box, 2),
        fill="",
        outline=OVERLAY_THEME["stat_low_sample"],
        width=2,
    )


def _draw_stat_panel(
    canvas: CanvasLike,
    box: tuple[int, int, int, int],
    row: StatPanelModel,
    *,
    typography: OverlayTypographyMetrics,
) -> dict[str, Any] | None:
    _draw_embedded_bar(canvas, box, tier=row["tier"])
    if bool(row.get("low_sample_outline")):
        _draw_low_sample_outline(canvas, box)
    x0, y0, x1, y1 = box
    font_family = "Microsoft YaHei UI"
    metrics = canvas_text_metrics(canvas)
    if row["status_code"] not in {"READY", "GENERATION_DEGRADED"}:
        status_colors = {
            "DETECTING": OVERLAY_THEME["highlight_cyan"],
            "RECOGNITION_MISSING": OVERLAY_THEME["stat_value"],
            "PRIVACY_OFF": OVERLAY_THEME["text_secondary"],
            "NO_STATS": OVERLAY_THEME["text_muted"],
            "SOURCE_STAT_MISSING": OVERLAY_THEME["text_muted"],
            "CHAMPION_STAT_MISSING": OVERLAY_THEME["text_muted"],
            "IDENTITY_UNRESOLVED": OVERLAY_THEME["text_muted"],
            "SNAPSHOT_UNAVAILABLE": OVERLAY_THEME["text_secondary"],
            "STATS_PREPARING": OVERLAY_THEME["text_secondary"],
            "STATS_STALE": OVERLAY_THEME["text_secondary"],
            "GENERATION_DEGRADED": OVERLAY_THEME["stat_value"],
            "CONTEXT_MISSING": OVERLAY_THEME["highlight_cyan"],
            "CONTEXT_EXPIRED": OVERLAY_THEME["highlight_cyan"],
        }
        size = typography["synergy_title_px"]
        def measure(text: str, px: int) -> float:
            return metrics.width(text, px, True)
        lines = _wrap_visual_text(row["status_text"], max_width=max(1, x1 - x0 - 12), font_size=size, measure=measure)
        line_height = metrics.line_height(size, True)
        capacity = max(1, (y1 - y0 - 4) // line_height)
        if len(lines) > capacity:
            lines = lines[:capacity]
            lines[-1] = _ellipsize_visual(lines[-1] + "…", max_width=max(1, x1 - x0 - 12), font_size=size, measure=measure)
        for index, line in enumerate(lines):
            _draw_shadowed_text(
                canvas, (x0 + x1) // 2,
                int(round((y0 + y1) / 2 + (index - (len(lines) - 1) / 2) * line_height)),
                text=line, fill=status_colors[row["status_code"]],
                font=_pixel_font(font_family, size, "bold"), anchor="center",
            )
        return

    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    # 统计必须作为一个整体围绕卡片中轴线居中；拆成固定左右列会因百分位长度不同
    # 产生 5px+ 的视觉偏移，真机帧上尤其明显。
    stats_text = row["stats_text"] or f"胜率 {row['winrate_text']} · 出场 {row['pickrate_text']}"
    tone_colors = {
        "default": OVERLAY_THEME["stat_value"],
        "low_sample": OVERLAY_THEME["stat_low_sample"],
        "aggregate": OVERLAY_THEME["stat_aggregate"],
    }
    item = _draw_shadowed_text(
        canvas,
        cx,
        cy,
        text=stats_text,
        fill=tone_colors.get(str(row.get("stats_tone") or "default"), OVERLAY_THEME["stat_value"]),
        font=_pixel_font(font_family, typography["stats_pixel_size"], "bold"),
        anchor="center",
        justify="center",
    )
    bbox = getattr(canvas, "bbox", None)
    actual: Any = bbox(item) if callable(bbox) and item is not None else None
    if actual is not None:
        if not (x0 <= actual[0] <= actual[2] <= x1 and y0 <= actual[1] <= actual[3] <= y1):
            raise ValueError("stats_text_exceeds_safe_area")
        return {"slot": row["slot"], "actual_bbox": list(actual)}
    return None


def _stage_indicator_box(
    viewport_size: tuple[int, int],
    label: str,
    warning: str = "",
    *,
    typography: OverlayTypographyMetrics,
) -> tuple[int, int, int, int]:
    width, height = viewport_size
    right_margin = _clamp(28, width * 0.012, 40)
    top = _clamp(64, height * 0.065, 108)
    font_size = typography["stage_px"]
    warning_size = typography["stage_warning_px"]
    padding_x = _clamp(12, width * 0.006, 18)
    badge_height = _clamp(40, font_size + 18, 54)
    if warning:
        badge_height = min(height - top, badge_height + max(24, int(round(font_size * 1.2))))
    badge_width = max(
        112,
        int(
            round(
                max(
                    _visual_text_width(label, font_size),
                    _visual_text_width(warning, warning_size),
                )
            )
        )
        + padding_x * 2,
    )
    x1 = max(1, width - right_margin)
    return max(0, x1 - badge_width), top, x1, min(height, top + badge_height)


def _draw_stage_indicator(
    canvas: CanvasLike,
    box: tuple[int, int, int, int],
    indicator: StageIndicatorModel,
    *,
    typography: OverlayTypographyMetrics,
) -> None:
    x0, y0, x1, y1 = box
    canvas.create_polygon(
        _chamfered_points(box),
        fill=OVERLAY_THEME["panel_bg"],
        outline=OVERLAY_THEME["outer_gold"],
        width=2,
    )
    font_size = typography["stage_px"]
    notice = indicator.get("data_notice")
    warning = str(notice.get("text") or "") if isinstance(notice, dict) else ""
    if warning:
        label_y = y0 + max(2, int(round((y1 - y0) * 0.30)))
        warning_y = y0 + max(4, int(round((y1 - y0) * 0.72)))
        _draw_shadowed_text(
            canvas,
            (x0 + x1) // 2,
            label_y,
            text=indicator["label"],
            fill=OVERLAY_THEME["text_primary"],
            font=_pixel_font("Microsoft YaHei UI", font_size, "bold"),
            anchor="center",
        )
        _draw_shadowed_text(
            canvas,
            (x0 + x1) // 2,
            warning_y,
            text=warning,
            fill=OVERLAY_THEME["text_secondary"],
            font=_pixel_font(
                "Microsoft YaHei UI",
                typography["stage_warning_px"],
                "bold",
            ),
            anchor="center",
        )
        return
    _draw_shadowed_text(
        canvas,
        (x0 + x1) // 2,
        (y0 + y1) // 2,
        text=indicator["label"],
        fill=OVERLAY_THEME["text_primary"],
        font=_pixel_font("Microsoft YaHei UI", font_size, "bold"),
        anchor="center",
    )


def _draw_synergy_panel(
    canvas: CanvasLike,
    box: tuple[int, int, int, int],
    row: SynergyPanelModel,
    *,
    typography: OverlayTypographyMetrics,
    minimum_height: int,
) -> dict[str, Any]:
    """兼容 façade：联动正文实现位于独立、无反向导入的绘制模块。"""

    return _draw_expanded_synergy_panel_impl(
        canvas,
        box,
        row,
        typography=typography,
        minimum_height=minimum_height,
        theme=OVERLAY_THEME,
        draw_panel=_draw_native_panel,
        draw_text=_draw_shadowed_text,
        tier_color=_tier_color,
        pixel_font=_pixel_font,
    )



def _draw_compact_synergy_panel(
    canvas: CanvasLike,
    box: tuple[int, int, int, int],
    row: SynergyPanelModel,
    *,
    typography: OverlayTypographyMetrics,
) -> dict[str, Any]:
    """兼容 façade：compact 联动绘制委托给独立实现。"""

    return _draw_compact_synergy_panel_impl(
        canvas,
        box,
        row,
        typography=typography,
        theme=OVERLAY_THEME,
        draw_panel=_draw_native_panel,
        draw_text=_draw_shadowed_text,
        pixel_font=_pixel_font,
    )



def draw_overlay_frame(
    canvas: CanvasLike,
    model: OverlayRenderModel,
    *,
    viewport_size: tuple[int, int] | None = None,
    dpi_scale: float = 1.0,
    layout_transform: LayoutTransform | None = None,
    perf_sink: dict[str, Any] | None = None,
    expanded: bool = True,
    show_synergy: bool = True,
    exclusion_zones: Sequence[tuple[int, int, int, int]] = (),
) -> OverlayLayout:
    """绘制安全区外的统计与按槽位对齐联动。"""

    started_at = time.perf_counter()
    if viewport_size is None:
        viewport_size = (max(1, int(canvas.winfo_width())), max(1, int(canvas.winfo_height())))
    viewport_width, viewport_height = viewport_size
    base_layout = resolve_overlay_layout(viewport_size, layout_transform=layout_transform)
    card_boxes = base_layout["card_boxes"]
    card_width = max(1, card_boxes[0][2] - card_boxes[0][0])
    card_height = max(1, card_boxes[0][3] - card_boxes[0][1])
    typography = resolve_overlay_typography(
        dpi_scale,
        card_size=(card_width, card_height),
    )
    metrics = canvas_text_metrics(canvas)
    size = max(8, int(30 * viewport_width / 2560 + .5))
    metrics.bind_context((viewport_size, dpi_scale, expanded, repr(layout_transform),
                          repr(display_geometry_metadata(viewport_size))))
    measurement_start = metrics.measurement_seconds
    hits_start, misses_start = metrics.hits, metrics.misses
    layout_ready_at = time.perf_counter()
    typography["stats_pixel_size"] = size
    pad = 5 if (viewport_width, viewport_height) == (2560, 1440) else max(2, int(6 * viewport_width / 2560 + .5))
    prepared_stats: list[StatPanelModel] = []
    stats_diagnostics: list[dict[str, Any]] = []
    for row, box in zip(model["stats"], base_layout["stat_boxes"]):
        prepared = row.copy()
        if row["status_code"] in {"READY", "GENERATION_DEGRADED"}:
            text = row["stats_text"] or f"胜率 {row['winrate_text']} · 出场 {row['pickrate_text']}"
            fitted, spacing = fit_stats_block(metrics, text, size, box[2]-box[0]-2*pad, box[3]-box[1]-2*pad)
            prepared["stats_text"] = fitted
            stats_diagnostics.append({"slot": row["slot"], "spacing": spacing, "font_px": size,
                                      "advance_px": max(metrics.width(line, size, True) for line in fitted.splitlines()),
                                      "line_count": len(fitted.splitlines()), "safe_box": list(box),
                                      "padding_px": pad})
        prepared_stats.append(prepared)
    # 上限随视口放大：120px 旧上限在 1600p 下容不下放大后的三段文字。
    minimum_panel_height = typography["expanded_panel_min_px"]
    stats_ready_at = time.perf_counter()
    proposed_synergy_rows = list(model["synergies"])
    synergy_rows = list(proposed_synergy_rows) if show_synergy else []
    suppression_reason = "" if show_synergy else "synergy_display_disabled"
    margin = max(_clamp(8, viewport_width * 0.008, 20), int(round(viewport_height * .04)))
    synergy_gap = max(8, int(round(16 * typography["geometry_scale"])))
    available_synergy_height = max(0, card_boxes[0][1] - synergy_gap - margin)
    required_synergy_height = (
        minimum_panel_height
        if expanded
        else typography["compact_panel_px"]
    )
    if available_synergy_height < required_synergy_height:
        # 不在薄框里切断条件句；报告必须保留每个未画槽及明确原因。
        suppression_reason = "insufficient_vertical_space"
        synergy_rows = []
    synergy_heights = [available_synergy_height if expanded else typography["compact_panel_px"]] * len(synergy_rows)
    layout = resolve_overlay_layout(
        viewport_size,
        layout_transform=layout_transform,
        synergy_count=len(synergy_rows),
        synergy_heights=synergy_heights,
        synergy_slots=[row["slot"] for row in synergy_rows],
        expanded=expanded,
    )
    canvas_started_at = time.perf_counter()
    canvas.delete("all")
    def safe(box: tuple[int, int, int, int]) -> bool:
        x0, y0, x1, y1 = box
        return not any(x0 < rx1 and x1 > rx0 and y0 < ry1 and y1 > ry0 for rx0, ry0, rx1, ry1 in exclusion_zones)

    indicator = model.get("stage_indicator")
    if not isinstance(indicator, dict):
        notice = model.get("data_notice")
        if isinstance(notice, dict) and _clean_text(notice.get("text")):
            indicator = {
                "stage": 0,
                "label": "数据状态",
                "data_notice": notice,
            }
    if isinstance(indicator, dict) and _clean_text(indicator.get("label")):
        notice_payload = indicator.get("data_notice")
        warning = _clean_text(notice_payload.get("text"), limit=40) if isinstance(notice_payload, dict) else ""
        indicator_box = _stage_indicator_box(
            viewport_size,
            _clean_text(indicator.get("label"), limit=16),
            warning,
            typography=typography,
        )
        layout["stage_indicator_box"] = indicator_box
        if safe(indicator_box):
            _draw_stage_indicator(canvas, indicator_box, indicator, typography=typography)
    for box, row in zip(layout["stat_boxes"], prepared_stats):
        if safe(box):
            audit = _draw_stat_panel(canvas, box, row, typography=typography)
            if audit is not None:
                for entry in stats_diagnostics:
                    if entry["slot"] == audit["slot"]:
                        entry.update(audit)
    synergy_audits: dict[int, dict[str, Any]] = {
        int(row["slot"]): {
            "slot": int(row["slot"]),
            "proposed": True,
            "panel_drawn": False,
            "content_drawn": False,
            "content_verified": False,
            "fallback_drawn": False,
            "fallback_verified": False,
            "reason": suppression_reason,
            "line_count": 0,
            "original_line_count": 0,
            "original_char_count": len(str(row.get("raw_content", row.get("content")) or "")),
            "truncated": False,
            "source_omitted": bool(suppression_reason),
            "summary_status": str(
                (row.get("display_summary") or {}).get("status") or "legacy_unprepared"
                if isinstance(row.get("display_summary"), dict)
                else "legacy_unprepared"
            ),
            "source_sha256": str(
                (row.get("display_summary") or {}).get("source_sha256") or ""
                if isinstance(row.get("display_summary"), dict)
                else ""
            ),
            "rule_version": str(
                (row.get("display_summary") or {}).get("rule_version") or ""
                if isinstance(row.get("display_summary"), dict)
                else ""
            ),
            "display_spec": dict((row.get("display_summary") or {}).get("display_spec") or {})
            if isinstance(row.get("display_summary"), dict)
            and isinstance((row.get("display_summary") or {}).get("display_spec"), dict)
            else {},
            "bbox_available": False,
            "bbox_verified": False,
            "actual_bboxes": [],
        }
        for row in proposed_synergy_rows
    }
    for box, row in zip(layout["synergy_boxes"], synergy_rows):
        if safe(box):
            if expanded:
                audit = _draw_synergy_panel(
                    canvas,
                    box,
                    row,
                    typography=typography,
                    minimum_height=minimum_panel_height,
                )
            else:
                audit = _draw_compact_synergy_panel(canvas, box, row, typography=typography)
            synergy_audits[int(row["slot"])] = {
                **audit,
                "proposed": True,
                "panel_drawn": True,
            }
        else:
            synergy_audits[int(row["slot"])]["reason"] = "exclusion_zone_overlap"
            synergy_audits[int(row["slot"])]["source_omitted"] = True
    for audit in synergy_audits.values():
        if not audit["panel_drawn"] and not audit["reason"]:
            audit["reason"] = "layout_box_missing"
            audit["source_omitted"] = True
    panel_drawn_synergy_slots = [
        int(audit["slot"]) for audit in synergy_audits.values() if audit["panel_drawn"]
    ]
    drawn_synergy_slots = [
        int(audit["slot"]) for audit in synergy_audits.values() if audit["content_verified"]
    ]
    fallback_drawn_synergy_slots = [
        int(audit["slot"]) for audit in synergy_audits.values() if audit["fallback_drawn"]
    ]
    missing_synergy_slots = [
        {"slot": audit["slot"], "reason": audit["reason"] or "content_not_drawn"}
        for audit in synergy_audits.values()
        if not audit["panel_drawn"] or not audit["content_verified"]
    ]
    if isinstance(perf_sink, dict):
        perf_sink["last_draw_ms"] = (time.perf_counter() - started_at) * 1000.0
        perf_sink["draw_phases_ms"] = {
            "layout": (layout_ready_at - started_at) * 1000,
            "stats_layout": (stats_ready_at - layout_ready_at) * 1000,
            "synergy_layout": (canvas_started_at - stats_ready_at) * 1000,
            "canvas_update": (time.perf_counter() - canvas_started_at) * 1000,
            "font_measurement_subset": (metrics.measurement_seconds - measurement_start) * 1000,
            "font_cache_hits": metrics.hits - hits_start,
            "font_cache_misses": metrics.misses - misses_start,
        }
        perf_sink["typography"] = dict(typography)
        perf_sink["drawn_synergy_slots"] = drawn_synergy_slots
        perf_sink["panel_drawn_synergy_slots"] = panel_drawn_synergy_slots
        perf_sink["fallback_drawn_synergy_slots"] = fallback_drawn_synergy_slots
        perf_sink["proposed_synergy_slots"] = [int(row["slot"]) for row in proposed_synergy_rows]
        perf_sink["missing_synergy_slots"] = missing_synergy_slots
        perf_sink["synergy_render"] = list(synergy_audits.values())
        perf_sink["display_layout"] = {
            "contract": "fixed_card_layout_v2", "viewport": list(viewport_size),
            **display_geometry_metadata(viewport_size),
            "stats_text": stats_diagnostics,
            "safe_boxes": [list(box) for box in layout["stat_boxes"]],
            "card_boxes": [list(box) for box in layout["card_boxes"]],
            "stat_boxes": [list(box) for box in layout["stat_boxes"]],
            "synergy_boxes": [list(box) for box in layout["synergy_boxes"]],
            "synergy_slots": [row["slot"] for row in synergy_rows],
            "synergy_proposed_slots": [row["slot"] for row in proposed_synergy_rows],
            "synergy_panel_drawn_slots": panel_drawn_synergy_slots,
            "synergy_content_drawn_slots": drawn_synergy_slots,
            "synergy_fallback_drawn_slots": fallback_drawn_synergy_slots,
            "synergy_missing_slots": missing_synergy_slots,
            "synergy_render": list(synergy_audits.values()),
        }
    return layout

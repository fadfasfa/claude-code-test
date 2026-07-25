"""Overlay 布局模型与 Canvas 绘制实现。"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Sequence
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from hextech.modules.vision.layout import pick_card_panels

INNER_BAR_HEIGHT_RATIO = 0.052
INNER_BAR_TOP_MARGIN_RATIO = 0.825
INNER_BAR_SIDE_INSET_RATIO = 0.055
SYNERGY_CARD_GAP_RATIO = 0.010
COMPACT_SYNERGY_HEIGHT_RATIO = 0.066
CARD_TEXT_POINT_SIZE = 16

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


class SynergyPanelModel(TypedDict):
    slot: int
    augment_name: str
    tier: str
    hero_name: str
    rating: str
    tag: str
    content: str
    data_status: NotRequired[SynergyStatusCode]
    status_text: NotRequired[str]


class OverlayRenderModel(TypedDict):
    stats: list[StatPanelModel]
    synergies: list[SynergyPanelModel]


class OverlayLayout(TypedDict):
    stat_boxes: list[tuple[int, int, int, int]]
    card_boxes: list[tuple[int, int, int, int]]
    synergy_rail: tuple[int, int, int, int]
    synergy_boxes: list[tuple[int, int, int, int]]


class SynergyTextLayout(TypedDict):
    header: str
    meta: str
    body_lines: list[str]
    title_size: int
    body_size: int
    title_offset: int
    meta_offset: int
    body_offset: int
    line_height: int
    desired_height: int
    truncated: bool


def _clean_text(value: Any, *, limit: int = 120) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _format_percent(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    percent = numeric * 100.0 if abs(numeric) <= 1.0 else numeric
    return f"{percent:.1f}%"


def _clamp(low: int, value: float, high: int) -> int:
    return max(low, min(high, int(value)))


def _card_panel_ratios(viewport_size: tuple[int, int]) -> tuple[tuple[float, float, float, float], ...]:
    """选择与 vision 识别一致的三卡比例，避免统计层和真实卡片中轴线分离。"""

    return pick_card_panels(viewport_size)


def resolve_overlay_layout(
    viewport_size: tuple[int, int],
    *,
    synergy_count: int = 0,
    synergy_heights: Sequence[int] | None = None,
    synergy_slots: Sequence[int] | None = None,
) -> OverlayLayout:
    width, height = (max(1, int(value)) for value in viewport_size)
    margin = _clamp(8, width * 0.008, 20)
    card_panels = _card_panel_ratios((width, height))
    card_y0 = int(height * card_panels[0][1])
    card_y1 = int(height * card_panels[0][3])
    card_height = card_y1 - card_y0
    stat_height = _clamp(46, height * INNER_BAR_HEIGHT_RATIO, 72)
    stat_top_margin = int(card_height * INNER_BAR_TOP_MARGIN_RATIO)
    stat_y0 = card_y0 + stat_top_margin
    stat_y1 = stat_y0 + stat_height
    card_boxes: list[tuple[int, int, int, int]] = []
    stat_boxes: list[tuple[int, int, int, int]] = []
    for left, top, right, bottom in card_panels:
        x0, x1 = int(width * left), int(width * right)
        panel_y0, panel_y1 = int(height * top), int(height * bottom)
        card_boxes.append((x0, panel_y0, x1, panel_y1))
        stat_side_inset = _clamp(10, (x1 - x0) * INNER_BAR_SIDE_INSET_RATIO, 20)
        stat_boxes.append((x0 + stat_side_inset, stat_y0, x1 - stat_side_inset, stat_y1))
    synergy_gap = _clamp(8, height * SYNERGY_CARD_GAP_RATIO, 16)
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
        default_height = _clamp(64, height * COMPACT_SYNERGY_HEIGHT_RATIO, 96)
        if not requested_heights:
            requested_heights = [default_height] * len(slots)
        for slot, requested_height in zip(slots, requested_heights):
            card_x0, _, card_x1, _ = card_boxes[slot]
            panel_height = min(max(1, int(requested_height)), available_height)
            boxes.append((card_x0, synergy_bottom - panel_height, card_x1, synergy_bottom))
    return {
        "stat_boxes": stat_boxes,
        "card_boxes": card_boxes,
        "synergy_rail": rail,
        "synergy_boxes": boxes,
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


def _visual_text_width(value: str, font_size: int) -> float:
    """按字形类别估算像素宽度；用于纯 renderer 中的稳定换行，不依赖 Tk 状态。"""

    total = 0.0
    for char in str(value or ""):
        if unicodedata.combining(char):
            continue
        if char.isspace():
            factor = 0.35
        elif unicodedata.east_asian_width(char) in {"W", "F", "A"}:
            factor = 1.0
        elif char in "ilI.,:;|!'`":
            factor = 0.36
        else:
            factor = 0.62
        total += max(1, int(font_size)) * factor
    return total


def _ellipsize_visual(value: str, *, max_width: int, font_size: int) -> str:
    text = str(value or "").rstrip()
    if _visual_text_width(text, font_size) <= max_width:
        return text
    suffix = "…"
    while text and _visual_text_width(text + suffix, font_size) > max_width:
        text = text[:-1].rstrip()
    return (text + suffix) if text else suffix


def _wrap_visual_text(value: str, *, max_width: int, font_size: int) -> list[str]:
    text = _clean_text(value, limit=180)
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if char.isspace() and not current:
            continue
        candidate = current + char
        if current and _visual_text_width(candidate, font_size) > max_width:
            lines.append(current.rstrip())
            current = "" if char.isspace() else char
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    return [line for line in lines if line]


def _resolve_synergy_text_layout(
    row: SynergyPanelModel,
    width: int,
    *,
    minimum_height: int,
    panel_height: int | None = None,
) -> SynergyTextLayout:
    pad = _clamp(12, width * 0.05, 20)
    text_width = max(40, width - pad * 2)
    title_size = _clamp(10, width * 0.035, 15)
    body_size = _clamp(8, width * 0.028, 12)
    title_line_height = max(title_size + 2, int(round(title_size * 1.22)))
    line_height = max(body_size + 3, int(round(body_size * 1.35)))
    title_offset = _clamp(14, width * 0.045, 22)
    meta_offset = title_offset + title_line_height + 4
    body_offset = meta_offset + line_height + 6
    bottom_pad = _clamp(10, width * 0.025, 16)

    header_parts = [row["augment_name"]]
    if row["rating"]:
        header_parts.append(row["rating"])
    header = _ellipsize_visual(" · ".join(header_parts), max_width=text_width, font_size=title_size)
    meta = _ellipsize_visual(
        " · ".join(part for part in (row["hero_name"], row["tag"], row.get("status_text", "")) if part),
        max_width=text_width,
        font_size=body_size,
    )
    body_lines = _wrap_visual_text(row["content"], max_width=text_width, font_size=body_size)
    desired_height = max(
        max(1, int(minimum_height)),
        body_offset + len(body_lines) * line_height + bottom_pad,
    )
    truncated = False
    if panel_height is not None and body_lines:
        available_body_height = max(0, int(panel_height) - body_offset - bottom_pad)
        max_lines = max(1, available_body_height // line_height)
        if len(body_lines) > max_lines:
            body_lines = body_lines[:max_lines]
            body_lines[-1] = _ellipsize_visual(
                body_lines[-1] + "…",
                max_width=text_width,
                font_size=body_size,
            )
            truncated = True
    return {
        "header": header,
        "meta": meta,
        "body_lines": body_lines,
        "title_size": title_size,
        "body_size": body_size,
        "title_offset": title_offset,
        "meta_offset": meta_offset,
        "body_offset": body_offset,
        "line_height": line_height,
        "desired_height": desired_height,
        "truncated": truncated,
    }


def _draw_shadowed_text(canvas: CanvasLike, x: int, y: int, **kwargs: Any) -> None:
    shadow_kwargs = dict(kwargs)
    shadow_kwargs["fill"] = OVERLAY_THEME.get("text_shadow", "#000000")
    canvas.create_text(x + 1, y + 1, **shadow_kwargs)
    canvas.create_text(x, y, **kwargs)


def _pixel_font(family: str, size: int, *styles: str) -> tuple[Any, ...]:
    """Tk 负数字号表示像素，避免 Windows DPI 把布局字号再次按点缩放。"""

    return (family, -max(1, int(size)), *styles)


def _draw_embedded_bar(canvas: CanvasLike, box: tuple[int, int, int, int], *, tier: str = "") -> None:
    """完全透明的统计底条占位，不再绘制任何阻挡原生游戏背景的矩形或线条。"""
    pass


def _draw_stat_panel(canvas: CanvasLike, box: tuple[int, int, int, int], row: StatPanelModel) -> None:
    _draw_embedded_bar(canvas, box, tier=row["tier"])
    x0, y0, x1, y1 = box
    font_family = "Microsoft YaHei UI"
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
            "GENERATION_DEGRADED": OVERLAY_THEME["stat_value"],
            "CONTEXT_MISSING": OVERLAY_THEME["highlight_cyan"],
            "CONTEXT_EXPIRED": OVERLAY_THEME["highlight_cyan"],
        }
        _draw_shadowed_text(
            canvas,
            (x0 + x1) // 2,
            (y0 + y1) // 2,
            text=row["status_text"],
            fill=status_colors[row["status_code"]],
            # 卡内文字沿用用户确认的点字号；联动说明继续使用像素字号，
            # 避免把此前为联动框设置的 DPI 限制错误应用到核心统计。
            font=(font_family, CARD_TEXT_POINT_SIZE, "bold"),
            anchor="center",
        )
        return

    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    # 统计必须作为一个整体围绕卡片中轴线居中；拆成固定左右列会因百分位长度不同
    # 产生 5px+ 的视觉偏移，真机帧上尤其明显。
    stats_text = row["stats_text"] or f"胜率 {row['winrate_text']} · 出场 {row['pickrate_text']}"
    _draw_shadowed_text(
        canvas,
        cx,
        cy,
        text=stats_text,
        fill=OVERLAY_THEME["stat_value"],
        font=(font_family, CARD_TEXT_POINT_SIZE, "bold"),
        anchor="center",
    )


def _draw_synergy_panel(
    canvas: CanvasLike,
    box: tuple[int, int, int, int],
    row: SynergyPanelModel,
    *,
    minimum_height: int,
) -> None:
    _draw_native_panel(canvas, box, tier=row["tier"])
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    pad = _clamp(12, width * 0.05, 20)
    text_layout = _resolve_synergy_text_layout(
        row,
        width,
        minimum_height=minimum_height,
        panel_height=height,
    )
    font_family = "Segoe UI"
    _draw_shadowed_text(
        canvas,
        x0 + pad,
        y0 + text_layout["title_offset"],
        text=text_layout["header"],
        fill=OVERLAY_THEME["text_primary"],
        font=_pixel_font(font_family, text_layout["title_size"], "bold"),
        anchor="nw",
    )
    _draw_shadowed_text(
        canvas,
        x0 + pad,
        y0 + text_layout["meta_offset"],
        text=text_layout["meta"],
        fill=_tier_color(row["tier"]),
        font=_pixel_font(font_family, text_layout["body_size"], "bold"),
        anchor="nw",
    )
    if text_layout["body_lines"]:
        _draw_shadowed_text(
            canvas,
            x0 + pad,
            y0 + text_layout["body_offset"],
            text="\n".join(text_layout["body_lines"]),
            fill=OVERLAY_THEME["text_secondary"],
            font=_pixel_font(font_family, text_layout["body_size"]),
            anchor="nw",
        )


def _draw_compact_synergy_panel(
    canvas: CanvasLike,
    box: tuple[int, int, int, int],
    row: SynergyPanelModel,
) -> None:
    """按卡槽绘制一行联动摘要，避免 compact 模式重新占用右侧通知区。"""

    _draw_native_panel(canvas, box, tier=row["tier"])
    x0, y0, x1, y1 = box
    width = x1 - x0
    pad = _clamp(12, width * 0.05, 20)
    font_size = _clamp(10, width * 0.032, 14)
    summary = " · ".join(
        part
        for part in (
            row["hero_name"],
            row["rating"],
            row["tag"],
            row.get("status_text", ""),
            row["content"],
        )
        if _clean_text(part)
    )
    summary = _ellipsize_visual(summary, max_width=max(40, width - pad * 2), font_size=font_size)
    _draw_shadowed_text(
        canvas,
        x0 + pad,
        (y0 + y1) // 2,
        text=summary,
        fill=OVERLAY_THEME["text_primary"],
        font=_pixel_font("Microsoft YaHei UI", font_size, "bold"),
        anchor="w",
    )


def draw_overlay_frame(
    canvas: CanvasLike,
    model: OverlayRenderModel,
    *,
    viewport_size: tuple[int, int] | None = None,
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
    card_boxes = resolve_overlay_layout(viewport_size)["card_boxes"]
    card_width = max(1, card_boxes[0][2] - card_boxes[0][0])
    minimum_panel_height = _clamp(72, viewport_height * 0.085, 120)
    synergy_rows = list(model["synergies"]) if show_synergy else []
    margin = _clamp(8, viewport_width * 0.008, 20)
    synergy_gap = _clamp(8, viewport_height * SYNERGY_CARD_GAP_RATIO, 16)
    available_synergy_height = max(0, card_boxes[0][1] - synergy_gap - margin)
    required_synergy_height = (
        minimum_panel_height
        if expanded
        else _clamp(64, viewport_height * COMPACT_SYNERGY_HEIGHT_RATIO, 96)
    )
    if available_synergy_height < required_synergy_height:
        # 低分辨率或卡片过高时宁可隐藏联动，也不绘制无法阅读的薄框。
        synergy_rows = []
    synergy_heights = [
        (
            _resolve_synergy_text_layout(
                row,
                card_width,
                minimum_height=minimum_panel_height,
            )["desired_height"]
            if expanded
            else _clamp(64, viewport_height * COMPACT_SYNERGY_HEIGHT_RATIO, 96)
        )
        for row in synergy_rows
    ]
    layout = resolve_overlay_layout(
        viewport_size,
        synergy_count=len(synergy_rows),
        synergy_heights=synergy_heights,
        synergy_slots=[row["slot"] for row in synergy_rows],
    )
    canvas.delete("all")
    def safe(box: tuple[int, int, int, int]) -> bool:
        x0, y0, x1, y1 = box
        return not any(x0 < rx1 and x1 > rx0 and y0 < ry1 and y1 > ry0 for rx0, ry0, rx1, ry1 in exclusion_zones)

    for box, row in zip(layout["stat_boxes"], model["stats"]):
        if safe(box):
            _draw_stat_panel(canvas, box, row)
    for box, row in zip(layout["synergy_boxes"], synergy_rows):
        if safe(box):
            if expanded:
                _draw_synergy_panel(canvas, box, row, minimum_height=minimum_panel_height)
            else:
                _draw_compact_synergy_panel(canvas, box, row)
    if isinstance(perf_sink, dict):
        perf_sink["last_draw_ms"] = (time.perf_counter() - started_at) * 1000.0
    return layout

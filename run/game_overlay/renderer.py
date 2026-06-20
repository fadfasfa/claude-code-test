"""LoL 原生风格的游戏内统计窗和英雄联动列。

本模块是纯展示层：不读文件、不启动进程、不依赖 ``display`` 或 Web。输入是已经
加载到内存的 event/hint/context，输出只通过 Canvas-like 接口绘制。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Protocol, TypedDict

CARD_X_RANGES = ((0.195, 0.385), (0.405, 0.595), (0.620, 0.810))
CARD_Y_RANGE = (0.17, 0.68)
SYNERGY_X_RANGE = (0.825, 0.992)

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

    def create_text(self, *args: Any, **kwargs: Any) -> Any: ...


class StatPanelModel(TypedDict):
    slot: int
    state: str
    name: str
    tier: str
    stats_text: str


class SynergyPanelModel(TypedDict):
    slot: int
    augment_name: str
    tier: str
    hero_name: str
    rating: str
    tag: str
    content: str


class OverlayRenderModel(TypedDict):
    stats: list[StatPanelModel]
    synergies: list[SynergyPanelModel]


class OverlayLayout(TypedDict):
    stat_boxes: list[tuple[int, int, int, int]]
    card_boxes: list[tuple[int, int, int, int]]
    synergy_rail: tuple[int, int, int, int]
    synergy_boxes: list[tuple[int, int, int, int]]


def _clean_text(value: Any, *, limit: int = 120) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _format_percent(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    percent = numeric * 100.0 if abs(numeric) <= 1.0 else numeric
    return f"{percent:.1f}%"


def _query_hint(slot: Mapping[str, Any], hint_cache: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(hint_cache, Mapping):
        return {}
    hints = hint_cache.get("hints")
    name_index = hint_cache.get("name_index")
    if not isinstance(hints, Mapping):
        return {}
    augment_id = _clean_text(slot.get("augment_id"))
    slot_name = _clean_text(slot.get("name"))
    hint = hints.get(augment_id) if augment_id else None
    if not isinstance(hint, Mapping) and slot_name and isinstance(name_index, Mapping):
        indexed_id = _clean_text(name_index.get(slot_name))
        hint = hints.get(indexed_id) if indexed_id else None
    if not isinstance(hint, Mapping) and slot_name:
        hint = next(
            (
                candidate
                for candidate in hints.values()
                if isinstance(candidate, Mapping) and _clean_text(candidate.get("name")) == slot_name
            ),
            None,
        )
    return dict(hint) if isinstance(hint, Mapping) else {}


def _format_stats_entry(stats: Mapping[str, Any]) -> str:
    parts: list[str] = []
    winrate = _format_percent(stats.get("winrate"))
    pickrate = _format_percent(stats.get("pickrate"))
    if winrate:
        parts.append(f"胜率 {winrate}")
    if pickrate:
        parts.append(f"出场 {pickrate}")
    return " · ".join(parts)


def _current_champion_stats(hint: Mapping[str, Any], context: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not (isinstance(context, Mapping) and context.get("ok")):
        return None
    champion_id = _clean_text(context.get("champion_id"))
    champion_name = _clean_text(context.get("champion_name"))
    by_id = hint.get("stats_by_champion_id")
    if champion_id and isinstance(by_id, Mapping):
        stats = by_id.get(champion_id)
        if isinstance(stats, Mapping):
            return stats
    by_name = hint.get("stats_by_champion_name")
    if champion_name and isinstance(by_name, Mapping):
        stats = by_name.get(champion_name)
        if isinstance(stats, Mapping):
            return stats
    return None


def _stats_text(
    hint: Mapping[str, Any],
    hint_cache: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> tuple[str, bool]:
    source = hint_cache.get("source") if isinstance(hint_cache, Mapping) else None
    if not (isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True):
        return "已开启隐私模式", False
    stats = _current_champion_stats(hint, context)
    if not isinstance(stats, Mapping):
        return "暂无该英雄统计", False
    text = _format_stats_entry(stats)
    return (text or "暂无该英雄统计"), bool(text)


def _matched_synergy(hint: Mapping[str, Any], context: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not (isinstance(context, Mapping) and context.get("ok")):
        return None
    champion_id = _clean_text(context.get("champion_id"))
    champion_name = _clean_text(context.get("champion_name"))
    if not champion_id and not champion_name:
        return None
    synergies = hint.get("synergies")
    if not isinstance(synergies, list):
        return None
    for item in synergies:
        if not isinstance(item, Mapping):
            continue
        hero_id = _clean_text(item.get("hero_id"))
        hero_name = _clean_text(item.get("hero_name"))
        if (champion_id and hero_id == champion_id) or (champion_name and hero_name == champion_name):
            return item
    return None


def build_render_model(
    snapshot: Mapping[str, Any],
    *,
    hint_cache: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> OverlayRenderModel:
    """把共享数据收口为稳定三统计窗和仅命中联动的展示模型。"""

    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    stats: list[StatPanelModel] = []
    synergies: list[SynergyPanelModel] = []
    for index in range(3):
        slot = slots[index] if index < len(slots) and isinstance(slots[index], Mapping) else {}
        state = _clean_text(slot.get("state"), limit=32)
        slot_name = _clean_text(slot.get("name"), limit=60)
        ready = state == "ready" and bool(slot_name or _clean_text(slot.get("augment_id")))
        hint = _query_hint(slot, hint_cache) if ready else {}
        name = _clean_text(hint.get("name") or slot_name, limit=60)
        tier = _clean_text(hint.get("tier") or slot.get("tier"), limit=24)
        stats_text, has_current_stats = _stats_text(hint, hint_cache, context) if ready else ("识别中…", False)
        stats.append(
            {
                "slot": index,
                "state": ("matched" if has_current_stats else "missing_stats") if ready else "detecting",
                "name": name,
                "tier": tier,
                "stats_text": stats_text,
            }
        )
        if not ready:
            continue
        matched = _matched_synergy(hint, context)
        if not isinstance(matched, Mapping):
            continue
        synergies.append(
            {
                "slot": index,
                "augment_name": name,
                "tier": tier,
                "hero_name": _clean_text(matched.get("hero_name"), limit=40),
                "rating": _clean_text(matched.get("rating"), limit=12),
                "tag": _clean_text(matched.get("tag"), limit=24),
                "content": _clean_text(matched.get("content"), limit=180),
            }
        )
    return {"stats": stats, "synergies": synergies}


def _clamp(low: int, value: float, high: int) -> int:
    return max(low, min(high, int(value)))


def resolve_overlay_layout(viewport_size: tuple[int, int], *, synergy_count: int = 0) -> OverlayLayout:
    width, height = (max(1, int(value)) for value in viewport_size)
    margin = _clamp(8, width * 0.008, 20)
    card_y0 = int(height * CARD_Y_RANGE[0])
    card_y1 = int(height * CARD_Y_RANGE[1])
    stat_height = _clamp(48, height * 0.052, 76)
    stat_gap = _clamp(8, height * 0.008, 14)
    stat_y1 = max(stat_height + margin, card_y0 - stat_gap)
    stat_y0 = stat_y1 - stat_height
    card_boxes: list[tuple[int, int, int, int]] = []
    stat_boxes: list[tuple[int, int, int, int]] = []
    for left, right in CARD_X_RANGES:
        x0, x1 = int(width * left), int(width * right)
        card_boxes.append((x0, card_y0, x1, card_y1))
        stat_boxes.append((x0, stat_y0, x1, stat_y1))
    rail = (int(width * SYNERGY_X_RANGE[0]), card_y0, width - margin, card_y1)
    count = max(0, min(3, int(synergy_count)))
    boxes: list[tuple[int, int, int, int]] = []
    if count:
        rail_height = rail[3] - rail[1]
        gap = _clamp(8, height * 0.010, 16)
        desired = _clamp(96, height * 0.11, 176)
        panel_height = min(desired, max(1, (rail_height - gap * (count - 1)) // count))
        group_height = panel_height * count + gap * (count - 1)
        y0 = rail[1] + max(0, (rail_height - group_height) // 2)
        for index in range(count):
            top = y0 + index * (panel_height + gap)
            boxes.append((rail[0], top, rail[2], top + panel_height))
    return {
        "stat_boxes": stat_boxes,
        "card_boxes": card_boxes,
        "synergy_rail": rail,
        "synergy_boxes": boxes,
    }


def _tier_color(tier: Any) -> str:
    value = _clean_text(tier, limit=24).lower()
    if value == "prismatic":
        return OVERLAY_THEME["prismatic"]
    if value == "silver":
        return OVERLAY_THEME["silver"]
    return OVERLAY_THEME["gold"]


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
    
    # Holographic Scanlines
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
    
    # Simulated Top Gradient / Glass Reflection
    for i in range(1, 4):
        ly = y0 + i * 2
        lx0 = x0 + corner + i * 3
        lx1 = x1 - corner - i * 3
        if lx1 > lx0:
            canvas.create_line(lx0, ly, lx1, ly, fill=highlight, width=1)
            
    canvas.create_line(x0 + corner, y0 + 1, x1 - corner, y0 + 1, fill=highlight, width=1)
    canvas.create_line(x0 + 1, y0 + corner, x0 + 1, y1 - corner, fill=highlight, width=1)

    # Floating HUD Brackets
    canvas.create_line(x0 - 2, y0 + corner, x0 - 2, y0 - 2, x0 + corner, y0 - 2, fill=highlight, width=2)
    canvas.create_line(x1 + 2, y1 - corner, x1 + 2, y1 + 2, x1 - corner, y1 + 2, fill=highlight, width=2)

    tier_color = _tier_color(tier)
    canvas.create_line(x0 + 16, y0 + 6, x1 - 16, y0 + 6, fill=tier_color, width=2)
    canvas.create_rectangle(x0 + 13, y0 + 5, x0 + 15, y0 + 7, fill=tier_color, outline="")
    canvas.create_rectangle(x1 - 15, y0 + 5, x1 - 13, y0 + 7, fill=tier_color, outline="")


def _short_text(value: Any, limit: int) -> str:
    text = _clean_text(value, limit=max(1, limit + 1))
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _draw_shadowed_text(canvas: CanvasLike, x: int, y: int, **kwargs: Any) -> None:
    shadow_kwargs = dict(kwargs)
    shadow_kwargs["fill"] = OVERLAY_THEME.get("text_shadow", "#000000")
    canvas.create_text(x + 1, y + 1, **shadow_kwargs)
    canvas.create_text(x, y, **kwargs)


def _draw_stat_panel(canvas: CanvasLike, box: tuple[int, int, int, int], row: StatPanelModel) -> None:
    _draw_native_panel(canvas, box, tier=row["tier"])
    x0, y0, x1, y1 = box
    height = y1 - y0
    width = x1 - x0
    title_size = _clamp(11, height * 0.22, 18)
    body_size = _clamp(9, height * 0.17, 14)
    font_family = "Microsoft YaHei UI"
    if row["state"] == "detecting":
        _draw_shadowed_text(
            canvas,
            (x0 + x1) // 2,
            (y0 + y1) // 2,
            text=row["stats_text"],
            fill=OVERLAY_THEME["text_muted"],
            font=(font_family, title_size, "bold"),
            anchor="center",
        )
        return
    title = row["name"]
    _draw_shadowed_text(
        canvas,
        (x0 + x1) // 2,
        y0 + int(height * 0.34),
        text=_short_text(title, max(12, width // 13)),
        fill=OVERLAY_THEME["text_primary"],
        font=(font_family, title_size, "bold"),
        anchor="center",
    )
    _draw_shadowed_text(
        canvas,
        (x0 + x1) // 2,
        y0 + int(height * 0.69),
        text=_short_text(row["stats_text"], max(14, width // 10)),
        fill=OVERLAY_THEME["text_secondary"],
        font=(font_family, body_size),
        anchor="center",
    )


def _wrap_content(value: str, *, line_chars: int, max_lines: int = 3) -> str:
    text = _clean_text(value, limit=180)
    if not text:
        return ""
    chunks = [text[index : index + line_chars] for index in range(0, len(text), line_chars)]
    if len(chunks) > max_lines:
        chunks = chunks[:max_lines]
        chunks[-1] = chunks[-1][:-1] + "…"
    return "\n".join(chunks)


def _draw_synergy_panel(canvas: CanvasLike, box: tuple[int, int, int, int], row: SynergyPanelModel) -> None:
    _draw_native_panel(canvas, box, tier=row["tier"])
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    pad = _clamp(12, width * 0.05, 20)
    title_size = _clamp(10, height * 0.10, 15)
    body_size = _clamp(8, height * 0.08, 12)
    font_family = "Microsoft YaHei UI"
    header_parts = [row["augment_name"]]
    if row["rating"]:
        header_parts.append(row["rating"])
    _draw_shadowed_text(
        canvas,
        x0 + pad,
        y0 + _clamp(16, height * 0.15, 26),
        text=_short_text(" · ".join(header_parts), max(12, width // 11)),
        fill=OVERLAY_THEME["text_primary"],
        font=(font_family, title_size, "bold"),
        anchor="nw",
        width=max(40, width - pad * 2),
    )
    meta = " · ".join(part for part in (row["hero_name"], row["tag"]) if part)
    _draw_shadowed_text(
        canvas,
        x0 + pad,
        y0 + _clamp(38, height * 0.34, 52),
        text=_short_text(meta, max(12, width // 10)),
        fill=_tier_color(row["tier"]),
        font=(font_family, body_size, "bold"),
        anchor="nw",
        width=max(40, width - pad * 2),
    )
    _draw_shadowed_text(
        canvas,
        x0 + pad,
        y0 + _clamp(58, height * 0.52, 78),
        text=_wrap_content(row["content"], line_chars=max(10, (width - pad * 2) // 10)),
        fill=OVERLAY_THEME["text_secondary"],
        font=(font_family, body_size),
        anchor="nw",
        width=max(40, width - pad * 2),
    )


def draw_overlay_frame(
    canvas: CanvasLike,
    model: OverlayRenderModel,
    *,
    viewport_size: tuple[int, int] | None = None,
    perf_sink: dict[str, Any] | None = None,
) -> OverlayLayout:
    """清屏并绘制三统计窗和 0–3 条联动，返回实际几何供验证。"""

    started_at = time.perf_counter()
    if viewport_size is None:
        viewport_size = (max(1, int(canvas.winfo_width())), max(1, int(canvas.winfo_height())))
    layout = resolve_overlay_layout(viewport_size, synergy_count=len(model["synergies"]))
    canvas.delete("all")
    for box, row in zip(layout["stat_boxes"], model["stats"]):
        _draw_stat_panel(canvas, box, row)
    for box, row in zip(layout["synergy_boxes"], model["synergies"]):
        _draw_synergy_panel(canvas, box, row)
    if isinstance(perf_sink, dict):
        perf_sink["last_draw_ms"] = (time.perf_counter() - started_at) * 1000.0
    return layout

"""联动正文的 Canvas 绘制与完整性诊断。

几何、主题和基础绘制 primitive 由 ``canvas_renderer`` 注入，避免本模块反向
导入 façade 形成循环。正文只绘制完整候选或明确待审标记，不做半句省略。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .text_metrics import canvas_text_metrics, _resolve_synergy_text_layout, _wrap_visual_text
from .typography import OverlayTypographyMetrics


DrawPanel = Callable[..., None]
DrawText = Callable[..., Any]
TierColor = Callable[[Any], str]
PixelFont = Callable[..., tuple[Any, ...]]


def _summary_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("display_summary")
    return dict(value) if isinstance(value, Mapping) else {}


def _identity_fields(summary: Mapping[str, Any]) -> dict[str, Any]:
    spec = summary.get("display_spec")
    return {
        "summary_status": str(summary.get("status") or "legacy_unprepared"),
        "source_sha256": str(summary.get("source_sha256") or ""),
        "rule_version": str(summary.get("rule_version") or ""),
        "display_spec": dict(spec) if isinstance(spec, Mapping) else {},
    }


def _verify_items(
    canvas: Any,
    box: tuple[int, int, int, int],
    items: list[Any],
) -> tuple[bool, bool, list[list[int]]]:
    x0, y0, x1, y1 = box
    actual_bboxes: list[list[int]] = []
    bbox = getattr(canvas, "bbox", None)
    if callable(bbox):
        for item in items:
            actual = bbox(item) if item is not None else None
            if actual is not None:
                actual_bboxes.append([int(value) for value in actual])
    available = bool(items) and len(actual_bboxes) == len(items)
    inside = available and all(
        x0 <= actual[0] <= actual[2] <= x1 and y0 <= actual[1] <= actual[3] <= y1
        for actual in actual_bboxes
    )
    return available, inside, actual_bboxes


def _finish_audit(
    canvas: Any,
    box: tuple[int, int, int, int],
    row: Mapping[str, Any],
    summary: Mapping[str, Any],
    *,
    items: list[Any],
    line_count: int,
    reason: str,
) -> dict[str, Any]:
    bbox_available, bbox_verified, actual_bboxes = _verify_items(canvas, box, items)
    if bbox_available and not bbox_verified:
        for item in items:
            if item is not None:
                canvas.delete(item)
        reason = "actual_canvas_bbox_exceeds_panel"
    rendered_any = bool(line_count) and (not bbox_available or bbox_verified)
    source_omitted = bool(reason)
    if rendered_any and not bbox_available and not reason:
        reason = "canvas_bbox_unavailable"
    if not rendered_any:
        source_omitted = True
    content_drawn = rendered_any and not source_omitted
    fallback_drawn = rendered_any and source_omitted
    if not rendered_any and not reason:
        reason = "display_text_not_drawn"
    original = row.get("raw_content", row.get("content"))
    return {
        "slot": row["slot"],
        **_identity_fields(summary),
        "content_drawn": content_drawn,
        "content_verified": content_drawn and bbox_verified,
        "fallback_drawn": fallback_drawn,
        "fallback_verified": fallback_drawn and bbox_verified,
        "reason": reason,
        "line_count": line_count if rendered_any else 0,
        "original_line_count": int(summary.get("line_count") or 0),
        "original_char_count": int(summary.get("original_char_count") or len(str(original or ""))),
        "truncated": False,
        "source_omitted": source_omitted,
        "bbox_available": bbox_available,
        "bbox_verified": bbox_verified,
        "actual_bboxes": actual_bboxes if bbox_available else [],
    }


def draw_expanded_synergy_panel(
    canvas: Any,
    box: tuple[int, int, int, int],
    row: Mapping[str, Any],
    *,
    typography: OverlayTypographyMetrics,
    minimum_height: int,
    theme: Mapping[str, str],
    draw_panel: DrawPanel,
    draw_text: DrawText,
    tier_color: TierColor,
    pixel_font: PixelFont,
) -> dict[str, Any]:
    draw_panel(canvas, box, tier=str(row.get("tier") or ""))
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    pad = max(10, int(round(20 * typography["geometry_scale"])))
    metrics = canvas_text_metrics(canvas)
    text_layout = _resolve_synergy_text_layout(
        row,
        width,
        typography=typography,
        minimum_height=minimum_height,
        panel_height=height,
        measure=lambda text, size: metrics.width(text, size, True),
        measured_line_height=metrics.line_height(typography["synergy_body_px"]),
    )
    summary = _summary_metadata(row)
    source_status = str(summary.get("status") or "legacy_unprepared")
    reason = str(summary.get("omission_reason") or "") if source_status == "review_required" else ""
    if text_layout["truncated"]:
        fallback = str(summary.get("fallback_text") or "内容较长，已保留完整原文待审")
        text_layout = _resolve_synergy_text_layout(
            {**row, "content": fallback},
            width,
            typography=typography,
            minimum_height=minimum_height,
            panel_height=height,
            measure=lambda text, size: metrics.width(text, size, True),
            measured_line_height=metrics.line_height(typography["synergy_body_px"]),
        )
        reason = reason or (
            "actual_text_exceeds_budget" if source_status == "ready"
            else "canvas_overflow_without_safe_summary"
        )

    family = "Microsoft YaHei UI"
    draw_text(
        canvas, x0 + pad, y0 + text_layout["title_offset"],
        text=text_layout["header"], fill=theme["text_primary"],
        font=pixel_font(family, text_layout["title_size"], "bold"), anchor="nw",
    )
    if text_layout["rating"]:
        badge_x1 = x1 - pad
        badge_x0 = badge_x1 - max(text_layout["badge_width"], text_layout["title_size"])
        badge_y0 = y0 + text_layout["title_offset"] - 2
        badge_y1 = badge_y0 + max(
            text_layout["title_size"] + 6,
            int(round(text_layout["title_size"] * 1.25)),
        )
        canvas.create_rectangle(
            badge_x0, badge_y0, badge_x1, badge_y1,
            fill=tier_color(row.get("tier")), outline="",
        )
        canvas.create_text(
            (badge_x0 + badge_x1) // 2, (badge_y0 + badge_y1) // 2,
            text=text_layout["rating"], fill=theme["panel_bg"],
            font=pixel_font(family, text_layout["title_size"] - 2, "bold"), anchor="center",
        )
    draw_text(
        canvas, x0 + pad, y0 + text_layout["meta_offset"],
        text=text_layout["meta"], fill=tier_color(row.get("tier")),
        font=pixel_font(family, text_layout["body_size"], "bold"), anchor="nw",
    )
    items: list[Any] = []
    for index, line in enumerate(text_layout["body_lines"]):
        items.append(draw_text(
            canvas,
            x0 + pad,
            y0 + text_layout["body_offset"] + index * text_layout["line_height"],
            text=line,
            fill=theme["text_secondary"],
            font=pixel_font(family, text_layout["body_size"]),
            anchor="nw",
        ))
    return _finish_audit(
        canvas,
        box,
        row,
        summary,
        items=items,
        line_count=len(text_layout["body_lines"]),
        reason=reason,
    )


def draw_compact_synergy_panel(
    canvas: Any,
    box: tuple[int, int, int, int],
    row: Mapping[str, Any],
    *,
    typography: OverlayTypographyMetrics,
    theme: Mapping[str, str],
    draw_panel: DrawPanel,
    draw_text: DrawText,
    pixel_font: PixelFont,
) -> dict[str, Any]:
    draw_panel(canvas, box, tier=str(row.get("tier") or ""))
    x0, y0, x1, y1 = box
    width = x1 - x0
    pad = max(10, int(round(20 * typography["geometry_scale"])))
    font_size = typography["compact_synergy_px"]
    summary_text = " · ".join(
        str(part) for part in (
            row.get("hero_name"), row.get("rating"), row.get("tag"),
            row.get("status_text"), row.get("content"),
        ) if str(part or "").strip()
    )
    metrics = canvas_text_metrics(canvas)
    def measure(text: str, size: int) -> float:
        return metrics.width(text, size, True)

    max_width = max(40, width - pad * 2)
    lines = _wrap_visual_text(summary_text, max_width=max_width, font_size=font_size, measure=measure)
    line_height = metrics.line_height(font_size, True)
    capacity = max(1, (y1 - y0 - pad) // line_height)
    summary = _summary_metadata(row)
    source_status = str(summary.get("status") or "legacy_unprepared")
    reason = str(summary.get("omission_reason") or "") if source_status == "review_required" else ""
    if len(lines) > capacity:
        fallback = str(summary.get("fallback_text") or "内容较长，已保留完整原文待审")
        lines = _wrap_visual_text(fallback, max_width=max_width, font_size=font_size, measure=measure)
        reason = reason or (
            "actual_text_exceeds_budget" if source_status == "ready"
            else "canvas_overflow_without_safe_summary"
        )
        if len(lines) > capacity:
            lines = []
            reason = "review_marker_exceeds_budget"
    items: list[Any] = []
    for index, line in enumerate(lines):
        items.append(draw_text(
            canvas,
            x0 + pad,
            int(round((y0 + y1) / 2 + (index - (len(lines) - 1) / 2) * line_height)),
            text=line,
            fill=theme["text_primary"],
            font=pixel_font("Microsoft YaHei UI", font_size, "bold"),
            anchor="w",
        ))
    return _finish_audit(
        canvas,
        box,
        row,
        summary,
        items=items,
        line_count=len(lines),
        reason=reason,
    )


__all__ = ["draw_compact_synergy_panel", "draw_expanded_synergy_panel"]

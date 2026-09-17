"""生产 Tk 与离线 Canvas 共用的实际字体测量边界，不改变数据内容。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping
from copy import deepcopy
import unicodedata
import weakref

from hextech.modules.recommendation.display_summary import (
    DisplaySummaryCache,
    SUMMARY_READY,
    ensure_display_summary,
)

from .display_geometry import display_card_panels, display_geometry_key
from .typography import OverlayTypographyMetrics, SynergyTextLayout


FONT_FAMILY = "Microsoft YaHei UI"


class TextMetrics:
    """每个 Canvas 缓存字体对象；Tk 字体只能在所属 GUI 线程访问。"""

    def __init__(self, canvas: Any) -> None:
        # Canvas 已持有 metrics，反向强引用会把 Tcl 对象留进循环垃圾，可能被后台 GC。
        self.canvas = weakref.proxy(canvas) if hasattr(canvas, "tk") else canvas
        self._fonts: dict[tuple[int, bool], Any] = {}

    def _font(self, size: int, bold: bool) -> Any:
        key = (max(1, int(size)), bold)
        if key not in self._fonts:
            if hasattr(self.canvas, "tk"):
                from tkinter.font import Font

                self._fonts[key] = Font(
                    root=self.canvas, family=FONT_FAMILY, size=-key[0],
                    weight="bold" if bold else "normal",
                )
            else:
                self._fonts[key] = _pillow_font(*key)
        return self._fonts[key]

    def width(self, text: str, size: int, bold: bool = False) -> float:
        font = self._font(size, bold)
        return float(font.measure(text) if hasattr(font, "measure") else font.getlength(text))

    def line_height(self, size: int, bold: bool = False) -> int:
        font = self._font(size, bold)
        if hasattr(font, "metrics"):
            return int(font.metrics("linespace"))
        ascent, descent = font.getmetrics()
        return int(ascent + descent)


def fit_stats_spacing(metrics: TextMetrics, text: str, size: int, budget: int) -> tuple[str, str]:
    """固定字号，仅在超宽时收紧排版空隙；预留 Canvas 的两侧字形余量。"""
    for name, gap in (("normal", " "), ("thin", "\u2009"), ("hair", "\u200a"), ("compact", "")):
        candidate = text.replace(" ", gap)
        if metrics.width(candidate, size, True) + 2 <= budget:
            return candidate, name
    raise ValueError("stats_text_exceeds_safe_area")


def fit_stats_block(metrics: TextMetrics, text: str, size: int, width: int, height: int) -> tuple[str, str]:
    """Keep the approved font: tighten gaps first, then split the two metrics if necessary."""
    try:
        return fit_stats_spacing(metrics, text, size, width)
    except ValueError:
        parts = text.split("·")
        if len(parts) != 2 or metrics.line_height(size, True) * 2 + 2 > height:
            raise
        lines = [fit_stats_spacing(metrics, part.strip(), size, width)[0] for part in parts]
        return "\n".join(lines), "two_lines"


@lru_cache(maxsize=64)
def _pillow_font(size: int, bold: bool) -> Any:
    from PIL import ImageFont

    name = "msyhbd.ttc" if bold else "msyh.ttc"
    path = Path("C:/Windows/Fonts") / name
    if path.is_file():
        # TTC face 0 是 Microsoft YaHei，face 1 才与生产的 YaHei UI 相同。
        return ImageFont.truetype(str(path), size, index=1)
    # 非 Windows 仅供纯合同回归，不作为生产中文像素验收。
    return ImageFont.load_default(size=size)


def pillow_text_width(text: str, font_px: int) -> float:
    """后台使用 YaHei UI face 1 的粗体宽度做保守候选预算测量。"""

    font = _pillow_font(max(1, int(font_px)), True)
    return float(font.getlength(str(text or "")))


def _pillow_line_height(font_px: int, *, bold: bool = False) -> int:
    ascent, descent = _pillow_font(max(1, int(font_px)), bold).getmetrics()
    return int(ascent + descent)


def canvas_text_metrics(canvas: Any) -> TextMetrics:
    metrics = getattr(canvas, "_hextech_text_metrics", None)
    if not isinstance(metrics, TextMetrics):
        metrics = TextMetrics(canvas)
        setattr(canvas, "_hextech_text_metrics", metrics)
    return metrics


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


def _ellipsize_visual(
    value: str, *, max_width: int, font_size: int,
    measure: Callable[[str, int], float] = _visual_text_width,
) -> str:
    text = str(value or "").rstrip()
    if measure(text, font_size) <= max_width:
        return text
    suffix = "…"
    while text and measure(text + suffix, font_size) > max_width:
        text = text[:-1].rstrip()
    return (text + suffix) if text else suffix


def _wrap_visual_text(
    value: str, *, max_width: int, font_size: int,
    measure: Callable[[str, int], float] = _visual_text_width,
) -> list[str]:
    # 上游 display_summary 已决定完整候选是否能放下；此处不得再用字符上限
    # 预先截断，否则会出现“先截 180/512 字、再按行截”的双重省略。
    text = " ".join(str(value or "").split())
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if char.isspace() and not current:
            continue
        candidate = current + char
        if current and measure(candidate, font_size) > max_width:
            lines.append(current.rstrip())
            current = "" if char.isspace() else char
        else:
            current = candidate
    if current:
        lines.append(current.rstrip())
    return [line for line in lines if line]


def _resolve_synergy_text_layout(
    row: Mapping[str, Any],
    width: int,
    *,
    typography: OverlayTypographyMetrics,
    minimum_height: int,
    panel_height: int | None = None,
    measure: Callable[[str, int], float] = _visual_text_width,
    measured_line_height: int = 0,
) -> SynergyTextLayout:
    pad = max(10, int(round(20 * typography["geometry_scale"])))
    text_width = max(40, width - pad * 2)
    # 字号与内边距统一来自真实卡框比例；DPI 仅提供极小卡框的可读性下限。
    title_size = typography["synergy_title_px"]
    body_size = typography["synergy_body_px"]
    title_line_height = max(title_size + 2, int(round(title_size * 1.25)))
    line_height = max(body_size + 4, int(round(body_size * 1.35)), measured_line_height)
    title_offset = max(12, min(26, int(round(title_size * 0.8))))
    meta_offset = title_offset + title_line_height + 6
    body_offset = meta_offset + line_height + 8
    bottom_pad = max(10, min(20, int(round(body_size * 0.8))))

    rating = " ".join(str(row["rating"] or "").split())[:12]
    # 评级改为右上角 tier 色徽章；标题只留卡名，可用宽度需扣除徽章占位。
    badge_width = int(measure(rating, title_size)) + title_size if rating else 0
    header_width = max(24, text_width - (badge_width + 8 if badge_width else 0))
    header = _ellipsize_visual(row["augment_name"], max_width=header_width, font_size=title_size, measure=measure)
    meta = _ellipsize_visual(
        " · ".join(part for part in (row["hero_name"], row["tag"], row.get("status_text", "")) if part),
        max_width=text_width,
        font_size=body_size,
        measure=measure,
    )
    body_lines = _wrap_visual_text(row["content"], max_width=text_width, font_size=body_size, measure=measure)
    desired_height = max(
        max(1, int(minimum_height)),
        body_offset + len(body_lines) * line_height + bottom_pad,
    )
    truncated = False
    if panel_height is not None and body_lines:
        available_body_height = max(0, int(panel_height) - body_offset - bottom_pad)
        max_lines = max(0, available_body_height // line_height)
        if len(body_lines) > max_lines:
            # 不能把条件句切到一半再加省略号。调用方应绘制明确的待审标记，
            # 并把完整原文保留在 row/raw diagnostics 中。
            body_lines = []
            truncated = True
    return {
        "header": header,
        "rating": rating,
        "badge_width": badge_width,
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


def _ratio_box(
    panel: tuple[float, float, float, float],
    viewport_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = viewport_size
    return (
        max(0, min(width - 1, int(round(panel[0] * width)))),
        max(0, min(height - 1, int(round(panel[1] * height)))),
        max(1, min(width, int(round(panel[2] * width)))),
        max(1, min(height, int(round(panel[3] * height)))),
    )


def synergy_display_spec(
    viewport_size: tuple[int, int],
    display_mode: str,
    *,
    prefix: str = "",
) -> dict[str, Any]:
    """解析当前卡槽、字号、元信息占位后的正文像素预算。"""

    width, height = (int(value) for value in viewport_size)
    if width <= 1 or height <= 1:
        raise ValueError("overlay viewport is not ready")
    mode = "expanded" if str(display_mode) == "expanded" else "compact"
    card = _ratio_box(display_card_panels((width, height))[0], (width, height))
    card_width = max(1, card[2] - card[0])
    card_height = max(1, card[3] - card[1])
    from .typography import resolve_overlay_typography

    typography = resolve_overlay_typography(1.0, card_size=(card_width, card_height))
    pad = max(10, int(round(20 * typography["geometry_scale"])))
    max_width = max(40, card_width - pad * 2)
    if mode == "compact":
        font_px = typography["compact_synergy_px"]
        line_height = _pillow_line_height(font_px, bold=True)
        max_lines = max(1, (typography["compact_panel_px"] - pad) // max(1, line_height))
    else:
        font_px = typography["synergy_body_px"]
        title_size = typography["synergy_title_px"]
        title_line_height = max(title_size + 2, int(round(title_size * 1.25)))
        line_height = max(
            font_px + 4,
            int(round(font_px * 1.35)),
            _pillow_line_height(font_px),
        )
        title_offset = max(12, min(26, int(round(title_size * 0.8))))
        meta_offset = title_offset + title_line_height + 6
        body_offset = meta_offset + line_height + 8
        bottom_pad = max(10, min(20, int(round(font_px * 0.8))))
        margin = max(max(8, min(20, int(width * 0.008))), int(round(height * 0.04)))
        synergy_gap = max(8, int(round(16 * typography["geometry_scale"])))
        panel_height = max(0, card[1] - synergy_gap - margin)
        max_lines = max(1, (panel_height - body_offset - bottom_pad) // max(1, line_height))
    return {
        "id": f"{mode}-{width}x{height}-{display_geometry_key((width, height))[:12]}",
        "max_width_px": max_width,
        "font_px": font_px,
        "max_lines": max_lines,
        "prefix": prefix if mode == "compact" else "",
    }


def prepare_synergy_display_summaries(
    model: Mapping[str, Any],
    *,
    viewport_size: tuple[int, int],
    display_mode: str,
    cache: DisplaySummaryCache,
) -> dict[str, Any]:
    """后台按当前规格派生展示文字；原始 content 始终留在 raw_content。"""

    prepared = deepcopy(dict(model))
    rows = prepared.get("synergies")
    if not isinstance(rows, list):
        return prepared
    next_rows: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            continue
        row = deepcopy(dict(raw_row))
        source_content = row.get("raw_content", row.get("content"))
        prefix = " · ".join(
            str(part).strip()
            for part in (
                row.get("hero_name"),
                row.get("rating"),
                row.get("tag"),
                row.get("status_text"),
            )
            if str(part or "").strip()
        )
        if prefix:
            prefix += " · "
        spec = synergy_display_spec(viewport_size, display_mode, prefix=prefix)
        with_summary = ensure_display_summary(
            {**row, "content": source_content},
            display_spec=spec,
            measure=pillow_text_width,
            cache=cache,
        )
        summary = with_summary["display_summary"]
        row["raw_content"] = source_content
        row["display_summary"] = summary
        row["content"] = (
            summary["text"]
            if summary["status"] == SUMMARY_READY
            else summary["fallback_text"]
        )
        next_rows.append(row)
    prepared["synergies"] = next_rows
    return prepared


__all__ = [
    "TextMetrics",
    "canvas_text_metrics",
    "fit_stats_spacing",
    "fit_stats_block",
    "pillow_text_width",
    "prepare_synergy_display_summaries",
    "synergy_display_spec",
]

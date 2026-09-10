"""Overlay 按真实卡框解析统一物理像素度量。"""

from __future__ import annotations

from typing import TypedDict


REFERENCE_CARD_WIDTH_PX = 477
REFERENCE_CARD_HEIGHT_PX = 768
# 已确认的30px说明样图仅锁定视觉方向；真实内框锚点资格独立验收。
CARD_TEXT_BASE_PX = 30
SYNERGY_TITLE_BASE_PX = 24
SYNERGY_BODY_BASE_PX = 18
COMPACT_SYNERGY_TEXT_BASE_PX = 18
STAGE_TEXT_BASE_PX = 24
SYNERGY_PANEL_MIN_BASE_PX = 135
COMPACT_SYNERGY_HEIGHT_BASE_PX = 96

CARD_TEXT_MIN_PX = 16
SYNERGY_TITLE_MIN_PX = 14
SYNERGY_BODY_MIN_PX = 11
STAGE_TEXT_MIN_PX = 16
SYNERGY_PANEL_MIN_FLOOR_PX = 90
COMPACT_SYNERGY_HEIGHT_FLOOR_PX = 64


class OverlayTypographyMetrics(TypedDict):
    dpi_scale: float
    geometry_scale: float
    stats_pixel_size: int
    synergy_title_px: int
    synergy_body_px: int
    compact_synergy_px: int
    stage_px: int
    stage_warning_px: int
    expanded_panel_min_px: int
    compact_panel_px: int


class SynergyTextLayout(TypedDict):
    header: str
    rating: str
    badge_width: int
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


def resolve_overlay_typography(
    dpi_scale: object,
    *,
    card_size: tuple[int, int] = (REFERENCE_CARD_WIDTH_PX, REFERENCE_CARD_HEIGHT_PX),
) -> OverlayTypographyMetrics:
    """以卡框比例解析物理像素；DPI 只保护极小卡框的最低可读性。"""

    try:
        scale = float(dpi_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if scale <= 0:
        scale = 1.0
    scale = max(0.5, min(2.5, scale))

    try:
        card_width = max(1, int(card_size[0]))
        card_height = max(1, int(card_size[1]))
    except (IndexError, TypeError, ValueError):
        card_width = REFERENCE_CARD_WIDTH_PX
        card_height = REFERENCE_CARD_HEIGHT_PX
    geometry_scale = min(
        card_width / REFERENCE_CARD_WIDTH_PX,
        card_height / REFERENCE_CARD_HEIGHT_PX,
    )
    geometry_scale = max(0.1, min(2.5, geometry_scale))

    # 只有卡框小到会低于固定可读下限时，系统 DPI 才能略微抬高文字；
    # AOC 100% 与 BOE 150% 的参考卡框都不会触发该下限。
    # 游戏布局以物理客户区为准；同一客户区不能因 DPI 改变字体或面板尺寸。
    readability_scale = 1.0

    def px(value: int, minimum: int) -> int:
        geometric = int(round(value * geometry_scale))
        readable = int(round(minimum * readability_scale))
        return max(1, geometric, readable)

    return {
        "dpi_scale": round(scale, 3),
        "geometry_scale": round(geometry_scale, 4),
        "stats_pixel_size": px(CARD_TEXT_BASE_PX, CARD_TEXT_MIN_PX),
        "synergy_title_px": px(SYNERGY_TITLE_BASE_PX, SYNERGY_TITLE_MIN_PX),
        "synergy_body_px": px(SYNERGY_BODY_BASE_PX, SYNERGY_BODY_MIN_PX),
        "compact_synergy_px": px(COMPACT_SYNERGY_TEXT_BASE_PX, SYNERGY_BODY_MIN_PX),
        "stage_px": px(STAGE_TEXT_BASE_PX, STAGE_TEXT_MIN_PX),
        "stage_warning_px": px(SYNERGY_BODY_BASE_PX, SYNERGY_BODY_MIN_PX),
        "expanded_panel_min_px": max(
            SYNERGY_PANEL_MIN_FLOOR_PX,
            int(round(SYNERGY_PANEL_MIN_BASE_PX * geometry_scale)),
        ),
        "compact_panel_px": max(
            COMPACT_SYNERGY_HEIGHT_FLOOR_PX,
            int(round(COMPACT_SYNERGY_HEIGHT_BASE_PX * geometry_scale)),
        ),
    }


__all__ = [
    "CARD_TEXT_BASE_PX",
    "OverlayTypographyMetrics",
    "SynergyTextLayout",
    "resolve_overlay_typography",
]

"""显示专用固定锚点；识别 ROI 调整不能改变用户看到的布局。

这些数值是既有显示基线，不是新测量结果。缺少同规格真实内框证据时保持
pending_real_device；禁止拿比例公式或说明图将其提升为已验收。
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from .display_contract import display_profile

DISPLAY_GEOMETRY_VERSION = "display-anchors-v1"
_PANELS_16_9 = ((.198, .155, .384, .690), (.410, .155, .597, .690), (.623, .155, .811, .690))
_PANELS_16_10 = ((.198, .175, .384, .655), (.410, .175, .597, .655), (.623, .175, .811, .655))
# 每个已支持尺寸的统计内框单独定义，不由视觉识别卡框计算。
_STAT_BOXES = {
    (2560, 1440): ((557, 862, 933, 934), (1101, 862, 1477, 934), (1647, 862, 2023, 934)),
    (2560, 1600): ((557, 916, 933, 988), (1101, 916, 1477, 988), (1647, 916, 2023, 988)),
    (1920, 1080): ((417, 646, 699, 700), (825, 646, 1107, 700), (1235, 646, 1517, 700)),
    (1920, 1200): ((417, 687, 699, 741), (825, 687, 1107, 741), (1235, 687, 1517, 741)),
    (1280, 720): ((278, 431, 466, 477), (550, 431, 738, 477), (823, 431, 1011, 477)),
}


def display_card_panels(viewport: tuple[int, int]) -> tuple[tuple[float, float, float, float], ...]:
    width, height = viewport
    if width <= 1 or height <= 1:
        raise ValueError("overlay viewport is not ready")
    # 非目标比例沿用旧的显示 fallback，但元数据明确标记未验收。
    return _PANELS_16_9 if width / height >= 1.70 else _PANELS_16_10


def display_stat_boxes(viewport: tuple[int, int]) -> tuple[tuple[int, int, int, int], ...]:
    width, height = viewport
    panels = display_card_panels(viewport)
    if viewport in _STAT_BOXES:
        return _STAT_BOXES[viewport]
    cards = [tuple(round(value * (width if i % 2 == 0 else height)) for i, value in enumerate(p)) for p in panels]
    card_height = cards[0][3] - cards[0][1]
    scale = max(.1, min(2.5, min((cards[0][2] - cards[0][0]) / 477, card_height / 768)))
    top = cards[0][1] + int(card_height * .829)
    bottom = min(cards[0][3], top + min(card_height, max(46, round(72 * scale))))
    safe_width = max(1, int(376 * width / 2560 + .5))
    return tuple(((left + right) // 2 - safe_width // 2, top,
                  (left + right) // 2 - safe_width // 2 + safe_width, bottom)
                 for left, _, right, _ in cards)


@lru_cache(maxsize=32)
def display_geometry_key(viewport: tuple[int, int]) -> str:
    encoded = json.dumps((DISPLAY_GEOMETRY_VERSION, viewport, display_card_panels(viewport),
                          display_stat_boxes(viewport)), separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def display_geometry_metadata(viewport: tuple[int, int]) -> dict[str, object]:
    return {
        "display_profile": display_profile(*viewport),
        "anchor_version": DISPLAY_GEOMETRY_VERSION,
        "anchor_fingerprint": display_geometry_key(viewport),
        "anchor_qualification": "pending_real_device",
        "calibration_evidence_sha256": "",
        "anchor_source": "preserved_display_baseline",
        "resolution_qualification": "compatibility" if viewport == (1280, 720)
        else "target" if viewport in _STAT_BOXES else "unqualified",
    }

"""Overlay V2 选择版式检测。

本模块只在预期三卡区域内联合判断选择按钮与卡面结构，并返回当前帧的局部
平移/缩放。它不识别海克斯内容、不写运行态，也不持久化窗口校准。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from PIL import Image


BUTTON_CENTER_X = 0.50
BUTTON_CENTER_Y = 0.794
BUTTON_EXPECTED_WIDTH = 0.111
BUTTON_EXPECTED_HEIGHT = 0.043
BUTTON_INNER_HEIGHT_RATIO = 0.92
BUTTON_CENTER_SNAP_TOLERANCE = 0.008
BUTTON_SEARCH_REGION = (0.39, 0.765, 0.61, 0.855)
BUTTON_CENTER_X_TOLERANCE = 0.045
BUTTON_CENTER_Y_TOLERANCE = 0.038
BUTTON_WIDTH_RANGE = (0.085, 0.18)
BUTTON_HEIGHT_RANGE = (0.016, 0.060)
BUTTON_ASPECT_RANGE = (3.5, 10.5)
BUTTON_MIN_BLUE_PIXELS = 80
BUTTON_MIN_BLUE_RATIO = 0.15
BUTTON_MIN_SOLIDITY = 0.42
BUTTON_SCAN_DOWNSAMPLE = 4
CARD_MIN_BORDER_GOLD_RATIO = 0.025

CARD_PANELS_16_10 = (
    (0.198, 0.175, 0.384, 0.655),
    (0.410, 0.175, 0.597, 0.655),
    (0.623, 0.175, 0.811, 0.655),
)
CARD_PANELS_16_9 = (
    (0.198, 0.155, 0.384, 0.690),
    (0.410, 0.155, 0.597, 0.690),
    (0.623, 0.155, 0.811, 0.690),
)
CARD_PANEL_16_9_MIN_ASPECT = 1.70


@dataclass(frozen=True)
class LayoutTransform:
    """相对屏幕中心的平移与统一缩放。"""

    dx_ratio: float = 0.0
    dy_ratio: float = 0.0
    scale: float = 1.0


@dataclass(frozen=True)
class SceneObservation:
    """单帧选择版式观察；`present` 不包含跨帧稳定语义。"""

    present: bool
    scene_state: str
    score: float
    layout_id: str
    button_box: tuple[int, int, int, int] | None
    button_blue_ratio: float
    panel_scores: tuple[float, float, float]
    transform: LayoutTransform
    card_residue: bool
    reason: str = ""


def pick_card_panels(viewport_size: tuple[int, int]) -> tuple[tuple[float, float, float, float], ...]:
    """按窗口宽高比选择三卡面板比例；renderer 和 sidecar 必须共用这一条规则。"""

    width, height = (max(1, int(value)) for value in viewport_size)
    aspect = width / max(1, height)
    return CARD_PANELS_16_9 if aspect >= CARD_PANEL_16_9_MIN_ASPECT else CARD_PANELS_16_10


def _component_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """返回二值 mask 的 8 邻域组件框与像素数；输入已下采样。"""

    height, width = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    boxes: list[tuple[int, int, int, int, int]] = []
    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y, start_x] or seen[start_y, start_x]:
                continue
            stack = [(start_x, start_y)]
            seen[start_y, start_x] = True
            min_x = max_x = start_x
            min_y = max_y = start_y
            count = 0
            while stack:
                x, y = stack.pop()
                count += 1
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                for ny in range(max(0, y - 1), min(height, y + 2)):
                    for nx in range(max(0, x - 1), min(width, x + 2)):
                        if mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((nx, ny))
            boxes.append((min_x, min_y, max_x + 1, max_y + 1, count))
    return boxes


def _button_geometry_valid(
    box: tuple[int, int, int, int],
    image_size: tuple[int, int],
    *,
    blue_pixels: int,
) -> bool:
    width, height = image_size
    left, top, right, bottom = box
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    center_x = (left + right) / 2.0 / max(1, width)
    center_y = (top + bottom) / 2.0 / max(1, height)
    width_ratio = box_width / max(1, width)
    height_ratio = box_height / max(1, height)
    aspect = box_width / max(1, box_height)
    solidity = blue_pixels / max(1, box_width * box_height)
    return bool(
        abs(center_x - BUTTON_CENTER_X) <= BUTTON_CENTER_X_TOLERANCE
        and abs(center_y - BUTTON_CENTER_Y) <= BUTTON_CENTER_Y_TOLERANCE
        and BUTTON_WIDTH_RANGE[0] <= width_ratio <= BUTTON_WIDTH_RANGE[1]
        and BUTTON_HEIGHT_RANGE[0] <= height_ratio <= BUTTON_HEIGHT_RANGE[1]
        and BUTTON_ASPECT_RANGE[0] <= aspect <= BUTTON_ASPECT_RANGE[1]
        and blue_pixels >= BUTTON_MIN_BLUE_PIXELS
        and solidity >= BUTTON_MIN_SOLIDITY
    )


def detect_expected_button(image: Image.Image) -> tuple[tuple[int, int, int, int] | None, float]:
    """只在底部中央预期区域寻找真实选择按钮。"""

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    height, width, _ = rgb.shape
    sx0 = int(round(BUTTON_SEARCH_REGION[0] * width))
    sy0 = int(round(BUTTON_SEARCH_REGION[1] * height))
    sx1 = int(round(BUTTON_SEARCH_REGION[2] * width))
    sy1 = int(round(BUTTON_SEARCH_REGION[3] * height))
    region = rgb[sy0:sy1:BUTTON_SCAN_DOWNSAMPLE, sx0:sx1:BUTTON_SCAN_DOWNSAMPLE]
    if region.size == 0:
        return None, 0.0
    red, green, blue = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    mask = (
        (blue >= 100)
        & (green >= 70)
        & (red <= 110)
        & ((blue.astype(np.int16) - red.astype(np.int16)) >= 45)
        & ((green.astype(np.int16) - red.astype(np.int16)) >= 15)
        & (blue.astype(np.int16) >= green.astype(np.int16) - 20)
    )
    candidates: list[tuple[float, tuple[int, int, int, int], float]] = []
    for left, top, right, bottom, count in _component_boxes(mask):
        box = (
            sx0 + left * BUTTON_SCAN_DOWNSAMPLE,
            sy0 + top * BUTTON_SCAN_DOWNSAMPLE,
            min(width, sx0 + right * BUTTON_SCAN_DOWNSAMPLE),
            min(height, sy0 + bottom * BUTTON_SCAN_DOWNSAMPLE),
        )
        blue_pixels = count * BUTTON_SCAN_DOWNSAMPLE * BUTTON_SCAN_DOWNSAMPLE
        if not _button_geometry_valid(box, image.size, blue_pixels=blue_pixels):
            continue
        crop = rgb[box[1] : box[3], box[0] : box[2]]
        if crop.size == 0:
            continue
        crop_red = crop[:, :, 0].astype(np.int16)
        crop_green = crop[:, :, 1].astype(np.int16)
        crop_blue = crop[:, :, 2].astype(np.int16)
        crop_mask = (
            (crop_blue >= 100)
            & (crop_green >= 70)
            & (crop_red <= 110)
            & ((crop_blue - crop_red) >= 45)
            & ((crop_green - crop_red) >= 15)
            & (crop_blue >= crop_green - 20)
        )
        blue_ratio = float(np.mean(crop_mask)) if crop_mask.size else 0.0
        if blue_ratio < BUTTON_MIN_BLUE_RATIO:
            continue
        center_x = (box[0] + box[2]) / 2.0 / max(1, width)
        center_y = (box[1] + box[3]) / 2.0 / max(1, height)
        position_error = (
            abs(center_x - BUTTON_CENTER_X) / BUTTON_CENTER_X_TOLERANCE
            + abs(center_y - BUTTON_CENTER_Y) / BUTTON_CENTER_Y_TOLERANCE
        )
        candidates.append((blue_ratio - 0.08 * position_error, box, blue_ratio))
    if not candidates:
        return None, 0.0
    _, box, ratio = max(candidates, key=lambda item: item[0])
    return box, ratio


def _ratio_box(box: Sequence[float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    return (
        max(0, min(width - 1, int(round(float(box[0]) * width)))),
        max(0, min(height - 1, int(round(float(box[1]) * height)))),
        max(1, min(width, int(round(float(box[2]) * width)))),
        max(1, min(height, int(round(float(box[3]) * height)))),
    )


def apply_transform(
    box: Sequence[float],
    size: tuple[int, int],
    transform: LayoutTransform,
) -> tuple[int, int, int, int]:
    """把归一化 ROI 围绕屏幕中心应用版式 transform。"""

    left, top, right, bottom = (float(value) for value in box)
    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    half_width = (right - left) * transform.scale / 2.0
    half_height = (bottom - top) * transform.scale / 2.0
    next_box = (
        center_x + transform.dx_ratio - half_width,
        center_y + transform.dy_ratio - half_height,
        center_x + transform.dx_ratio + half_width,
        center_y + transform.dy_ratio + half_height,
    )
    return _ratio_box(next_box, size)


def _button_transform(
    button_box: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> LayoutTransform:
    """把蓝色按钮组件转换为稳定 ROI transform。

    Win32 捕获到的选择按钮会随动画和抗锯齿在“外框+内填充”和“仅内填充”
    之间跳变。内填充框足以证明按钮存在，但它比真实按钮更矮、更靠下；若直接
    用它的中心计算 ROI，会把名称区域下移并截到按钮边缘。
    """

    width, height = image_size
    left, top, right, bottom = button_box
    center_x = (left + right) / 2.0 / max(1, width)
    raw_center_y = (top + bottom) / 2.0 / max(1, height)
    button_width = (right - left) / max(1, width)
    button_height = (bottom - top) / max(1, height)

    center_y = raw_center_y
    if button_height < BUTTON_EXPECTED_HEIGHT * BUTTON_INNER_HEIGHT_RATIO:
        expanded_center_y = bottom / max(1, height) - BUTTON_EXPECTED_HEIGHT / 2.0
        if abs(expanded_center_y - BUTTON_CENTER_Y) < abs(raw_center_y - BUTTON_CENTER_Y):
            center_y = expanded_center_y
        button_width = max(button_width, BUTTON_EXPECTED_WIDTH)

    if abs(center_y - BUTTON_CENTER_Y) <= BUTTON_CENTER_SNAP_TOLERANCE:
        center_y = BUTTON_CENTER_Y
    if abs(center_x - BUTTON_CENTER_X) <= BUTTON_CENTER_SNAP_TOLERANCE:
        center_x = BUTTON_CENTER_X

    return LayoutTransform(
        dx_ratio=center_x - BUTTON_CENTER_X,
        dy_ratio=center_y - BUTTON_CENTER_Y,
        scale=max(0.90, min(1.10, button_width / BUTTON_EXPECTED_WIDTH)),
    )


def _panel_score(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    panel = np.asarray(image.crop(box).convert("RGB"), dtype=np.uint8)
    if panel.size == 0:
        return 0.0
    height, width, _ = panel.shape
    inset_x = max(2, int(width * 0.10))
    inset_y = max(2, int(height * 0.08))
    interior = panel[inset_y : max(inset_y + 1, height - inset_y), inset_x : max(inset_x + 1, width - inset_x)]
    high = interior.max(axis=2)
    dark_ratio = float(np.mean(high < 95)) if high.size else 0.0

    edge = max(2, int(min(width, height) * 0.045))
    border_mask = np.zeros((height, width), dtype=bool)
    border_mask[:edge, :] = True
    border_mask[-edge:, :] = True
    border_mask[:, :edge] = True
    border_mask[:, -edge:] = True
    red = panel[:, :, 0].astype(np.int16)
    green = panel[:, :, 1].astype(np.int16)
    blue = panel[:, :, 2].astype(np.int16)
    gold = (
        (red >= 85)
        & (green >= 65)
        & (red >= blue + 12)
        & (green >= blue - 10)
    )
    border_gold_ratio = float(np.mean(gold[border_mask])) if np.any(border_mask) else 0.0
    # 纯暗背景在旧评分中仅凭 dark_ratio 就能达到通过线；卡面必须同时有可见边框。
    if border_gold_ratio < CARD_MIN_BORDER_GOLD_RATIO:
        return 0.0
    dark_score = min(1.0, dark_ratio / 0.62)
    border_score = min(1.0, border_gold_ratio / 0.22)
    return 0.62 * dark_score + 0.38 * border_score


def detect_selection_scene(image: Image.Image, *, layout_id: str) -> SceneObservation:
    """联合按钮和三卡面板判断单帧选择场景。"""

    button_box, blue_ratio = detect_expected_button(image)
    panel_defs = pick_card_panels(image.size)
    if button_box is None:
        transform = LayoutTransform()
        panel_scores = tuple(_panel_score(image, apply_transform(box, image.size, transform)) for box in panel_defs)
        return SceneObservation(
            present=False,
            scene_state="absent",
            score=0.0,
            layout_id=layout_id,
            button_box=None,
            button_blue_ratio=0.0,
            panel_scores=tuple(round(value, 6) for value in panel_scores),
            transform=transform,
            card_residue=any(score >= 0.35 for score in panel_scores),
            reason="selection_scene_not_detected",
        )

    transform = _button_transform(button_box, image.size)
    panel_scores = tuple(_panel_score(image, apply_transform(box, image.size, transform)) for box in panel_defs)
    passed_panels = sum(score >= 0.58 for score in panel_scores)
    panel_mean = sum(panel_scores) / 3.0
    score = min(1.0, 0.42 * min(1.0, blue_ratio / 0.45) + 0.58 * panel_mean)
    present = passed_panels >= 2 and score >= 0.62
    return SceneObservation(
        present=present,
        scene_state="candidate" if present else "absent",
        score=round(score, 6),
        layout_id=layout_id,
        button_box=button_box,
        button_blue_ratio=round(blue_ratio, 6),
        panel_scores=tuple(round(value, 6) for value in panel_scores),
        transform=transform,
        card_residue=any(score >= 0.35 for score in panel_scores),
        reason="" if present else "selection_layout_missing",
    )

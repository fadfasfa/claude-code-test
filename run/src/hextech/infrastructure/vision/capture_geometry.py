"""Vision 局部捕获的纯几何与有效像素合同。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache

from PIL import Image

from hextech.infrastructure.vision.sidecar_common import (
    BLOCKING_MODAL_BUTTON_REGION,
    BLOCKING_MODAL_PANEL_REGION,
)
from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset
from hextech.modules.vision.layout import (
    BUTTON_CENTER_X_TOLERANCE,
    BUTTON_CENTER_Y_TOLERANCE,
    BUTTON_SEARCH_REGION,
    LayoutTransform,
    apply_transform,
    pick_card_panels,
)


Box = tuple[int, int, int, int]
_CAPTURE_METADATA_KEYS = frozenset(
    {
        "hextech_capture_mode",
        "hextech_roi_origin",
        "hextech_roi_size",
        "hextech_client_size",
    }
)
_TRANSFORM_SCALES = (0.90, 1.0, 1.10)


def _relative_box(box: Sequence[float], size: tuple[int, int]) -> Box:
    return apply_transform(box, size, LayoutTransform())


def _transform_envelope(
    boxes: Iterable[Sequence[float]],
    size: tuple[int, int],
) -> list[Box]:
    """枚举按钮校准允许的极值，得到所有受变换 ROI 的保守包络。"""

    transformed: list[Box] = []
    for dx_ratio in (-BUTTON_CENTER_X_TOLERANCE, 0.0, BUTTON_CENTER_X_TOLERANCE):
        for dy_ratio in (-BUTTON_CENTER_Y_TOLERANCE, 0.0, BUTTON_CENTER_Y_TOLERANCE):
            for scale in _TRANSFORM_SCALES:
                transform = LayoutTransform(dx_ratio=dx_ratio, dy_ratio=dy_ratio, scale=scale)
                transformed.extend(apply_transform(box, size, transform) for box in boxes)
    return transformed


@lru_cache(maxsize=32)
def required_capture_bounds(
    client_size: tuple[int, int],
    preset_name: str = "auto",
) -> Box:
    """返回覆盖所有生产识别消费者的 client 坐标联合框。

    未知 preset 会抛出 ``ValueError``，由捕获层改抓同一后端的完整游戏客户区；
    不在纯几何层猜测尚未定义的布局。
    """

    width, height = (int(value) for value in client_size)
    if width <= 0 or height <= 0:
        raise ValueError("invalid_client_size")
    size = (width, height)
    preset = resolve_roi_preset(width, height, preset=preset_name)

    # 场景检测读取完整卡面；模板/OCR/碎片检测读取 icon 与 name ROI，且三者
    # 都会消费由选择按钮推导出的有限平移/缩放。
    transformed_definitions = (
        *pick_card_panels(size),
        *preset.slots,
        *preset.name_slots,
    )
    boxes = _transform_envelope(transformed_definitions, size)
    # 按钮粗定位和阻塞弹窗不消费版式 transform，直接加入实际读取区域。
    boxes.extend(
        _relative_box(box, size)
        for box in (
            BUTTON_SEARCH_REGION,
            BLOCKING_MODAL_PANEL_REGION,
            BLOCKING_MODAL_BUTTON_REGION,
        )
    )
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _coerce_pair(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    try:
        return int(value[0]), int(value[1])
    except (TypeError, ValueError):
        return None


def _coerce_box(value: object) -> Box | None:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    try:
        return tuple(int(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def capture_regions_valid(frame: Image.Image, boxes: Iterable[Box]) -> bool:
    """确认每个 client 坐标 ROI 都有真实捕获像素，而非黑色补边。

    普通离线 Pillow 图没有 capture 元数据时按完整 frame 处理。只要出现任一
    capture 字段，就要求整组元数据自洽，避免残缺元数据把补边伪装成有效像素。
    """

    info = frame.info
    metadata_present = any(key in info for key in _CAPTURE_METADATA_KEYS)
    if metadata_present and not _CAPTURE_METADATA_KEYS.issubset(info):
        return False

    frame_width, frame_height = (int(value) for value in frame.size)
    if min(frame_width, frame_height) <= 0:
        return False
    if metadata_present:
        client_size = _coerce_pair(info.get("hextech_client_size"))
        origin = _coerce_pair(info.get("hextech_roi_origin"))
        roi_size = _coerce_pair(info.get("hextech_roi_size"))
        if client_size != frame.size or origin is None or roi_size is None:
            return False
        origin_x, origin_y = origin
        roi_width, roi_height = roi_size
        if (
            origin_x < 0
            or origin_y < 0
            or roi_width <= 0
            or roi_height <= 0
            or origin_x + roi_width > frame_width
            or origin_y + roi_height > frame_height
        ):
            return False
        valid = (origin_x, origin_y, origin_x + roi_width, origin_y + roi_height)
    else:
        valid = (0, 0, frame_width, frame_height)

    for raw_box in boxes:
        box = _coerce_box(raw_box)
        if box is None:
            return False
        left, top, right, bottom = box
        if left >= right or top >= bottom:
            return False
        if left < valid[0] or top < valid[1] or right > valid[2] or bottom > valid[3]:
            return False
    return True


__all__ = ["capture_regions_valid", "required_capture_bounds"]


def timeline_capture_fields(source) -> dict:
    """有限捕获几何诊断；保留实际范围而不是默认整帧。"""
    origin = list(source.get("capture_roi_origin")) if isinstance(source.get("capture_roi_origin"), list) else []
    size = list(source.get("capture_roi_size")) if isinstance(source.get("capture_roi_size"), list) else []
    valid = [int(origin[0]), int(origin[1]), int(origin[0])+int(size[0]), int(origin[1])+int(size[1])] \
        if len(origin) == 2 and len(size) == 2 else []
    return {"capture_roi_origin": origin, "capture_roi_size": size, "capture_valid_rect": valid,
            "capture_mode": str(source.get("capture_mode") or ""),
            "capture_size": list(source.get("capture_size")) if isinstance(source.get("capture_size"), list) else []}

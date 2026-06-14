"""游戏内 overlay Vision MVP。

本模块只做本地窗口截图、固定 ROI 切片、Pillow 指纹匹配和事件写入。它不读游戏
内存、不注入进程、不修改客户端文件、不自动点击，也不访问远端网络。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab

from processing.overlay_event_channel import OVERLAY_EVENT_FILE, build_overlay_event, write_overlay_event
from processing.overlay_hint_cache import load_overlay_hint_cache, normalize_augment_id, query_overlay_hint
from tools.atomic_io import atomic_write_json

try:
    import win32gui
except ImportError:  # pragma: no cover - Windows 之外只允许离线测试纯函数
    win32gui = None


SLOT_COUNT = 3
FINGERPRINT_SIZE = (48, 48)
NAME_FINGERPRINT_SIZE = (220, 48)
DEFAULT_MIN_CONFIDENCE = 0.80
SCENE_SLOT_MIN_CONFIDENCE = 0.55
DEFAULT_TEXT_MIN_CONFIDENCE = 0.74
DEFAULT_TEXT_MIN_MARGIN = 0.025
TEXT_DECORATION_MAX_WIDTH = 7
TEXT_DECORATION_MIN_HEIGHT_RATIO = 0.55
# top1 与 top2 置信度差下限：平坦/无关画面对所有模板得分接近，靠区分度而不是绝对分拒绝。
DEFAULT_MIN_MARGIN = 0.03
# 置信度达到该值时豁免 margin 门槛：模板库存在近孪生图标（不同海克斯共用近似图），
# 孪生间 margin 恒为 0，但真匹配的绝对置信度极高，显示 top1 名字好过不显示。
TWIN_CONFIDENCE_OVERRIDE = 0.92
# 指纹灰度标准差下限：低于它视为平坦区域（如 ESC 暗色菜单），直接不参与匹配。
FLAT_CROP_STD_THRESHOLD = 12.0
# active 掉到 unstable 后先观察几帧再写隐藏事件，避免识别抖动让 overlay 闪烁。
DEFAULT_EXIT_UNSTABLE_FRAMES = 3
LOL_GAME_WINDOW_TITLE = "League of Legends (TM) Client"
DEFAULT_LOOP_FRAME_INTERVAL_MS = 250
DEFAULT_LOOP_IDLE_INTERVAL_SECONDS = 2.0
DEFAULT_LOOP_HEARTBEAT_SECONDS = 60.0
# --loop 自动转储上限：每个进程最多转储前几个选择窗口，避免长局刷盘。
# 载入画面青色水面会误触发按钮检测吃掉名额，上限需覆盖载入误报 + 真实三选一。
LOOP_DEBUG_DUMP_MAX_WINDOWS = 12
ANCHOR_CALIBRATION_SCHEMA_VERSION = 1
OVERLAY_ANCHOR_CALIBRATION_FILE = OVERLAY_EVENT_FILE.with_name("overlay_anchor_calibration.v1.json")
BUTTON_SEARCH_REGION = (0.20, 0.55, 0.80, 0.95)
BUTTON_MIN_BLUE_PIXELS = 80
BUTTON_MIN_BLUE_RATIO = 0.15
BUTTON_SCAN_DOWNSAMPLE = 4
BUTTON_MIN_SOLIDITY = 0.45
BUTTON_CENTER_MIN_RATIO = 0.42
BUTTON_CENTER_MAX_RATIO = 0.58
# 真按钮垂直中心实测约 0.81H；0.70 下带约束排除强化界面卡片描述区等中部蓝色误锁。
BUTTON_CENTER_MIN_Y_RATIO = 0.70
ANCHOR_SIZE_TOLERANCE_RATIO = 0.03
ANCHOR_RECALIBRATION_INTERVAL_SECONDS = 2.5
BODY_SHARD_KEYWORDS = ("body_shard", "body-shard", "body shard", "锻体", "碎片")

logger = logging.getLogger(__name__)
_ANCHOR_RESCAN_LAST_AT: dict[str, float] = {}


@dataclass(frozen=True)
class RoiPreset:
    """按游戏窗口比例描述三张海克斯卡片 ROI。"""

    name: str
    base_size: tuple[int, int]
    slots: tuple[tuple[float, float, float, float], ...]
    name_slots: tuple[tuple[float, float, float, float], ...]

    def slot_boxes(self, capture_size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
        width, height = capture_size
        boxes: list[tuple[int, int, int, int]] = []
        for left, top, right, bottom in self.slots:
            boxes.append(
                (
                    max(0, min(width - 1, int(round(left * width)))),
                    max(0, min(height - 1, int(round(top * height)))),
                    max(1, min(width, int(round(right * width)))),
                    max(1, min(height, int(round(bottom * height)))),
                )
            )
        return boxes

    def name_boxes(self, capture_size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
        width, height = capture_size
        boxes: list[tuple[int, int, int, int]] = []
        for left, top, right, bottom in self.name_slots:
            boxes.append(
                (
                    max(0, min(width - 1, int(round(left * width)))),
                    max(0, min(height - 1, int(round(top * height)))),
                    max(1, min(width, int(round(right * width)))),
                    max(1, min(height, int(round(bottom * height)))),
                )
            )
        return boxes


@dataclass(frozen=True)
class TemplateEntry:
    augment_id: str
    name: str
    tier: str
    summary: str
    fingerprint: tuple[float, ...]
    name_fingerprint: tuple[float, ...] | None = None


@dataclass(frozen=True)
class AnchorCalibration:
    """一次性定位后的按钮与三槽位 ROI。"""

    preset: str
    capture_size: tuple[int, int]
    button_box: tuple[int, int, int, int]
    slot_boxes: tuple[tuple[int, int, int, int], ...]
    name_boxes: tuple[tuple[int, int, int, int], ...]


# ROI 直接框住三张卡片的图标区（非整卡）：模板与截屏 crop 几何一致才有可比性。
# 2560x1600 数值来自真实选择界面截图标定；16:9 预设按相同布局推算，待 --debug-dump 实测校准。
ROI_PRESETS: dict[str, RoiPreset] = {
    "1920x1080": RoiPreset(
        name="1920x1080",
        base_size=(1920, 1080),
        slots=(
            (0.2625, 0.209, 0.3375, 0.329),
            (0.4625, 0.209, 0.5375, 0.329),
            (0.6625, 0.209, 0.7375, 0.329),
        ),
        name_slots=(
            (0.240, 0.390, 0.355, 0.425),
            (0.440, 0.390, 0.555, 0.425),
            (0.640, 0.390, 0.755, 0.425),
        ),
    ),
    "2560x1440": RoiPreset(
        name="2560x1440",
        base_size=(2560, 1440),
        slots=(
            (0.2625, 0.209, 0.3375, 0.329),
            (0.4625, 0.209, 0.5375, 0.329),
            (0.6625, 0.209, 0.7375, 0.329),
        ),
        name_slots=(
            (0.240, 0.390, 0.355, 0.425),
            (0.440, 0.390, 0.555, 0.425),
            (0.640, 0.390, 0.755, 0.425),
        ),
    ),
    "2560x1600": RoiPreset(
        name="2560x1600",
        base_size=(2560, 1600),
        slots=(
            (0.2625, 0.224, 0.3375, 0.344),
            (0.4625, 0.224, 0.5375, 0.344),
            (0.6625, 0.224, 0.7375, 0.344),
        ),
        name_slots=(
            (0.240, 0.400, 0.355, 0.430),
            (0.440, 0.400, 0.555, 0.430),
            (0.640, 0.400, 0.755, 0.430),
        ),
    ),
}


def _clean_text(value: Any, *, fallback: str = "") -> str:
    return " ".join(str(value or fallback).split()).strip()


def resolve_roi_preset(width: int, height: int, *, preset: str = "auto") -> RoiPreset:
    """按捕获尺寸选择 ROI 预设；16:10 2K 优先落到 2560x1600。"""

    requested = str(preset or "auto").strip().lower()
    if requested != "auto":
        normalized = requested.replace("*", "x")
        if normalized in ROI_PRESETS:
            return ROI_PRESETS[normalized]
        raise ValueError(f"未知 overlay ROI preset: {preset}")

    if width <= 0 or height <= 0:
        return ROI_PRESETS["2560x1600"]
    aspect = width / max(1, height)
    if abs(aspect - 1.6) <= 0.035:
        return ROI_PRESETS["2560x1600"]

    target = (int(width), int(height))
    if target in {preset.base_size for preset in ROI_PRESETS.values()}:
        for roi_preset in ROI_PRESETS.values():
            if roi_preset.base_size == target:
                return roi_preset
    return ROI_PRESETS["2560x1440"] if width >= 2300 else ROI_PRESETS["1920x1080"]


def _clip_box(box: tuple[int, int, int, int], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = box
    return (
        max(0, min(width - 1, int(left))),
        max(0, min(height - 1, int(top))),
        max(1, min(width, int(right))),
        max(1, min(height, int(bottom))),
    )


def _box_to_ratios(box: tuple[int, int, int, int], image_size: tuple[int, int]) -> list[float]:
    width, height = image_size
    left, top, right, bottom = box
    return [
        round(left / max(1, width), 6),
        round(top / max(1, height), 6),
        round(right / max(1, width), 6),
        round(bottom / max(1, height), 6),
    ]


def _box_from_ratios(ratios: Sequence[Any], image_size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    if not isinstance(ratios, Sequence) or isinstance(ratios, (str, bytes)) or len(ratios) != 4:
        return None
    width, height = image_size
    try:
        left, top, right, bottom = [float(value) for value in ratios]
    except (TypeError, ValueError):
        return None
    box = (
        int(round(left * width)),
        int(round(top * height)),
        int(round(right * width)),
        int(round(bottom * height)),
    )
    clipped = _clip_box(box, image_size)
    return clipped if clipped[0] < clipped[2] and clipped[1] < clipped[3] else None


def _is_selection_button_pixel(pixel: tuple[int, int, int] | tuple[int, int, int, int]) -> bool:
    """识别海克斯选择界面底部偏蓝按钮像素；只用于本地截图门控。"""

    red, green, blue = int(pixel[0]), int(pixel[1]), int(pixel[2])
    return (
        blue >= 100
        and green >= 70
        and red <= 110
        and (blue - red) >= 45
        and (green - red) >= 15
        and blue >= green - 20
    )


def _selection_button_blue_ratio(image: Image.Image) -> tuple[int, float]:
    rgb = image.convert("RGB")
    pixels = list(rgb.getdata())
    if not pixels:
        return 0, 0.0
    count = sum(1 for pixel in pixels if _is_selection_button_pixel(pixel))
    return count, count / len(pixels)


def selection_button_present(image: Image.Image) -> bool:
    """固定按钮 ROI 内仍需每轮确认按钮存在，避免无关页面误显示。"""

    count, ratio = _selection_button_blue_ratio(image)
    return count >= BUTTON_MIN_BLUE_PIXELS and ratio >= BUTTON_MIN_BLUE_RATIO


def detect_selection_button_box(frame: Image.Image) -> tuple[int, int, int, int] | None:
    """首次校准时在宽搜索区内定位蓝色按钮，后续只复用固定 ROI。"""

    image = frame.convert("RGB")
    width, height = image.size
    scan_width = max(1, width // BUTTON_SCAN_DOWNSAMPLE)
    scan_height = max(1, height // BUTTON_SCAN_DOWNSAMPLE)
    resampling_nearest = getattr(Image, "Resampling", Image).NEAREST
    scan_image = image.resize((scan_width, scan_height), resampling_nearest)
    scale_x = width / scan_width
    scale_y = height / scan_height

    left_r, top_r, right_r, bottom_r = BUTTON_SEARCH_REGION
    search_box = _clip_box(
        (
            int(round(left_r * scan_width)),
            int(round(top_r * scan_height)),
            int(round(right_r * scan_width)),
            int(round(bottom_r * scan_height)),
        ),
        scan_image.size,
    )
    left, top, right, bottom = search_box
    min_run_width = max(8, int(round(scan_width * 0.06)))
    pixels = scan_image.load()

    row_runs: list[tuple[int, int, int]] = []
    for y in range(top, bottom):
        run_start: int | None = None
        for x in range(left, right):
            if _is_selection_button_pixel(pixels[x, y]):
                if run_start is None:
                    run_start = x
            elif run_start is not None:
                if x - run_start >= min_run_width:
                    row_runs.append((y, run_start, x - 1))
                run_start = None
        if run_start is not None and right - run_start >= min_run_width:
            row_runs.append((y, run_start, right - 1))

    if not row_runs:
        return None

    groups: list[dict[str, int]] = []
    for y, run_left, run_right in row_runs:
        target: dict[str, int] | None = None
        run_center = (run_left + run_right) // 2
        for group in reversed(groups):
            if y - group["max_y"] > 2:
                continue
            group_center = (group["min_x"] + group["max_x"]) // 2
            overlap = min(run_right, group["max_x"]) - max(run_left, group["min_x"])
            if overlap >= min_run_width // 3 or abs(run_center - group_center) <= int(scan_width * 0.035):
                target = group
                break
        if target is None:
            groups.append(
                {
                    "min_x": run_left,
                    "max_x": run_right,
                    "min_y": y,
                    "max_y": y,
                    "rows": 1,
                    "run_pixels": run_right - run_left + 1,
                }
            )
            continue
        target["min_x"] = min(target["min_x"], run_left)
        target["max_x"] = max(target["max_x"], run_right)
        target["min_y"] = min(target["min_y"], y)
        target["max_y"] = max(target["max_y"], y)
        target["rows"] += 1
        target["run_pixels"] += run_right - run_left + 1

    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for group in groups:
        box_width = group["max_x"] - group["min_x"] + 1
        box_height = group["max_y"] - group["min_y"] + 1
        center_x = (group["min_x"] + group["max_x"]) / 2.0
        center_y = (group["min_y"] + group["max_y"]) / 2.0
        if not (0.06 * scan_width <= box_width <= 0.30 * scan_width):
            continue
        if not (0.012 * scan_height <= box_height <= 0.10 * scan_height):
            continue
        if not (BUTTON_CENTER_MIN_RATIO * scan_width <= center_x <= BUTTON_CENTER_MAX_RATIO * scan_width):
            continue
        if center_y < BUTTON_CENTER_MIN_Y_RATIO * scan_height:
            continue
        if group["rows"] < max(3, int(round(scan_height * 0.006))):
            continue
        solidity = group["run_pixels"] / max(1, box_width * box_height)
        if solidity < BUTTON_MIN_SOLIDITY:
            continue
        # 候选越居中、越靠下、连续蓝段面积越大越像底部选择按钮。
        centered_penalty = abs((center_x / scan_width) - 0.5)
        lower_bonus = center_y / scan_height
        area_bonus = min(1.0, group["run_pixels"] / max(1.0, scan_width * scan_height * 0.01))
        score = area_bonus + lower_bonus - centered_penalty
        candidates.append((score, (group["min_x"], group["min_y"], group["max_x"] + 1, group["max_y"] + 1)))

    if not candidates:
        return None

    _, best_box = max(candidates, key=lambda item: item[0])
    pad_x = max(4, int(round(width * 0.008)))
    pad_y = max(3, int(round(height * 0.008)))
    best_left, best_top, best_right, best_bottom = best_box
    original_box = (
        int(round(best_left * scale_x)),
        int(round(best_top * scale_y)),
        int(round(best_right * scale_x)),
        int(round(best_bottom * scale_y)),
    )
    candidate = _clip_box(
        (
            original_box[0] - pad_x,
            original_box[1] - pad_y,
            original_box[2] + pad_x,
            original_box[3] + pad_y,
        ),
        image.size,
    )
    return candidate if selection_button_present(image.crop(candidate)) else None


def _anchor_cache_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else OVERLAY_ANCHOR_CALIBRATION_FILE


def _calibration_size_matches(stored_size: tuple[int, int], current_size: tuple[int, int]) -> bool:
    stored_width, stored_height = stored_size
    current_width, current_height = current_size
    if min(stored_width, stored_height, current_width, current_height) <= 0:
        return False
    return (
        abs(stored_width - current_width) / stored_width <= ANCHOR_SIZE_TOLERANCE_RATIO
        and abs(stored_height - current_height) / stored_height <= ANCHOR_SIZE_TOLERANCE_RATIO
    )


def _coerce_anchor_calibration(payload: Mapping[str, Any], image_size: tuple[int, int]) -> AnchorCalibration | None:
    if payload.get("schema_version") != ANCHOR_CALIBRATION_SCHEMA_VERSION:
        return None
    raw_size = payload.get("capture_size")
    if not isinstance(raw_size, Sequence) or isinstance(raw_size, (str, bytes)) or len(raw_size) != 2:
        return None
    try:
        stored_size = (int(raw_size[0]), int(raw_size[1]))
    except (TypeError, ValueError):
        return None
    if not _calibration_size_matches(stored_size, image_size):
        return None

    button_box = _box_from_ratios(payload.get("button_box") or [], image_size)
    raw_slots = payload.get("slot_boxes")
    if button_box is None or not isinstance(raw_slots, Sequence) or isinstance(raw_slots, (str, bytes)):
        return None
    slot_boxes = [_box_from_ratios(raw_box, image_size) for raw_box in list(raw_slots)[:SLOT_COUNT]]
    if len(slot_boxes) != SLOT_COUNT or any(box is None for box in slot_boxes):
        return None
    raw_name_boxes = payload.get("name_boxes")
    if isinstance(raw_name_boxes, Sequence) and not isinstance(raw_name_boxes, (str, bytes)):
        name_boxes = [_box_from_ratios(raw_box, image_size) for raw_box in list(raw_name_boxes)[:SLOT_COUNT]]
    else:
        try:
            name_boxes = resolve_roi_preset(*image_size, preset=_clean_text(payload.get("preset"), fallback="auto")).name_boxes(image_size)
        except ValueError:
            name_boxes = resolve_roi_preset(*image_size, preset="auto").name_boxes(image_size)
    if len(name_boxes) != SLOT_COUNT or any(box is None for box in name_boxes):
        return None
    return AnchorCalibration(
        preset=_clean_text(payload.get("preset"), fallback="auto"),
        capture_size=image_size,
        button_box=button_box,
        slot_boxes=tuple(box for box in slot_boxes if box is not None),
        name_boxes=tuple(box for box in name_boxes if box is not None),
    )


def load_anchor_calibration(path: str | Path | None = None) -> dict[str, Any] | None:
    try:
        payload = json.loads(_anchor_cache_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_anchor_calibration_payload(frame: Image.Image, *, preset_name: str = "auto") -> dict[str, Any] | None:
    image = frame.convert("RGB")
    button_box = detect_selection_button_box(image)
    if button_box is None:
        return None
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    slot_boxes = preset.slot_boxes(image.size)
    name_boxes = preset.name_boxes(image.size)
    return {
        "schema_version": ANCHOR_CALIBRATION_SCHEMA_VERSION,
        "generated_at": time.time(),
        "source": {"tag": "vision-sidecar", "kind": "overlay_anchor_calibration"},
        "capture_size": [int(image.size[0]), int(image.size[1])],
        "preset": preset.name,
        "button_box": _box_to_ratios(button_box, image.size),
        "slot_boxes": [_box_to_ratios(box, image.size) for box in slot_boxes],
        "name_boxes": [_box_to_ratios(box, image.size) for box in name_boxes],
    }


def write_anchor_calibration(payload: Mapping[str, Any], path: str | Path | None = None) -> Path:
    target = _anchor_cache_path(path)
    atomic_write_json(target, payload, ensure_ascii=False, indent=2)
    return target


def _anchor_recalibration_due(path: str | Path | None = None) -> bool:
    cache_key = str(_anchor_cache_path(path).resolve())
    now = time.monotonic()
    last_at = _ANCHOR_RESCAN_LAST_AT.get(cache_key, 0.0)
    if now - last_at < ANCHOR_RECALIBRATION_INTERVAL_SECONDS:
        return False
    _ANCHOR_RESCAN_LAST_AT[cache_key] = now
    return True


def resolve_anchor_calibration(
    frame: Image.Image,
    *,
    preset_name: str = "auto",
    path: str | Path | None = None,
) -> tuple[AnchorCalibration | None, str]:
    """返回可用校准；缓存 ROI 当前无按钮时节流重扫，避免长期中毒。"""

    image = frame.convert("RGB")
    cached_payload = load_anchor_calibration(path)
    if isinstance(cached_payload, Mapping):
        calibration = _coerce_anchor_calibration(cached_payload, image.size)
        if calibration is not None:
            if selection_button_present(image.crop(calibration.button_box)):
                return calibration, "cached"
            if _anchor_recalibration_due(path):
                payload = build_anchor_calibration_payload(image, preset_name=preset_name)
                if payload is not None:
                    write_anchor_calibration(payload, path)
                    recalibrated = _coerce_anchor_calibration(payload, image.size)
                    if recalibrated is not None:
                        return recalibrated, "recalibrated"
            return calibration, "cached"

    payload = build_anchor_calibration_payload(image, preset_name=preset_name)
    if payload is None:
        return None, "anchor_missing"
    write_anchor_calibration(payload, path)
    calibration = _coerce_anchor_calibration(payload, image.size)
    return calibration, "calibrated" if calibration is not None else "anchor_missing"


def _resampling_lanczos() -> int:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _fit_mask_to_canvas(mask: Image.Image, size: tuple[int, int]) -> Image.Image | None:
    """按前景 bbox 紧裁并等比放入固定画布，避免透明边或卡面背景主导匹配。"""

    gray = mask.convert("L")
    binary = gray.point(lambda value: 255 if value >= 32 else 0)
    bbox = binary.getbbox()
    if bbox is None:
        return None
    foreground_ratio = sum(1 for value in binary.getdata() if value) / max(1, binary.width * binary.height)
    if foreground_ratio >= 0.98:
        return None
    cropped = gray.crop(bbox)
    target_width, target_height = size
    scale = min(target_width / max(1, cropped.width), target_height / max(1, cropped.height))
    resized = cropped.resize(
        (max(1, int(round(cropped.width * scale))), max(1, int(round(cropped.height * scale)))),
        _resampling_lanczos(),
    )
    canvas = Image.new("L", size, 0)
    canvas.paste(resized, ((target_width - resized.width) // 2, (target_height - resized.height) // 2))
    return canvas


def _alpha_or_luminance_mask(image: Image.Image) -> Image.Image:
    """模板优先取 alpha 作为字形轮廓；无 alpha 时退化为亮度前景。"""

    if image.mode in {"RGBA", "LA"}:
        alpha = image.getchannel("A")
        alpha_bbox = alpha.point(lambda value: 255 if value >= 32 else 0).getbbox()
        if alpha_bbox is not None:
            alpha_ratio = sum(1 for value in alpha.getdata() if value >= 32) / max(1, alpha.width * alpha.height)
            if alpha_ratio < 0.98:
                return alpha
    if image.mode == "P" and "transparency" in image.info:
        alpha = image.convert("RGBA").getchannel("A")
        alpha_bbox = alpha.point(lambda value: 255 if value >= 32 else 0).getbbox()
        if alpha_bbox is not None:
            alpha_ratio = sum(1 for value in alpha.getdata() if value >= 32) / max(1, alpha.width * alpha.height)
            if alpha_ratio < 0.98:
                return alpha
    gray = image.convert("L")
    return gray.point(lambda value: 255 if value >= 24 else 0)


def _bright_glyph_mask(image: Image.Image) -> Image.Image:
    """从深色卡面截图中分割棱彩/浅色字形，压掉暗纹理背景。"""

    rgb = image.convert("RGB")
    mask = Image.new("L", rgb.size, 0)
    source = rgb.load()
    target = mask.load()
    width, height = rgb.size
    for y in range(height):
        for x in range(width):
            red, green, blue = source[x, y]
            high = max(red, green, blue)
            low = min(red, green, blue)
            # 真机图标/卡名明显亮于暗卡面；高饱和棱彩和浅奶油字都应保留。
            if high >= 112 or (high >= 78 and high - low >= 34):
                target[x, y] = 255
    return mask.filter(ImageFilter.MaxFilter(3))


def _name_text_mask(image: Image.Image) -> Image.Image:
    """名字区专用 mask：去掉卡框竖线等窄高装饰，只保留中文标题字形。"""

    mask = _bright_glyph_mask(image)
    width, height = mask.size
    pixels = mask.load()
    cleaned = Image.new("L", mask.size, 0)
    cleaned_pixels = cleaned.load()
    seen: set[tuple[int, int]] = set()

    for start_y in range(height):
        for start_x in range(width):
            if pixels[start_x, start_y] < 128 or (start_x, start_y) in seen:
                continue
            component = [(start_x, start_y)]
            seen.add((start_x, start_y))
            xs: list[int] = []
            ys: list[int] = []
            for x, y in component:
                xs.append(x)
                ys.append(y)
                for nx in (x - 1, x, x + 1):
                    for ny in (y - 1, y, y + 1):
                        if nx < 0 or nx >= width or ny < 0 or ny >= height or (nx, ny) in seen:
                            continue
                        if pixels[nx, ny] >= 128:
                            seen.add((nx, ny))
                            component.append((nx, ny))

            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            component_width = max_x - min_x + 1
            component_height = max_y - min_y + 1
            touches_edge = min_x <= 2 or max_x >= width - 3
            if len(component) < 8 or component_height < 4:
                continue
            if component_width <= TEXT_DECORATION_MAX_WIDTH and component_height >= height * TEXT_DECORATION_MIN_HEIGHT_RATIO:
                continue
            if touches_edge and (component_height >= height * 0.45 or component_width <= TEXT_DECORATION_MAX_WIDTH + 1):
                continue
            for x, y in component:
                cleaned_pixels[x, y] = 255

    return cleaned


def _mask_levels(mask: Image.Image, size: tuple[int, int]) -> list[int]:
    fitted = _fit_mask_to_canvas(mask, size)
    if fitted is None:
        return []
    edges = fitted.filter(ImageFilter.FIND_EDGES)
    return list(fitted.getdata()) + list(edges.getdata())


def _icon_levels(image: Image.Image, *, template: bool) -> list[int]:
    mask = _alpha_or_luminance_mask(image) if template else _bright_glyph_mask(image)
    return _mask_levels(mask, FINGERPRINT_SIZE)


def _text_levels(image: Image.Image) -> list[int]:
    return _mask_levels(_name_text_mask(image), NAME_FINGERPRINT_SIZE)


def _grayscale_levels(image: Image.Image) -> list[int]:
    """兼容旧调用名：现在返回图标字形轮廓指纹输入，而非整块灰度。"""

    return _icon_levels(image, template=False)


def _levels_std(levels: Sequence[int]) -> float:
    if not levels:
        return 0.0
    mean = sum(levels) / len(levels)
    variance = sum((value - mean) ** 2 for value in levels) / len(levels)
    return variance**0.5


def _normalized_fingerprint(levels: Sequence[int]) -> tuple[float, ...] | None:
    """零均值/单位方差归一化指纹；平坦图像（纯色、暗面板）没有有效指纹。"""

    if not levels:
        return None
    mean = sum(levels) / len(levels)
    std = _levels_std(levels)
    if std < 1e-6:
        return None
    return tuple((value - mean) / std for value in levels)


def _fingerprint(image: Image.Image, *, template: bool = False) -> tuple[float, ...] | None:
    return _normalized_fingerprint(_icon_levels(image, template=template))


def _fingerprint_confidence(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """归一化互相关（NCC）映射到 [0,1]；只对形状敏感，不受亮度/色调影响。"""

    if not left or not right or len(left) != len(right):
        return 0.0
    correlation = sum(a * b for a, b in zip(left, right)) / len(left)
    return max(0.0, min(1.0, (correlation + 1.0) / 2.0))


def _load_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for font_path in (
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ):
        try:
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render_name_mask(name: str) -> Image.Image | None:
    clean_name = _clean_text(name)
    if not clean_name:
        return None
    canvas = Image.new("L", NAME_FINGERPRINT_SIZE, 0)
    draw = ImageDraw.Draw(canvas)
    font_size = 34
    font = _load_cjk_font(font_size)
    max_width = int(NAME_FINGERPRINT_SIZE[0] * 0.94)
    while font_size >= 16:
        bbox = draw.textbbox((0, 0), clean_name, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= max_width:
            break
        font_size -= 2
        font = _load_cjk_font(font_size)
    bbox = draw.textbbox((0, 0), clean_name, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    draw.text(
        ((NAME_FINGERPRINT_SIZE[0] - text_width) // 2 - bbox[0], (NAME_FINGERPRINT_SIZE[1] - text_height) // 2 - bbox[1]),
        clean_name,
        fill=255,
        font=font,
    )
    return canvas.point(lambda value: 255 if value >= 32 else 0).filter(ImageFilter.MaxFilter(3))


def _name_fingerprint(name: str) -> tuple[float, ...] | None:
    mask = _render_name_mask(name)
    if mask is None:
        return None
    return _normalized_fingerprint(_mask_levels(mask, NAME_FINGERPRINT_SIZE))


def _load_manifest_by_name(root: Path) -> dict[str, Mapping[str, Any]]:
    manifest_path = root / "data" / "static" / "Augment_Icon_Manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        name = _clean_text(item.get("name"))
        if not name:
            continue
        result.setdefault(name, item)
        result.setdefault(normalize_augment_id(name), item)
    return result


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    """从内存模板构建匹配索引；测试和真实模板加载共用这条纯函数。"""

    index: list[TemplateEntry] = []
    for augment_id, payload in raw_templates.items():
        if not isinstance(payload, Mapping):
            continue
        image = payload.get("image")
        if not isinstance(image, Image.Image):
            continue
        normalized_id = normalize_augment_id(augment_id, str(payload.get("name") or ""))
        if not normalized_id:
            continue
        fingerprint = _fingerprint(image, template=True)
        if fingerprint is None:
            # 平坦模板（纯色图）没有可匹配形状，留着只会制造假阳性。
            continue
        name = _clean_text(payload.get("name"), fallback=normalized_id)
        index.append(
            TemplateEntry(
                augment_id=normalized_id,
                name=name,
                tier=_clean_text(payload.get("tier"), fallback="Unknown"),
                summary=_clean_text(payload.get("summary"), fallback="本地模板识别结果"),
                fingerprint=fingerprint,
                name_fingerprint=_name_fingerprint(name),
            )
        )
    return index


def load_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> list[TemplateEntry]:
    """从随包稳定资源加载海克斯图标模板，不触发远端抓取。"""

    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1]
    mapping_path = root / "data" / "indexes" / "augment.name-to-icon.v1.json"
    try:
        name_to_icon = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(name_to_icon, Mapping):
        return []

    manifest_by_name = _load_manifest_by_name(root)
    raw_templates: dict[str, Mapping[str, Any]] = {}
    for name, icon_path in name_to_icon.items():
        clean_name = _clean_text(name)
        relative_icon = str(icon_path or "").lstrip("/")
        if not clean_name or not relative_icon:
            continue
        path = (root / relative_icon).resolve()
        try:
            if root.resolve() not in path.parents:
                continue
            digest = hashlib.md5(path.read_bytes()).hexdigest()
            with Image.open(path) as opened:
                image = opened.copy()
        except OSError:
            continue
        hint_result = query_overlay_hint(hint_cache or {}, clean_name)
        hint = hint_result.get("hint") if hint_result.get("ok") and isinstance(hint_result.get("hint"), Mapping) else {}
        manifest_item = manifest_by_name.get(clean_name) or manifest_by_name.get(normalize_augment_id(clean_name)) or {}
        template_id = normalize_augment_id(
            hint.get("augment_id") or manifest_item.get("augment_name_id") or manifest_item.get("cdragon_id") or clean_name,
            clean_name,
        )
        raw_templates[template_id] = {
            "name": _clean_text(hint.get("name") or manifest_item.get("name"), fallback=clean_name),
            "tier": _clean_text(hint.get("tier") or manifest_item.get("tier"), fallback="Unknown"),
            "summary": _clean_text(
                hint.get("summary") or manifest_item.get("tooltip_plain") or manifest_item.get("description"),
                fallback="本地模板识别结果",
            ),
            "image": image,
            "digest": digest,
        }
    return build_template_index(raw_templates)


@dataclass(frozen=True)
class _RankMatrices:
    """模板指纹的向量化缓存：把逐模板 Python NCC 循环换成单次矩阵乘。"""

    index_ref: Sequence[TemplateEntry]  # 强引用，防止 id() 复用导致缓存串味
    icon_templates: tuple[TemplateEntry, ...]
    icon_matrix: np.ndarray  # (N_icon, D_icon)，行=已归一化的图标指纹
    name_templates: tuple[TemplateEntry, ...]
    name_matrix: np.ndarray  # (N_name, D_name)，行=已归一化的名字指纹


# 单进程通常只有一份长驻 template_index；缓存按 id 命中，限容防 eval/测试反复建表泄漏。
_RANK_MATRIX_CACHE: "dict[int, _RankMatrices]" = {}
_RANK_MATRIX_CACHE_MAX = 4


def _stack_fingerprints(rows: Sequence[Sequence[float]]) -> np.ndarray:
    if not rows:
        return np.empty((0, 0), dtype=np.float32)
    return np.asarray(rows, dtype=np.float32)


def _rank_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    key = id(template_index)
    cached = _RANK_MATRIX_CACHE.get(key)
    if cached is not None and cached.index_ref is template_index:
        return cached
    icon_templates = tuple(template_index)
    icon_matrix = _stack_fingerprints([template.fingerprint for template in icon_templates])
    name_templates = tuple(t for t in template_index if t.name_fingerprint is not None)
    name_matrix = _stack_fingerprints([t.name_fingerprint for t in name_templates])
    entry = _RankMatrices(template_index, icon_templates, icon_matrix, name_templates, name_matrix)
    if key not in _RANK_MATRIX_CACHE and len(_RANK_MATRIX_CACHE) >= _RANK_MATRIX_CACHE_MAX:
        _RANK_MATRIX_CACHE.pop(next(iter(_RANK_MATRIX_CACHE)))
    _RANK_MATRIX_CACHE[key] = entry
    return entry


def _rank_with_matrix(
    crop_fingerprint: Sequence[float],
    templates: Sequence[TemplateEntry],
    matrix: np.ndarray,
) -> list[tuple[TemplateEntry, float]]:
    """向量化 NCC：指纹已零均值/单位方差，置信度 = clip((M·v / D + 1) / 2)。"""

    if not templates or matrix.size == 0:
        return []
    vec = np.asarray(crop_fingerprint, dtype=np.float32)
    if vec.shape[0] != matrix.shape[1]:
        return []
    correlation = (matrix @ vec) / vec.shape[0]
    confidence = np.clip((correlation + 1.0) / 2.0, 0.0, 1.0)
    # argsort 升序取负 = 置信度降序；stable 保持模板原顺序，与旧 sorted(reverse=True) 对齐。
    order = np.argsort(-confidence, kind="stable")
    return [(templates[int(i)], float(confidence[int(i)])) for i in order]


def _rank_templates(
    crop: Image.Image,
    template_index: Sequence[TemplateEntry],
) -> tuple[float, list[tuple[TemplateEntry, float]]]:
    """返回图标 crop 形状标准差与按置信度降序的模板候选；平坦 crop 没有候选。"""

    levels = _grayscale_levels(crop)
    crop_std = _levels_std(levels)
    crop_fingerprint = _normalized_fingerprint(levels)
    if crop_fingerprint is None:
        return crop_std, []
    matrices = _rank_matrices(template_index)
    return crop_std, _rank_with_matrix(crop_fingerprint, matrices.icon_templates, matrices.icon_matrix)


def _rank_name_templates(
    crop: Image.Image | None,
    template_index: Sequence[TemplateEntry],
) -> tuple[float, list[tuple[TemplateEntry, float]]]:
    if crop is None:
        return 0.0, []
    levels = _text_levels(crop)
    crop_std = _levels_std(levels)
    crop_fingerprint = _normalized_fingerprint(levels)
    if crop_fingerprint is None:
        return crop_std, []
    matrices = _rank_matrices(template_index)
    return crop_std, _rank_with_matrix(crop_fingerprint, matrices.name_templates, matrices.name_matrix)


def _slot_match_decision(
    crop_std: float,
    confidence: float,
    margin: float,
    *,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> bool:
    """槽位判定：平坦 crop 与低置信度直接拒绝；区分度不足时只接受极高置信度（孪生图标）。"""

    if crop_std < FLAT_CROP_STD_THRESHOLD or confidence < min_confidence:
        return False
    return margin >= min_margin or confidence >= TWIN_CONFIDENCE_OVERRIDE


def _top_candidates(ranked: Sequence[tuple[TemplateEntry, float]]) -> list[dict[str, Any]]:
    return [
        {
            "augment_id": template.augment_id,
            "name": template.name,
            "confidence": confidence,
        }
        for template, confidence in list(ranked)[:3]
    ]


def _candidate_margin(ranked: Sequence[tuple[TemplateEntry, float]]) -> float:
    if not ranked:
        return 0.0
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    return ranked[0][1] - runner_up


def _channels_payload(
    *,
    icon_crop_std: float,
    icon_ranked: Sequence[tuple[TemplateEntry, float]],
    name_crop_std: float,
    name_ranked: Sequence[tuple[TemplateEntry, float]],
) -> dict[str, Any]:
    return {
        "icon": {
            "crop_std": round(icon_crop_std, 3),
            "margin": round(_candidate_margin(icon_ranked), 4),
            "top_candidates": _top_candidates(icon_ranked),
        },
        "text": {
            "crop_std": round(name_crop_std, 3),
            "margin": round(_candidate_margin(name_ranked), 4),
            "top_candidates": _top_candidates(name_ranked),
        },
    }


def _slot_result(
    *,
    slot_index: int,
    state: str,
    template: TemplateEntry | None,
    confidence: float,
    diagnostic: str,
    top_candidates: Sequence[dict[str, Any]],
    channels: Mapping[str, Any],
    summary: str,
) -> dict[str, Any]:
    return {
        "slot": slot_index,
        "state": state,
        "augment_id": template.augment_id if template is not None and state == "ready" else "",
        "name": template.name if template is not None and state == "ready" else "",
        "tier": template.tier if template is not None and state == "ready" else "",
        "summary": template.summary if template is not None and state == "ready" else summary,
        "confidence": confidence,
        "diagnostic": diagnostic,
        "top_candidates": list(top_candidates),
        "channels": dict(channels),
    }


def _detect_slot(
    frame: Image.Image,
    box: tuple[int, int, int, int],
    slot_index: int,
    template_index: Sequence[TemplateEntry],
    *,
    name_box: tuple[int, int, int, int] | None = None,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
    min_text_confidence: float = DEFAULT_TEXT_MIN_CONFIDENCE,
    min_text_margin: float = DEFAULT_TEXT_MIN_MARGIN,
) -> dict[str, Any]:
    crop_std, ranked = _rank_templates(frame.crop(box), template_index)
    name_crop_std, name_ranked = _rank_name_templates(frame.crop(name_box) if name_box is not None else None, template_index)
    icon_template, icon_confidence = ranked[0] if ranked else (None, 0.0)
    name_template, name_confidence = name_ranked[0] if name_ranked else (None, 0.0)
    icon_margin = _candidate_margin(ranked)
    name_margin = _candidate_margin(name_ranked)
    icon_candidates = _top_candidates(ranked)
    name_candidates = _top_candidates(name_ranked)
    channels = _channels_payload(
        icon_crop_std=crop_std,
        icon_ranked=ranked,
        name_crop_std=name_crop_std,
        name_ranked=name_ranked,
    )
    has_name_channel = name_box is not None and bool(name_ranked)
    text_ready = (
        name_template is not None
        and name_crop_std >= FLAT_CROP_STD_THRESHOLD
        and name_confidence >= min_text_confidence
        and (name_margin >= min_text_margin or name_confidence >= TWIN_CONFIDENCE_OVERRIDE)
    )
    icon_ready = (
        icon_template is not None
        and _slot_match_decision(crop_std, icon_confidence, icon_margin, min_confidence=min_confidence, min_margin=min_margin)
    )
    candidates = name_candidates if name_candidates else icon_candidates
    if crop_std < FLAT_CROP_STD_THRESHOLD:
        return _slot_result(
            slot_index=slot_index,
            state="empty",
            template=None,
            confidence=max(icon_confidence, name_confidence),
            diagnostic="flat_crop",
            top_candidates=candidates,
            channels=channels,
            summary="未检测到选择卡片",
        )
    if text_ready:
        diagnostic = "text_icon_agree" if icon_template is not None and icon_template.name == name_template.name else "text_channel_ready"
        if icon_template is not None and icon_template.name != name_template.name and icon_confidence >= SCENE_SLOT_MIN_CONFIDENCE:
            diagnostic = "text_icon_disagree"
        return _slot_result(
            slot_index=slot_index,
            state="ready",
            template=name_template,
            confidence=name_confidence,
            diagnostic=diagnostic,
            top_candidates=name_candidates,
            channels=channels,
            summary="",
        )
    if icon_ready and not has_name_channel:
        return _slot_result(
            slot_index=slot_index,
            state="ready",
            template=icon_template,
            confidence=icon_confidence,
            diagnostic="icon_channel_ready",
            top_candidates=icon_candidates,
            channels=channels,
            summary="",
        )
    if icon_template is None:
        return _slot_result(
            slot_index=slot_index,
            state="detecting",
            template=None,
            confidence=max(icon_confidence, name_confidence),
            diagnostic="template_candidate_missing",
            top_candidates=candidates,
            channels=channels,
            summary="识别中",
        )
    if icon_confidence < min_confidence and name_confidence < min_text_confidence:
        state = "low_confidence" if max(icon_confidence, name_confidence) >= SCENE_SLOT_MIN_CONFIDENCE else "detecting"
        return _slot_result(
            slot_index=slot_index,
            state=state,
            template=None,
            confidence=max(icon_confidence, name_confidence),
            diagnostic="confidence_below_threshold",
            top_candidates=candidates,
            channels=channels,
            summary="候选置信度不足",
        )
    diagnostic = "icon_only_low_confidence" if icon_ready else "margin_below_threshold"
    return _slot_result(
        slot_index=slot_index,
        state="low_confidence",
        template=None,
        confidence=max(icon_confidence, name_confidence),
        diagnostic=diagnostic,
        top_candidates=candidates,
        channels=channels,
        summary="候选区分度不足",
    )


def _slot_crop_fingerprint(frame: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, ...] | None:
    return _fingerprint(frame.crop(box))


def _slots_look_like_body_shards(frame: Image.Image, slot_boxes: Sequence[tuple[int, int, int, int]]) -> bool:
    """锻体碎片三选一通常呈现三张同类碎片图，当前 MVP 先按三槽高度相似隐藏。"""

    fingerprints = [_slot_crop_fingerprint(frame, box) for box in list(slot_boxes)[:SLOT_COUNT]]
    if len(fingerprints) != SLOT_COUNT or any(fingerprint is None for fingerprint in fingerprints):
        return False
    similarities = [
        _fingerprint_confidence(fingerprints[0], fingerprints[1]),
        _fingerprint_confidence(fingerprints[0], fingerprints[2]),
        _fingerprint_confidence(fingerprints[1], fingerprints[2]),
    ]
    return min(similarities) >= 0.97


def _slots_have_body_shard_keywords(slots: Sequence[Mapping[str, Any]]) -> bool:
    matched = 0
    for slot in list(slots)[:SLOT_COUNT]:
        text = " ".join(
            str(slot.get(key) or "").lower()
            for key in ("augment_id", "name", "summary", "diagnostic")
        )
        candidates = slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else []
        for candidate in candidates[:1]:
            if isinstance(candidate, Mapping):
                text += " " + " ".join(
                    str(candidate.get(key) or "").lower()
                    for key in ("augment_id", "name")
                )
        if any(keyword in text for keyword in BODY_SHARD_KEYWORDS):
            matched += 1
    return matched == SLOT_COUNT


def _scene_active_from_slots(slots: Sequence[Mapping[str, Any]]) -> bool:
    """至少一张卡识别 ready 才算 active：低置信度槽位只会渲染空占位框，
    真机证明载入画面杂乱内容也能蹭到 low_confidence，不能作为显示依据。"""

    return any(slot.get("state") == "ready" for slot in slots)


def detect_overlay_choices(
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    calibration_path: str | Path | None = None,
) -> dict[str, Any]:
    """按钮存在才识别三槽位；active 表示可显示的海克斯选择场景。"""

    started_at = time.perf_counter()
    image = frame.convert("RGB")
    calibration, calibration_source = resolve_anchor_calibration(
        image,
        preset_name=preset_name,
        path=calibration_path,
    )
    if calibration is None:
        event = _build_loop_inactive_event("anchor_missing")
        event["source"].update(
            {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "preset": preset_name,
                "capture_size": [int(image.size[0]), int(image.size[1])],
                "calibration": calibration_source,
            }
        )
        return event

    button_crop = image.crop(calibration.button_box)
    if not selection_button_present(button_crop):
        event = _build_loop_inactive_event("selection_button_missing")
        event["source"].update(
            {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "preset": calibration.preset,
                "capture_size": [int(image.size[0]), int(image.size[1])],
                "calibration": calibration_source,
            }
        )
        return event

    if _slots_look_like_body_shards(image, calibration.slot_boxes):
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "preset": calibration.preset,
                "capture_size": [int(image.size[0]), int(image.size[1])],
                "calibration": calibration_source,
                "reason": "body_shard_only",
            }
        )
        return event

    slots = [
        _detect_slot(
            image,
            box,
            index,
            template_index,
            name_box=calibration.name_boxes[index] if index < len(calibration.name_boxes) else None,
            min_confidence=min_confidence,
        )
        for index, box in enumerate(calibration.slot_boxes)
    ]
    if _slots_have_body_shard_keywords(slots):
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "preset": calibration.preset,
                "capture_size": [int(image.size[0]), int(image.size[1])],
                "calibration": calibration_source,
                "reason": "body_shard_only",
            }
        )
        return event

    active = _scene_active_from_slots(slots)
    event = build_overlay_event(
        slots,
        source_tag="vision-sidecar",
        selection_type="hextech",
        active=active,
    )
    event["source"].update(
        {
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "preset": calibration.preset,
            "capture_size": [int(image.size[0]), int(image.size[1])],
            "calibration": calibration_source,
            "ready_slots": sum(1 for slot in slots if slot.get("state") == "ready"),
            "reason": "" if active else "selection_scene_not_detected",
        }
    )
    return event


def _slot_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    signature: list[str] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        candidates = slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else []
        top_name = ""
        if candidates and isinstance(candidates[0], Mapping):
            top_name = str(candidates[0].get("augment_id") or candidates[0].get("name") or "")
        signature.append(f"{slot.get('state') or 'empty'}:{slot.get('augment_id') or top_name}")
    return tuple(signature)


def _loop_event_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    if bool(event_payload.get("active")):
        return ("active", *_slot_signature(event_payload))
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    return ("inactive", str(source.get("reason") or "inactive"))


def _inactive_reason(event_payload: Mapping[str, Any]) -> str:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    return str(source.get("reason") or "")


def _build_loop_inactive_event(reason: str) -> dict[str, Any]:
    event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    event["source"].update({"reason": reason})
    return event


def should_write_loop_event(
    event_payload: Mapping[str, Any],
    *,
    last_signature: tuple[str, ...] | None,
    last_write_at: float,
    now: float,
    heartbeat_seconds: float = DEFAULT_LOOP_HEARTBEAT_SECONDS,
) -> bool:
    """判断 loop 是否需要写事件，避免每帧刷新运行态文件。"""

    signature = _loop_event_signature(event_payload)
    if bool(event_payload.get("active")):
        return signature != last_signature or (now - float(last_write_at or 0.0)) >= float(heartbeat_seconds)
    return last_signature is None or signature != last_signature or (bool(last_signature) and last_signature[0] == "active")


def should_defer_unstable_event(
    last_signature: tuple[str, ...] | None,
    unstable_streak: int,
    *,
    exit_unstable_frames: int = DEFAULT_EXIT_UNSTABLE_FRAMES,
) -> bool:
    """active 掉到 unstable 的前几帧先不写隐藏事件，吸收识别抖动避免 overlay 闪烁。"""

    if not last_signature or last_signature[0] != "active":
        return False
    return int(unstable_streak) < max(1, int(exit_unstable_frames))


def stabilize_detections(events: Sequence[Mapping[str, Any]], *, required_frames: int = 2) -> dict[str, Any]:
    """要求连续多帧槽位 ID 一致，避免动画早期误输出。"""

    recent = list(events)[-max(1, int(required_frames)) :]
    if len(recent) < max(1, int(required_frames)):
        return build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    first_signature = _slot_signature(recent[0])
    if (
        bool(first_signature)
        and all(bool(event.get("active")) for event in recent)
        and all(_slot_signature(event) == first_signature for event in recent)
    ):
        stable = dict(recent[-1])
        stable["source"] = dict(stable.get("source") if isinstance(stable.get("source"), Mapping) else {})
        stable["source"]["stable_frames"] = len(recent)
        return stable
    if all(not bool(event.get("active")) for event in recent):
        first_reason = _inactive_reason(recent[0])
        if first_reason and all(_inactive_reason(event) == first_reason for event in recent):
            stable = dict(recent[-1])
            stable["source"] = dict(stable.get("source") if isinstance(stable.get("source"), Mapping) else {})
            stable["source"]["stable_frames"] = len(recent)
            return stable
    unstable = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    unstable["source"].update({"stable_frames": 0, "reason": "unstable"})
    return unstable


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logger.debug("设置 Vision sidecar DPI 感知失败。", exc_info=True)


def _find_lol_game_window() -> tuple[int, tuple[int, int, int, int]] | None:
    if win32gui is None:
        return None
    hwnd = win32gui.FindWindow(None, LOL_GAME_WINDOW_TITLE)
    if not hwnd or not win32gui.IsWindowVisible(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right <= left or bottom <= top:
        return None
    return int(hwnd), (int(left), int(top), int(right), int(bottom))


def _find_lol_game_rect() -> tuple[int, int, int, int] | None:
    target = _find_lol_game_window()
    return target[1] if target is not None else None


def _is_lol_game_foreground(hwnd: int | None) -> bool:
    if win32gui is None or not hwnd:
        return False
    try:
        return int(win32gui.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def _capture_lol_game_rect(rect: tuple[int, int, int, int]) -> Image.Image | None:
    try:
        return ImageGrab.grab(bbox=rect).convert("RGB")
    except OSError:
        return None


def capture_lol_game_frame() -> Image.Image | None:
    """截取 LoL 游戏窗口矩形；找不到窗口时返回 None。"""

    rect = _find_lol_game_rect()
    if rect is None:
        return None
    return _capture_lol_game_rect(rect)


def _write_debug_dump(
    dump_dir: str | Path,
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
) -> Path:
    """保存单帧、各槽位 ROI crop 与 top3 候选分数，用于离线校准 ROI 和阈值。"""

    target = Path(dump_dir)
    target.mkdir(parents=True, exist_ok=True)
    image = frame.convert("RGB")
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    image.save(target / "frame.png")
    report: dict[str, Any] = {
        "generated_at": time.time(),
        "preset": preset.name,
        "capture_size": [int(image.size[0]), int(image.size[1])],
        "flat_crop_std_threshold": FLAT_CROP_STD_THRESHOLD,
        "min_margin": DEFAULT_MIN_MARGIN,
        "button": {"box": [], "present": False, "blue_pixels": 0, "blue_ratio": 0.0},
        "slots": [],
    }
    calibration_payload = build_anchor_calibration_payload(image, preset_name=preset_name)
    calibration = _coerce_anchor_calibration(calibration_payload, image.size) if calibration_payload is not None else None
    calibration_source = "debug-scan" if calibration is not None else "anchor_missing"
    slot_boxes = preset.slot_boxes(image.size)
    if calibration is not None:
        button_crop = image.crop(calibration.button_box)
        button_crop.save(target / "anchor_button.png")
        blue_pixels, blue_ratio = _selection_button_blue_ratio(button_crop)
        report["button"] = {
            "box": list(calibration.button_box),
            "present": selection_button_present(button_crop),
            "blue_pixels": blue_pixels,
            "blue_ratio": round(blue_ratio, 4),
            "calibration": calibration_source,
        }
        slot_boxes = list(calibration.slot_boxes)
        name_boxes = list(calibration.name_boxes)
    else:
        report["button"] = {"box": [], "present": False, "blue_pixels": 0, "blue_ratio": 0.0, "calibration": calibration_source}
        name_boxes = preset.name_boxes(image.size)
    for index, box in enumerate(slot_boxes):
        crop = image.crop(box)
        crop.save(target / f"slot_{index}.png")
        name_box = name_boxes[index] if index < len(name_boxes) else None
        if name_box is not None:
            image.crop(name_box).save(target / f"name_{index}.png")
        crop_std, ranked = _rank_templates(crop, template_index)
        name_crop_std, name_ranked = _rank_name_templates(image.crop(name_box) if name_box is not None else None, template_index)
        slot_result = _detect_slot(
            image,
            box,
            index,
            template_index,
            name_box=name_box,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
        )
        report["slots"].append(
            {
                "slot": index,
                "box": list(box),
                "name_box": list(name_box) if name_box is not None else [],
                "crop_std": round(crop_std, 3),
                "top_candidates": [
                    {"augment_id": template.augment_id, "name": template.name, "confidence": round(confidence, 4)}
                    for template, confidence in ranked[:3]
                ],
                "text_crop_std": round(name_crop_std, 3),
                "text_top_candidates": [
                    {"augment_id": template.augment_id, "name": template.name, "confidence": round(confidence, 4)}
                    for template, confidence in name_ranked[:3]
                ],
                "decision": {
                    "state": slot_result.get("state"),
                    "augment_id": slot_result.get("augment_id"),
                    "name": slot_result.get("name"),
                    "confidence": round(float(slot_result.get("confidence") or 0.0), 4),
                    "diagnostic": slot_result.get("diagnostic"),
                },
            }
        )
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def run_once(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_frames: int = 2,
    frame_interval_ms: int = 80,
    debug_dump_dir: str | Path | None = None,
) -> dict[str, Any]:
    """执行一次短窗口识别；无 LoL 窗口时写入 inactive 诊断事件。"""

    _set_dpi_awareness()
    hint_cache = load_overlay_hint_cache()
    template_index = load_default_template_index(hint_cache=hint_cache)
    if not template_index:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "template_missing"})
    else:
        detections: list[dict[str, Any]] = []
        dumped = False
        for _ in range(max(1, int(required_frames))):
            frame = capture_lol_game_frame()
            if frame is None:
                event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
                event["source"].update({"reason": "capture_unavailable"})
                break
            if debug_dump_dir and not dumped:
                _write_debug_dump(debug_dump_dir, frame, template_index, preset_name=preset)
                dumped = True
            detections.append(
                detect_overlay_choices(
                    frame,
                    template_index,
                    preset_name=preset,
                    min_confidence=min_confidence,
                )
            )
            time.sleep(max(0, int(frame_interval_ms)) / 1000.0)
        else:
            event = stabilize_detections(detections, required_frames=required_frames)

    if write_event:
        write_overlay_event(event, event_path)
    return event


def run_loop(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_frames: int = 2,
    frame_interval_ms: int = DEFAULT_LOOP_FRAME_INTERVAL_MS,
    idle_interval_seconds: float = DEFAULT_LOOP_IDLE_INTERVAL_SECONDS,
    heartbeat_seconds: float = DEFAULT_LOOP_HEARTBEAT_SECONDS,
    debug_dump_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """常驻自门控识别循环；游戏非前台时只低频待机，不截图。
    指定 debug_dump_dir 时，每个选择窗口(按钮从无到有)首帧自动转储，便于真机校准。"""

    _set_dpi_awareness()
    hint_cache = load_overlay_hint_cache()
    template_index = load_default_template_index(hint_cache=hint_cache)
    if not template_index:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "template_missing"})
        if write_event:
            write_overlay_event(event, event_path)
        logger.error("Vision sidecar 模板缺失，已退出。")
        return event

    detections: list[dict[str, Any]] = []
    last_signature: tuple[str, ...] | None = None
    last_write_at = 0.0
    unstable_streak = 0
    frame_sleep_seconds = max(0.05, int(frame_interval_ms) / 1000.0)
    idle_sleep_seconds = max(0.5, float(idle_interval_seconds))
    required = max(1, int(required_frames))
    dump_root = Path(debug_dump_dir) if debug_dump_dir else None
    dumped_windows = 0
    button_was_present = False

    while True:
        target = _find_lol_game_window()
        if target is None:
            detections.clear()
            unstable_streak = 0
            button_was_present = False
            event = _build_loop_inactive_event("game_window_missing")
            now = time.time()
            if write_event and should_write_loop_event(
                event,
                last_signature=last_signature,
                last_write_at=last_write_at,
                now=now,
                heartbeat_seconds=heartbeat_seconds,
            ):
                write_overlay_event(event, event_path)
                last_signature = _loop_event_signature(event)
                last_write_at = now
            time.sleep(idle_sleep_seconds)
            continue

        hwnd, rect = target
        if not _is_lol_game_foreground(hwnd):
            detections.clear()
            unstable_streak = 0
            button_was_present = False
            event = _build_loop_inactive_event("game_not_foreground")
            now = time.time()
            if write_event and should_write_loop_event(
                event,
                last_signature=last_signature,
                last_write_at=last_write_at,
                now=now,
                heartbeat_seconds=heartbeat_seconds,
            ):
                write_overlay_event(event, event_path)
                last_signature = _loop_event_signature(event)
                last_write_at = now
            time.sleep(idle_sleep_seconds)
            continue

        frame = _capture_lol_game_rect(rect)
        if frame is None:
            detections.clear()
            unstable_streak = 0
            button_was_present = False
            event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
            event["source"].update({"reason": "capture_unavailable"})
        else:
            detections.append(
                detect_overlay_choices(
                    frame,
                    template_index,
                    preset_name=preset,
                    min_confidence=min_confidence,
                )
            )
            frame_reason = str((detections[-1].get("source") or {}).get("reason") or "")
            button_present = frame_reason not in {"anchor_missing", "selection_button_missing"}
            if (
                dump_root is not None
                and button_present
                and not button_was_present
                and dumped_windows < LOOP_DEBUG_DUMP_MAX_WINDOWS
            ):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                try:
                    _write_debug_dump(dump_root / f"selection-{stamp}", frame, template_index, preset_name=preset)
                    dumped_windows += 1
                except OSError:
                    logger.debug("选择窗口调试转储失败。", exc_info=True)
            button_was_present = button_present
            detections = detections[-required:]
            event = stabilize_detections(detections, required_frames=required)
            if bool(event.get("active")):
                unstable_streak = 0
            else:
                unstable_streak += 1
                if should_defer_unstable_event(last_signature, unstable_streak):
                    time.sleep(frame_sleep_seconds)
                    continue

        now = time.time()
        if write_event and should_write_loop_event(
            event,
            last_signature=last_signature,
            last_write_at=last_write_at,
            now=now,
            heartbeat_seconds=heartbeat_seconds,
        ):
            write_overlay_event(event, event_path)
            last_signature = _loop_event_signature(event)
            last_write_at = now

        time.sleep(frame_sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech overlay Vision sidecar。")
    parser.add_argument("--once", action="store_true", help="执行一次短窗口识别后退出。")
    parser.add_argument("--loop", action="store_true", help="常驻自门控识别循环；未指定 --once 时默认启用。")
    parser.add_argument("--preset", default="auto", help="ROI preset: auto, 1920x1080, 2560x1440, 2560x1600。")
    parser.add_argument("--write-event", action="store_true", help="把识别结果写入 overlay 事件文件。")
    parser.add_argument("--event-path", default="", help="调试用事件文件路径；默认写运行态 state。")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--required-frames", type=int, default=2)
    parser.add_argument("--frame-interval-ms", type=int, default=DEFAULT_LOOP_FRAME_INTERVAL_MS)
    parser.add_argument("--idle-interval-ms", type=int, default=int(DEFAULT_LOOP_IDLE_INTERVAL_SECONDS * 1000))
    parser.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_LOOP_HEARTBEAT_SECONDS)
    parser.add_argument(
        "--debug-dump",
        default="",
        help="把单帧、ROI crop 和 top3 候选分数转储到该目录用于校准；--once 转储首帧，--loop 在每个选择窗口首帧自动转储。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.once:
        event = run_once(
            preset=args.preset,
            write_event=args.write_event,
            event_path=args.event_path or None,
            min_confidence=args.min_confidence,
            required_frames=args.required_frames,
            frame_interval_ms=args.frame_interval_ms,
            debug_dump_dir=args.debug_dump or None,
        )
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return 0

    event = run_loop(
        preset=args.preset,
        write_event=args.write_event,
        event_path=args.event_path or None,
        min_confidence=args.min_confidence,
        required_frames=args.required_frames,
        frame_interval_ms=args.frame_interval_ms,
        idle_interval_seconds=max(0, int(args.idle_interval_ms)) / 1000.0,
        heartbeat_seconds=args.heartbeat_seconds,
        debug_dump_dir=args.debug_dump or None,
    )
    if event is not None:
        print(json.dumps(event, ensure_ascii=False, indent=2))
        return 1 if event.get("source", {}).get("reason") == "template_missing" else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

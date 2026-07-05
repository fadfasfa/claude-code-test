"""游戏内 overlay Vision V2 运行入口。

本模块负责截图、模板装载、双字体/多图标变体评分和事件/诊断写入；版式检测、
候选规则、跨帧状态分别位于 ``hextech.overlay.vision.layout``、
``hextech.overlay.vision.matcher`` 和 ``hextech.overlay.vision.state``。
它不读游戏内存、不注入客户端、不自动点击，也不联网。
"""

from __future__ import annotations

from hextech.support.python_runtime import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source(module_name="hextech.overlay.vision.sidecar")

import argparse
import ctypes
import hashlib
import json
import logging
import os
import pickle
import shutil
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab

from hextech.overlay.events import OVERLAY_EVENT_FILE, build_overlay_event, write_overlay_event
from hextech.overlay.hints import load_overlay_hint_cache, normalize_augment_id, query_overlay_hint
from hextech.overlay.vision.layout import (
    BUTTON_SEARCH_REGION as V2_BUTTON_SEARCH_REGION,
    LayoutTransform,
    apply_transform,
    detect_selection_scene,
    pick_card_panels,
)
from hextech.overlay.vision.state import SelectionTracker
from hextech.overlay.window import cursor_in_client_boxes, find_lol_game_window, is_scoreboard_key_down, root_window_hwnd
from hextech.catalog.runtime_store import build_runtime_cache_path, build_runtime_state_path
from hextech.catalog.version_catalog import load_augment_manifest_entries, load_augment_name_to_icon_map
from hextech.scraping._paths import ASSET_DIR, INDEX_DATA_DIR, STATIC_DATA_DIR
from hextech.support.atomic_io import atomic_write_json

try:
    import win32gui
except ImportError:  # pragma: no cover - Windows 之外只允许离线测试纯函数
    win32gui = None


SLOT_COUNT = 3
TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION = 1
TEMPLATE_RUNTIME_CACHE_FILE = Path(build_runtime_cache_path("overlay_vision/template_runtime_cache.v1.pkl"))
SIDECAR_STATUS_FILE = Path(build_runtime_state_path("game_overlay_sidecar_status.json"))
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
ICON_SHORTLIST_MIN_CONFIDENCE = 0.62
ICON_SHORTLIST_MAX_DELTA = 0.10
ICON_SHORTLIST_MAX_GROUPS = 16
# 指纹灰度标准差下限：低于它视为平坦区域（如 ESC 暗色菜单），直接不参与匹配。
FLAT_CROP_STD_THRESHOLD = 12.0
# 按钮粗定位的搜索区域（相对捕获尺寸）：限制扫描范围以加速和减少误检
BUTTON_SEARCH_REGION = (0.20, 0.55, 0.80, 0.95)
# 按钮检测阈值：蓝色像素数下限和占比下限
BUTTON_MIN_BLUE_PIXELS = 80
BUTTON_MIN_BLUE_RATIO = 0.15
BUTTON_SCAN_DOWNSAMPLE = 4      # 按钮扫描下采样倍数（加速粗定位）
BUTTON_MIN_SOLIDITY = 0.45      # 连通域最小紧实度（排除长条状误检）
BUTTON_CENTER_MIN_RATIO = 0.42  # 连通域中心 X 范围下限（相对卡片宽度）
BUTTON_CENTER_MAX_RATIO = 0.58  # 连通域中心 X 范围上限
# 真按钮垂直中心实测约 0.81H；0.70 下限排除强化界面卡片描述区等中部蓝色误锁。
BUTTON_CENTER_MIN_Y_RATIO = 0.70
BUTTON_CENTER_MAX_Y_RATIO = 0.87
BODY_SHARD_KEYWORDS = ("body_shard", "body-shard", "body shard", "锻体", "碎片")
BODY_SHARD_SUFFIX = "碎片"            # 锻体卡片名称后缀标识
BODY_SHARD_STRONG_CONFIDENCE = 0.80    # 锻体强匹配置信度阈值
BODY_SHARD_VERY_STRONG_CONFIDENCE = 0.85  # 锻体极强匹配置信度（直接确认）
BODY_SHARD_SUPPORT_CONFIDENCE = 0.70     # 锻体辅助证据置信度阈值
BODY_SHARD_SUFFIX_SIZE = (100, 48)       # 锻体后缀区域裁剪尺寸
BODY_SHARD_SUFFIX_WIDTH_PERCENTS = (24, 28, 32, 36, 40, 44, 48, 50)  # 后缀宽度候选比例列表
BLOCKING_MODAL_PANEL_REGION = (0.34, 0.25, 0.66, 0.50)     # 阻塞弹窗面板搜索区域（相对坐标）
BLOCKING_MODAL_BUTTON_REGION = (0.42, 0.40, 0.58, 0.50)    # 阻塞弹窗按钮搜索区域
BLOCKING_MODAL_MIN_DARK_RATIO = 0.75                        # 面板区域最低暗像素占比
BLOCKING_MODAL_MIN_BUTTON_GOLD_RATIO = 0.05                 # 按钮区域最低金色像素占比

# active 掉到 unstable 后先观察几帧再写隐藏事件，避免识别抖动让 overlay 闪烁。
DEFAULT_EXIT_UNSTABLE_FRAMES = 2
DEFAULT_LOOP_FRAME_INTERVAL_MS = 160        # --loop 模式帧间隔（毫秒）
DEFAULT_LOOP_IDLE_INTERVAL_SECONDS = 0.25   # 空闲等待间隔
DEFAULT_LOOP_HEARTBEAT_SECONDS = 1.0        # 心跳日志间隔
# --loop 自动转储上限：每个进程最多转储前几个选择窗口，避免长局刷盘。
# 载入画面青色水面会误触发按钮检测吃掉名额，上限需覆盖载入误报 + 真实三选一。
LOOP_DEBUG_DUMP_MAX_WINDOWS = 12
ROI_DIAGNOSTIC_LIMIT = 32
VISION_TRACE_SCHEMA_VERSION = 2
OVERLAY_VISION_TRACE_FILE = OVERLAY_EVENT_FILE.with_name("overlay_vision_trace.v1.json")
OVERLAY_VISION_TRACE_HISTORY_FILE = OVERLAY_EVENT_FILE.with_name("overlay_vision_trace_history.v1.json")
VISION_TRACE_HISTORY_LIMIT = 256             # trace 历史最大保留条数
VISION_TRACE_REFRESH_SECONDS = 1.0           # trace 刷新间隔

logger = logging.getLogger(__name__)
_LAST_VISION_TRACE_SIGNATURES: dict[str, tuple[str, ...]] = {}
_LAST_VISION_TRACE_WRITES: dict[str, float] = {}
SIDECAR_READY_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_FILE"
SIDECAR_READY_TOKEN_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_TOKEN"
SIDECAR_EXIT_FILE_ENV = "HEXTECH_OVERLAY_EXIT_FILE"


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


def _write_sidecar_status(status: str, **fields: Any) -> None:
    """写入 sidecar 分阶段状态；失败只影响诊断，不阻断识别循环。"""

    payload = {
        "schema_version": 1,
        "status": status,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    payload.update(fields)
    try:
        atomic_write_json(SIDECAR_STATUS_FILE, payload, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("写入 Vision sidecar 状态失败。", exc_info=True)


def _write_sidecar_ready_from_env(
    *,
    template_count: int,
    started_at: float,
    startup_profile: Mapping[str, Any] | None = None,
) -> None:
    """向父进程报告冷启动完成；ready 前 watchdog 不应按 trace stale 杀进程。"""

    startup_seconds = round(max(0.0, time.perf_counter() - started_at), 3)
    profile = dict(startup_profile or {})
    _write_sidecar_status(
        "running",
        phase="ready",
        template_count=int(template_count),
        startup_seconds=startup_seconds,
        startup_profile=profile,
    )
    ready_path = str(os.environ.get(SIDECAR_READY_FILE_ENV) or "").strip()
    if not ready_path:
        return
    payload = {
        "pid": os.getpid(),
        "token": str(os.environ.get(SIDECAR_READY_TOKEN_ENV) or ""),
        "generation": str(os.environ.get("HEXTECH_OVERLAY_GENERATION") or ""),
        "template_count": int(template_count),
        "ready_at": time.time(),
        "startup_seconds": startup_seconds,
        "startup_profile": profile,
    }
    atomic_write_json(Path(ready_path), payload, ensure_ascii=False, indent=2)


def _sidecar_exit_requested() -> bool:
    """检查父进程写入的 graceful exit 文件。"""

    exit_path = str(os.environ.get(SIDECAR_EXIT_FILE_ENV) or "").strip()
    if not exit_path:
        return False
    return Path(exit_path).exists()


@dataclass(frozen=True)
class TemplateEntry:
    augment_id: str
    name: str
    tier: str
    summary: str
    fingerprint: tuple[float, ...]
    icon_fingerprints: tuple[tuple[float, ...], ...] = ()
    icon_digest: str = ""
    priority: int = 0
    name_fingerprint: tuple[float, ...] | None = None
    name_fingerprint_alt: tuple[float, ...] | None = None
    source_icon_filenames: tuple[str, ...] = ()
    text_only_icon_filenames: tuple[str, ...] = ()


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


def _selection_button_box_geometry_valid(
    box: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> bool:
    """统一校验扫描结果和缓存按钮框，拒绝载入进度条等过低横条。"""

    width, height = image_size
    left, top, right, bottom = box
    box_width = max(0, right - left)
    box_height = max(0, bottom - top)
    if min(width, height, box_width, box_height) <= 0:
        return False
    center_x_ratio = ((left + right) / 2.0) / width
    center_y_ratio = ((top + bottom) / 2.0) / height
    return (
        0.05 <= box_width / width <= 0.32
        and 0.01 <= box_height / height <= 0.12
        and BUTTON_CENTER_MIN_RATIO <= center_x_ratio <= BUTTON_CENTER_MAX_RATIO
        and BUTTON_CENTER_MIN_Y_RATIO <= center_y_ratio <= BUTTON_CENTER_MAX_Y_RATIO
    )


def _selection_button_box_valid(image: Image.Image, box: tuple[int, int, int, int]) -> bool:
    return _selection_button_box_geometry_valid(box, image.size) and selection_button_present(image.crop(box))


def _selection_button_source_fields(
    *,
    present: bool,
    window_active: bool | None = None,
    button_box: tuple[int, int, int, int] | None = None,
    blue_ratio: float = 0.0,
) -> dict[str, Any]:
    """把按钮检测结果稳定写入事件 source，供 host 判断选择窗口生命周期。"""

    return {
        "selection_button_present": bool(present),
        "selection_window_active": bool(present if window_active is None else window_active),
        "button_blue_ratio": round(max(0.0, float(blue_ratio)), 6),
        "button_box": list(button_box) if button_box is not None else [],
    }


def detect_selection_button_box(frame: Image.Image) -> tuple[int, int, int, int] | None:
    """首次校准时在宽搜索区内定位蓝色按钮，后续只复用固定 ROI。

    级联策略：
    1. 下采样扫描蓝色像素 → 提取水平游程
    2. 游程按 Y 邻近度分组为连通域
    3. 连通域按紧实度 + 中心位置校验 → 选出最佳候选按钮框
    """

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
        if not (BUTTON_CENTER_MIN_Y_RATIO * scan_height <= center_y <= BUTTON_CENTER_MAX_Y_RATIO * scan_height):
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
    return candidate if _selection_button_box_valid(image, candidate) else None


def _resampling_lanczos() -> int:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _fit_mask_to_canvas(mask: Image.Image, size: tuple[int, int]) -> Image.Image | None:
    """按前景 bbox 紧裁并等比放入固定画布，避免透明边或卡面背景主导匹配。"""

    gray = mask.convert("L")
    binary = gray.point(lambda value: 255 if value >= 32 else 0)
    bbox = binary.getbbox()
    if bbox is None:
        return None
    foreground_ratio = float(np.mean(np.asarray(binary, dtype=np.uint8) > 0))
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

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    high = rgb.max(axis=2).astype(np.int16)
    low = rgb.min(axis=2).astype(np.int16)
    # 真机图标/卡名明显亮于暗卡面；高饱和棱彩和浅奶油字都应保留。
    foreground = (high >= 112) | ((high >= 78) & ((high - low) >= 34))
    return Image.fromarray(foreground.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))


def _adaptive_icon_mask(image: Image.Image, *, template: bool) -> Image.Image:
    """统一模板透明字形与实战金色卡面图标，避免固定亮度把背景一起缩放。"""

    if template:
        return _alpha_or_luminance_mask(image).point(lambda value: 255 if value >= 32 else 0)
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return Image.new("L", image.size, 0)
    high = rgb.max(axis=2).astype(np.int16)
    low = rgb.min(axis=2).astype(np.int16)
    # 按当前 ROI 自适应阈值，同时保留棱彩高色差部分；阈值有上下界，防止全暗 HUD 噪声抬升。
    threshold = int(max(88, min(150, float(np.percentile(high, 84)))))
    mask = (high >= threshold) | ((high >= max(72, threshold - 28)) & ((high - low) >= 34))
    return Image.fromarray(mask.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))


def _largest_mask_component(mask: Image.Image) -> Image.Image:
    """保留主连通组件，削弱卡框、粒子和小装饰对图标轮廓的影响。"""

    binary = np.asarray(mask.convert("L"), dtype=np.uint8) >= 128
    height, width = binary.shape
    seen = np.zeros(binary.shape, dtype=bool)
    largest: list[tuple[int, int]] = []
    for start_y in range(height):
        for start_x in range(width):
            if not binary[start_y, start_x] or seen[start_y, start_x]:
                continue
            component = [(start_x, start_y)]
            seen[start_y, start_x] = True
            for x, y in component:
                for ny in range(max(0, y - 1), min(height, y + 2)):
                    for nx in range(max(0, x - 1), min(width, x + 2)):
                        if binary[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            component.append((nx, ny))
            if len(component) > len(largest):
                largest = component
    output = np.zeros(binary.shape, dtype=np.uint8)
    for x, y in largest:
        output[y, x] = 255
    return Image.fromarray(output)


def _icon_fingerprints(image: Image.Image, *, template: bool) -> tuple[tuple[float, ...], ...]:
    """生成完整 glyph、主组件与轮廓三种去重指纹。"""

    full = _adaptive_icon_mask(image, template=template)
    foreground_ratio = float(np.mean(np.asarray(full, dtype=np.uint8) >= 128)) if full.size[0] and full.size[1] else 0.0
    if foreground_ratio <= 0.002 or foreground_ratio >= 0.98:
        return ()
    compact = _fit_mask_to_canvas(full, (72, 72)) or full
    main = _largest_mask_component(compact)
    contour = main.filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value >= 24 else 0)
    fingerprints: list[tuple[float, ...]] = []
    for mask in (full, main, contour):
        fingerprint = _normalized_fingerprint(_mask_levels(mask, FINGERPRINT_SIZE))
        if fingerprint is not None and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return tuple(fingerprints)


def _icon_mask_digest(image: Image.Image, *, template: bool) -> str:
    """共享图标按规范化主组件 mask 分组，不按源 PNG 文件字节分组。"""

    full = _adaptive_icon_mask(image, template=template)
    compact = _fit_mask_to_canvas(full, (72, 72)) or full
    fitted = _fit_mask_to_canvas(_largest_mask_component(compact), FINGERPRINT_SIZE)
    if fitted is None:
        return ""
    normalized = fitted.point(lambda value: 255 if value >= 64 else 0)
    return hashlib.sha256(bytes(normalized.getdata())).hexdigest()[:20]


def _name_text_mask(image: Image.Image) -> Image.Image:
    """名字区专用 mask：去掉卡框和星光等装饰，只保留标题主字形。"""

    mask = _bright_glyph_mask(image)
    width, height = mask.size
    pixels = mask.load()
    seen: set[tuple[int, int]] = set()
    components: list[tuple[list[tuple[int, int]], int, int]] = []

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
            components.append((component, component_height, len(component)))

    if not components:
        return mask

    # 真机卡名两侧会出现星光粒子。它们能通过固定像素阈值，却明显小于同一行主字形；
    # 相对当前裁剪中的最大字形过滤，既不依赖分辨率，也不会误删正常短名称。
    dominant_height = max(component_height for _component, component_height, _area in components)
    dominant_area = max(area for _component, _component_height, area in components)
    glyph_components = [
        component
        for component, component_height, area in components
        if component_height >= dominant_height * 0.72 and area >= dominant_area * 0.25
    ]
    if not glyph_components:
        glyph_components = [component for component, _component_height, _area in components]

    cleaned = Image.new("L", mask.size, 0)
    cleaned_pixels = cleaned.load()
    for component in glyph_components:
        for x, y in component:
            cleaned_pixels[x, y] = 255

    return cleaned


def _mask_levels(mask: Image.Image, size: tuple[int, int]) -> list[int]:
    fitted = _fit_mask_to_canvas(mask, size)
    if fitted is None:
        return []
    edges = fitted.filter(ImageFilter.FIND_EDGES)
    return list(fitted.getdata()) + list(edges.getdata())


def _text_mask_levels(mask: Image.Image, size: tuple[int, int]) -> list[int]:
    """文字主体优先的指纹输入；边缘仅作辅助，避免抗锯齿和装饰噪声反客为主。"""

    fitted = _fit_mask_to_canvas(mask, size)
    if fitted is None:
        return []
    fill = list(fitted.getdata())
    edges = list(fitted.filter(ImageFilter.FIND_EDGES).getdata())
    return fill * 3 + edges


def _icon_levels(image: Image.Image, *, template: bool) -> list[int]:
    mask = _adaptive_icon_mask(image, template=template)
    return _mask_levels(mask, FINGERPRINT_SIZE)


def _text_levels(image: Image.Image) -> list[int]:
    return _text_mask_levels(_name_text_mask(image), NAME_FINGERPRINT_SIZE)


def _grayscale_levels(image: Image.Image) -> list[int]:
    """兼容旧调用名：现在返回图标字形轮廓指纹输入，而非整块灰度。"""

    return _icon_levels(image, template=False)


def _levels_std(levels: Sequence[int]) -> float:
    if not levels:
        return 0.0
    return float(np.asarray(levels, dtype=np.float32).std())


def _normalized_fingerprint(levels: Sequence[int]) -> tuple[float, ...] | None:
    """零均值/单位方差归一化指纹；平坦图像（纯色、暗面板）没有有效指纹。"""

    if not levels:
        return None
    values = np.asarray(levels, dtype=np.float32)
    mean = float(values.mean())
    std = float(values.std())
    if std < 1e-6:
        return None
    return tuple(((values - mean) / std).tolist())


def _fingerprint(image: Image.Image, *, template: bool = False) -> tuple[float, ...] | None:
    return _normalized_fingerprint(_icon_levels(image, template=template))


def _fingerprint_confidence(left: Sequence[float] | None, right: Sequence[float] | None) -> float:
    """归一化互相关（NCC）映射到 [0,1]；只对形状敏感，不受亮度/色调影响。"""

    if not left or not right or len(left) != len(right):
        return 0.0
    correlation = sum(a * b for a, b in zip(left, right)) / len(left)
    return max(0.0, min(1.0, (correlation + 1.0) / 2.0))


def _load_cjk_font(size: int, *, family: str = "primary") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    primary_paths = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    )
    alt_paths = (
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    legacy_paths = (
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    font_paths = alt_paths if family == "alt" else (legacy_paths if family == "legacy" else primary_paths)
    for font_path in font_paths:
        try:
            if font_path.exists():
                return ImageFont.truetype(str(font_path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render_name_mask(name: str, *, family: str = "primary") -> Image.Image | None:
    clean_name = _clean_text(name)
    if not clean_name:
        return None
    canvas = Image.new("L", NAME_FINGERPRINT_SIZE, 0)
    draw = ImageDraw.Draw(canvas)
    font_size = 34
    font = _load_cjk_font(font_size, family=family)
    max_width = int(NAME_FINGERPRINT_SIZE[0] * 0.94)
    while font_size >= 16:
        bbox = draw.textbbox((0, 0), clean_name, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        if text_width <= max_width:
            break
        font_size -= 2
        font = _load_cjk_font(font_size, family=family)
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


def render_name_mask(name: str, *, family: str = "primary") -> Image.Image | None:
    """供离线刷新/评测工具复用的名称模板渲染入口。"""

    return _render_name_mask(name, family=family)


def _name_fingerprint(name: str, *, family: str = "primary") -> tuple[float, ...] | None:
    mask = _render_name_mask(name, family=family)
    if mask is None:
        return None
    return _normalized_fingerprint(_text_mask_levels(mask, NAME_FINGERPRINT_SIZE))


def _cleaned_name_fingerprint(name: str, *, family: str = "primary") -> tuple[float, ...] | None:
    mask = _render_name_mask(name, family=family)
    if mask is None:
        return None
    # 截图路径会先经过 _name_text_mask 清理装饰组件；额外保留同路径模板指纹，
    # 让长名称、数字和标点在全量合成回归里不会被相似中文名压过。
    cleaned = _name_text_mask(Image.merge("RGB", (mask, mask, mask)))
    return _normalized_fingerprint(_text_mask_levels(cleaned, NAME_FINGERPRINT_SIZE))


@lru_cache(maxsize=1)
def _body_shard_suffix_fingerprints() -> tuple[tuple[float, ...], ...]:
    fingerprints: list[tuple[float, ...]] = []
    # 碎片后缀门保留旧 SimHei 指纹，避免主名称通道切换字体后削弱既有硬阻断。
    for family in ("primary", "alt", "legacy"):
        mask = _render_name_mask(BODY_SHARD_SUFFIX, family=family)
        if mask is None:
            continue
        fingerprint = _normalized_fingerprint(_mask_levels(mask, BODY_SHARD_SUFFIX_SIZE))
        if fingerprint is not None and fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
    return tuple(fingerprints)


@lru_cache(maxsize=1)
def _body_shard_suffix_matrix() -> np.ndarray:
    fingerprints = _body_shard_suffix_fingerprints()
    if not fingerprints:
        return np.empty((0, 0), dtype=np.float32)
    return np.asarray(fingerprints, dtype=np.float32)


def _body_shard_name_scores(
    name_crops: Sequence[Image.Image],
    *,
    name_masks: Sequence[Image.Image] | None = None,
) -> tuple[float, ...]:
    """匹配名称右侧“碎片”后缀；只用于选择场景类型判定。"""

    template_matrix = _body_shard_suffix_matrix()
    scores: list[float] = []
    masks = list(name_masks or [])
    for index, crop in enumerate(list(name_crops)[:SLOT_COUNT]):
        mask = masks[index] if index < len(masks) else _name_text_mask(crop)
        bounds = mask.getbbox()
        if bounds is None or template_matrix.size == 0:
            scores.append(0.0)
            continue
        left, top, right, bottom = bounds
        width = max(1, right - left)
        candidate_fingerprints: list[tuple[float, ...]] = []
        for percent in BODY_SHARD_SUFFIX_WIDTH_PERCENTS:
            suffix_width = max(1, int(round(width * percent / 100.0)))
            suffix = mask.crop((max(left, right - suffix_width), top, right, bottom))
            fingerprint = _normalized_fingerprint(_mask_levels(suffix, BODY_SHARD_SUFFIX_SIZE))
            if fingerprint is not None:
                candidate_fingerprints.append(fingerprint)
        if not candidate_fingerprints:
            scores.append(0.0)
            continue
        candidate_matrix = np.asarray(candidate_fingerprints, dtype=np.float32)
        correlations = candidate_matrix @ template_matrix.T / candidate_matrix.shape[1]
        best = float(np.clip((correlations.max() + 1.0) / 2.0, 0.0, 1.0))
        scores.append(round(best, 6))
    while len(scores) < SLOT_COUNT:
        scores.append(0.0)
    return tuple(scores)


def _body_shard_scene_present(scores: Sequence[float]) -> bool:
    ranked = sorted((float(score) for score in list(scores)[:SLOT_COUNT]), reverse=True)
    if len(ranked) < 2:
        return False
    return bool(
        sum(score >= BODY_SHARD_STRONG_CONFIDENCE for score in ranked) >= 2
        or (
            ranked[0] >= BODY_SHARD_VERY_STRONG_CONFIDENCE
            and ranked[1] >= BODY_SHARD_SUPPORT_CONFIDENCE
        )
    )


def _name_crop_has_residue(crop: Image.Image, *, name_mask: Image.Image | None = None) -> bool:
    mask_array = np.asarray(name_mask if name_mask is not None else _name_text_mask(crop), dtype=np.uint8) >= 128
    if mask_array.size == 0:
        return False
    foreground_ratio = float(np.mean(mask_array))
    return 0.005 <= foreground_ratio <= 0.45


def _load_manifest_entries(root: Path, *, use_runtime_resources: bool = True) -> list[Mapping[str, Any]]:
    try:
        if use_runtime_resources:
            payload = load_augment_manifest_entries()
        else:
            version_data_dir = root / "data" / "static" / "version"
            if (version_data_dir / "海克斯资源目录.v1.json").exists():
                payload = load_augment_manifest_entries(version_data_dir)
            else:
                manifest_path = root / "data" / "static" / "Augment_Icon_Manifest.json"
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, Mapping)]


def _load_manifest_entries_by_name(root: Path, *, use_runtime_resources: bool = True) -> dict[str, list[Mapping[str, Any]]]:
    payload = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    result: dict[str, list[Mapping[str, Any]]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        name = _clean_text(item.get("name"))
        if not name:
            continue
        result.setdefault(name, []).append(item)
        result.setdefault(normalize_augment_id(name), []).append(item)
    return result


def _select_manifest_item(
    manifest_by_name: Mapping[str, Sequence[Mapping[str, Any]]],
    name: str,
    relative_icon: str,
) -> Mapping[str, Any]:
    entries = (
        manifest_by_name.get(name)
        or manifest_by_name.get(normalize_augment_id(name))
        or ()
    )
    if not entries:
        return {}

    # CDragon 有少数同中文名但不同玩法/图标的条目。模板图标来自 name-to-icon，
    # 因此元数据也要优先选择同 filename 的 manifest 项，避免图标正确但 tier/id 串到同名旧项。
    requested_filename = Path(str(relative_icon or "").replace("\\", "/")).name.lower()
    for item in entries:
        filename = _clean_text(item.get("filename")).lower()
        local_path = Path(_clean_text(item.get("local_path")).replace("\\", "/")).name.lower()
        if requested_filename and requested_filename in {filename, local_path}:
            return item
    return entries[0]


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    """从内存模板构建匹配索引。

    去重策略：同一 normalize_augment_id 只保留一个身份，同名图标作为指纹变体聚合。
    无图标指纹时仍保留文字模板，避免透明或低对比度官方图标让整个海克斯消失。
    双字体指纹：SimHei（主字体）和 SimSun（备选字体）各生成一份 name_fingerprint，
    匹配时双通道独立评分再综合判定。
    """

    index: list[TemplateEntry] = []
    for augment_id, payload in raw_templates.items():
        if not isinstance(payload, Mapping):
            continue
        normalized_id = normalize_augment_id(augment_id, str(payload.get("name") or ""))
        if not normalized_id:
            continue
        name = _clean_text(payload.get("name"), fallback=normalized_id)
        raw_images = payload.get("images")
        images = [item for item in raw_images if isinstance(item, Image.Image)] if isinstance(raw_images, Sequence) else []
        if not images and isinstance(payload.get("image"), Image.Image):
            images = [payload["image"]]
        filenames_value = payload.get("source_icon_filenames")
        filenames = [
            _clean_text(item)
            for item in filenames_value
            if _clean_text(item)
        ] if isinstance(filenames_value, Sequence) and not isinstance(filenames_value, (str, bytes)) else []

        icon_fingerprints_list: list[tuple[float, ...]] = []
        text_only_filenames: list[str] = []
        icon_digest = ""
        for image_index, image in enumerate(images):
            image_fingerprints = _icon_fingerprints(image, template=True)
            if image_fingerprints:
                for fingerprint in image_fingerprints:
                    if fingerprint not in icon_fingerprints_list:
                        icon_fingerprints_list.append(fingerprint)
                if not icon_digest:
                    icon_digest = _icon_mask_digest(image, template=True)
            elif image_index < len(filenames):
                text_only_filenames.append(filenames[image_index])
        icon_fingerprints = tuple(icon_fingerprints_list)
        name_fingerprint = _name_fingerprint(name)
        name_fingerprint_alt = _name_fingerprint(name, family="alt")
        if not icon_fingerprints and name_fingerprint is None and name_fingerprint_alt is None:
            continue
        index.append(
            TemplateEntry(
                augment_id=normalized_id,
                name=name,
                tier=_clean_text(payload.get("tier"), fallback="Unknown"),
                summary=_clean_text(payload.get("summary"), fallback="本地模板识别结果"),
                fingerprint=icon_fingerprints[0] if icon_fingerprints else (),
                icon_fingerprints=icon_fingerprints,
                icon_digest=icon_digest,
                priority=1 if bool(payload.get("priority")) else 0,
                name_fingerprint=name_fingerprint,
                name_fingerprint_alt=name_fingerprint_alt,
                source_icon_filenames=tuple(filenames),
                text_only_icon_filenames=tuple(text_only_filenames),
            )
        )
    return index


def load_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> list[TemplateEntry]:
    """从随包稳定资源加载海克斯图标模板，不触发远端抓取。"""

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_data_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "data" / "static" / "version"
    legacy_mapping_path = root / "data" / "indexes" / "augment.name-to-icon.v1.json"
    asset_dir = Path(ASSET_DIR) if use_runtime_resources else root / "data" / "static" / "assets"
    legacy_asset_dir = root / "assets"
    try:
        if use_runtime_resources or (version_data_dir / "海克斯资源目录.v1.json").exists():
            name_to_icon = load_augment_name_to_icon_map(version_data_dir)
        else:
            name_to_icon = json.loads(legacy_mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(name_to_icon, Mapping):
        return []

    manifest_entries = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    manifest_by_name = _load_manifest_entries_by_name(root, use_runtime_resources=use_runtime_resources)
    raw_templates: dict[str, Mapping[str, Any]] = {}
    names = {
        _clean_text(item.get("name"))
        for item in manifest_entries
        if _clean_text(item.get("name"))
    } | {_clean_text(name) for name in name_to_icon if _clean_text(name)}
    for name in sorted(names, key=normalize_augment_id):
        clean_name = _clean_text(name)
        manifest_items = manifest_by_name.get(clean_name) or manifest_by_name.get(normalize_augment_id(clean_name)) or []
        icon_paths = [str(item.get("local_path") or item.get("filename") or "") for item in manifest_items]
        mapped_icon = str(name_to_icon.get(clean_name) or "")
        if mapped_icon:
            icon_paths.append(mapped_icon)
        icon_paths = list(dict.fromkeys(path for path in icon_paths if path))
        if not clean_name or not icon_paths:
            continue
        images: list[Image.Image] = []
        filenames: list[str] = []
        loaded_paths: set[Path] = set()
        allowed_roots = (root.resolve(), asset_dir.resolve(), legacy_asset_dir.resolve())
        for icon_path in icon_paths:
            relative_icon = str(icon_path or "").lstrip("/")
            if relative_icon.startswith("assets/"):
                path = (asset_dir / relative_icon.removeprefix("assets/")).resolve()
            else:
                path = (root / relative_icon).resolve()
            try:
                if not any(path == allowed_root or allowed_root in path.parents for allowed_root in allowed_roots):
                    continue
                if path in loaded_paths:
                    continue
                with Image.open(path) as opened:
                    images.append(opened.copy())
                filenames.append(path.name)
                loaded_paths.add(path)
            except OSError:
                continue
        if not images:
            continue
        hint_result = query_overlay_hint(hint_cache or {}, clean_name)
        hint = hint_result.get("hint") if hint_result.get("ok") and isinstance(hint_result.get("hint"), Mapping) else {}
        manifest_item = _select_manifest_item(manifest_by_name, clean_name, mapped_icon)
        template_id = normalize_augment_id(
            manifest_item.get("augment_name_id")
            or manifest_item.get("cdragon_id")
            or hint.get("augment_id")
            or clean_name,
            clean_name,
        )
        existing = raw_templates.get(template_id)
        if isinstance(existing, Mapping) and _clean_text(existing.get("name")) != clean_name:
            template_id = normalize_augment_id(clean_name)
        raw_templates[template_id] = {
            "name": clean_name,
            "tier": _clean_text(hint.get("tier") or manifest_item.get("tier"), fallback="Unknown"),
            "summary": _clean_text(
                hint.get("summary") or manifest_item.get("tooltip_plain") or manifest_item.get("description"),
                fallback="本地模板识别结果",
            ),
            "images": images,
            "source_icon_filenames": filenames,
            "priority": 1 if hint_result.get("ok") else 0,
        }
    return build_template_index(raw_templates)


def audit_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """审计稳定资源是否为每个名称和图标变体建立了可识别模板。"""

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_data_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "data" / "static" / "version"
    entries = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    try:
        name_to_icon = load_augment_name_to_icon_map(version_data_dir)
    except (OSError, json.JSONDecodeError):
        name_to_icon = {}
    expected_identities = {
        normalize_augment_id(item.get("name"))
        for item in entries
        if normalize_augment_id(item.get("name"))
    } | {
        normalize_augment_id(name)
        for name in name_to_icon
        if normalize_augment_id(name)
    }
    expected_variants = {
        (normalize_augment_id(item.get("name")), Path(_clean_text(item.get("filename") or item.get("local_path"))).name.lower())
        for item in entries
        if normalize_augment_id(item.get("name")) and _clean_text(item.get("filename") or item.get("local_path"))
    } | {
        (normalize_augment_id(name), Path(str(icon_path).replace("\\", "/")).name.lower())
        for name, icon_path in name_to_icon.items()
        if normalize_augment_id(name) and _clean_text(icon_path)
    }
    template_index = load_default_template_index(base_dir, hint_cache=hint_cache)
    actual_identities = {normalize_augment_id(entry.name) for entry in template_index}
    actual_variants = {
        (normalize_augment_id(entry.name), filename.lower())
        for entry in template_index
        for filename in entry.source_icon_filenames
    }
    missing_identities = sorted(expected_identities - actual_identities)
    missing_variants = sorted(expected_variants - actual_variants)
    return {
        "manifest_count": len(entries),
        "identity_count": len(expected_identities),
        "variant_count": len(expected_variants),
        "template_count": len(template_index),
        "text_only_template_count": sum(1 for entry in template_index if not entry.icon_fingerprints),
        "text_only_variant_count": sum(len(entry.text_only_icon_filenames) for entry in template_index),
        "missing_identity_count": len(missing_identities),
        "missing_variant_count": len(missing_variants),
        "missing_identity_sample": missing_identities[:20],
        "missing_variant_sample": [list(item) for item in missing_variants[:20]],
    }


@dataclass(frozen=True)
class _RankMatrices:
    """模板指纹的向量化缓存：把逐模板 Python NCC 循环换成单次矩阵乘。"""

    index_ref: Sequence[TemplateEntry]  # 强引用，防止 id() 复用导致缓存串味
    icon_templates: tuple[TemplateEntry, ...]
    icon_matrix: np.ndarray  # (N_icon, D_icon)，行=已归一化的图标指纹
    name_templates: tuple[TemplateEntry, ...]
    name_matrix: np.ndarray  # (N_name, D_name)，行=已归一化的名字指纹
    alt_name_templates: tuple[TemplateEntry, ...]
    alt_name_matrix: np.ndarray


@dataclass(frozen=True)
class TemplateRuntime:
    """sidecar 启动所需的模板索引与矩阵，允许从本机 runtime cache 直接恢复。"""

    template_index: list[TemplateEntry]
    matrices: _RankMatrices
    stats: dict[str, Any]


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
    icon_rows: list[tuple[TemplateEntry, tuple[float, ...]]] = []
    for template in template_index:
        variants = tuple(fingerprint for fingerprint in (template.icon_fingerprints or (template.fingerprint,)) if fingerprint)
        icon_rows.extend((template, fingerprint) for fingerprint in variants)
    icon_templates = tuple(template for template, _fingerprint_row in icon_rows)
    icon_matrix = _stack_fingerprints([fingerprint_row for _template, fingerprint_row in icon_rows])
    name_rows: list[tuple[TemplateEntry, tuple[float, ...]]] = []
    alt_name_rows: list[tuple[TemplateEntry, tuple[float, ...]]] = []
    for template in template_index:
        primary_variants = [
            fingerprint
            for fingerprint in (template.name_fingerprint, _cleaned_name_fingerprint(template.name))
            if fingerprint is not None
        ]
        alt_variants = [
            fingerprint
            for fingerprint in (template.name_fingerprint_alt, _cleaned_name_fingerprint(template.name, family="alt"))
            if fingerprint is not None
        ]
        name_rows.extend((template, fingerprint) for fingerprint in dict.fromkeys(primary_variants))
        alt_name_rows.extend((template, fingerprint) for fingerprint in dict.fromkeys(alt_variants))
    name_templates = tuple(template for template, _fingerprint_row in name_rows)
    name_matrix = _stack_fingerprints([fingerprint_row for _template, fingerprint_row in name_rows])
    alt_name_templates = tuple(template for template, _fingerprint_row in alt_name_rows)
    alt_name_matrix = _stack_fingerprints([fingerprint_row for _template, fingerprint_row in alt_name_rows])
    entry = _RankMatrices(
        template_index,
        icon_templates,
        icon_matrix,
        name_templates,
        name_matrix,
        alt_name_templates,
        alt_name_matrix,
    )
    if key not in _RANK_MATRIX_CACHE and len(_RANK_MATRIX_CACHE) >= _RANK_MATRIX_CACHE_MAX:
        _RANK_MATRIX_CACHE.pop(next(iter(_RANK_MATRIX_CACHE)))
    _RANK_MATRIX_CACHE[key] = entry
    return entry


def rank_template_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    """供离线刷新/评测工具预热模板矩阵，避免直接依赖私有函数名。"""

    return _rank_matrices(template_index)


def _runtime_environment_signature() -> dict[str, Any]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "numpy": str(getattr(np, "__version__", "")),
        "pillow": str(getattr(Image, "__version__", "")),
    }


def _hash_runtime_resource_stats(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item).lower()):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(str(path).replace("\\", "/").encode("utf-8", errors="replace"))
        digest.update(str(int(stat.st_size)).encode("ascii"))
        digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
    return digest.hexdigest()


def template_runtime_resource_signature(base_dir: str | Path | None = None) -> dict[str, Any]:
    """生成模板缓存指纹；资源或代码 schema 变化时自动失效。"""

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "data" / "static" / "version"
    asset_dir = Path(ASSET_DIR) if use_runtime_resources else root / "data" / "static" / "assets"
    version_files = [path for path in version_dir.rglob("*.json") if path.is_file()] if version_dir.exists() else []
    asset_files = [
        path
        for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ] if asset_dir.exists() else []
    return {
        "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
        "environment": _runtime_environment_signature(),
        "version_digest": _hash_runtime_resource_stats(version_files),
        "asset_digest": _hash_runtime_resource_stats(asset_files),
        "version_file_count": len(version_files),
        "asset_file_count": len(asset_files),
    }


def _hint_cache_signature(hint_cache: Mapping[str, Any] | None) -> str:
    if not isinstance(hint_cache, Mapping):
        return ""
    try:
        return hashlib.sha256(
            json.dumps(hint_cache, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except TypeError:
        return hashlib.sha256(repr(hint_cache).encode("utf-8", errors="replace")).hexdigest()


def _read_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
) -> TemplateRuntime | None:
    try:
        with Path(cache_file).open("rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION:
        return None
    if payload.get("resource_signature") != dict(resource_signature):
        return None
    if str(payload.get("hint_signature") or "") != hint_signature:
        return None
    template_index = payload.get("template_index")
    matrices = payload.get("matrices")
    if not isinstance(template_index, list) or not isinstance(matrices, _RankMatrices):
        return None
    if matrices.index_ref is not template_index:
        return None
    _RANK_MATRIX_CACHE[id(template_index)] = matrices
    return TemplateRuntime(
        template_index=template_index,
        matrices=matrices,
        stats={
            "cache_hit": True,
            "cache_file": str(cache_file),
            "template_count": len(template_index),
        },
    )


def _write_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
    template_index: list[TemplateEntry],
    matrices: _RankMatrices,
) -> None:
    target = Path(cache_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    payload = {
        "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
        "resource_signature": dict(resource_signature),
        "hint_signature": hint_signature,
        "template_index": template_index,
        "matrices": matrices,
        "written_at": time.time(),
    }
    try:
        with temp_path.open("wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp_path, target)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def load_or_build_default_template_runtime(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    cache_file: str | Path | None = None,
    resource_signature: Mapping[str, Any] | None = None,
    status_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TemplateRuntime:
    """加载 sidecar 模板 runtime；cache miss 才重建模板索引和矩阵。"""

    started_at = time.perf_counter()
    target_cache = Path(cache_file) if cache_file is not None else TEMPLATE_RUNTIME_CACHE_FILE
    signature = dict(resource_signature or template_runtime_resource_signature(base_dir))
    hint_signature = _hint_cache_signature(hint_cache)
    if status_callback is not None:
        status_callback("template_runtime_cache_lookup", {"cache_file": str(target_cache)})
    runtime = _read_template_runtime_cache(
        target_cache,
        resource_signature=signature,
        hint_signature=hint_signature,
    )
    if runtime is not None:
        runtime.stats.update({"build_seconds": 0.0, "load_seconds": round(time.perf_counter() - started_at, 3)})
        return runtime
    if status_callback is not None:
        status_callback("template_index_build", {"cache_hit": False})
    template_index = load_default_template_index(base_dir, hint_cache=hint_cache)
    if status_callback is not None:
        status_callback("rank_matrix_build", {"template_count": len(template_index)})
    matrices = _rank_matrices(template_index)
    try:
        _write_template_runtime_cache(
            target_cache,
            resource_signature=signature,
            hint_signature=hint_signature,
            template_index=template_index,
            matrices=matrices,
        )
        cache_error = ""
    except Exception as exc:
        cache_error = str(exc)
        logger.debug("写入 Vision 模板 runtime cache 失败。", exc_info=True)
    stats = {
        "cache_hit": False,
        "cache_file": str(target_cache),
        "cache_error": cache_error,
        "template_count": len(template_index),
        "build_seconds": round(time.perf_counter() - started_at, 3),
        "load_seconds": 0.0,
    }
    return TemplateRuntime(template_index=template_index, matrices=matrices, stats=stats)


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
    crop_fingerprints = _icon_fingerprints(crop, template=False)
    if not crop_fingerprints:
        return crop_std, []
    matrices = _rank_matrices(template_index)
    best_by_identity: dict[str, tuple[TemplateEntry, float]] = {}
    for crop_fingerprint in crop_fingerprints:
        for template, confidence in _rank_with_matrix(
            crop_fingerprint,
            matrices.icon_templates,
            matrices.icon_matrix,
        ):
            identity = template.augment_id or template.name
            previous = best_by_identity.get(identity)
            if previous is None or confidence > previous[1]:
                best_by_identity[identity] = (template, confidence)
    return crop_std, sorted(
        best_by_identity.values(),
        key=lambda item: (-item[1], -item[0].priority, item[0].name),
    )


def _rank_name_templates(
    crop: Image.Image | None,
    template_index: Sequence[TemplateEntry],
    *,
    family: str = "primary",
) -> tuple[float, list[tuple[TemplateEntry, float]]]:
    if crop is None:
        return 0.0, []
    levels = _text_levels(crop)
    crop_std = _levels_std(levels)
    crop_fingerprint = _normalized_fingerprint(levels)
    if crop_fingerprint is None:
        return crop_std, []
    return crop_std, _rank_name_fingerprint(crop_fingerprint, template_index, family=family)


def _rank_name_fingerprint(
    crop_fingerprint: Sequence[float],
    template_index: Sequence[TemplateEntry],
    *,
    family: str,
) -> list[tuple[TemplateEntry, float]]:
    """同一名称 ROI 只分割一次，再分别投影到 SimHei / SimSun 模板矩阵。"""

    matrices = _rank_matrices(template_index)
    if family == "alt":
        ranked = _rank_with_matrix(
            crop_fingerprint,
            matrices.alt_name_templates,
            matrices.alt_name_matrix,
        )
    else:
        ranked = _rank_with_matrix(crop_fingerprint, matrices.name_templates, matrices.name_matrix)
    best_by_identity: dict[str, tuple[TemplateEntry, float]] = {}
    for template, confidence in ranked:
        identity = template.augment_id or template.name
        previous = best_by_identity.get(identity)
        if previous is None or confidence > previous[1]:
            best_by_identity[identity] = (template, confidence)
    return sorted(best_by_identity.values(), key=lambda item: (-item[1], -item[0].priority, item[0].name))


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


def _top_candidates(
    ranked: Sequence[tuple[TemplateEntry, float]],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    return [
        {
            "augment_id": template.augment_id,
            "name": template.name,
            "tier": template.tier,
            "summary": template.summary,
            "confidence": confidence,
            "icon_digest": template.icon_digest,
            "priority": template.priority,
        }
        for template, confidence in list(ranked)[: max(0, int(limit))]
    ]


def _template_group_key(template: TemplateEntry) -> str:
    return template.icon_digest or template.augment_id or template.name


def _build_icon_shortlist(
    ranked: Sequence[tuple[TemplateEntry, float]],
    *,
    min_confidence: float = ICON_SHORTLIST_MIN_CONFIDENCE,
    max_delta: float = ICON_SHORTLIST_MAX_DELTA,
    max_groups: int = ICON_SHORTLIST_MAX_GROUPS,
) -> list[tuple[TemplateEntry, float]]:
    """选择高分图标组；共享 digest 下的名称必须一起保留。"""

    if not ranked or max_groups <= 0:
        return []
    top_confidence = float(ranked[0][1])
    selected_groups: list[str] = []
    for template, confidence in ranked:
        if float(confidence) < min_confidence or top_confidence - float(confidence) > max_delta:
            continue
        group_key = _template_group_key(template)
        if group_key not in selected_groups:
            if len(selected_groups) >= max_groups:
                continue
            selected_groups.append(group_key)
    selected = set(selected_groups)
    return [
        (template, confidence)
        for template, confidence in ranked
        if _template_group_key(template) in selected
    ]


def _narrow_ranked_by_icon_shortlist(
    ranked: Sequence[tuple[TemplateEntry, float]],
    icon_shortlist: Sequence[tuple[TemplateEntry, float]],
) -> list[tuple[TemplateEntry, float]]:
    selected_groups = {_template_group_key(template) for template, _confidence in icon_shortlist}
    return [
        (template, confidence)
        for template, confidence in ranked
        if _template_group_key(template) in selected_groups
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
    alt_name_crop_std: float,
    alt_name_ranked: Sequence[tuple[TemplateEntry, float]],
    icon_shortlist: Sequence[tuple[TemplateEntry, float]],
    narrowed_name_ranked: Sequence[tuple[TemplateEntry, float]],
    narrowed_alt_name_ranked: Sequence[tuple[TemplateEntry, float]],
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
        "text_alt": {
            "crop_std": round(alt_name_crop_std, 3),
            "margin": round(_candidate_margin(alt_name_ranked), 4),
            "top_candidates": _top_candidates(alt_name_ranked),
        },
        "icon_shortlist": {
            "group_count": len({_template_group_key(template) for template, _confidence in icon_shortlist}),
            "top_candidates": _top_candidates(icon_shortlist, limit=len(icon_shortlist)),
        },
        "text_narrowed": {
            "margin": round(_candidate_margin(narrowed_name_ranked), 4),
            "top_candidates": _top_candidates(narrowed_name_ranked),
        },
        "text_alt_narrowed": {
            "margin": round(_candidate_margin(narrowed_alt_name_ranked), 4),
            "top_candidates": _top_candidates(narrowed_alt_name_ranked),
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
    name_mask: Image.Image | None = None,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
    min_text_confidence: float = DEFAULT_TEXT_MIN_CONFIDENCE,
    min_text_margin: float = DEFAULT_TEXT_MIN_MARGIN,
) -> dict[str, Any]:
    """单槽位双通道识别：图标指纹匹配 + SimHei/SimSun 双字体文字匹配。

    流程：
    1. 图标通道：截取卡片图标区域 → 灰度指纹 → 与模板库排名
    2. 文字通道：截取名称区域 → SimHei(主)/SimSun(备) 分别渲染 → 与截屏文字指纹排名
    3. 图标短名单：用图标通道 Top-N 缩窄文字候选空间
    4. 三通道结果汇入 channels 字典，供 matcher.candidate_from_slot 做最终判定
    """
    crop_std, ranked = _rank_templates(frame.crop(box), template_index)
    name_levels = (
        _text_mask_levels(name_mask, NAME_FINGERPRINT_SIZE)
        if name_mask is not None
        else (_text_levels(frame.crop(name_box)) if name_box is not None else [])
    )
    name_crop_std = _levels_std(name_levels)
    name_fingerprint = _normalized_fingerprint(name_levels)
    if name_fingerprint is None:
        name_ranked: list[tuple[TemplateEntry, float]] = []
        alt_name_ranked: list[tuple[TemplateEntry, float]] = []
    else:
        name_ranked = _rank_name_fingerprint(name_fingerprint, template_index, family="primary")
        alt_name_ranked = _rank_name_fingerprint(name_fingerprint, template_index, family="alt")
    alt_name_crop_std = name_crop_std
    icon_template, icon_confidence = ranked[0] if ranked else (None, 0.0)
    name_template, name_confidence = name_ranked[0] if name_ranked else (None, 0.0)
    alt_name_template, alt_name_confidence = alt_name_ranked[0] if alt_name_ranked else (None, 0.0)
    icon_margin = _candidate_margin(ranked)
    name_margin = _candidate_margin(name_ranked)
    alt_name_margin = _candidate_margin(alt_name_ranked)
    icon_shortlist = _build_icon_shortlist(ranked)
    narrowed_name_ranked = _narrow_ranked_by_icon_shortlist(name_ranked, icon_shortlist)
    narrowed_alt_name_ranked = _narrow_ranked_by_icon_shortlist(alt_name_ranked, icon_shortlist)
    icon_candidates = _top_candidates(ranked)
    name_candidates = _top_candidates(name_ranked)
    channels = _channels_payload(
        icon_crop_std=crop_std,
        icon_ranked=ranked,
        name_crop_std=name_crop_std,
        name_ranked=name_ranked,
        alt_name_crop_std=alt_name_crop_std,
        alt_name_ranked=alt_name_ranked,
        icon_shortlist=icon_shortlist,
        narrowed_name_ranked=narrowed_name_ranked,
        narrowed_alt_name_ranked=narrowed_alt_name_ranked,
    )
    name_box_present = name_box is not None
    has_name_channel = name_box_present and bool(name_ranked)
    primary_text_ready = (
        name_template is not None
        and name_crop_std >= FLAT_CROP_STD_THRESHOLD
        and name_confidence >= min_text_confidence
        and (name_margin >= min_text_margin or name_confidence >= TWIN_CONFIDENCE_OVERRIDE)
    )
    alt_text_ready = (
        alt_name_template is not None
        and alt_name_crop_std >= FLAT_CROP_STD_THRESHOLD
        and alt_name_confidence >= min_text_confidence
        and (alt_name_margin >= min_text_margin or alt_name_confidence >= TWIN_CONFIDENCE_OVERRIDE)
    )
    dual_font_ready = bool(
        name_template is not None
        and alt_name_template is not None
        and (name_template.augment_id or name_template.name) == (alt_name_template.augment_id or alt_name_template.name)
        and name_confidence >= 0.70
        and alt_name_confidence >= 0.70
    )
    icon_ready = (
        icon_template is not None
        and _slot_match_decision(crop_std, icon_confidence, icon_margin, min_confidence=min_confidence, min_margin=min_margin)
    )
    icon_supports_text = bool(
        icon_template is not None
        and (name_template is not None or alt_name_template is not None)
        and (
            icon_template.name in {
                template.name
                for template in (name_template, alt_name_template)
                if template is not None
            }
            or (
                icon_template.icon_digest
                and icon_template.icon_digest
                in {
                    template.icon_digest
                    for template in (name_template, alt_name_template)
                    if template is not None
                }
            )
        )
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
    text_conflict = bool(
        primary_text_ready
        and alt_text_ready
        and name_template is not None
        and alt_name_template is not None
        and (name_template.augment_id or name_template.name) != (alt_name_template.augment_id or alt_name_template.name)
    )
    selected_text: tuple[TemplateEntry, float, list[dict[str, Any]]] | None = None
    if dual_font_ready and name_template is not None:
        selected_text = (
            name_template,
            max(name_confidence, alt_name_confidence),
            name_candidates if name_confidence >= alt_name_confidence else _top_candidates(alt_name_ranked),
        )
    elif text_conflict:
        dominant_texts = [
            (template, confidence, candidate_rows)
            for template, confidence, candidate_rows in (
                (name_template, name_confidence, name_candidates),
                (alt_name_template, alt_name_confidence, _top_candidates(alt_name_ranked)),
            )
            if template is not None and confidence >= 0.95
        ]
        if dominant_texts:
            selected_template, selected_confidence, selected_candidates = max(dominant_texts, key=lambda item: item[1])
            other_confidence = alt_name_confidence if selected_template is name_template else name_confidence
            if selected_confidence - other_confidence >= 0.10:
                selected_text = (selected_template, selected_confidence, selected_candidates)
    elif not text_conflict:
        ready_texts = [
            (template, confidence, candidate_rows)
            for template, confidence, candidate_rows, ready in (
                (name_template, name_confidence, name_candidates, primary_text_ready),
                (alt_name_template, alt_name_confidence, _top_candidates(alt_name_ranked), alt_text_ready),
            )
            if template is not None and ready
        ]
        if ready_texts:
            selected_text = max(ready_texts, key=lambda item: item[1])

    if selected_text is not None:
        selected_template, selected_confidence, selected_candidates = selected_text
        diagnostic = "dual_font_ready" if dual_font_ready else (
            "text_icon_agree" if icon_supports_text else "text_channel_ready"
        )
        if icon_template is not None and not icon_supports_text and icon_confidence >= SCENE_SLOT_MIN_CONFIDENCE:
            diagnostic = "text_icon_disagree"
        return _slot_result(
            slot_index=slot_index,
            state="ready",
            template=selected_template,
            confidence=selected_confidence,
            diagnostic=diagnostic,
            top_candidates=selected_candidates,
            channels=channels,
            summary="",
        )
    # V2 不允许 icon-only 授权显示；图标只用于收窄候选和佐证文字。
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
    if icon_confidence < min_confidence and max(name_confidence, alt_name_confidence) < min_text_confidence:
        best_confidence = max(icon_confidence, name_confidence, alt_name_confidence)
        state = "low_confidence" if best_confidence >= SCENE_SLOT_MIN_CONFIDENCE else "detecting"
        return _slot_result(
            slot_index=slot_index,
            state=state,
            template=None,
            confidence=best_confidence,
            diagnostic="confidence_below_threshold",
            top_candidates=candidates,
            channels=channels,
            summary="候选置信度不足",
        )
    diagnostic = "icon_only_low_confidence" if icon_ready else "margin_below_threshold"
    if name_box_present and not has_name_channel and icon_ready:
        diagnostic = "text_channel_missing"
    return _slot_result(
        slot_index=slot_index,
        state="low_confidence",
        template=None,
        confidence=max(icon_confidence, name_confidence, alt_name_confidence),
        diagnostic=diagnostic,
        top_candidates=candidates,
        channels=channels,
        summary="候选区分度不足",
    )


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


def _relative_crop_array(image: Image.Image, region: tuple[float, float, float, float]) -> np.ndarray:
    width, height = image.size
    left, top, right, bottom = region
    box = (
        max(0, min(width - 1, int(round(left * width)))),
        max(0, min(height - 1, int(round(top * height)))),
        max(1, min(width, int(round(right * width)))),
        max(1, min(height, int(round(bottom * height)))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return np.empty((0, 0, 3), dtype=np.uint8)
    return np.asarray(image.crop(box).convert("RGB"), dtype=np.uint8)


def _blocking_modal_present(image: Image.Image) -> bool:
    """识别 LoL 中央阻塞弹窗；弹窗存在时仍可诊断识别，但 overlay 不显示。"""

    panel = _relative_crop_array(image, BLOCKING_MODAL_PANEL_REGION)
    button = _relative_crop_array(image, BLOCKING_MODAL_BUTTON_REGION)
    if panel.size == 0 or button.size == 0:
        return False

    panel_red = panel[:, :, 0]
    panel_green = panel[:, :, 1]
    panel_blue = panel[:, :, 2]
    dark_ratio = float(np.mean((panel_red < 55) & (panel_green < 60) & (panel_blue < 70)))

    button_red = button[:, :, 0]
    button_green = button[:, :, 1]
    button_blue = button[:, :, 2]
    gold_ratio = float(
        np.mean(
            (button_red > 80)
            & (button_green > 45)
            & (button_green < 165)
            & (button_blue < 95)
            & (button_red > button_blue + 25)
        )
    )
    return dark_ratio >= BLOCKING_MODAL_MIN_DARK_RATIO and gold_ratio >= BLOCKING_MODAL_MIN_BUTTON_GOLD_RATIO


def _slot_ready_for_display(slot: Mapping[str, Any]) -> bool:
    return bool(slot.get("state") == "ready" and (slot.get("augment_id") or slot.get("name")))


def _ready_slot_count(slots: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for slot in list(slots)[:SLOT_COUNT] if _slot_ready_for_display(slot))


def _scene_active_from_slots(slots: Sequence[Mapping[str, Any]]) -> bool:
    """三张卡全部 ready 才具备正式显示条件。"""

    return len(list(slots)[:SLOT_COUNT]) == SLOT_COUNT and _ready_slot_count(slots) == SLOT_COUNT


def detect_overlay_choices(
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    calibration_path: str | Path | None = None,
) -> dict[str, Any]:
    """执行单帧 V2 观察；跨帧生命周期由 ``SelectionTracker`` 负责。"""

    _ = calibration_path  # 保留旧调用签名；V2 不再读取或写入持久化 anchor。
    started_at = time.perf_counter()
    image = frame.convert("RGB")
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    scene = detect_selection_scene(image, layout_id=preset.name)
    transform_payload = {
        "dx_ratio": round(scene.transform.dx_ratio, 6),
        "dy_ratio": round(scene.transform.dy_ratio, 6),
        "scale": round(scene.transform.scale, 6),
    }
    if not scene.present or scene.button_box is None:
        residual_name_boxes = tuple(
            apply_transform(box, image.size, scene.transform) for box in preset.name_slots
        )
        name_residue = [
            _name_crop_has_residue(image.crop(box)) for box in residual_name_boxes
        ]
        event = _build_loop_inactive_event("selection_scene_not_detected")
        event["source"].update(
            {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "preset": preset.name,
                "capture_size": [int(image.size[0]), int(image.size[1])],
                "calibration": "layout_v2",
                "scene_present": False,
                "scene_state": "absent",
                "scene_kind": "absent",
                "scene_score": scene.score,
                "layout_id": scene.layout_id,
                "layout_transform": transform_payload,
                "panel_scores": list(scene.panel_scores),
                "name_residue": name_residue,
                "card_residue": bool(scene.card_residue or any(name_residue)),
                "scene_reject_reason": scene.reason or "selection_scene_not_detected",
                "content_ready": False,
                **_selection_button_source_fields(
                    present=scene.button_box is not None,
                    window_active=False,
                    button_box=scene.button_box,
                    blue_ratio=scene.button_blue_ratio,
                ),
            }
        )
        event["_raw_slots"] = []
        return event

    slot_boxes = tuple(apply_transform(box, image.size, scene.transform) for box in preset.slots)
    name_boxes = tuple(apply_transform(box, image.size, scene.transform) for box in preset.name_slots)
    name_crops = [image.crop(box) for box in name_boxes]
    name_masks = [_name_text_mask(crop) for crop in name_crops]
    body_shard_scores = _body_shard_name_scores(name_crops, name_masks=name_masks)
    name_residue = [
        _name_crop_has_residue(crop, name_mask=name_masks[index])
        for index, crop in enumerate(name_crops)
    ]
    common_source = {
        "latency_ms": 0.0,
        "preset": preset.name,
        "capture_size": [int(image.size[0]), int(image.size[1])],
        "calibration": "layout_v2",
        "scene_present": True,
        "scene_state": "candidate",
        "scene_kind": "body_shard" if _body_shard_scene_present(body_shard_scores) else "hextech",
        "scene_score": scene.score,
        "layout_id": scene.layout_id,
        "layout_transform": transform_payload,
        "panel_scores": list(scene.panel_scores),
        "name_residue": name_residue,
        "card_residue": bool(scene.card_residue or any(name_residue)),
        "body_shard_scores": list(body_shard_scores),
    }

    if _body_shard_scene_present(body_shard_scores):
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                **common_source,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "gate_state": "blocked",
                "ready_slots": 0,
                "content_ready": False,
                "stable_frames": 0,
                "unstable_reason": "body_shard_only",
                "poll_mode": "high",
                "reason": "body_shard_only",
                "body_shard_latched": True,
                **_selection_button_source_fields(
                    present=True,
                    window_active=False,
                    button_box=scene.button_box,
                    blue_ratio=scene.button_blue_ratio,
                ),
            }
        )
        event["_raw_slots"] = []
        return event

    slots = [
        _detect_slot(
            image,
            box,
            index,
            template_index,
            name_box=name_boxes[index] if index < len(name_boxes) else None,
            name_mask=name_masks[index] if index < len(name_masks) else None,
            min_confidence=min_confidence,
        )
        for index, box in enumerate(slot_boxes)
    ]
    if _slots_have_body_shard_keywords(slots):
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                **common_source,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "gate_state": "blocked",
                "ready_slots": _ready_slot_count(slots),
                "content_ready": False,
                "stable_frames": 0,
                "unstable_reason": "body_shard_only",
                "poll_mode": "high",
                "reason": "body_shard_only",
                "scene_kind": "body_shard",
                "body_shard_latched": True,
                **_selection_button_source_fields(
                    present=True,
                    window_active=False,
                    button_box=scene.button_box,
                    blue_ratio=scene.button_blue_ratio,
                ),
            }
        )
        event["_raw_slots"] = slots
        return event

    blocking_modal = _blocking_modal_present(image)
    ready_slots = _ready_slot_count(slots)
    content_ready = _scene_active_from_slots(slots)
    active = content_ready and not blocking_modal
    if active and content_ready:
        reason = ""
        gate_state = "visible_ready"
        unstable_reason = ""
    elif blocking_modal:
        reason = "blocking_modal_present"
        gate_state = "blocked"
        unstable_reason = reason
    elif ready_slots:
        reason = "partial_ready"
        gate_state = "partial_ready"
        unstable_reason = reason
    else:
        reason = "selection_scene_not_detected"
        gate_state = "detecting"
        unstable_reason = reason
    event = build_overlay_event(
        slots,
        source_tag="vision-sidecar",
        selection_type="hextech",
        active=active,
    )
    event["source"].update(
        {
            **common_source,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "gate_state": gate_state,
            "ready_slots": ready_slots,
            "content_ready": content_ready,
            "stable_frames": 0,
            "slot_states": [str(slot.get("state") or "") for slot in slots[:SLOT_COUNT]],
            "blocking_modal": blocking_modal,
            "unstable_reason": unstable_reason,
            "poll_mode": "high",
            "reason": reason,
            **_selection_button_source_fields(
                present=True,
                window_active=not blocking_modal,
                button_box=scene.button_box,
                blue_ratio=scene.button_blue_ratio,
            ),
        }
    )
    event["_raw_slots"] = slots
    return event


def _cursor_over_card_panels(
    client_rect: tuple[int, int, int, int],
    frame_size: tuple[int, int],
    source: Mapping[str, Any],
) -> bool:
    raw_transform = source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {}
    try:
        transform = LayoutTransform(
            dx_ratio=float(raw_transform.get("dx_ratio") or 0.0),
            dy_ratio=float(raw_transform.get("dy_ratio") or 0.0),
            scale=float(raw_transform.get("scale") or 1.0),
        )
    except (TypeError, ValueError):
        transform = LayoutTransform()
    panel_defs = pick_card_panels(frame_size)
    boxes = [apply_transform(box, frame_size, transform) for box in panel_defs]
    return cursor_in_client_boxes(client_rect, boxes)


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
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if bool(event_payload.get("active")):
        return ("active", *_slot_signature(event_payload))
    if source.get("selection_window_active") is True:
        return (
            "selection",
            str(source.get("gate_state") or ""),
            str(_event_ready_slots(event_payload)),
            *_slot_signature(event_payload),
        )
    return ("inactive", str(source.get("reason") or "inactive"))


def _inactive_reason(event_payload: Mapping[str, Any]) -> str:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    return str(source.get("reason") or "")


def _event_ready_slots(event_payload: Mapping[str, Any]) -> int:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    try:
        return int(source.get("ready_slots"))
    except (TypeError, ValueError):
        slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
        return _ready_slot_count([slot for slot in slots if isinstance(slot, Mapping)])


def _event_selection_button_fields(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_box = source.get("button_box")
    button_box = list(raw_box) if isinstance(raw_box, Sequence) and not isinstance(raw_box, (str, bytes)) else []
    try:
        blue_ratio = max(0.0, float(source.get("button_blue_ratio") or 0.0))
    except (TypeError, ValueError):
        blue_ratio = 0.0
    return {
        "selection_button_present": bool(source.get("selection_button_present")),
        "selection_window_active": bool(source.get("selection_window_active")),
        "button_blue_ratio": round(blue_ratio, 6),
        "button_box": button_box,
    }


def _event_content_ready(event_payload: Mapping[str, Any]) -> bool:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if isinstance(source.get("content_ready"), bool):
        return bool(source.get("content_ready"))
    slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    return _scene_active_from_slots([slot for slot in slots if isinstance(slot, Mapping)])


def _active_gate_state(event_payload: Mapping[str, Any]) -> tuple[str, str, str]:
    ready_slots = _event_ready_slots(event_payload)
    if _event_content_ready(event_payload):
        return "visible_ready", "", ""
    if ready_slots:
        return "partial_ready", "partial_ready", "partial_ready"
    return "detecting", "selection_scene_not_detected", "selection_scene_not_detected"


def _copy_active_lifecycle_event(event_payload: Mapping[str, Any], *, stable_frames: int) -> dict[str, Any]:
    event = dict(event_payload)
    source = dict(event.get("source") if isinstance(event.get("source"), Mapping) else {})
    gate_state, reason, unstable_reason = _active_gate_state(event)
    event["active"] = True
    event["source"] = source
    source["selection_window_active"] = True
    source["stable_frames"] = int(stable_frames)
    source["content_ready"] = _event_content_ready(event)
    source["ready_slots"] = _event_ready_slots(event)
    source["gate_state"] = gate_state
    source["reason"] = reason
    source["unstable_reason"] = unstable_reason
    return event


def _copy_inactive_detection(
    event_payload: Mapping[str, Any],
    *,
    reason: str,
    gate_state: str,
    stable_frames: int = 0,
) -> dict[str, Any]:
    event = dict(event_payload)
    source = dict(event.get("source") if isinstance(event.get("source"), Mapping) else {})
    event["active"] = False
    event["source"] = source
    source["stable_frames"] = int(stable_frames)
    source["gate_state"] = gate_state
    source["reason"] = reason
    source["unstable_reason"] = reason
    return event


def _build_loop_inactive_event(reason: str, *, poll_mode: str = "high") -> dict[str, Any]:
    event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    event["source"].update(
        {
            "reason": reason,
            "gate_state": "inactive",
            "ready_slots": 0,
            "stable_frames": 0,
            "unstable_reason": reason,
            "poll_mode": poll_mode,
            **_selection_button_source_fields(present=False),
        }
    )
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
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if bool(event_payload.get("active")) or source.get("selection_window_active") is True:
        return signature != last_signature or (now - float(last_write_at or 0.0)) >= float(heartbeat_seconds)
    return (
        last_signature is None
        or signature != last_signature
        or (bool(last_signature) and last_signature[0] in {"active", "selection"})
    )


def should_defer_unstable_event(
    last_signature: tuple[str, ...] | None,
    unstable_streak: int,
    *,
    selection_window_active: bool | None = None,
    reason: str = "",
    exit_unstable_frames: int = DEFAULT_EXIT_UNSTABLE_FRAMES,
) -> bool:
    """吸收短抖动；blocking/body 等硬阻断仍立即结束选择生命周期。"""

    if not last_signature or last_signature[0] != "active":
        return False
    if reason in {"blocking_modal_present", "body_shard_only", "game_not_foreground", "game_window_missing"}:
        return False
    return int(unstable_streak) < max(1, int(exit_unstable_frames))


def remaining_frame_sleep_seconds(frame_interval_ms: int, *, elapsed_seconds: float) -> float:
    """把 interval 解释为帧起点周期，识别耗时不得与固定休眠重复累加。"""

    target_seconds = max(0.05, int(frame_interval_ms) / 1000.0)
    return max(0.0, target_seconds - max(0.0, float(elapsed_seconds)))


def stabilize_detections(events: Sequence[Mapping[str, Any]], *, required_frames: int = 2) -> dict[str, Any]:
    """要求连续多帧槽位 ID 一致，避免动画早期误输出。"""

    required = max(1, int(required_frames))
    recent = list(events)[-required:]
    if recent:
        latest_reason = _inactive_reason(recent[-1])
        if latest_reason in {"blocking_modal_present", "body_shard_only"}:
            return _copy_inactive_detection(
                recent[-1],
                reason=latest_reason,
                gate_state="blocked",
                stable_frames=1,
            )
    if len(recent) < required:
        if recent:
            return _copy_inactive_detection(
                recent[-1],
                reason="warming_up",
                gate_state="unstable",
                stable_frames=len(recent),
            )
        return _build_loop_inactive_event("warming_up")
    first_signature = _slot_signature(recent[0])
    if (
        bool(first_signature)
        and _event_content_ready(recent[0])
        and all(bool(event.get("active")) for event in recent)
        and all(_event_content_ready(event) for event in recent)
        and all(_slot_signature(event) == first_signature for event in recent)
    ):
        return _copy_active_lifecycle_event(recent[-1], stable_frames=len(recent))
    if all(not bool(event.get("active")) for event in recent):
        first_reason = _inactive_reason(recent[0])
        if first_reason and all(_inactive_reason(event) == first_reason for event in recent):
            gate_state = "partial_ready" if first_reason == "partial_ready" else "inactive"
            return _copy_inactive_detection(
                recent[-1],
                reason=first_reason,
                gate_state=gate_state,
                stable_frames=len(recent),
            )
    return _copy_inactive_detection(
        recent[-1],
        reason="unstable",
        gate_state="unstable",
    )


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logger.debug("设置 Vision sidecar DPI 感知失败。", exc_info=True)


def _find_lol_game_window() -> tuple[int, tuple[int, int, int, int]] | None:
    return find_lol_game_window()


def _find_lol_game_rect() -> tuple[int, int, int, int] | None:
    target = _find_lol_game_window()
    return target[1] if target is not None else None


def _is_lol_game_foreground(hwnd: int | None) -> bool:
    if win32gui is None or not hwnd:
        return False
    try:
        foreground = int(win32gui.GetForegroundWindow())
        if not foreground:
            return False
        return root_window_hwnd(foreground) == root_window_hwnd(hwnd)
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


def _write_roi_diagnostic_dump(
    dump_root: str | Path,
    frame: Image.Image,
    event_payload: Mapping[str, Any],
) -> Path:
    """只保存按钮、图标和卡名 ROI；禁止写入完整游戏帧。"""

    root = Path(dump_root) / "overlay_roi_v2"
    stamp = f"roi-{time.strftime('%Y%m%d-%H%M%S')}-{int((time.time() % 1) * 1000):03d}"
    target = root / stamp
    target.mkdir(parents=True, exist_ok=False)
    image = frame.convert("RGB")
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    preset = resolve_roi_preset(*image.size, preset=str(source.get("preset") or "auto"))
    transform_source = source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {}
    transform = LayoutTransform(
        dx_ratio=float(transform_source.get("dx_ratio") or 0.0),
        dy_ratio=float(transform_source.get("dy_ratio") or 0.0),
        scale=float(transform_source.get("scale") or 1.0),
    )
    raw_button_box = source.get("button_box")
    if isinstance(raw_button_box, list) and len(raw_button_box) == 4:
        button_box = tuple(int(value) for value in raw_button_box)
    else:
        button_box = apply_transform(V2_BUTTON_SEARCH_REGION, image.size, LayoutTransform())
    image.crop(button_box).save(target / "button.png")
    for index, box in enumerate(preset.slots):
        image.crop(apply_transform(box, image.size, transform)).save(target / f"icon_{index}.png")
    for index, box in enumerate(preset.name_slots):
        image.crop(apply_transform(box, image.size, transform)).save(target / f"name_{index}.png")
    atomic_write_json(
        target / "report.json",
        build_vision_trace_payload(event_payload),
        ensure_ascii=False,
        indent=2,
    )

    directories = sorted((path for path in root.glob("roi-*") if path.is_dir()), key=lambda path: path.name)
    for expired in directories[:-ROI_DIAGNOSTIC_LIMIT]:
        shutil.rmtree(expired, ignore_errors=True)
    return target


def build_vision_trace_payload(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    """生成最近一次识别链路诊断；正式 overlay 不读取该文件。"""

    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_slots = event_payload.get("_raw_slots") if isinstance(event_payload.get("_raw_slots"), list) else (
        event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    )
    rendered_slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    acceptance_rules = event_payload.get("_acceptance_rules") if isinstance(event_payload.get("_acceptance_rules"), list) else []
    slots: list[dict[str, Any]] = []
    for index, slot in enumerate(raw_slots[:SLOT_COUNT]):
        if not isinstance(slot, Mapping):
            continue
        rendered_slot = rendered_slots[index] if index < len(rendered_slots) and isinstance(rendered_slots[index], Mapping) else {}
        slots.append(
            {
                "slot": int(slot.get("slot") if slot.get("slot") is not None else index),
                "state": str(slot.get("state") or ""),
                "augment_id": str(slot.get("augment_id") or ""),
                "name": str(slot.get("name") or ""),
                "confidence": slot.get("confidence"),
                "diagnostic": str(slot.get("diagnostic") or ""),
                "acceptance_rule": str(
                    (acceptance_rules[index] if index < len(acceptance_rules) else "")
                    or rendered_slot.get("acceptance_rule")
                    or slot.get("acceptance_rule")
                    or ""
                ),
                "top_candidates": slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else [],
                "channels": slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {},
            }
        )
    return {
        "schema_version": VISION_TRACE_SCHEMA_VERSION,
        "generated_at": time.time(),
        "active": bool(event_payload.get("active")),
        "selection_type": str(event_payload.get("selection_type") or ""),
        "source": {
            "reason": str(source.get("reason") or ""),
            "gate_state": str(source.get("gate_state") or ""),
            "ready_slots": source.get("ready_slots"),
            "content_ready": source.get("content_ready"),
            "selection_button_present": source.get("selection_button_present"),
            "selection_window_active": source.get("selection_window_active"),
            "button_blue_ratio": source.get("button_blue_ratio"),
            "button_box": source.get("button_box") if isinstance(source.get("button_box"), list) else [],
            "blocking_modal": source.get("blocking_modal"),
            "calibration": source.get("calibration"),
            "preset": source.get("preset"),
            "capture_size": source.get("capture_size"),
            "latency_ms": source.get("latency_ms"),
            "scene_state": source.get("scene_state"),
            "scene_kind": source.get("scene_kind"),
            "scene_score": source.get("scene_score"),
            "layout_id": source.get("layout_id"),
            "layout_transform": source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {},
            "selection_epoch": source.get("selection_epoch"),
            "scoreboard_key_down": source.get("scoreboard_key_down"),
            "body_shard_scores": source.get("body_shard_scores")
            if isinstance(source.get("body_shard_scores"), list)
            else [],
            "body_shard_latched": bool(source.get("body_shard_latched")),
            "cursor_over_cards": bool(source.get("cursor_over_cards")),
            "card_residue": bool(source.get("card_residue")),
            "name_residue": source.get("name_residue") if isinstance(source.get("name_residue"), list) else [],
            "hover_occluded": bool(source.get("hover_occluded")),
            "slot_states": source.get("slot_states") if isinstance(source.get("slot_states"), list) else [],
        },
        "thresholds": {
            "min_confidence": DEFAULT_MIN_CONFIDENCE,
            "scene_slot_min_confidence": SCENE_SLOT_MIN_CONFIDENCE,
            "text_min_confidence": DEFAULT_TEXT_MIN_CONFIDENCE,
            "min_margin": DEFAULT_MIN_MARGIN,
            "flat_crop_std": FLAT_CROP_STD_THRESHOLD,
            "dual_font_confidence": 0.70,
            "weak_text_confidence": 0.68,
            "weak_text_margin": 0.01,
            "body_shard_strong_confidence": BODY_SHARD_STRONG_CONFIDENCE,
            "body_shard_very_strong_confidence": BODY_SHARD_VERY_STRONG_CONFIDENCE,
            "body_shard_support_confidence": BODY_SHARD_SUPPORT_CONFIDENCE,
            "icon_shortlist_min_confidence": ICON_SHORTLIST_MIN_CONFIDENCE,
            "icon_shortlist_max_delta": ICON_SHORTLIST_MAX_DELTA,
        },
        "slots": slots,
    }


def _public_event_payload(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    """移除只供进程内 trace 使用的私有字段。"""

    payload = dict(event_payload)
    payload.pop("_raw_slots", None)
    payload.pop("_acceptance_rules", None)
    return payload


def _vision_trace_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_slots = event_payload.get("_raw_slots") if isinstance(event_payload.get("_raw_slots"), list) else []
    raw_signature: list[str] = []
    for slot in raw_slots[:SLOT_COUNT]:
        if not isinstance(slot, Mapping):
            raw_signature.append("")
            continue
        channels = slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {}
        for channel_name in ("text", "text_alt", "icon", "text_narrowed", "text_alt_narrowed"):
            channel = channels.get(channel_name) if isinstance(channels.get(channel_name), Mapping) else {}
            candidates = channel.get("top_candidates") if isinstance(channel.get("top_candidates"), list) else []
            top = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
            raw_signature.append(f"{channel_name}:{top.get('augment_id') or top.get('name') or ''}")
    acceptance_rules = event_payload.get("_acceptance_rules") if isinstance(event_payload.get("_acceptance_rules"), list) else []
    return (
        "active" if event_payload.get("active") else "inactive",
        str(source.get("reason") or ""),
        str(source.get("gate_state") or ""),
        str(source.get("ready_slots") or ""),
        str(source.get("selection_window_active") or ""),
        str(source.get("scene_kind") or ""),
        "body_shard_latched" if source.get("body_shard_latched") else "",
        "cursor_over_cards" if source.get("cursor_over_cards") else "",
        "hover_occluded" if source.get("hover_occluded") else "",
        *_slot_signature(event_payload),
        *(str(rule or "") for rule in acceptance_rules[:SLOT_COUNT]),
        *raw_signature,
    )


def _vision_trace_path_for_event(event_path: str | Path | None = None) -> Path:
    if event_path is None:
        return OVERLAY_VISION_TRACE_FILE
    return Path(event_path).with_name(OVERLAY_VISION_TRACE_FILE.name)


def _vision_trace_history_path(trace_path: str | Path | None = None) -> Path:
    target = Path(trace_path) if trace_path is not None else OVERLAY_VISION_TRACE_FILE
    return target.with_name(OVERLAY_VISION_TRACE_HISTORY_FILE.name)


def _vision_trace_history_entry(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_box = source.get("button_box")
    button_box = list(raw_box) if isinstance(raw_box, list) else []
    capture_size = source.get("capture_size")
    center_y_ratio = None
    if (
        len(button_box) == 4
        and isinstance(capture_size, Sequence)
        and not isinstance(capture_size, (str, bytes))
        and len(capture_size) == 2
    ):
        try:
            capture_height = float(capture_size[1])
            if capture_height > 0:
                center_y_ratio = round((float(button_box[1]) + float(button_box[3])) / 2.0 / capture_height, 6)
        except (TypeError, ValueError):
            center_y_ratio = None
    ready_slots = _event_ready_slots(event_payload)
    blocking_modal = bool(source.get("blocking_modal"))
    visible = bool(
        event_payload.get("active")
        and str(event_payload.get("selection_type") or "") == "hextech"
        and ready_slots >= 1
        and not blocking_modal
        and not bool(source.get("scoreboard_key_down"))
    )
    return {
        "generated_at": time.time(),
        "active": bool(event_payload.get("active")),
        "visible": visible,
        "selection_type": str(event_payload.get("selection_type") or ""),
        "reason": str(source.get("reason") or ""),
        "gate_state": str(source.get("gate_state") or ""),
        "calibration": str(source.get("calibration") or ""),
        "scene_state": str(source.get("scene_state") or ""),
        "scene_kind": str(source.get("scene_kind") or ""),
        "scene_score": float(source.get("scene_score") or 0.0),
        "layout_id": str(source.get("layout_id") or ""),
        "selection_epoch": int(source.get("selection_epoch") or 0),
        "scoreboard_key_down": bool(source.get("scoreboard_key_down")),
        "body_shard_scores": list(source.get("body_shard_scores"))
        if isinstance(source.get("body_shard_scores"), list)
        else [],
        "body_shard_latched": bool(source.get("body_shard_latched")),
        "cursor_over_cards": bool(source.get("cursor_over_cards")),
        "card_residue": bool(source.get("card_residue")),
        "hover_occluded": bool(source.get("hover_occluded")),
        "ready_slots": ready_slots,
        "slot_states": list(source.get("slot_states")) if isinstance(source.get("slot_states"), list) else [],
        "stable_frames": int(source.get("stable_frames") or 0),
        "selection_button_present": bool(source.get("selection_button_present")),
        "selection_window_active": bool(source.get("selection_window_active")),
        "button_blue_ratio": float(source.get("button_blue_ratio") or 0.0),
        "button_center_y_ratio": center_y_ratio,
        "button_box": button_box,
        "slot_signature": list(_slot_signature(event_payload)),
    }


def _append_vision_trace_history(
    event_payload: Mapping[str, Any],
    path: str | Path,
) -> Path:
    target = Path(path)
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    entries = existing.get("entries") if isinstance(existing, Mapping) else None
    history = [dict(item) for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []
    history.append(_vision_trace_history_entry(event_payload))
    payload = {
        "schema_version": VISION_TRACE_SCHEMA_VERSION,
        "updated_at": time.time(),
        "entries": history[-VISION_TRACE_HISTORY_LIMIT:],
    }
    atomic_write_json(target, payload, ensure_ascii=False, indent=2)
    return target


def write_vision_trace_if_changed(
    event_payload: Mapping[str, Any],
    path: str | Path | None = None,
    *,
    history_path: str | Path | None = None,
) -> Path | None:
    """状态变化写历史；分数抖动仅按 1Hz 刷新最新 trace。"""

    signature = _vision_trace_signature(event_payload)
    target = Path(path) if path is not None else OVERLAY_VISION_TRACE_FILE
    signature_key = str(target.resolve())
    now = time.monotonic()
    state_changed = signature != _LAST_VISION_TRACE_SIGNATURES.get(signature_key)
    if (
        not state_changed
        and target.exists()
        and now - _LAST_VISION_TRACE_WRITES.get(signature_key, 0.0) < VISION_TRACE_REFRESH_SECONDS
    ):
        return None
    atomic_write_json(target, build_vision_trace_payload(event_payload), ensure_ascii=False, indent=2)
    if state_changed:
        _append_vision_trace_history(
            event_payload,
            Path(history_path) if history_path is not None else _vision_trace_history_path(target),
        )
    _LAST_VISION_TRACE_SIGNATURES[signature_key] = signature
    _LAST_VISION_TRACE_WRITES[signature_key] = now
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

    started_at = time.perf_counter()
    _set_dpi_awareness()
    _write_sidecar_status("starting", phase="hint_cache_load")
    hint_cache = load_overlay_hint_cache()
    runtime = load_or_build_default_template_runtime(
        hint_cache=hint_cache,
        status_callback=lambda phase, fields: _write_sidecar_status(
            "starting",
            phase=phase,
            startup_seconds=round(time.perf_counter() - started_at, 3),
            **dict(fields),
        ),
    )
    template_index = runtime.template_index
    tracker = SelectionTracker(scene_enter_frames=max(1, int(required_frames)))
    if not template_index:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "template_missing"})
    else:
        event = tracker.block("warming_up")
        for index in range(max(1, int(required_frames))):
            if is_scoreboard_key_down():
                event = tracker.block("scoreboard_key_down", scoreboard_key_down=True)
                break
            frame = capture_lol_game_frame()
            if frame is None:
                event = tracker.block("capture_unavailable")
                break
            raw_event = detect_overlay_choices(
                frame,
                template_index,
                preset_name=preset,
                min_confidence=min_confidence,
            )
            event = tracker.update(raw_event)
            if debug_dump_dir and index == 0:
                _write_roi_diagnostic_dump(debug_dump_dir, frame, event)
            if index + 1 < max(1, int(required_frames)):
                time.sleep(max(0, int(frame_interval_ms)) / 1000.0)

    if write_event:
        write_overlay_event(_public_event_payload(event), event_path)
        try:
            write_vision_trace_if_changed(event, _vision_trace_path_for_event(event_path))
        except OSError:
            logger.debug("写入 Vision trace 失败。", exc_info=True)
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
    """常驻 V2 视觉循环；场景和槽位分别稳定，非前台时低频待机。

    循环状态机：
    1. 检测前台窗口 → 非前台进入 idle 模式（低频待机）
    2. 检测阻塞弹窗/计分板 → 写阻塞事件，进入 blocked 模式
    3. 截图 → 场景检测（按钮/面板）→ 槽位识别（图标+文字双通道）
    4. SelectionTracker 累积帧数 → 稳定后写 overlay 事件
    5. debug_dump_dir 不为空时自动转储前 LOOP_DEBUG_DUMP_MAX_WINDOWS 个选择窗口
    """

    started_at = time.perf_counter()
    _set_dpi_awareness()
    _write_sidecar_status("starting", phase="hint_cache_load")
    hint_cache = load_overlay_hint_cache()
    runtime = load_or_build_default_template_runtime(
        hint_cache=hint_cache,
        status_callback=lambda phase, fields: _write_sidecar_status(
            "starting",
            phase=phase,
            startup_seconds=round(time.perf_counter() - started_at, 3),
            **dict(fields),
        ),
    )
    template_index = runtime.template_index
    trace_path = _vision_trace_path_for_event(event_path)
    tracker = SelectionTracker(scene_enter_frames=max(1, int(required_frames)))

    def write_runtime_trace(event_payload: Mapping[str, Any]) -> None:
        if not write_event:
            return
        try:
            write_vision_trace_if_changed(event_payload, trace_path)
        except OSError:
            logger.debug("写入 Vision trace 失败。", exc_info=True)

    if not template_index:
        event = _build_loop_inactive_event("template_missing", poll_mode="idle")
        if write_event:
            write_overlay_event(_public_event_payload(event), event_path)
            write_runtime_trace(event)
        logger.error("Vision sidecar 模板缺失，已退出。")
        return event

    last_signature: tuple[str, ...] | None = None
    last_write_at = 0.0
    idle_sleep_seconds = max(0.12, float(idle_interval_seconds))
    dump_root = Path(debug_dump_dir) if debug_dump_dir else None
    last_dump_signature: tuple[str, ...] | None = None
    tab_was_down = False

    def commit_event(event_payload: dict[str, Any], *, poll_mode: str) -> None:
        nonlocal last_signature, last_write_at
        source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
        event_payload["source"] = dict(source)
        event_payload["source"]["poll_mode"] = poll_mode
        write_runtime_trace(event_payload)
        now = time.time()
        if write_event and should_write_loop_event(
            event_payload,
            last_signature=last_signature,
            last_write_at=last_write_at,
            now=now,
            heartbeat_seconds=heartbeat_seconds,
        ):
            write_overlay_event(_public_event_payload(event_payload), event_path)
            last_signature = _loop_event_signature(event_payload)
            last_write_at = now

    def maybe_dump(frame: Image.Image, event_payload: Mapping[str, Any]) -> None:
        nonlocal last_dump_signature
        if dump_root is None:
            return
        source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
        raw_slots = event_payload.get("_raw_slots") if isinstance(event_payload.get("_raw_slots"), list) else []
        top_ids: list[str] = []
        for slot in raw_slots[:SLOT_COUNT]:
            candidates = slot.get("top_candidates") if isinstance(slot, Mapping) and isinstance(slot.get("top_candidates"), list) else []
            top = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
            top_ids.append(str(top.get("augment_id") or top.get("name") or ""))
        signature = (
            str(source.get("scene_state") or ""),
            str(source.get("reason") or ""),
            str(source.get("ready_slots") or 0),
            str(source.get("scoreboard_key_down") or False),
            *top_ids,
        )
        should_dump = bool(
            signature != last_dump_signature
            and (
                source.get("scoreboard_key_down")
                or source.get("scene_state") in {"active", "candidate"}
                or int(source.get("ready_slots") or 0) < SLOT_COUNT
            )
        )
        if should_dump:
            try:
                _write_roi_diagnostic_dump(dump_root, frame, event_payload)
            except OSError:
                logger.debug("V2 ROI 诊断转储失败。", exc_info=True)
            last_dump_signature = signature

    logger.info(
        "Vision sidecar V2 已启动：frame_interval_ms=%s heartbeat_seconds=%.1f",
        int(frame_interval_ms),
        float(heartbeat_seconds),
    )
    _write_sidecar_ready_from_env(
        template_count=len(template_index),
        started_at=started_at,
        startup_profile=runtime.stats,
    )

    while True:
        if _sidecar_exit_requested():
            logger.info("Vision sidecar 收到 graceful exit 信号，准备退出。")
            return None
        frame_started_at = time.perf_counter()
        target = _find_lol_game_window()
        if target is None:
            event = tracker.block("game_window_missing")
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue

        hwnd, rect = target
        if not _is_lol_game_foreground(hwnd):
            event = tracker.block("game_not_foreground")
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue

        tab_down = is_scoreboard_key_down()
        if tab_down:
            event = tracker.block("scoreboard_key_down", scoreboard_key_down=True)
            if dump_root is not None and not tab_was_down:
                frame = _capture_lol_game_rect(rect)
                if frame is not None:
                    maybe_dump(frame, event)
            tab_was_down = True
            commit_event(event, poll_mode="high")
            time.sleep(
                remaining_frame_sleep_seconds(
                    frame_interval_ms,
                    elapsed_seconds=time.perf_counter() - frame_started_at,
                )
            )
            continue
        tab_was_down = False

        frame = _capture_lol_game_rect(rect)
        if frame is None:
            event = tracker.block("capture_unavailable")
        else:
            raw_event = detect_overlay_choices(
                frame,
                template_index,
                preset_name=preset,
                min_confidence=min_confidence,
            )
            raw_source = raw_event.get("source") if isinstance(raw_event.get("source"), Mapping) else {}
            raw_event["source"] = dict(raw_source)
            raw_event["source"]["cursor_over_cards"] = _cursor_over_card_panels(
                rect,
                frame.size,
                raw_event["source"],
            )
            event = tracker.update(raw_event)
            maybe_dump(frame, event)

        commit_event(event, poll_mode="high")
        time.sleep(
            remaining_frame_sleep_seconds(
                frame_interval_ms,
                elapsed_seconds=time.perf_counter() - frame_started_at,
            )
        )


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

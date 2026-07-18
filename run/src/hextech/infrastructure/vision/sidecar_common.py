# ruff: noqa: F401
# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportCallIssue=false
# pyright: reportOperatorIssue=false, reportOptionalIterable=false
# pyright: reportOptionalMemberAccess=false, reportOptionalSubscript=false
"""游戏内 overlay Vision V2 运行入口。

本模块负责截图、模板装载、双字体/多图标变体评分和事件/诊断写入；版式检测、
候选规则、跨帧状态分别位于 ``hextech.modules.vision.layout``、
``hextech.infrastructure.vision.matcher`` 和 ``hextech.infrastructure.vision.state``。
它不读游戏内存、不注入客户端、不自动点击，也不联网。

调用方: bootstrap.overlay、tooling checks; 关键依赖: runtime.python_environment、numpy、overlay.events。
"""

from __future__ import annotations

from hextech.modules.session.python_environment import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source(module_name="hextech.infrastructure.vision.sidecar")

import ctypes
import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageGrab

from hextech.modules.vision.events import OVERLAY_EVENT_FILE, build_overlay_event
from hextech.modules.recommendation.hints import normalize_augment_id
from hextech.infrastructure.vision import template_runtime as _template_runtime_module
from hextech.modules.vision.layout import (
    BUTTON_SEARCH_REGION as V2_BUTTON_SEARCH_REGION,
    LayoutTransform,
    apply_transform,
    detect_selection_scene,
    pick_card_panels,
)
# runner/dev_checks 通过 sidecar 注入按键状态，保留该兼容 re-export。
from hextech.modules.vision.window import (  # noqa: F401
    cursor_in_client_boxes,
    find_lol_game_window,
    is_left_mouse_button_down,
    is_scoreboard_key_down,
    root_window_hwnd,
)
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_augment_name_to_icon_map
from hextech.modules.data.ports.paths import INDEX_DATA_DIR
from hextech.modules.data.ports.atomic import atomic_write_json

try:
    import win32gui
except ImportError:  # pragma: no cover - Windows 之外只允许离线测试纯函数
    win32gui = None


SLOT_COUNT = 3
TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION = _template_runtime_module.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION
TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE = _template_runtime_module.TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE
TEMPLATE_RUNTIME_CACHE_V1_FILE = _template_runtime_module.TEMPLATE_RUNTIME_CACHE_V1_FILE
TEMPLATE_RUNTIME_CACHE_FILE = _template_runtime_module.TEMPLATE_RUNTIME_CACHE_FILE
TemplateRuntime = _template_runtime_module.TemplateRuntime
TemplateEntry = _template_runtime_module.TemplateEntry
TemplateIndex = _template_runtime_module.TemplateIndex
_RankMatrices = _template_runtime_module._RankMatrices
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
DEFAULT_LOOP_FRAME_INTERVAL_MS = 80         # --loop fast 模式帧间隔（毫秒）
DEFAULT_LOOP_SCAN_FRAME_INTERVAL_MS = 160   # 前台但未进入选择态时的扫描间隔
DEFAULT_LOOP_IDLE_INTERVAL_SECONDS = 0.25   # 空闲等待间隔
DEFAULT_LOOP_FAST_HOLD_SECONDS = 1.2        # 最近疑似选择态后保持 fast 的窗口
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



__all__ = [name for name in globals() if not name.startswith("__")]

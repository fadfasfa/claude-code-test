# ruff: noqa: F401
"""桌面 UI 主入口。

这个文件保留 Tk 界面结构、主状态对象和主要交互方法。
后台线程、Web 协同、LCU 查询和头像下载等运行时细节委托给 `hextech.interfaces.desktop.runtime`，
以便在保持热路径聚合的前提下，让后续需求变更有明确落点。

调用方: display.desktop.runtime、hextech_ui、tests.test_desktop_diagnostics_button; 关键依赖: data_snapshot、core.settings、overlay.events。
"""

import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

import tkinter as tk
from hextech.modules.session.settings import load_ui_feature_flags, save_ui_feature_flags

from . import runtime as ui_runtime

from .startup_timing import StartupTimingProbe, build_desktop_runtime_state_path
from .single_instance import DesktopInstanceAlreadyRunning, DesktopInstanceOwner

if TYPE_CHECKING:
    from .service_manager import ServiceManager

WEB_PORT_FILE = str(build_desktop_runtime_state_path("web_server_port.txt"))
# 悬浮窗固定几何（px，不乘 DPI）：按用户要求维持基线"狭长"观感；
# 高度基值 740，跟随客户端时由 runtime_window 按客户端底缘动态压缩。
WINDOW_EXPANDED_WIDTH = 320
# 折叠态预算：左 padding 10 + 胜率色条 3+5 + 头像 48+2(边框) + 间距 8 + T 级徽章 ≈ 98px；
# 80px 会把头像裁半、徽章挤出视口（审查用真实 Tk 实测确认），留余量取 112。
WINDOW_COLLAPSED_WIDTH = 112
WINDOW_BASE_HEIGHT = 740
# 跟随限高的下限：极矮客户端下宁可轻微越界也不把窗口压到不可用
WINDOW_MIN_FOLLOW_HEIGHT = 320
# 取值对齐游戏内 Overlay 的 OVERLAY_THEME（canvas_renderer.py）拳头金蓝系；
# 刻意不 import overlay 模块，避免与 Overlay 侧实现互相耦合。
UI_COLORS = {
    "base": "#091428",
    "header": "#0A1428",
    "surface": "#13233A",
    "surface_alt": "#0E1B2E",
    "border": "#785A28",
    "gold": "#C8AA6E",
    "cyan": "#0AC8B9",
    "green": "#3AA17E",
    "red": "#C45D5B",
    "text": "#F0E6D2",
    "muted": "#A09B8C",
    "dim": "#5C5B57",
    "warn": "#FFB23E",
    "error": "#F38BA8",
}

# T1–T5 徽章色阶：金 → 青 → 蓝 → 灰 → 暗红；前景色保证与底色的对比度
TIER_COLORS: dict[str, dict[str, str]] = {
    "T1": {"bg": "#C8AA6E", "fg": "#0A1428"},
    "T2": {"bg": "#0AC8B9", "fg": "#0A1428"},
    "T3": {"bg": "#4B7CF3", "fg": "#F0E6D2"},
    "T4": {"bg": "#5C6B7A", "fg": "#F0E6D2"},
    "T5": {"bg": "#8C3B45", "fg": "#F0E6D2"},
}


def window_dpi_scale(root) -> float:
    """读取窗口实际 DPI 缩放（GetDpiForWindow/96）；失败回退 1.0。

    进程已声明 System DPI Aware，这里参照 overlay/host_platform 的做法
    做实时缩放，避免高分屏上像素常量整体偏小。
    """

    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(int(root.winfo_id()))
        if dpi:
            return max(1.0, float(dpi) / 96.0)
    except Exception:
        logger.debug("读取窗口 DPI 失败，按 1.0 缩放。", exc_info=True)
    return 1.0


def scaled(px: int, scale: float) -> int:
    """像素常量乘 DPI 缩放并取整，下限 1px。"""

    return max(1, round(px * scale))


def ui_font(scale: float, size_px: int, bold: bool = False) -> tuple:
    """构造按 DPI 缩放的字体元组。

    Tk 负数字号表示像素，避免 Windows DPI 把点值字号再次缩放
    （做法对齐 overlay canvas_renderer 的注释约定）。
    """

    size = -scaled(size_px, scale)
    if bold:
        return ("Microsoft YaHei", size, "bold")
    return ("Microsoft YaHei", size)


def resolve_overlay_follow_height(
    target_y: int, client_bottom: int, workarea_bottom: int | None = None
) -> int:
    """跟随客户端时的悬浮窗高度：下端不越过客户端底缘。

    以 740 为基值；客户端更矮时压缩到"客户端底缘 - 窗口顶部"（有工作区信息时
    与工作区底缘取更小者），下限保证极矮客户端下窗口仍可用。
    """

    bottom_limit = int(client_bottom)
    if workarea_bottom is not None:
        bottom_limit = min(bottom_limit, int(workarea_bottom))
    return max(WINDOW_MIN_FOLLOW_HEIGHT, min(WINDOW_BASE_HEIGHT, bottom_limit - int(target_y)))


def _format_game_overlay_host_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    return {
        "user_disabled": "已关闭",
        "gameflow_not_in_progress": "等待实际对局",
        "waiting_gameflow": "等待游戏状态",
        "game_window_missing": "等待游戏窗口",
        "game_window_not_renderable": "游戏窗口不可渲染",
        "game_not_foreground": "切回游戏后显示",
        "selection_window_inactive": "等待海克斯选择",
        "waiting_selection": "等待海克斯选择",
        "event_stale_after_tab": "等待最新选择画面",
        "event_expired": "选择数据已过期",
        "blocking_modal_present": "等待弹窗关闭",
        "scoreboard_key_down": "记分板显示中",
        "transient_pause": "切回游戏后继续识别",
        "visible_detecting": "检测选择中",
        "visible_partial": "部分识别",
        "visible_ready": "已显示",
    }.get(reason, "暂不显示")


def _format_supervisor_game_overlay_status(overlay: Mapping[str, object]) -> tuple[str, str]:
    """把 Supervisor overlay 组件状态压成游戏内显示的二级状态短句。"""

    build_id = str(overlay.get("build_id") or "").strip()
    build_suffix = f" / 构建 {build_id[:18]}" if build_id else ""

    def result(text: str, color: str) -> tuple[str, str]:
        return (text + build_suffix, color)

    status = str(overlay.get("status") or "").strip()
    phase = str(overlay.get("phase") or "").strip()
    cache_status = str(overlay.get("cache_status") or "").strip()
    context_status = str(overlay.get("context_status") or "").strip()
    visible_reason = str(overlay.get("visible_reason") or "").strip()
    functional_status = str(overlay.get("functional_status") or "unknown").strip()
    functional_reason = str(overlay.get("functional_reason") or "").strip()
    sidecar_liveness = overlay.get("sidecar_liveness")
    sidecar_state = (
        str(sidecar_liveness.get("status") or "unknown").strip()
        if isinstance(sidecar_liveness, Mapping)
        else "unknown"
    )
    sidecar_reason = (
        str(sidecar_liveness.get("reason") or "").strip()
        if isinstance(sidecar_liveness, Mapping)
        else ""
    )
    last_error = str(overlay.get("last_error") or "").strip()
    if status == "error":
        return result(f"游戏内显示异常: {last_error or phase or '未知错误'}", UI_COLORS["error"])
    if status == "stale":
        return result(f"游戏内显示: 识别进程失效 / {sidecar_reason or '等待自动恢复'}", UI_COLORS["warn"])
    if status == "stopping":
        return result("游戏内显示: 正在关闭", UI_COLORS["warn"])
    if status == "stopped":
        if cache_status in {"queued", "prewarming", "lookup", "building"}:
            return result("游戏内显示: 海克斯卡识别模板预热中", UI_COLORS["warn"])
        if cache_status == "ready":
            return result("游戏内显示: 识别模板已预热", UI_COLORS["muted"])
        return result("游戏内显示: 已关闭", UI_COLORS["muted"])
    if status == "starting":
        if phase == "sidecar_restart":
            return result("游戏内显示: 识别重启中", UI_COLORS["warn"])
        if phase == "vision_prewarming":
            return result("游戏内显示: 窗口已就绪 / 海克斯卡识别模板预热中", UI_COLORS["warn"])
        if phase in {"prepare_data", "context_start"}:
            return result("游戏内显示: 正在准备数据", UI_COLORS["warn"])
        if phase == "sidecar_start":
            return result("游戏内显示: 正在启动识别", UI_COLORS["warn"])
        if phase == "host_start":
            return result("游戏内显示: 正在启动窗口", UI_COLORS["warn"])
        return result("游戏内显示: 正在启动", UI_COLORS["warn"])
    if status == "running":
        reason = _format_game_overlay_host_reason(visible_reason) if visible_reason else "等待选择窗口"
        if sidecar_state in {"stale", "failed"}:
            return result(f"游戏内显示: 识别进程失效 / {sidecar_reason or '等待自动恢复'}", UI_COLORS["warn"])
        if bool(overlay.get("build_mismatch")):
            return result("游戏内显示: 构建身份不一致 / 请重新部署", UI_COLORS["error"])
        if functional_status == "failed":
            return result(f"游戏内显示异常: {functional_reason or 'Host 功能不可用'}", UI_COLORS["error"])
        if context_status == "context_missing":
            return result(f"游戏内显示: {reason} / 等待当前英雄", UI_COLORS["warn"])
        if functional_status == "degraded":
            return result(f"游戏内显示: {reason} / {functional_reason or '功能降级'}", UI_COLORS["warn"])
        if context_status == "degraded":
            return result(f"游戏内显示: {reason} / 上下文降级", UI_COLORS["warn"])
        if cache_status in {"queued", "prewarming", "lookup", "building"}:
            return result(f"游戏内显示: {reason} / 海克斯卡识别模板预热中", UI_COLORS["warn"])
        return result(f"游戏内显示: {reason} / 识别已就绪", UI_COLORS["green"])
    return result("游戏内显示: 等待 Supervisor 状态", UI_COLORS["warn"])

logger = logging.getLogger(__name__)


def _empty_champions() -> list[dict]:
    """桌面只消费快照 DTO，不再把只读 generation 转回 DataFrame。"""

    return []


def export_user_diagnostics(*args, **kwargs):
    """延迟导入诊断导出，避免首屏加载 runtime_store 数据栈。"""

    from hextech.modules.session.diagnostics import export_user_diagnostics as _export_user_diagnostics

    return _export_user_diagnostics(*args, **kwargs)


def _lerp_hex_color(color_a: str, color_b: str, t: float) -> str:
    """两端十六进制颜色在 RGB 空间按比例 t 插值，返回 #rrggbb 形式。"""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(color_a[1:3], 16), int(color_a[3:5], 16), int(color_a[5:7], 16)
    br, bg, bb = int(color_b[1:3], 16), int(color_b[3:5], 16), int(color_b[5:7], 16)
    rr = int(ar + (br - ar) * t)
    rg = int(ag + (bg - ag) * t)
    rb = int(ab + (bb - ab) * t)
    return f"#{rr:02x}{rg:02x}{rb:02x}"


def _render_winrate_bar(canvas: "tk.Canvas", width: int, ratio: float, height: int = 4) -> None:
    """绘制胜率条：填充区域使用暗红→中性灰青→青绿三段渐变，并在 50% 处标记温饱基准线。"""
    canvas.delete("all")
    if width <= 0:
        return
    fill_px = max(0, int(ratio * width))
    # 三段色板：暗红 → 中性灰青（50% 锚点）→ 青绿
    color_low = "#5b3037"
    color_mid = "#5c6d75"
    color_high = "#3aa17e"
    segments = 24
    for i in range(segments):
        x0 = int(fill_px * i / segments)
        x1 = int(fill_px * (i + 1) / segments)
        if x1 <= x0:
            continue
        # 段中心点对应的归一化位置（0~1 映射回 0.40~0.60 真实胜率空间）
        center_ratio = (i + 0.5) / segments * ratio if ratio > 0 else 0
        if center_ratio < 0.5:
            seg_color = _lerp_hex_color(color_low, color_mid, center_ratio / 0.5)
        else:
            seg_color = _lerp_hex_color(color_mid, color_high, (center_ratio - 0.5) / 0.5)
        canvas.create_rectangle(x0, 0, x1, height, fill=seg_color, outline="")
    # 50% 温饱基准线（胜率 0.50 对应归一化 ratio=0.5）
    baseline_x = int(width * 0.5)
    canvas.create_line(baseline_x, 0, baseline_x, height, fill="#6c7086", dash=(2, 2))



__all__ = [name for name in globals() if not name.startswith("__")]

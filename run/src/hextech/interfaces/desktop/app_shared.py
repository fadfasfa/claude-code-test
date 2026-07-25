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
from typing import TYPE_CHECKING, TypedDict

import tkinter as tk
from hextech.modules.session.settings import load_ui_feature_flags, save_ui_feature_flags

from . import runtime as ui_runtime

from .startup_timing import StartupTimingProbe, build_desktop_runtime_state_path
from .single_instance import DesktopInstanceAlreadyRunning, DesktopInstanceOwner

if TYPE_CHECKING:
    from .service_manager import ServiceManager

WEB_PORT_FILE = str(build_desktop_runtime_state_path("web_server_port.txt"))
WINDOW_EXPANDED_GEOMETRY = "320x740"
WINDOW_COLLAPSED_GEOMETRY = "80x740"
UI_COLORS = {
    "base": "#010A13",
    "header": "#050F1B",
    "surface": "#111C2E",
    "surface_alt": "#0B1626",
    "border": "#2A3B55",
    "gold": "#C89B3C",
    "cyan": "#2DD4BF",
    "green": "#32D784",
    "red": "#C45D5B",
    "text": "#F5F8FF",
    "muted": "#9EAABC",
    "dim": "#667188",
    "warn": "#F5C26B",
    "error": "#F38BA8",
}


class _SelectionRoleStyle(TypedDict):
    accent: str
    surface: str
    text: str
    border_width: int
    marker_width: int


def _selection_role_style(role: str) -> _SelectionRoleStyle:
    """返回英雄角色的稳定视觉语义；样式不参与排序或卡片布局。"""

    if role == "self":
        return {"accent": "#22D3EE", "surface": "#0B2A30", "text": "#062A30", "border_width": 3, "marker_width": 5}
    if role == "teammate":
        return {"accent": "#F59E0B", "surface": "#2A2110", "text": "#2D1B00", "border_width": 2, "marker_width": 3}
    return {"accent": UI_COLORS["border"], "surface": UI_COLORS["surface"], "text": UI_COLORS["text"], "border_width": 1, "marker_width": 0}


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


def _render_winrate_bar(canvas: "tk.Canvas", width: int, ratio: float) -> None:
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
        canvas.create_rectangle(x0, 0, x1, 4, fill=seg_color, outline="")
    # 50% 温饱基准线（胜率 0.50 对应归一化 ratio=0.5）
    baseline_x = int(width * 0.5)
    canvas.create_line(baseline_x, 0, baseline_x, 4, fill="#6c7086", dash=(2, 2))



__all__ = [name for name in globals() if not name.startswith("__")]

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
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import tkinter as tk
from hextech.modules.session.settings import load_ui_feature_flags, save_ui_feature_flags

from . import runtime as ui_runtime

from .startup_timing import StartupTimingProbe, build_desktop_runtime_state_path
from .single_instance import (
    DesktopBuildConflict,
    DesktopInstanceAlreadyRunning,
    DesktopInstanceOwner,
    show_build_conflict_message,
)

if TYPE_CHECKING:
    from .service_manager import ServiceManager

WEB_PORT_FILE = str(build_desktop_runtime_state_path("web_server_port.txt"))
# 悬浮窗固定几何（px，不乘 DPI）：按用户要求维持基线"狭长"观感；
# 高度基值 740，跟随客户端时由 runtime_window 按客户端底缘动态压缩。
WINDOW_EXPANDED_WIDTH = 320
WINDOW_BASE_HEIGHT = 740
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
    "selected": "#F2C94C",
    "teammate": "#18D6C4",
    "green": "#3AA17E",
    "red": "#C45D5B",
    "text": "#F0E6D2",
    "muted": "#A09B8C",
    "dim": "#5C5B57",
    "warn": "#FFB23E",
    "error": "#F38BA8",
}

# T1–T5 高饱和电竞色阶：强度色条与徽章共用，前景色保证可读性。
TIER_COLORS: dict[str, dict[str, str]] = {
    "T1": {"bg": "#F2C94C", "fg": "#07111F"},
    "T2": {"bg": "#22D3A6", "fg": "#07111F"},
    "T3": {"bg": "#4169E1", "fg": "#F0E6D2"},
    "T4": {"bg": "#7F8C9D", "fg": "#07111F"},
    "T5": {"bg": "#D64550", "fg": "#F0E6D2"},
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


def ui_font(size_px: int, bold: bool = False) -> tuple:
    """构造随系统 DPI 缩放的字体元组。

    使用正数磅值（1px@96dpi = 0.75pt）：进程已声明 DPI Aware，Tk 会把磅值按
    真实 DPI 换算成像素（px × dpi/96），与 main 基线观感逐点一致。字体由 Tk
    隐式补偿 DPI，几何常量（窗宽/头像/padding）保持物理像素并与 _ui_scale=1.0
    解耦——上一轮"锁几何时误伤字体"的回归由此被结构性消除。
    """

    size = max(1, round(size_px * 0.75))
    if bold:
        return ("Microsoft YaHei", size, "bold")
    return ("Microsoft YaHei", size)


def parse_generation_created_ts(created_at: object) -> float:
    """把 generation 的 created_at（UTC ISO 串）解析为 epoch 秒；失败返回 0.0。

    0.0 表示"时效未知"，状态行据此隐藏"数据 X 前"后缀，不虚构时间。
    """

    text = str(created_at or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def snapshot_data_timestamp(status: Mapping[str, object]) -> float:
    """桌面时效优先使用 ARAMKit 的真实 data_at，旧代缺字段才回退 generation。"""

    source_status = status.get("source_status")
    aramkit = source_status.get("aramkit") if isinstance(source_status, Mapping) else None
    data_at = aramkit.get("data_at") if isinstance(aramkit, Mapping) else None
    if str(data_at or "").strip():
        return parse_generation_created_ts(data_at)
    return parse_generation_created_ts(status.get("created_at"))


def format_data_age_suffix(created_ts: float, now_ts: float) -> str:
    """状态行的数据时效后缀：小时级粒度，≥24h 换天，未知返回空串。"""

    if created_ts <= 0:
        return ""
    hours = int(max(now_ts - created_ts, 0) // 3600)
    if hours < 1:
        return " · 数据刚更新"
    if hours >= 24:
        return f" · 数据 {hours // 24} 天前"
    return f" · 数据 {hours} 小时前"


def data_refresh_status_signature(status: Mapping[str, object]) -> tuple[object, ...]:
    """Build the immutable UI de-duplication key from the service-owned result."""

    raw_outcomes = status.get("source_outcomes")
    source_outcomes = tuple(sorted(
        (
            str(source),
            str(outcome.get("state") or ""),
            str(outcome.get("availability") or ""),
            bool(outcome.get("used_last_good")),
            str(outcome.get("reason_code") or ""),
            str(outcome.get("check_status") or ""),
            str(outcome.get("upstream_revision") or ""),
            str(outcome.get("applied_revision") or ""),
            str(outcome.get("last_checked_at") or ""),
        )
        for source, outcome in raw_outcomes.items()
        if isinstance(outcome, Mapping)
    )) if isinstance(raw_outcomes, Mapping) else ()
    return (
        str(status.get("state") or "idle"),
        str(status.get("scope") or ""),
        str(status.get("phase") or ""),
        str(status.get("reason_code") or ""),
        str(status.get("generation_id") or ""),
        bool(status.get("checked")),
        bool(status.get("content_changed")),
        bool(status.get("catalog_changed")),
        bool(status.get("last_good_available")),
        str(status.get("catalog_state") or ""),
        source_outcomes,
        tuple(str(item) for item in status.get("pending_sources") or []),
        float(status.get("started_at") or 0.0),
        float(status.get("completed_at") or 0.0),
    )


def log_data_refresh_result(status: Mapping[str, object]) -> None:
    state = str(status.get("state") or "idle")
    if state not in {"completed", "unchanged", "failed", "deferred"}:
        return
    signature = data_refresh_status_signature(status)
    outcomes = signature[10]
    logger.info(
        "DataService 检测结果 state=%s checked=%s content_changed=%s catalog_changed=%s sources=%s",
        state,
        bool(status.get("checked")),
        bool(status.get("content_changed")),
        bool(status.get("catalog_changed")),
        ",".join(f"{source}:{outcome_state}" for source, outcome_state, *_ in outcomes),
    )


def format_data_refresh_status(status: Mapping[str, object]) -> tuple[str, str]:
    """把 DataService refresh_status 压成桌面单行状态与颜色。"""

    state = str(status.get("state") or "idle")
    scope = str(status.get("scope") or "due")
    reason = str(status.get("reason_code") or "")
    phase = str(status.get("phase") or "")
    optional_sources = status.get("optional_sources")
    optional_failed = isinstance(optional_sources, Mapping) and any(
        isinstance(item, Mapping) and item.get("state") == "failed" for item in optional_sources.values())
    source_outcomes = status.get("source_outcomes")
    outcomes = source_outcomes if isinstance(source_outcomes, Mapping) else {}
    authoritative = all(key in status for key in ("checked", "content_changed", "catalog_changed"))
    checked_sources = {
        str(source)
        for source, value in outcomes.items()
        if isinstance(value, Mapping)
        and (
            value.get("checked") is True
            or str(value.get("check_status") or "") in {"up_to_date", "changed"}
        )
    }
    all_not_due = bool(outcomes) and all(
        isinstance(value, Mapping)
        and str(value.get("state") or "") in {"not_due", "deferred"}
        for value in outcomes.values()
    )

    def terminal_copy(default: str) -> tuple[str, str]:
        if not authoritative:
            return default, UI_COLORS["green"]
        if all_not_due:
            return ("尚未到检测时间", UI_COLORS["muted"])
        if status.get("checked") is not True:
            has_last_good = bool(status.get("last_good_available")) or any(
                isinstance(value, Mapping) and value.get("used_last_good") is True
                for value in outcomes.values()
            )
            return (
                "刷新未完成，沿用旧数据" if has_last_good else "刷新未完成",
                UI_COLORS["error"],
            )
        if outcomes and not checked_sources:
            if status.get("catalog_state") == "deferred":
                return ("识别资源赛后更新", UI_COLORS["warn"])
            return ("检测状态未知", UI_COLORS["warn"])
        if status.get("content_changed"):
            text = "数据已更新"
        elif status.get("catalog_changed"):
            text = "识别目录已更新"
        else:
            text = "已检来源与上游一致" if checked_sources else "已检查，与上游一致"
        blitz = outcomes.get("blitz")
        if isinstance(blitz, Mapping):
            if blitz.get("state") == "confirmed_empty":
                text += " · Blitz 已确认无记录"
            elif blitz.get("state") == "last_good":
                text += " · Blitz 沿用旧源"
            elif blitz.get("state") == "unavailable":
                text += " · Blitz 暂不可用"
        if status.get("catalog_state") == "deferred":
            text += " · 识别资源赛后更新"
        if any(
            source != "blitz"
            and isinstance(value, Mapping)
            and value.get("state") in {"last_good", "unavailable"}
            for source, value in outcomes.items()
        ):
            text += " · 部分源待重试"
        warned = status.get("catalog_state") == "deferred" or optional_failed or any(
            isinstance(value, Mapping) and value.get("state") in {"last_good", "unavailable"}
            for source, value in outcomes.items()
            if source in {"blitz", "apex", "mayhem"}
        )
        return text, UI_COLORS["warn" if warned else "green"]
    if state == "core_ready":
        return ("核心数据已就绪，后台补齐其他英雄", UI_COLORS["green"])
    if state == "running" and (
        reason in {"local_missing", "local_corrupt", "repair_missing"}
        or phase in {"repair", "repairing"}
    ):
        return ("本地数据缺失，正在补齐", UI_COLORS["warn"])
    if state == "running" and any(
        isinstance(value, Mapping) and str(value.get("check_status") or "") == "changed"
        for value in outcomes.values()
    ):
        return ("发现更新，正在更新", UI_COLORS["warn"])
    if state == "running" and phase in {"rankings", "details", "ranking_fallback", "download"}:
        labels = {"rankings": "正在检测英雄排行", "details": "正在核对英雄海克斯详情",
                  "ranking_fallback": "正在检测补充排名", "download": "正在检测来源资料"}
        return (labels[phase], UI_COLORS["warn"])
    if state in {"queued", "running"}:
        if reason == "resumed_after_game":
            return ("赛后继续检测", UI_COLORS["warn"])
        if scope == "core":
            return ("正在检测核心数据", UI_COLORS["warn"])
        return ("正在检查数据更新", UI_COLORS["warn"])
    if state == "deferred":
        return ("对局中暂停，赛后继续", UI_COLORS["warn"])
    if state == "completed":
        if authoritative:
            return terminal_copy("数据已更新")
        if reason == "core_complete_optional_failed" or optional_failed:
            return ("核心数据可用，可选来源待重试", UI_COLORS["warn"])
        return ("数据处理完成，更新状态未知", UI_COLORS["warn"])
    if state == "unchanged":
        if authoritative:
            return terminal_copy("数据已检查，无变化")
        if status.get("catalog_state") == "deferred":
            return ("核心数据已检查，无变化；识别资源赛后更新", UI_COLORS["warn"])
        optional = status.get("optional")
        if reason == "core_complete_optional_failed" or optional_failed or (isinstance(optional, Mapping) and optional.get("state") == "failed"):
            return ("核心数据已检查，无变化；可选来源待重试", UI_COLORS["warn"])
        return ("数据已检查，无变化", UI_COLORS["green"])
    if state == "failed":
        if authoritative:
            has_last_good = bool(status.get("last_good_available")) or any(
                isinstance(value, Mapping) and value.get("used_last_good") is True
                for value in outcomes.values()
            )
            return (
                "检测失败，继续使用已验证数据" if has_last_good else "检测失败，暂无可用数据",
                UI_COLORS["error"],
            )
        return ("检测失败，继续使用已验证数据", UI_COLORS["error"])
    return ("", UI_COLORS["muted"])


def resolve_overlay_follow_height(
    target_y: int, client_bottom: int, workarea_bottom: int | None = None
) -> int:
    """跟随客户端时的悬浮窗高度：下端不越过客户端底缘。

    以 740 为基值；客户端更矮时压缩到"客户端底缘 - 窗口顶部"（有工作区信息时
    与工作区底缘取更小者）。1px 只用于防御异常零高几何；正常跟随目标始终位于
    客户端底缘上方，因此窗口不会越过客户端或工作区底缘。
    """

    bottom_limit = int(client_bottom)
    if workarea_bottom is not None:
        bottom_limit = min(bottom_limit, int(workarea_bottom))
    return max(1, min(WINDOW_BASE_HEIGHT, bottom_limit - int(target_y)))


def _format_game_overlay_host_reason(reason: str) -> str:
    reason = str(reason or "").strip()
    return {
        "user_disabled": "已关闭",
        "gameflow_not_in_progress": "等待实际对局",
        "waiting_gameflow": "等待游戏状态",
        "game_window_missing": "等待游戏窗口",
        "game_window_not_renderable": "游戏窗口不可渲染",
        "unsupported_fullscreen_mode": "请在游戏内切换无边框",
        # 无法确认时仍由 Host fail-closed；Desktop 不持续占用状态栏催促用户。
        "game_window_mode_unknown": "",
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
    """把 Supervisor overlay 组件状态压成单行状态栏可容纳的短语。

    构建号不再拼进文案：320px 窗口装不下整串且用户不消费它，构建身份仍在
    supervisor 状态文件与诊断导出里；sidecar 细节原因同理留在日志。
    """

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
    last_error = str(overlay.get("last_error") or "").strip()
    if status == "error":
        return (f"显示异常 · {last_error or phase or '未知错误'}", UI_COLORS["error"])
    if status == "stale":
        return ("识别失效 · 自动恢复中", UI_COLORS["warn"])
    if status == "stopping":
        return ("正在关闭", UI_COLORS["warn"])
    if status == "stopped":
        if cache_status in {"queued", "prewarming", "lookup", "building"}:
            return ("模板预热中", UI_COLORS["warn"])
        if cache_status == "ready":
            return ("模板已预热", UI_COLORS["muted"])
        return ("已关闭", UI_COLORS["muted"])
    if status == "starting":
        if phase == "sidecar_restart":
            return ("识别重启中", UI_COLORS["warn"])
        if phase == "vision_prewarming":
            return ("窗口就绪 · 模板预热中", UI_COLORS["warn"])
        if phase in {"prepare_data", "context_start"}:
            return ("准备数据中", UI_COLORS["warn"])
        if phase == "sidecar_start":
            return ("识别启动中", UI_COLORS["warn"])
        if phase == "host_start":
            return ("窗口启动中", UI_COLORS["warn"])
        return ("正在启动", UI_COLORS["warn"])
    if status == "running":
        reason = _format_game_overlay_host_reason(visible_reason) if visible_reason else "等待选择窗口"
        if sidecar_state in {"stale", "failed"}:
            return ("识别失效 · 自动恢复中", UI_COLORS["warn"])
        if bool(overlay.get("build_mismatch")):
            return ("构建不一致 · 请重新部署", UI_COLORS["error"])
        if functional_reason == "unsupported_fullscreen_mode":
            return (
                "游戏当前为全屏模式，请在游戏内视频设置切换为无边框",
                UI_COLORS["warn"],
            )
        if functional_reason == "game_window_mode_unknown" or (
            not functional_reason and visible_reason == "game_window_mode_unknown"
        ):
            return ("", UI_COLORS["muted"])
        if functional_status == "failed":
            return (f"显示异常 · {functional_reason or 'Host 功能不可用'}", UI_COLORS["error"])
        if context_status == "context_missing":
            return (f"{reason} · 等待英雄", UI_COLORS["warn"])
        if functional_status == "degraded":
            return (f"{reason} · {functional_reason or '功能降级'}", UI_COLORS["warn"])
        if context_status == "degraded":
            return (f"{reason} · 上下文降级", UI_COLORS["warn"])
        if cache_status in {"queued", "prewarming", "lookup", "building"}:
            return (f"{reason} · 模板预热中", UI_COLORS["warn"])
        if reason == "已显示":
            return ("游戏内显示中", UI_COLORS["green"])
        return (f"识别就绪 · {reason}", UI_COLORS["green"])
    return ("等待识别服务", UI_COLORS["warn"])

logger = logging.getLogger(__name__)


def _empty_champions() -> list[dict]:
    """桌面只消费快照 DTO，不再把只读 generation 转回 DataFrame。"""

    return []


def export_user_diagnostics(*args, **kwargs):
    """延迟导入诊断导出，避免首屏加载 runtime_store 数据栈。"""

    from hextech.modules.session.diagnostics import export_user_diagnostics as _export_user_diagnostics

    return _export_user_diagnostics(*args, **kwargs)


__all__ = [name for name in globals() if not name.startswith("__")]

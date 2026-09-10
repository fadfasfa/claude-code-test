"""备战席的唯一窗口呈现 owner：后台只投递快照，Tk 主线程应用窗口状态。"""

from __future__ import annotations

import json
import logging
import threading
import time
import tkinter as tk
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, cast
from .responsive_layout import (
    DesktopLayout, DesktopMonitor, resolve_desktop_layout,
)
from .docking_grace import DockingGrace


logger = logging.getLogger(__name__)
DesktopDisplayMode = Literal["client_foreground", "champ_select_only", "client_right"]
DEFAULT_DESKTOP_DISPLAY_MODE: DesktopDisplayMode = "client_foreground"
Rect = tuple[int, int, int, int]
WINDOW_OBSERVATION_TTL = 1.0
PHASE_OBSERVATION_TTL = 2.0
GAMEFLOW_OBSERVATION_TTL = 3.0
UI_TICK_MS = 25


@dataclass(frozen=True)
class DesktopWindowObservation:
    client_hwnd: int = 0
    foreground_hwnd: int = 0
    client_rect: Rect | None = None
    workarea: Rect | None = None
    client_visible: bool = False
    client_active: bool = False
    overlay_active: bool = False
    game_visible: bool = False
    gameflow_in_progress: bool = False
    gameflow_known: bool = False
    gameflow_observed_at: float = 0.0
    gameflow_client_hwnd: int = 0
    observed_at: float = 0.0
    dpi_scale: float = 1.0
    monitor_device: str = ""
    monitors: tuple[DesktopMonitor, ...] = ()
    client_probe_status: str = ""
    client_probe_reason: str = ""
    client_process_id: int = 0
    client_candidate_count: int = 0


@dataclass(frozen=True)
class DesktopWindowDecision:
    visible: bool = False
    topmost: bool = False
    rect: Rect | None = None
    reason: str = "waiting_client"
    layout: DesktopLayout | None = None
    layout_candidates: tuple[DesktopLayout, ...] = ()


def right_dock_rect(client: Rect | None, workarea: Rect | None) -> Rect | None:
    """仅接受同屏客户端右侧的完整空间；不得把 320px 窗口向左挤进客户端。"""
    if client is None or workarea is None:
        return None
    left, top, right, bottom = client
    wl, wt, wr, wb = workarea
    y = max(top, wt)
    end = min(bottom, wb, y + 740)
    if right <= left or bottom <= top or right < wl or right + 320 > wr or end - y < 200:
        return None
    return right, y, right + 320, end


def resolve_desktop_window(
    window: DesktopWindowObservation, *, phase: str, connection: str,
    phase_hwnd: int, phase_at: float, user_hidden: bool, manual_open: bool,
    already_visible: bool, mode: DesktopDisplayMode, now: float, previous_mode: str = "",
    position_mode: str = "auto", manual_rect: Rect | None = None,
    manual_dpi_scale: float = 1.0, manual_client_scale: float = 1.0, dragging: bool = False,
    manual_cursor: tuple[int, int] | None = None, previous_monitor_device: str = "",
) -> DesktopWindowDecision:
    """旧手动参数只保持调用兼容，不再授予位置或显隐豁免。"""
    if user_hidden:
        return DesktopWindowDecision(reason="user_hidden")
    current_gameflow = (window.gameflow_in_progress and window.gameflow_known
                       and window.gameflow_client_hwnd == window.client_hwnd
                       and 0 <= now-window.gameflow_observed_at <= GAMEFLOW_OBSERVATION_TTL)
    if window.game_visible or current_gameflow:
        return DesktopWindowDecision(reason="game_in_progress")
    if not window.client_hwnd or not window.client_visible:
        return DesktopWindowDecision(reason="client_not_visible")
    if not 0 <= now-window.observed_at <= WINDOW_OBSERVATION_TTL:
        return DesktopWindowDecision(reason="window_observation_stale")
    if not window.client_active:
        return DesktopWindowDecision(reason="client_not_foreground")
    if mode != "client_foreground" and (phase_hwnd != window.client_hwnd or not 0 <= now-phase_at <= PHASE_OBSERVATION_TTL):
        return DesktopWindowDecision(reason="context_unconfirmed")
    if mode != "client_foreground" and phase != "champ_select":
        return DesktopWindowDecision(reason="not_in_champ_select")
    if mode != "client_foreground" and connection != "connected" and not (connection == "degraded" and already_visible):
        return DesktopWindowDecision(reason="context_unconfirmed")
    layout = resolve_desktop_layout(window.client_rect, window.workarea, dpi_scale=window.dpi_scale,
                                    previous_mode=previous_mode)
    layout = replace(layout, monitor_device=window.monitor_device)
    if layout.rect is None:
        return DesktopWindowDecision(reason=layout.reason, layout=layout)
    return DesktopWindowDecision(True, False, layout.rect,
                                 "client_foreground" if mode == "client_foreground" else "champ_select", layout, (layout,))


class DesktopWindowPresentation:
    """容量一的窗口/阶段 mailbox；操作回调只由构造时所属的 Tk 线程执行。"""

    def __init__(self, ui: Any, *, mode: DesktopDisplayMode = DEFAULT_DESKTOP_DISPLAY_MODE) -> None:
        if mode not in {"client_foreground", "champ_select_only", "client_right"}:
            raise ValueError("unsupported desktop display mode")
        self.ui = ui
        self.mode = mode
        self._owner_thread = threading.get_ident()
        self._lock = threading.Lock()
        self._window = DesktopWindowObservation()
        self._phase: tuple[str, str, int, float] = ("", "", 0, 0.0)
        self._phase_started_at = 0.0
        self._user_hidden = False
        self._control_at = 0.0
        self._closed = False
        self._after_id: Any = None
        self._mapped_client = 0
        self._mapped_at = 0.0
        self._last_signature: object = None
        self._status: dict[str, Any] = {}
        self._first_visible = False
        self._dock_grace = DockingGrace()
        self._rejected_layout_key: object = None
        self._rejected_minimum_height = 0
        self._candidate_groups: dict[str, Any] | None = None
        self._candidate_ready = threading.Event()
        self._applied_content_key: object = None
        self._measured_layout_key: object = None
        self._measured_height = 0

    def start(self) -> None:
        self._assert_ui_thread()
        if self._after_id is None and self._ui_available():
            self._after_id = self.ui.root.after(UI_TICK_MS, self._tick)

    def _ui_available(self) -> bool:
        ui = self.ui
        if self._closed or ui is None or getattr(ui, "_closing", False):
            return False
        exists = getattr(ui.root, "winfo_exists", None)
        try:
            return not callable(exists) or bool(exists())
        except (tk.TclError, RuntimeError):
            return False

    def publish_window(self, observation: DesktopWindowObservation) -> None:
        with self._lock:
            if not self._closed and observation.observed_at >= self._window.observed_at:
                self._window = observation

    def publish_phase(self, groups: dict[str, Any], *, client_hwnd: int, observed_at: float) -> None:
        phase = str(groups.get("context_phase") or "")
        connection = str(groups.get("context_connection_state") or "")
        with self._lock:
            if self._closed or observed_at < self._phase[3]:
                return
            if (phase, client_hwnd) != (self._phase[0], self._phase[2]):
                self._phase_started_at = observed_at
            self._phase = phase, connection, client_hwnd, observed_at

    def request_hide(self) -> None:
        with self._lock:
            self._user_hidden = True
            self._control_at = time.time()

    def request_show(self, *, seconds: float) -> None:
        """只解除用户关闭；不采纳当前位置，也不绕过选人和前台门。"""
        self._assert_ui_thread()
        with self._lock:
            self._user_hidden = False
            self._control_at = time.time()

    @property
    def position_mode(self) -> str:
        return "auto"

    @property
    def manual_rect(self) -> Rect | None:
        return None

    def begin_manual_drag(self, cursor: tuple[int, int]) -> None:
        self._assert_ui_thread()

    def drag_to_cursor(self, cursor: tuple[int, int]) -> None:
        self._assert_ui_thread()

    def end_manual_drag(self) -> None:
        self._assert_ui_thread()

    def request_restore_auto_docking(self, *, reason: str = "user_restore") -> None:
        self._assert_ui_thread()
        self.ui._resume_auto_follow()

    def foreground_poll_seconds(self) -> float:
        with self._lock:
            return .25 if (self._window.client_active or self._window.overlay_active) and not self._window.game_visible else 1.5

    def wait_for_phase_poll(self, stop: threading.Event) -> None:
        """低频等待可在前台切换后50ms内缩短；不把旧后台档固定睡满1.5s。"""
        started = time.monotonic()
        while not stop.is_set() and not self._closed:
            remaining = self.foreground_poll_seconds() - (time.monotonic() - started)
            if remaining <= 0:
                return
            stop.wait(min(.05, remaining))

    def publish_candidates(self, groups: dict[str, Any]) -> None:
        with self._lock:
            if not self._closed:
                self._candidate_groups = dict(groups)
                self._candidate_ready.set()

    def take_candidates(self) -> dict[str, Any] | None:
        self._candidate_ready.wait(.1)
        with self._lock:
            groups = self._candidate_groups
            self._candidate_groups = None
            self._candidate_ready.clear()
            return None if self._closed else groups

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _assert_ui_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("desktop window operation must run on Tk thread")

    def close(self) -> None:
        self._assert_ui_thread()
        if self.ui is not None:
            # exit_application先置closing；撤租约不能被该标记跳过。
            revoke_layer = getattr(self.ui, "_maintain_foreground_layer", None)
            if callable(revoke_layer):
                try:
                    revoke_layer(self._mapped_client, eligible=False)
                    if getattr(self.ui, "_window_visible", False) or self.ui.root.winfo_ismapped():
                        self.ui._hide_overlay()
                except (tk.TclError, RuntimeError, ValueError):
                    # 已destroy/身份失效时不对旧HWND补写。
                    pass
        with self._lock:
            self._closed = True
            self._candidate_groups = None
            self._candidate_ready.set()
        if self._after_id is not None:
            self.ui.root.after_cancel(self._after_id)
            self._after_id = None
        # UI 持有 owner；关闭时必须断开反向引用，避免 Tcl 对象留在循环垃圾里被后台 GC。
        self.ui = None

    def _tick(self) -> None:
        self._after_id = None
        self._assert_ui_thread()
        if not self._ui_available():
            return
        try:
            self.apply_latest()
        except Exception as exc:
            if self._ui_available():
                logger.exception("备战席窗口呈现失败，保持隐藏并等待新观察。")
                hidden = False
                try:
                    self.ui._hide_overlay()
                    hidden = not bool(self.ui.root.winfo_ismapped())
                except Exception:
                    logger.exception("备战席失败后的隐藏未确认。")
                with self._lock:
                    self._status = {**self._status, "reason": "presentation_failed",
                                    "desired_visible": False, "visible": not hidden,
                                    "hide_confirmed": hidden, "error_type": type(exc).__name__,
                                    "error_detail": str(exc)}
                self._applied_content_key = None
                self._measured_layout_key = None
        finally:
            if self._ui_available():
                self.start()

    def apply_latest(self, *, now: float | None = None) -> DesktopWindowDecision:
        self._assert_ui_thread()
        if not self._ui_available():
            return DesktopWindowDecision(reason="closed")
        ui = self.ui
        if callable(getattr(ui, "_drain_ui_callbacks", None)):
            ui._drain_ui_callbacks()
        if not self._ui_available():
            return DesktopWindowDecision(reason="closed")
        with self._lock:
            window, phase = self._window, self._phase
            user_hidden, control_at = self._user_hidden, self._control_at
        timestamp = time.time() if now is None else now
        # GUI tick 只做廉价 Win32 前台查询，不等后台阶段/窗口扫描，关闭延迟<=100ms。
        foreground_probe = getattr(ui, "_desktop_client_is_foreground", None)
        if callable(foreground_probe):
            window = replace(window, client_active=foreground_probe(window.client_hwnd))
        foreground_hwnd_probe = getattr(ui, "_desktop_foreground_hwnd", None)
        if callable(foreground_hwnd_probe):
            window = replace(window, foreground_hwnd=foreground_hwnd_probe())
        self._dock_grace.observe(window, timestamp)
        was_visible = bool(ui._window_visible)
        decision = resolve_desktop_window(
            window, phase=phase[0], connection=phase[1], phase_hwnd=phase[2], phase_at=phase[3],
            user_hidden=user_hidden, manual_open=False, already_visible=was_visible,
            mode=self.mode, now=timestamp, previous_mode=getattr(getattr(ui, "_desktop_layout", None), "mode", ""),
        )
        if decision.visible and window.observed_at < control_at:
            decision = DesktopWindowDecision(reason="waiting_fresh_window")
        minimum_height = 0
        apply_layout = cast(Callable[[DesktopLayout], int] | None, getattr(ui, "_apply_desktop_layout", None))
        if decision.visible and decision.layout is not None and callable(apply_layout):
            layout = decision.layout
            measure_key = (decision.rect[2]-decision.rect[0], decision.rect[3]-decision.rect[1],
                           layout.dpi_scale, layout.client_scale, layout.mode,
                           repr(getattr(ui, "current_candidate_groups", None)),
                           getattr(ui, "_candidate_content_revision", 0))
            if measure_key == self._measured_layout_key:
                minimum_height = self._measured_height
            else:
                minimum_height = (self._rejected_minimum_height if measure_key == self._rejected_layout_key
                                  else apply_layout(decision.layout))
                self._measured_layout_key, self._measured_height = measure_key, minimum_height
            if decision.rect[3]-decision.rect[1] < minimum_height:
                self._rejected_layout_key, self._rejected_minimum_height = measure_key, minimum_height
                decision = replace(decision, visible=False, reason="right_height_insufficient")
            else:
                self._rejected_layout_key = None
        insufficient = decision.reason in {"right_width_insufficient", "right_height_insufficient"}
        space_reason = decision.reason if insufficient else ""
        if insufficient and window.observed_at >= control_at:
            held = self._dock_grace.fallback(timestamp)
            if held is not None:
                minimum_height = apply_layout(held) if callable(apply_layout) else 0
                if held.rect[3]-held.rect[1] >= minimum_height:
                    decision = DesktopWindowDecision(True, False, held.rect, "adjustment_grace", held)
        actual_rect = None
        if decision.visible and decision.rect is not None:
            if callable(foreground_probe) and not foreground_probe(window.client_hwnd):
                decision = DesktopWindowDecision(reason="client_not_foreground")
            else:
                bind_layer = getattr(ui, "_bind_client_layer", None)
                root = ui.root
                current_rect = (int(root.winfo_rootx()), int(root.winfo_rooty()),
                                int(root.winfo_rootx())+int(root.winfo_width()),
                                int(root.winfo_rooty())+int(root.winfo_height()))
                content_key = (decision.layout, repr(getattr(ui, "current_candidate_groups", None)), window.client_hwnd)
                layer_matches = getattr(ui, "_desktop_layer_matches", None)
                unchanged = (was_visible and root.winfo_ismapped() and current_rect == decision.rect
                             and content_key == self._applied_content_key and ui._auto_follow_enabled
                             and (not callable(layer_matches) or layer_matches(window.client_hwnd)))
                if not unchanged:
                    if callable(bind_layer):
                        bind_layer(window.client_hwnd)
                    if not ui._auto_follow_enabled or self._mapped_client != window.client_hwnd:
                        ui._resume_auto_follow()
                    x0, y0, _, y1 = decision.rect
                    ui._move_overlay_to(x0, y0, height=y1-y0)
                    if callable(bind_layer):
                        bind_layer(window.client_hwnd)
                    ui._overlay_position_initialized = True
                    ui._show_overlay(topmost=False)
                    self._applied_content_key = content_key
                self._mapped_client = window.client_hwnd
                root = ui.root
                if root.winfo_ismapped() and root.winfo_width() > 1 and root.winfo_height() > 1:
                    x, y = int(root.winfo_rootx()), int(root.winfo_rooty())
                    actual_rect = (x, y, x+int(root.winfo_width()), y+int(root.winfo_height()))
                    if not was_visible:
                        self._mapped_at = timestamp
                    if not self._first_visible:
                        self._first_visible = True
                        ui.startup_timing.mark("first_idle_visible", reason=decision.reason)
                    if decision.reason in {"champ_select", "client_foreground"} and actual_rect == decision.rect:
                        self._dock_grace.last_layout = decision.layout
        if not decision.visible:
            if was_visible or ui.root.winfo_ismapped():
                ui._hide_overlay()
            self._applied_content_key = None
            self._mapped_at = 0.0
            ui._overlay_position_initialized = False
        layer_status = {}
        maintain_layer = getattr(ui, "_maintain_foreground_layer", None)
        if callable(maintain_layer):
            geometry_confirmed = actual_rect is not None and actual_rect == decision.rect
            layer_status = maintain_layer(window.client_hwnd, eligible=bool(decision.visible and geometry_confirmed))
            # 前台二次复核失败不能只降层留下可见的背景面板。
            if decision.visible and not layer_status.get("desired_topmost"):
                ui._hide_overlay()
                decision = DesktopWindowDecision(reason=str(layer_status.get("reason") or "layer_unconfirmed")
                                                 if geometry_confirmed else "geometry_unconfirmed")
                actual_rect = None
                self._applied_content_key = None
        status = {
            "mode": self.mode, "phase": phase[0], "connection": phase[1],
            "reason": decision.reason, "visible": actual_rect is not None,
            "desired_visible": decision.visible, "inner_mapped": bool(ui.root.winfo_ismapped()),
            "wrapper_hwnd": int(getattr(ui, "_desktop_hwnd", 0)),
            "wrapper_revision": int(getattr(ui, "_desktop_wrapper_revision", 0)),
            "inner_hwnd": int(ui.root.winfo_id()) if callable(getattr(ui.root, "winfo_id", None)) else 0,
            "topmost": bool(layer_status.get("actual_topmost", False)), "user_hidden": user_hidden,
            "layer": layer_status,
            "client_hwnd": window.client_hwnd, "foreground_hwnd": window.foreground_hwnd,
            "client_probe_status": window.client_probe_status, "client_probe_reason": window.client_probe_reason,
            "client_process_id": window.client_process_id, "client_candidate_count": window.client_candidate_count,
            "target_rect": decision.rect, "actual_rect": actual_rect,
            "phase_observed_at": self._phase_started_at, "mapped_at": self._mapped_at,
            "mapping_changed": bool(actual_rect is not None and not was_visible),
            "build_id": str(getattr(ui.startup_timing, "_build_id", "")),
            "layout": decision.layout.diagnostic() if decision.layout is not None else {},
            "minimum_height_px": minimum_height, "monitor_device": window.monitor_device,
            "position_mode": "auto", "position_reason": "client_right_only",
            "client_monitor_device": window.monitor_device,
            "dock_monitor_device": decision.layout.monitor_device if decision.layout else "",
            "candidate_rejections": [space_reason] if space_reason else [],
            "space_reason": space_reason,
            "user_message": (
                "右侧空间不足，请将客户端左移以留出空间。"
                if space_reason == "right_width_insufficient" else
                "右侧可用高度不足，请调整客户端大小或位置以留出空间。"
                if space_reason == "right_height_insufficient" else ""
            ),
            "adjustment_remaining_seconds": round(self._dock_grace.remaining(timestamp), 2) if insufficient else 0.0,
        }
        # 倒计时可查询，不为每个25ms tick写日志。
        signature = (decision, actual_rect, phase[:3], user_hidden, window.client_hwnd, window.foreground_hwnd,
                     layer_status.get("reason"), layer_status.get("actual_topmost"),
                     (layer_status.get("occlusion") or {}).get("occluder_hwnd"), layer_status.get("corrections_total"))
        with self._lock:
            self._status = status
        if signature != self._last_signature:
            self._last_signature = signature
            logger.info("desktop_window=%s", json.dumps(status, ensure_ascii=False, separators=(",", ":")))
        return decision


__all__ = ["DesktopWindowObservation", "DesktopWindowPresentation", "resolve_desktop_window", "right_dock_rect"]

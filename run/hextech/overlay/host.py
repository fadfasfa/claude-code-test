"""独立游戏内 overlay 的 Tk/Win32 窗口宿主。

本模块只负责透明置顶窗口、点击穿透、热键、游戏窗口跟随和本地事件渲染。
真实识别由 Vision sidecar 写入本地事件文件，host 不截图、不识别、不访问远端。

调用方: hextech_ui、tests.test_overlay_host_visibility、collect_runtime_diagnostics; 关键依赖: support.python_runtime、overlay.window、overlay.window_titles。
"""

from __future__ import annotations

from hextech.support.python_runtime import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source(module_name="hextech.overlay.host")

import argparse
import ctypes
import json
import logging
import os
import queue
import threading
import tkinter as tk
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.overlay.window import (
    find_lol_game_window,
    is_scoreboard_key_down,
    is_window_foreground,
    is_window_renderable,
)
from hextech.overlay.window_titles import LOL_GAME_WINDOW_TITLE
from hextech.support.atomic_io import atomic_write_json
from hextech.adapters import build_runtime_session
from hextech.contracts import GameSessionState, PresentationMode, VisionSlotState
from hextech.session.evidence import build_evidence_bundle, write_evidence_bundle

from .data_source import OverlayDataSource, SharedOverlayDataSource, prepare_shared_overlay_data
from .gameflow import probe_gameflow_in_progress
from .renderer import build_render_model, build_render_model_from_session, draw_overlay_frame
from .runtime_paths import overlay_runtime_state_path


logger = logging.getLogger(__name__)

GWL_EXSTYLE = -20
GWL_WNDPROC = -4
GA_ROOT = 2
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HOTKEY_ID = 0x4848
MOD_ALT = 0x0001
VK_MENU = 0x12
WM_HOTKEY = 0x0312
WM_MOUSEACTIVATE = 0x0021
WM_QUIT = 0x0012
MA_NOACTIVATE = 3
HOTKEY_FALLBACK_POLL_SECONDS = 0.05
HOTKEY_FALLBACK_DEBOUNCE_SECONDS = 0.3
OVERLAY_EXIT_POLL_MS = 100
RENDER_ERROR_BACKOFF_AFTER = 3
RENDER_ERROR_BACKOFF_MAX_MS = 30_000
RECENT_CONTEXT_HOLD_SECONDS = 8.0
VISIBILITY_FALLBACK_POLL_MS = 250
GAMEFLOW_REFRESH_SECONDS = 1.0
FOREGROUND_EVENT_DRAIN_MS = 100
VISIBILITY_DIAGNOSTIC_LOG_SECONDS = 1.5
HOST_VISIBILITY_STATUS_HEARTBEAT_SECONDS = 2.0
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)
OVERLAY_READY_FILE_ENV = "HEXTECH_OVERLAY_READY_FILE"
OVERLAY_READY_TOKEN_ENV = "HEXTECH_OVERLAY_READY_TOKEN"
OVERLAY_EXIT_FILE_ENV = "HEXTECH_OVERLAY_EXIT_FILE"
GAME_OVERLAY_VISIBILITY_FILE = Path(overlay_runtime_state_path("game_overlay_visibility.v1.json"))

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class HotkeyController:
    """保存全局热键线程状态，便于主线程退出时发 WM_QUIT 收尾。"""

    def __init__(self, request_queue: "queue.Queue[str]") -> None:
        self.request_queue = request_queue
        self.thread_id = 0
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.mode = "starting"
        self.thread: threading.Thread | None = None


@dataclass(frozen=True)
class OverlayVisibilitySnapshot:
    """host 自有的窗口生命周期快照；不包含视觉识别结果。"""

    user_enabled: bool
    gameflow_in_progress: bool
    game_hwnd: int | None
    game_rect: tuple[int, int, int, int] | None
    game_renderable: bool
    game_foreground: bool
    visible: bool
    reason: str
    updated_at: float


class GameflowPoller:
    """后台刷新 gameflow 缓存，避免 Tk render tick 发起 HTTP/进程扫描。"""

    def __init__(
        self,
        *,
        probe: Callable[[], bool] = probe_gameflow_in_progress,
        interval_seconds: float = GAMEFLOW_REFRESH_SECONDS,
    ) -> None:
        self._probe = probe
        self._interval_seconds = max(0.2, float(interval_seconds))
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._in_progress = False
        self._checked_at = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hextech-gameflow-poller")
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, float(timeout_seconds)))

    def current(self) -> tuple[bool, float]:
        with self._lock:
            return self._in_progress, self._checked_at

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                in_progress = bool(self._probe())
            except Exception:
                logger.debug("刷新 gameflow 缓存失败。", exc_info=True)
                in_progress = False
            checked_at = time.time()
            with self._lock:
                self._in_progress = in_progress
                self._checked_at = checked_at
            self._stop_requested.wait(self._interval_seconds)


class WindowTargetPoller:
    """后台刷新 LoL 游戏窗口缓存，避免 Tk render tick 枚举进程窗口。"""

    def __init__(
        self,
        window_titles: list[str],
        *,
        initial_target: tuple[int, tuple[int, int, int, int]] | None = None,
        interval_seconds: float = 0.35,
    ) -> None:
        self._window_titles = list(window_titles)
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._target = initial_target

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hextech-window-target-poller")
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.0, float(timeout_seconds)))

    def current(self) -> tuple[int, tuple[int, int, int, int]] | None:
        with self._lock:
            return self._target

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                target = _find_target_game_window(self._window_titles)
            except Exception:
                logger.debug("刷新 LoL 游戏窗口缓存失败。", exc_info=True)
                target = None
            with self._lock:
                self._target = target
            self._stop_requested.wait(self._interval_seconds)


class ForegroundEventHook:
    """保存 WinEvent hook 回调引用，防止 ctypes callback 被回收。"""

    def __init__(self, handle: int, callback: Any) -> None:
        self.handle = int(handle or 0)
        self.callback = callback


def build_overlay_window_config() -> dict[str, Any]:
    """返回 overlay 的可验收窗口能力配置。"""

    return {
        "title": "Hextech Game Overlay",
        "width": 900,
        "height": 150,
        "topmost": True,
        "click_through": True,
        "no_activate": True,
        "hotkey": "Alt+H",
        "alpha": 0.96,
        "transparent_color": "#010203",
        "badge_height": 72,
        "badge_width_ratio": 0.15,
        "show_missing_synergy_reason": True,
        "follow_window_titles": [LOL_GAME_WINDOW_TITLE],
        "top_offset": 132,
        "event_poll_ms": 120,
        "fast_event_poll_ms": 60,
        "fast_event_hold_ms": 1200,
        "diagnostic_mode": False,
    }


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logger.debug("设置 overlay DPI 感知失败。", exc_info=True)


def _prepare_host_hint_cache() -> str:
    try:
        prepare_shared_overlay_data()
    except Exception as exc:
        logger.warning("overlay host 启动前准备 hint cache 失败：%s", exc, exc_info=True)
        return exc.__class__.__name__
    return ""


def _root_hwnd(root: tk.Tk) -> int:
    """解析 Tk 顶层窗口 HWND，避免把扩展样式写到内部子窗口。"""

    user32 = ctypes.windll.user32
    raw_hwnd = int(root.winfo_id())
    top_hwnd = int(user32.GetAncestor(raw_hwnd, GA_ROOT))
    return top_hwnd or raw_hwnd


def _get_window_exstyle(hwnd: int) -> int:
    return int(ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE))


def _set_window_exstyle(hwnd: int, exstyle: int) -> None:
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)


def _install_no_activate_proc(root: tk.Tk) -> None:
    """拦截鼠标激活消息，避免 hover/click 让 overlay 抢走 LoL 前台。"""

    hwnd = _root_hwnd(root)
    if getattr(root, "_hextech_wndproc_hwnd", None) == hwnd:
        return

    user32 = ctypes.windll.user32
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    old_proc = ctypes.c_void_p()

    def _window_proc(hwnd_arg: int, message: int, w_param: int, l_param: int) -> int:
        if message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if old_proc.value:
            return int(user32.CallWindowProcW(old_proc.value, hwnd_arg, message, w_param, l_param))
        return int(user32.DefWindowProcW(hwnd_arg, message, w_param, l_param))

    callback = WNDPROC(_window_proc)
    set_proc = user32.SetWindowLongPtrW
    set_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    set_proc.restype = ctypes.c_void_p
    old_proc.value = int(set_proc(hwnd, GWL_WNDPROC, ctypes.cast(callback, ctypes.c_void_p).value) or 0)
    if not old_proc.value:
        logger.debug("安装 overlay no-activate WndProc 未返回旧过程。")
    root._hextech_wndproc = callback  # type: ignore[attr-defined]
    root._hextech_old_wndproc = old_proc  # type: ignore[attr-defined]
    root._hextech_wndproc_hwnd = hwnd  # type: ignore[attr-defined]


def _apply_overlay_window_styles(root: tk.Tk, *, click_through: bool, no_activate: bool = True) -> bool:
    """仅在扩展样式实际变化时提交 FRAMECHANGED。"""

    hwnd = _root_hwnd(root)
    user32 = ctypes.windll.user32
    current_style = _get_window_exstyle(hwnd)
    next_style = current_style | WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
    if click_through:
        next_style |= WS_EX_TRANSPARENT
    if no_activate:
        next_style |= WS_EX_NOACTIVATE
    style_changed = next_style != current_style
    if style_changed:
        _set_window_exstyle(hwnd, next_style)
    if click_through and not (_get_window_exstyle(hwnd) & WS_EX_TRANSPARENT):
        # Tk/overrideredirect 在窗口初始化早期偶尔会覆盖扩展样式；读回失败时立即重试一次。
        _set_window_exstyle(hwnd, next_style)
        style_changed = True
    if no_activate:
        _install_no_activate_proc(root)
    if style_changed:
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    return style_changed


def _ensure_overlay_window_styles(root: tk.Tk, config: Mapping[str, Any]) -> bool:
    """显示或重定位后再次保证透明点击穿透，防止 Tk 覆盖扩展样式。"""

    return _apply_overlay_window_styles(
        root,
        click_through=bool(config.get("click_through", True)),
        no_activate=bool(config.get("no_activate", True)),
    )


def _poll_alt_h_hotkey(controller: HotkeyController, user32: Any) -> None:
    """全局热键被占用时，用按键边沿轮询保留关闭 overlay 的能力。"""

    was_pressed = False
    last_toggle_at = 0.0
    while not controller.stop_requested.is_set():
        alt_pressed = bool(int(user32.GetAsyncKeyState(VK_MENU)) & 0x8000)
        h_pressed = bool(int(user32.GetAsyncKeyState(ord("H"))) & 0x8000)
        pressed = alt_pressed and h_pressed
        now = time.monotonic()
        if pressed and not was_pressed and now - last_toggle_at >= HOTKEY_FALLBACK_DEBOUNCE_SECONDS:
            controller.request_queue.put("toggle")
            last_toggle_at = now
        was_pressed = pressed
        if controller.stop_requested.wait(HOTKEY_FALLBACK_POLL_SECONDS):
            break


def _start_hotkey_thread(request_queue: "queue.Queue[str]") -> HotkeyController:
    """用独立消息循环接收 Alt+H，避免 Tk WndProc 吃掉 WM_HOTKEY。"""

    controller = HotkeyController(request_queue)

    def hotkey_loop() -> None:
        user32 = ctypes.windll.user32
        controller.thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT, ord("H")):
            controller.mode = "poll"
            logger.warning("注册 Alt+H 全局热键失败，降级为按键轮询。")
            controller.ready.set()
            _poll_alt_h_hotkey(controller, user32)
            return

        controller.mode = "registered"
        controller.ready.set()
        try:
            while True:
                result = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    logger.warning("读取 Alt+H 热键消息失败。")
                    break
                if int(msg.message) == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                    request_queue.put("toggle")
        finally:
            try:
                user32.UnregisterHotKey(None, HOTKEY_ID)
            except Exception:
                logger.debug("注销 overlay 热键失败。", exc_info=True)

    controller.thread = threading.Thread(
        target=hotkey_loop,
        name="hextech-overlay-hotkey",
        daemon=True,
    )
    controller.thread.start()
    controller.ready.wait(timeout=0.5)
    return controller


def _stop_hotkey_thread(controller: HotkeyController | None) -> None:
    if controller is None or controller.thread is None:
        return
    controller.stop_requested.set()
    thread_id = int(controller.thread_id or 0)
    if thread_id:
        try:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        except Exception:
            logger.debug("发送 overlay 热键线程退出消息失败。", exc_info=True)
    controller.thread.join(timeout=1.0)


def _register_foreground_event_hook(
    foreground_event: threading.Event,
    *,
    user32: Any | None = None,
) -> ForegroundEventHook | None:
    """订阅前台变化；WinEvent 回调只置位，Tk 主线程另行 drain。"""

    if user32 is None:
        if not hasattr(ctypes, "windll"):
            return None
        user32 = ctypes.windll.user32

    def _callback(
        _hook: int,
        event: int,
        _hwnd: int,
        _object_id: int,
        _child_id: int,
        _event_thread: int,
        _event_time: int,
    ) -> None:
        if int(event) == EVENT_SYSTEM_FOREGROUND:
            foreground_event.set()

    callback = WINEVENTPROC(_callback)
    try:
        handle = int(
            user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND,
                EVENT_SYSTEM_FOREGROUND,
                None,
                callback,
                0,
                0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
            or 0
        )
    except Exception:
        logger.debug("注册前台窗口 WinEvent hook 失败。", exc_info=True)
        return None
    if not handle:
        return None
    return ForegroundEventHook(handle, callback)


def _stop_foreground_event_hook(hook: ForegroundEventHook | None, *, user32: Any | None = None) -> None:
    if hook is None or not hook.handle:
        return
    if user32 is None:
        if not hasattr(ctypes, "windll"):
            return
        user32 = ctypes.windll.user32
    try:
        user32.UnhookWinEvent(hook.handle)
    except Exception:
        logger.debug("注销前台窗口 WinEvent hook 失败。", exc_info=True)


def _schedule_foreground_event_drain(
    root: tk.Tk,
    foreground_event: threading.Event,
    request_render: Callable[[], None],
    *,
    poll_ms: int = FOREGROUND_EVENT_DRAIN_MS,
) -> None:
    """在 Tk 主线程合并前台变化事件，避免 WinEvent 回调重入 Tk after。"""

    if foreground_event.is_set():
        foreground_event.clear()
        request_render()
    root.after(max(20, int(poll_ms)), lambda: _schedule_foreground_event_drain(root, foreground_event, request_render, poll_ms=poll_ms))


def _find_target_game_window(window_titles: list[str]) -> tuple[int, tuple[int, int, int, int]] | None:
    return find_lol_game_window(window_titles=window_titles)


def _find_target_game_rect(window_titles: list[str]) -> tuple[int, int, int, int] | None:
    target = _find_target_game_window(window_titles)
    return target[1] if target is not None else None


def _is_game_window_foreground(hwnd: int | None, *, overlay_hwnd: int | None = None) -> bool:
    return is_window_foreground(hwnd, overlay_hwnd=overlay_hwnd)


def resolve_overlay_visibility(
    *,
    user_enabled: bool,
    gameflow_in_progress: bool,
    game_hwnd: int | None,
    game_rect: tuple[int, int, int, int] | None,
    game_renderable: bool,
    game_foreground: bool,
    content_ready: bool,
    now: float | None = None,
) -> OverlayVisibilitySnapshot:
    """只按 host 自有信号决定窗口生命周期；视觉事件只影响内容。"""

    normalized_hwnd = int(game_hwnd) if game_hwnd else None
    normalized_rect = tuple(int(value) for value in game_rect) if game_rect is not None else None
    if not user_enabled:
        visible, reason = False, "user_disabled"
    elif not gameflow_in_progress:
        visible, reason = False, "gameflow_not_in_progress"
    elif not normalized_hwnd:
        visible, reason = False, "game_window_missing"
    elif not game_renderable:
        visible, reason = False, "game_window_not_renderable"
    elif not game_foreground:
        visible, reason = False, "game_not_foreground"
    elif content_ready:
        visible, reason = True, "visible_ready"
    else:
        visible, reason = True, "visible_detecting"
    return OverlayVisibilitySnapshot(
        user_enabled=bool(user_enabled),
        gameflow_in_progress=bool(gameflow_in_progress),
        game_hwnd=normalized_hwnd,
        game_rect=normalized_rect,
        game_renderable=bool(game_renderable),
        game_foreground=bool(game_foreground),
        visible=visible,
        reason=reason,
        updated_at=time.time() if now is None else float(now),
    )


def _query_gameflow_in_progress() -> bool:
    """优先用 2999 判断实际对局，再用 LCU gameflow 兜底；不读取凭据文件。"""

    return probe_gameflow_in_progress()


def _refresh_gameflow_in_progress(visibility: dict[str, Any], *, now: float | None = None) -> bool:
    poller = visibility.get("gameflow_poller")
    if isinstance(poller, GameflowPoller):
        in_progress, checked_at = poller.current()
        visibility["gameflow_in_progress"] = in_progress
        visibility["gameflow_checked_at"] = checked_at
        return in_progress
    in_progress = bool(visibility.get("gameflow_in_progress", True))
    if "gameflow_checked_at" not in visibility:
        visibility["gameflow_checked_at"] = time.time() if now is None else float(now)
    return in_progress


def _target_overlay_geometry(rect: tuple[int, int, int, int], config: dict[str, Any]) -> str:
    left, top, right, bottom = rect
    game_width = max(1, right - left)
    game_height = max(1, bottom - top)
    x_offset = f"+{left}" if left >= 0 else str(left)
    y_offset = f"+{top}" if top >= 0 else str(top)
    return f"{game_width}x{game_height}{x_offset}{y_offset}"


def _resolve_initial_overlay_viewport(
    initial_target: tuple[int, tuple[int, int, int, int]] | None,
    config: dict[str, Any],
) -> tuple[int, int, str]:
    """未发现游戏窗口时仅保留隐藏占位，避免 900x150 参与正式布局。"""

    if initial_target is None:
        return (1, 1, "1x1+0+0")
    _, rect = initial_target
    width = max(1, rect[2] - rect[0])
    height = max(1, rect[3] - rect[1])
    return (width, height, _target_overlay_geometry(rect, config))


def _snapshot_source_reason(snapshot: Mapping[str, Any]) -> str:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    return str(source.get("reason") or "").strip()


def _snapshot_selection_window_active(snapshot: Mapping[str, Any]) -> bool | None:
    """读取 sidecar 生命周期字段；旧事件返回 None 以启用短暂兼容 hold。"""

    if snapshot.get("ok") is False:
        return False
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    value = source.get("selection_window_active")
    return value if isinstance(value, bool) else None


def _extract_event_status(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """提取仅用于去重日志的状态；正式与诊断画面均不显示状态 UI。"""

    if not isinstance(snapshot, Mapping):
        snapshot = {}
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}

    def optional_number(value: Any, converter: Any) -> Any:
        try:
            return converter(value)
        except (TypeError, ValueError):
            return None

    ready_slots = optional_number(source.get("ready_slots"), int)
    if ready_slots is None:
        ready_slots = _snapshot_ready_slot_count(snapshot)
    return {
        "gate_state": str(source.get("gate_state") or "").strip(),
        "ready_slots": max(0, min(3, int(ready_slots or 0))),
        "blocking_modal": bool(source.get("blocking_modal")),
        "latency_ms": optional_number(source.get("latency_ms"), float),
        "stable_frames": optional_number(source.get("stable_frames"), int),
        "selection_window_active": (
            source.get("selection_window_active")
            if isinstance(source.get("selection_window_active"), bool)
            else None
        ),
        "error": str(snapshot.get("error") or "").strip(),
    }


def _log_waiting_context_diagnostic(
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    context: Mapping[str, Any],
    model: Mapping[str, Any],
) -> None:
    statuses = [
        str(row.get("status_code") or "")
        for row in (model.get("stats") if isinstance(model.get("stats"), list) else [])
        if isinstance(row, Mapping)
    ]
    if not any(status in {"CONTEXT_MISSING", "CONTEXT_EXPIRED"} for status in statuses):
        return
    event_status = _extract_event_status(snapshot)
    diagnostic = {
        "context_status": "ok" if context.get("ok") else str(context.get("error") or "context_missing"),
        "context_source": str(context.get("source") or ""),
        "context_champion_id": str(context.get("champion_id") or ""),
        "context_champion_name": str(context.get("champion_name") or ""),
        "event_reason": _snapshot_source_reason(snapshot),
        "ready_slots": event_status.get("ready_slots"),
        "selection_window_active": event_status.get("selection_window_active"),
        "render_statuses": statuses,
    }
    diagnostic_key = tuple(diagnostic.items())
    if diagnostic_key == visibility.get("last_waiting_context_diagnostic"):
        return
    visibility["last_waiting_context_diagnostic"] = diagnostic_key
    logger.info("game_overlay waiting_context=%s", diagnostic)


def _snapshot_has_complete_ready_slots(snapshot: Mapping[str, Any]) -> bool:
    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    return len(slots) >= 3 and all(
        isinstance(slot, Mapping)
        and slot.get("state") == "ready"
        and bool(str(slot.get("augment_id") or slot.get("name") or "").strip())
        for slot in slots[:3]
    )


def _snapshot_ready_slot_count(snapshot: Mapping[str, Any]) -> int:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    try:
        return max(0, min(3, int(source.get("ready_slots"))))
    except (TypeError, ValueError):
        slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
        return sum(
            1
            for slot in slots[:3]
            if isinstance(slot, Mapping)
            and slot.get("state") == "ready"
            and bool(str(slot.get("augment_id") or slot.get("name") or "").strip())
        )


def _cache_allows_private_stats(hint_cache: Mapping[str, Any] | None) -> bool:
    source = hint_cache.get("source") if isinstance(hint_cache, Mapping) else None
    return bool(isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True)


def _count_current_context_synergy_hints(hints: Any, context: Mapping[str, Any] | None) -> int:
    if not isinstance(hints, Mapping) or not isinstance(context, Mapping):
        return 0
    champion_id = str(context.get("champion_id") or "").strip()
    champion_name = str(context.get("champion_name") or "").strip()
    return sum(
        1
        for hint in hints.values()
        if isinstance(hint, Mapping)
        and any(
            isinstance(item, Mapping)
            and (
                (champion_id and str(item.get("hero_id") or "").strip() == champion_id)
                or (champion_name and str(item.get("hero_name") or "").strip() == champion_name)
            )
            for item in (hint.get("synergies") if isinstance(hint.get("synergies"), list) else [])
        )
    )


def _signal_overlay_ready() -> None:
    """Tk 完成 idle 初始化后写 readiness；父进程验证 PID 后才报告 running。"""

    ready_path = str(os.environ.get(OVERLAY_READY_FILE_ENV) or "").strip()
    if not ready_path:
        return
    try:
        ready_token = str(os.environ.get(OVERLAY_READY_TOKEN_ENV) or "").strip()
        atomic_write_json(
            Path(ready_path),
            {"pid": os.getpid(), "token": ready_token, "ready_at": time.time()},
            ensure_ascii=False,
            indent=2,
        )
    except Exception:
        logger.exception("写入 game_overlay host readiness 失败。")


def _schedule_exit_file_watch(root: tk.Tk, exit_path: str | Path | None = None) -> None:
    """监听 lifecycle 写入的退出信号，让 host 优先走 Tk 主循环正常收尾。"""

    raw_path = str(exit_path or os.environ.get(OVERLAY_EXIT_FILE_ENV) or "").strip()
    if not raw_path:
        return
    signal_path = Path(raw_path)

    def poll_exit_signal() -> None:
        try:
            should_exit = signal_path.exists()
        except OSError:
            logger.debug("检查 game_overlay host 退出信号失败：%s", signal_path, exc_info=True)
            should_exit = False
        if should_exit:
            logger.info("收到 game_overlay host 退出信号。")
            root.quit()
            return
        root.after(OVERLAY_EXIT_POLL_MS, poll_exit_signal)

    root.after(OVERLAY_EXIT_POLL_MS, poll_exit_signal)

def _apply_overlay_rect(root: tk.Tk, rect: tuple[int, int, int, int]) -> None:
    """用整数虚拟屏坐标定位 client rect，兼容左侧/上方副屏。"""

    left, top, right, bottom = rect
    ctypes.windll.user32.SetWindowPos(
        _root_hwnd(root),
        HWND_TOPMOST,
        int(left),
        int(top),
        max(1, int(right) - int(left)),
        max(1, int(bottom) - int(top)),
        SWP_NOACTIVATE,
    )


def _show_overlay_window(root: tk.Tk, config: dict[str, Any], visibility: dict[str, Any]) -> None:
    pending_geometry = str(visibility.get("pending_geometry") or "")
    if pending_geometry:
        root.geometry(pending_geometry)
        visibility["applied_geometry"] = pending_geometry
    target_rect = visibility.get("target_rect")
    if isinstance(target_rect, tuple) and len(target_rect) == 4:
        _apply_overlay_rect(root, target_rect)
    root.deiconify()
    root.attributes("-topmost", True)
    _ensure_overlay_window_styles(root, config)


def _apply_transparent_background(root: tk.Tk, canvas: tk.Canvas, config: Mapping[str, Any]) -> None:
    transparent_color = str(config.get("transparent_color") or "").strip()
    if not transparent_color:
        return
    root.configure(bg=transparent_color)
    canvas.configure(bg=transparent_color)
    try:
        root.attributes("-transparentcolor", transparent_color)
    except tk.TclError:
        logger.debug("当前 Tk 环境不支持 transparentcolor。", exc_info=True)


def decide_visibility(
    *,
    user_enabled: bool,
    event_visible: bool,
    game_foreground: bool,
    content_ready: bool,
    selection_window_active: bool | None,
    gameflow_in_progress: bool = True,
    game_hwnd: int | None = None,
    game_rect: tuple[int, int, int, int] | None = None,
    game_renderable: bool = False,
    ready_slots: int | None = None,
    scoreboard_key_down: bool = False,
    event_fresh_after_tab: bool = True,
    event_error: str = "",
    blocking_modal: bool = False,
    diagnostic_mode: bool = False,
    stale_event_hold: bool = False,
) -> tuple[bool, str]:
    """统一显隐决策，避免显示结果和诊断原因分叉。"""

    resolved_ready_slots = (3 if content_ready else 0) if ready_slots is None else int(ready_slots)
    host_snapshot = resolve_overlay_visibility(
        user_enabled=user_enabled,
        gameflow_in_progress=gameflow_in_progress,
        game_hwnd=game_hwnd,
        game_rect=game_rect,
        game_renderable=game_renderable,
        game_foreground=game_foreground,
        content_ready=content_ready,
    )
    should_show, reason = host_snapshot.visible, host_snapshot.reason
    if should_show and blocking_modal:
        should_show, reason = False, "blocking_modal_present"
    elif should_show and scoreboard_key_down:
        should_show, reason = False, "scoreboard_key_down"
    elif should_show and not event_fresh_after_tab:
        should_show, reason = False, "event_stale_after_tab"
    elif should_show and (event_error or selection_window_active is False):
        # 游戏存在时保持轻量等待面；event/context 暂缺不再让整个 Overlay 静默消失。
        reason = "waiting_selection"
    elif should_show and resolved_ready_slots > 0 and resolved_ready_slots < 3:
        reason = "visible_partial"

    if diagnostic_mode and not should_show:
        return True, f"diagnostic:{reason}"
    return should_show, reason


def _draw_diagnostic_status(canvas: tk.Canvas, reason: str, snapshot: Mapping[str, Any]) -> None:
    """诊断模式下只画一行 heartbeat，避免非选择态看起来像进程崩溃。"""

    status = _extract_event_status(snapshot)
    parts = [
        "Hextech overlay diagnostic",
        f"reason={reason}",
        f"gate={status.get('gate_state') or '-'}",
        f"ready={status.get('ready_slots')}",
        f"error={status.get('error') or '-'}",
    ]
    message = " · ".join(parts)
    canvas.delete("all")
    try:
        canvas.create_rectangle(8, 8, 560, 34, fill="#010A13", outline="#785A28")
    except AttributeError:
        pass
    canvas.create_text(
        18,
        21,
        text=message,
        fill="#F0E6D2",
        anchor="w",
        font=("Segoe UI", 11, "bold"),
    )


def _log_visibility_diagnostic(
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: float,
    should_show: bool,
    reason: str,
) -> None:
    """限频输出显隐决策面，便于真机从日志定位是哪一层挡住。"""

    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    diagnostic_key = (
        bool(should_show),
        str(reason or ""),
        bool(visibility.get("gameflow_in_progress")),
        int(visibility.get("target_hwnd") or 0),
        bool(visibility.get("game_renderable")),
        bool(visibility.get("game_foreground")),
        source.get("selection_window_active"),
        int(visibility.get("ready_slots") or 0),
        bool(visibility.get("blocking_modal")),
        bool(visibility.get("scoreboard_key_down")),
        str(visibility.get("event_error") or ""),
        bool(visibility.get("context_ok")),
        str(visibility.get("context_champion_id") or ""),
        str(visibility.get("context_source") or ""),
        str(visibility.get("context_error") or ""),
    )
    try:
        last_logged_at = float(visibility.get("last_visibility_diagnostic_logged_at") or 0.0)
    except (TypeError, ValueError):
        last_logged_at = 0.0
    if (
        diagnostic_key == visibility.get("last_visibility_diagnostic_key")
        and now - last_logged_at < VISIBILITY_DIAGNOSTIC_LOG_SECONDS
    ):
        return
    visibility["last_visibility_diagnostic_key"] = diagnostic_key
    visibility["last_visibility_diagnostic_logged_at"] = float(now)
    logger.info(
        "game_overlay visibility=%s",
        {
            "host": {
                "user_enabled": bool(visibility.get("user_enabled")),
                "gameflow": bool(visibility.get("gameflow_in_progress")),
                "hwnd": int(visibility.get("target_hwnd") or 0),
                "renderable": bool(visibility.get("game_renderable")),
                "foreground": bool(visibility.get("game_foreground")),
            },
            "scene": {
                "selection_window_active": source.get("selection_window_active"),
                "ready_slots": int(visibility.get("ready_slots") or 0),
                "blocking_modal": bool(visibility.get("blocking_modal")),
                "scoreboard": bool(visibility.get("scoreboard_key_down")),
                "event_error": str(visibility.get("event_error") or ""),
            },
            "context": {
                "context_ok": bool(visibility.get("context_ok")),
                "champion_id": str(visibility.get("context_champion_id") or ""),
                "source": str(visibility.get("context_source") or ""),
                "error": str(visibility.get("context_error") or ""),
            },
            "decision": {
                "window_visible": bool(should_show),
                "reason": str(reason or ""),
            },
        },
    )


def _build_visibility_status_payload(
    visibility: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: float,
    should_show: bool,
    reason: str,
) -> dict[str, Any]:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    return {
        "schema_version": 1,
        "updated_at": float(now),
        "host": {
            "user_enabled": bool(visibility.get("user_enabled")),
            "gameflow": bool(visibility.get("gameflow_in_progress")),
            "hwnd": int(visibility.get("target_hwnd") or 0),
            "renderable": bool(visibility.get("game_renderable")),
            "foreground": bool(visibility.get("game_foreground")),
        },
        "scene": {
            "selection_window_active": source.get("selection_window_active"),
            "ready_slots": int(visibility.get("ready_slots") or 0),
            "blocking_modal": bool(visibility.get("blocking_modal")),
            "scoreboard": bool(visibility.get("scoreboard_key_down")),
            "event_error": str(visibility.get("event_error") or ""),
        },
        "context": {
            "context_ok": bool(visibility.get("context_ok")),
            "champion_id": str(visibility.get("context_champion_id") or ""),
            "source": str(visibility.get("context_source") or ""),
            "error": str(visibility.get("context_error") or ""),
        },
        "decision": {
            "window_visible": bool(should_show),
            "reason": str(reason or ""),
        },
    }


def _visibility_status_key(payload: Mapping[str, Any]) -> str:
    comparable = dict(payload)
    comparable.pop("updated_at", None)
    return json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_host_visibility_status(
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: float,
    should_show: bool,
    reason: str,
) -> None:
    """把 host 最终显隐原因写入 state，供桌面 UI 和赛后诊断读取。"""

    payload = _build_visibility_status_payload(
        visibility,
        snapshot,
        now=now,
        should_show=should_show,
        reason=reason,
    )
    status_key = _visibility_status_key(payload)
    try:
        last_written_at = float(visibility.get("last_visibility_status_written_at") or 0.0)
    except (TypeError, ValueError):
        last_written_at = 0.0
    if (
        status_key == visibility.get("last_visibility_status_key")
        and now - last_written_at < HOST_VISIBILITY_STATUS_HEARTBEAT_SECONDS
    ):
        return
    try:
        atomic_write_json(GAME_OVERLAY_VISIBILITY_FILE, payload)
    except OSError:
        logger.debug("写入 game_overlay visibility 状态失败。", exc_info=True)
        return
    visibility["last_visibility_status_key"] = status_key
    visibility["last_visibility_status_written_at"] = float(now)


def _sync_event_visibility(
    root: tk.Tk,
    config: dict[str, Any],
    visibility: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    apply_window: bool = True,
    resolved_should_show: bool | None = None,
) -> bool:
    event_visible = bool(snapshot.get("visible"))
    overlay_hwnd = _root_hwnd(root) if bool(config.get("no_activate", True)) else None
    game_hwnd = visibility.get("target_hwnd")
    game_rect = visibility.get("target_rect") if isinstance(visibility.get("target_rect"), tuple) else None
    game_renderable = bool(game_hwnd and is_window_renderable(game_hwnd))
    game_foreground = _is_game_window_foreground(game_hwnd, overlay_hwnd=overlay_hwnd) if game_renderable else False
    gameflow_in_progress = _refresh_gameflow_in_progress(visibility)
    user_enabled = bool(visibility.get("user_enabled"))
    content_ready = _snapshot_has_complete_ready_slots(snapshot)
    ready_slots = _snapshot_ready_slot_count(snapshot)
    selection_window_active = _snapshot_selection_window_active(snapshot)
    event_error = str(snapshot.get("error") or "").strip()
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    blocking_modal = bool(source.get("blocking_modal"))
    scoreboard_key_down = bool(visibility.get("scoreboard_key_down"))
    try:
        generated_at = float(snapshot.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        generated_at = 0.0
    tab_released_at = float(visibility.get("tab_released_at") or 0.0)
    event_fresh_after_tab = not tab_released_at or generated_at >= tab_released_at
    stale_hold_active = False
    visibility.pop("event_stale_hold_until", None)
    should_show = resolved_should_show
    reason = str(visibility.get("visibility_reason") or "")
    if should_show is None:
        should_show, reason = decide_visibility(
            user_enabled=user_enabled,
            event_visible=event_visible,
            game_foreground=game_foreground,
            content_ready=content_ready,
            selection_window_active=selection_window_active,
            gameflow_in_progress=gameflow_in_progress,
            game_hwnd=game_hwnd,
            game_rect=game_rect,
            game_renderable=game_renderable,
            ready_slots=ready_slots,
            scoreboard_key_down=scoreboard_key_down,
            event_fresh_after_tab=event_fresh_after_tab,
            event_error=event_error,
            blocking_modal=blocking_modal,
            diagnostic_mode=bool(config.get("diagnostic_mode")),
            stale_event_hold=stale_hold_active,
        )
    visibility["event_visible"] = event_visible
    visibility["gameflow_in_progress"] = gameflow_in_progress
    visibility["game_renderable"] = game_renderable
    visibility["game_foreground"] = game_foreground
    visibility["content_ready"] = content_ready
    visibility["ready_slots"] = ready_slots
    visibility["selection_window_active"] = selection_window_active
    visibility["event_error"] = event_error
    visibility["blocking_modal"] = blocking_modal
    visibility["event_stale_hold_active"] = stale_hold_active
    visibility["visibility_reason"] = reason
    visibility["render_full_overlay"] = bool(
        should_show
        and selection_window_active is not False
        and (
            event_visible
            or stale_hold_active
            or bool(config.get("diagnostic_mode"))
            or reason in {"visible_detecting", "visible_partial", "waiting_selection"}
        )
    )
    now = time.time()
    _write_host_visibility_status(visibility, snapshot, now=now, should_show=should_show, reason=reason)
    if not apply_window:
        _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
        return should_show
    if visibility.get("window_visible") is should_show:
        _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
        return should_show
    if should_show:
        _show_overlay_window(root, config, visibility)
    else:
        root.withdraw()
    visibility["window_visible"] = should_show
    _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
    return should_show


def _draw_waiting_status(canvas: tk.Canvas, reason: str) -> None:
    """非选择态只显示轻量提示，不能把空事件重新解释为三张检测中卡片。"""

    message = {
        "selection_completed": "海克斯选择已完成，等待下一次选择",
        "waiting_context": "等待英雄上下文",
    }.get(reason, "等待海克斯选择")
    canvas.delete("all")
    canvas.create_rectangle(8, 8, 420, 42, fill="#010A13", outline="#785A28")
    canvas.create_text(
        20,
        25,
        text=message,
        fill="#F0E6D2",
        anchor="w",
        font=("Microsoft YaHei UI", 11, "bold"),
    )


def _write_real_session_evidence(
    root: tk.Tk,
    state: GameSessionState,
    snapshot: Mapping[str, Any],
    visibility: dict[str, Any],
) -> None:
    """真实三槽内容稳定后只记录一次同局五联证据。"""

    vision = state.vision
    if (
        state.visibility.presentation_mode is not PresentationMode.CONTENT
        or state.context is None
        or state.context.local_champion_id is None
        or state.recommendation is None
        or vision is None
        or len(vision.slots) != 3
        or any(slot.state is not VisionSlotState.READY for slot in vision.slots)
    ):
        return
    key = (str(state.session_id), str(state.generation_id), int(vision.epoch))
    if visibility.get("last_evidence_key") == key or visibility.get("evidence_attempt_key") == key:
        return
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    client_rect = source.get("client_rect") if isinstance(source.get("client_rect"), list) else []
    capture_size = source.get("capture_size") if isinstance(source.get("capture_size"), list) else []
    if len(client_rect) != 4 or len(capture_size) != 2:
        return
    visibility["evidence_attempt_key"] = key
    root.update_idletasks()
    evidence_dir = Path(overlay_runtime_state_path("session_evidence")).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    safe_session = "".join(char for char in str(state.session_id) if char.isalnum())[:32] or "session"
    screenshot_path = evidence_dir / f"overlay-{safe_session}-e{int(vision.epoch)}.png"
    from PIL import ImageGrab

    left, top = root.winfo_rootx(), root.winfo_rooty()
    right, bottom = left + root.winfo_width(), top + root.winfo_height()
    ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB").save(screenshot_path)
    client_size = [int(client_rect[2]) - int(client_rect[0]), int(client_rect[3]) - int(client_rect[1])]
    context = state.context
    bundle = build_evidence_bundle(
        state,
        lcu_summary={
            "local_champion_id": str(context.local_champion_id),
            "teammate_champion_ids": [str(value) for value in context.teammate_champion_ids],
            "bench_champion_ids": [str(value) for value in context.bench_champion_ids],
        },
        window_summary={
            "hwnd": int(source.get("window_hwnd") or 0),
            "client_size": client_size,
            "capture_size": [int(value) for value in capture_size],
            "dpi_scale": float(source.get("dpi_scale") or 0.0),
        },
        screenshot=screenshot_path.name,
    )
    write_evidence_bundle(bundle, evidence_dir / "latest_real_session.v1.json")
    visibility["last_evidence_key"] = key
    logger.info("game_overlay real_session_evidence=%s", evidence_dir / "latest_real_session.v1.json")


def _drain_hotkey_requests(request_queue: "queue.Queue[str]", visibility: dict[str, bool]) -> None:
    while True:
        try:
            request = request_queue.get_nowait()
        except queue.Empty:
            return
        if request == "toggle":
            visibility["user_enabled"] = not bool(visibility.get("user_enabled"))


def _refresh_target_window(root: tk.Tk, config: Mapping[str, Any], visibility: dict[str, Any]) -> None:
    """从后台缓存同步 HWND 和 geometry；render tick 不枚举窗口/进程。"""

    poller = visibility.get("window_target_poller")
    if isinstance(poller, WindowTargetPoller):
        target = poller.current()
    else:
        hwnd = visibility.get("target_hwnd")
        rect = visibility.get("target_rect")
        target = (int(hwnd), rect) if hwnd and isinstance(rect, tuple) and len(rect) == 4 else None
    if target is None:
        if isinstance(poller, WindowTargetPoller):
            visibility["target_hwnd"] = None
            visibility["target_rect"] = None
            visibility["pending_geometry"] = ""
        return
    hwnd, rect = target
    visibility["target_hwnd"] = hwnd
    visibility["target_rect"] = rect
    next_geometry = _target_overlay_geometry(rect, dict(config))
    visibility["pending_geometry"] = next_geometry
    if visibility.get("window_visible") and next_geometry != visibility.get("applied_geometry"):
        root.geometry(next_geometry)
        _apply_overlay_rect(root, rect)
        _ensure_overlay_window_styles(root, config)
        visibility["applied_geometry"] = next_geometry


def resolve_event_render_delay_ms(config: Mapping[str, Any], visibility: Mapping[str, Any]) -> int:
    """根据最近选择态事件选择 overlay host 下一帧轮询间隔。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    fast_ms = max(50, int(visibility.get("fast_event_poll_ms") or config.get("fast_event_poll_ms", 60) or 60))
    if bool(visibility.get("render_full_overlay")) or bool(visibility.get("selection_window_active")):
        return min(base_ms, fast_ms)
    try:
        if int(visibility.get("ready_slots") or 0) > 0:
            return min(base_ms, fast_ms)
    except (TypeError, ValueError):
        pass
    try:
        fast_until = float(visibility.get("fast_event_until") or 0.0)
    except (TypeError, ValueError):
        fast_until = 0.0
    return min(base_ms, fast_ms) if time.monotonic() < fast_until else base_ms


def resolve_event_render_retry_delay_ms(config: Mapping[str, Any], failure_count: int) -> int:
    """渲染失败后的下一次重试间隔；失败路径不进入 fast poll。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    if int(failure_count or 0) <= RENDER_ERROR_BACKOFF_AFTER:
        return base_ms
    exponent = min(8, int(failure_count) - RENDER_ERROR_BACKOFF_AFTER)
    return min(RENDER_ERROR_BACKOFF_MAX_MS, base_ms * (2 ** exponent))


def _schedule_event_render(
    root: tk.Tk,
    canvas: tk.Canvas,
    config: dict[str, Any],
    visibility: dict[str, Any],
    hotkey_queue: "queue.Queue[str]",
    *,
    data_source: OverlayDataSource | None = None,
) -> Callable[[], None]:
    """单一 tick 同步窗口、事件和显隐；隐藏时不加载 hint/context。"""

    fast_poll_ms = max(50, int(config.get("fast_event_poll_ms", 60) or 60))
    fast_hold_seconds = max(0.0, float(config.get("fast_event_hold_ms", 1200) or 1200) / 1000.0)
    source = data_source or SharedOverlayDataSource()
    failure_count = 0
    render_after_id: str | None = None

    def retry_delay_ms() -> int:
        if failure_count <= 0:
            return resolve_event_render_delay_ms(config, visibility)
        return resolve_event_render_retry_delay_ms(config, failure_count)

    def note_fast_event(snapshot: Mapping[str, Any]) -> None:
        event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
        slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
        ready_slots = sum(
            1
            for slot in slots
            if isinstance(slot, Mapping)
            and (str(slot.get("state") or "") == "ready" or bool(str(slot.get("augment_id") or "").strip()))
        )
        if (
            bool(snapshot.get("active"))
            or bool(snapshot.get("visible"))
            or event_source.get("selection_window_active") is True
            or ready_slots > 0
        ):
            visibility["fast_event_until"] = max(
                float(visibility.get("fast_event_until") or 0.0),
                time.monotonic() + fast_hold_seconds,
            )
        visibility["fast_event_poll_ms"] = fast_poll_ms

    def schedule_render(delay_ms: int, *, replace: bool = False) -> None:
        nonlocal render_after_id
        if render_after_id is not None:
            if not replace:
                return
            try:
                canvas.after_cancel(render_after_id)
            except Exception:
                logger.debug("取消 overlay render after 失败。", exc_info=True)
            render_after_id = None
        render_after_id = canvas.after(max(0, int(delay_ms)), render_once)

    def request_render() -> None:
        schedule_render(0, replace=True)

    def render_once() -> None:
        nonlocal failure_count, render_after_id
        render_after_id = None
        success = False
        try:
            _drain_hotkey_requests(hotkey_queue, visibility)
            _refresh_target_window(root, config, visibility)
            tab_down = is_scoreboard_key_down()
            previous_tab_down = bool(visibility.get("scoreboard_key_down"))
            if tab_down:
                visibility["scoreboard_key_down"] = True
            else:
                visibility["scoreboard_key_down"] = False
                if previous_tab_down:
                    visibility["tab_released_at"] = time.time()
            snapshot = source.read_event()
            note_fast_event(snapshot)
            if bool(config.get("diagnostic_mode")):
                status = _extract_event_status(snapshot)
                diagnostic_key = tuple(status.get(key) for key in (
                    "gate_state", "ready_slots", "blocking_modal", "selection_window_active", "error"
                ))
                if diagnostic_key != visibility.get("last_diagnostic_key"):
                    logger.info("game_overlay diagnostic=%s", status)
                    visibility["last_diagnostic_key"] = diagnostic_key
            should_show = _sync_event_visibility(root, config, visibility, snapshot, apply_window=False)
            if not should_show:
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=False,
                )
                success = True
                return
            if bool(config.get("diagnostic_mode")) and not bool(visibility.get("render_full_overlay")):
                _draw_diagnostic_status(canvas, str(visibility.get("visibility_reason") or ""), snapshot)
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                )
                success = True
                return
            if not bool(visibility.get("render_full_overlay")):
                event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
                waiting_reason = str(event_source.get("reason") or visibility.get("visibility_reason") or "")
                _draw_waiting_status(canvas, waiting_reason)
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                )
                success = True
                return
            context = source.read_context()
            now = time.time()
            recent_context = None
            visibility["context_ok"] = bool(isinstance(context, Mapping) and context.get("ok"))
            visibility["context_champion_id"] = str(context.get("champion_id") or "") if isinstance(context, Mapping) else ""
            visibility["context_source"] = str(context.get("source") or "") if isinstance(context, Mapping) else ""
            visibility["context_error"] = str(context.get("error") or "") if isinstance(context, Mapping) else "context_missing"
            if isinstance(context, Mapping) and context.get("ok"):
                visibility["last_ok_context"] = dict(context)
                visibility["last_ok_context_seen_at"] = now
            else:
                try:
                    last_seen = float(visibility.get("last_ok_context_seen_at") or 0.0)
                except (TypeError, ValueError):
                    last_seen = 0.0
                if now - last_seen <= RECENT_CONTEXT_HOLD_SECONDS:
                    cached_context = visibility.get("last_ok_context")
                    if isinstance(cached_context, Mapping) and cached_context.get("ok"):
                        recent_context = cached_context
            effective_context = recent_context if not context.get("ok") and recent_context is not None else context
            snapshot_view = source.open_view()
            if snapshot_view is not None:
                hint_cache = snapshot_view.get_overlay_hints()
                hint_cache.setdefault("snapshot", {}).update(snapshot_view.status())
            else:
                hint_cache = source.read_hint_cache()
            session_state = build_runtime_session(
                event=snapshot,
                context_payload=effective_context,
                snapshot_view=snapshot_view,
                user_enabled=bool(visibility.get("user_enabled")),
                game_present=bool(visibility.get("target_hwnd")),
            )
            model = build_render_model_from_session(session_state, hint_cache=hint_cache)
            visibility["session_state"] = session_state
            _log_waiting_context_diagnostic(visibility, snapshot, context, model)
            draw_overlay_frame(canvas, model, perf_sink=visibility)
            _sync_event_visibility(
                root,
                config,
                visibility,
                snapshot,
                resolved_should_show=True,
            )
            try:
                _write_real_session_evidence(root, session_state, snapshot, visibility)
            except Exception:
                visibility.pop("evidence_attempt_key", None)
                logger.warning("写入真实会话验收证据失败。", exc_info=True)
            success = True
        except Exception:
            failure_count += 1
            if failure_count <= RENDER_ERROR_BACKOFF_AFTER:
                logger.exception("overlay 渲染轮询失败；下一 tick 将继续重试。")
            elif failure_count == RENDER_ERROR_BACKOFF_AFTER + 1:
                logger.exception("overlay 渲染轮询连续失败，开始退避。")
            else:
                logger.warning("overlay 渲染轮询仍在失败：连续失败=%s，退避中。", failure_count)
        finally:
            if success:
                failure_count = 0
            schedule_render(retry_delay_ms())

    render_once()
    return request_render


def run_overlay_host(*, diagnostic: bool = False) -> None:
    """启动独立 overlay 窗口；diagnostic 只增加去重日志。"""

    _prepare_host_hint_cache()
    _set_dpi_awareness()
    config = build_overlay_window_config()
    config["diagnostic_mode"] = bool(diagnostic)
    titles = [str(title) for title in config.get("follow_window_titles", [])]
    initial_target = _find_target_game_window(titles)
    initial_width, initial_height, initial_geometry = _resolve_initial_overlay_viewport(initial_target, config)

    root = tk.Tk()
    root.withdraw()
    root.title(config["title"])
    root.geometry(initial_geometry)
    root.attributes("-alpha", config["alpha"])
    root.attributes("-topmost", config["topmost"])
    root.overrideredirect(True)

    canvas = tk.Canvas(
        root,
        width=initial_width,
        height=initial_height,
        highlightthickness=0,
        bd=0,
    )
    _apply_transparent_background(root, canvas, config)
    canvas.pack(fill=tk.BOTH, expand=True)

    visibility: dict[str, Any] = {
        "user_enabled": True,
        "event_visible": False,
        "game_foreground": False,
        "window_visible": False,
        "target_hwnd": initial_target[0] if initial_target is not None else None,
        "target_rect": initial_target[1] if initial_target is not None else None,
        "pending_geometry": initial_geometry if initial_target is not None else "",
        "applied_geometry": initial_geometry if initial_target is not None else "",
        "scoreboard_key_down": False,
        "tab_released_at": 0.0,
    }
    hotkey_queue: "queue.Queue[str]" = queue.Queue()
    hotkey_controller: HotkeyController | None = None
    data_source = SharedOverlayDataSource()
    gameflow_poller = GameflowPoller()
    window_target_poller = WindowTargetPoller(titles, initial_target=initial_target)
    foreground_event = threading.Event()
    foreground_hook: ForegroundEventHook | None = None
    visibility["gameflow_poller"] = gameflow_poller
    visibility["window_target_poller"] = window_target_poller

    root.update_idletasks()
    _ensure_overlay_window_styles(root, config)
    gameflow_poller.start()
    window_target_poller.start()
    hotkey_controller = _start_hotkey_thread(hotkey_queue)
    request_overlay_render = _schedule_event_render(root, canvas, config, visibility, hotkey_queue, data_source=data_source)
    foreground_hook = _register_foreground_event_hook(foreground_event)
    _schedule_foreground_event_drain(root, foreground_event, request_overlay_render)
    _schedule_exit_file_watch(root)
    root.after_idle(_signal_overlay_ready)
    logger.info("game_overlay host 已启动：event_poll_ms=%s", config["event_poll_ms"])

    try:
        root.mainloop()
    finally:
        logger.info("game_overlay host 已停止")
        _stop_foreground_event_hook(foreground_hook)
        _stop_hotkey_thread(hotkey_controller)
        window_target_poller.stop()
        gameflow_poller.stop()


def run_self_check() -> dict[str, Any]:
    """无 GUI 自检：验证冻结态入口、配置和事件读取链路可用。"""

    config = build_overlay_window_config()
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    hints = hint_cache.get("hints") if isinstance(hint_cache, Mapping) else {}
    hint_count = len(hints) if isinstance(hints, Mapping) else 0
    synergy_hint_count = sum(
        1
        for hint in (hints.values() if isinstance(hints, Mapping) else [])
        if isinstance(hint, Mapping) and isinstance(hint.get("synergies"), list) and hint.get("synergies")
    )
    model = build_render_model(snapshot, hint_cache=hint_cache, context=context)
    status_counts: dict[str, int] = {}
    for row in model["stats"]:
        status_code = str(row.get("status_code") or "")
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
    event_status = _extract_event_status(snapshot)
    try:
        state_age_ms = int(max(0.0, time.time() - float(snapshot.get("generated_at") or 0.0)) * 1000)
    except (TypeError, ValueError):
        state_age_ms = None
    return {
        "ok": True,
        "title": config["title"],
        "process_health": {"host": "self-check", "sidecar": "not-inspected"},
        "state_age_ms": state_age_ms,
        "event_poll_ms": config["event_poll_ms"],
        "no_activate": bool(config.get("no_activate")),
        "event_ok": bool(snapshot.get("ok")),
        "event_visible": bool(snapshot.get("visible")),
        "event_error": str(snapshot.get("error") or ""),
        "event_reason": str((snapshot.get("source") or {}).get("reason") or "") if isinstance(snapshot.get("source"), dict) else "",
        "ready_slots": event_status["ready_slots"],
        "selection_window_active": event_status["selection_window_active"],
        "schema_version": snapshot.get("schema_version"),
        "cache_ok": not bool(hint_cache.get("error")) if isinstance(hint_cache, Mapping) else False,
        "hint_cache_error": str(hint_cache.get("error") or ""),
        "hint_count": hint_count,
        "private_stats_enabled": _cache_allows_private_stats(hint_cache),
        "synergy_hint_count": synergy_hint_count,
        "context_champion_id": str(context.get("champion_id") or ""),
        "context_champion_name": str(context.get("champion_name") or ""),
        "context_synergy_hint_count": _count_current_context_synergy_hints(hints, context),
        "render_stats_count": sum(1 for row in model["stats"] if row["status_code"] == "READY"),
        "render_synergy_count": len(model["synergies"]),
        "render_status_counts": status_counts,
        "context_status": "ok" if context.get("ok") else str(context.get("error") or "context_missing"),
        "context_source": str(context.get("source") or ""),
        "context_ok": bool(context.get("ok")),
        "context_error": str(context.get("error") or ""),
    }


def render_acceptance_screenshot(
    output_path: str | Path,
    *,
    width: int = 1280,
    height: int = 720,
) -> dict[str, Any]:
    """用真实 Tk Canvas 和当前 generation/event/context 生成验收截图。"""

    from PIL import ImageGrab

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    model = build_render_model(snapshot, hint_cache=hint_cache, context=context)

    _set_dpi_awareness()
    root = tk.Tk()
    root.title("Hextech Overlay Acceptance")
    root.geometry(f"{max(640, int(width))}x{max(360, int(height))}+0+0")
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, bg="#10131A", highlightthickness=0, bd=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    try:
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.update()
        draw_overlay_frame(canvas, model)
        root.update_idletasks()
        root.update()
        time.sleep(0.2)
        left = root.winfo_rootx()
        top = root.winfo_rooty()
        right = left + root.winfo_width()
        bottom = top + root.winfo_height()
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
        image.save(target)
    finally:
        root.destroy()

    status_counts: dict[str, int] = {}
    for row in model.get("stats", []):
        code = str(row.get("status_code") or "")
        status_counts[code] = status_counts.get(code, 0) + 1
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache.get("snapshot"), Mapping) else {}
    return {
        "ok": target.is_file() and target.stat().st_size > 0,
        "path": str(target),
        "width": image.width,
        "height": image.height,
        "generation_id": str(snapshot_status.get("generation_id") or ""),
        "status_counts": status_counts,
        "context_champion_id": str(context.get("champion_id") or ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech game overlay host。")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic", action="store_true", help="记录去重诊断日志，不绘制状态 UI。")
    parser.add_argument("--self-check", action="store_true", help="执行无 GUI overlay 入口自检后退出。")
    parser.add_argument("--acceptance-screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-width", type=int, default=1280, help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-height", type=int, default=720, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        print(json.dumps(run_self_check(), ensure_ascii=False, indent=2))
        return 0
    if args.acceptance_screenshot is not None:
        result = render_acceptance_screenshot(
            args.acceptance_screenshot,
            width=args.acceptance_width,
            height=args.acceptance_height,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    run_overlay_host(diagnostic=args.diagnostic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""独立游戏内 overlay 的 Tk/Win32 窗口宿主。

本模块只负责透明置顶窗口、点击穿透、热键、游戏窗口跟随和本地事件渲染。
真实识别由 Vision sidecar 写入本地事件文件，host 不截图、不识别、不访问远端。
"""

from __future__ import annotations

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
from pathlib import Path
from typing import Any, Mapping

from processing.lol_window import find_lol_game_window, is_scoreboard_key_down, root_window_hwnd
from tools.atomic_io import atomic_write_json

from .data_source import OverlayDataSource, SharedOverlayDataSource
from .renderer import build_render_model, draw_overlay_frame


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
LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
OVERLAY_READY_FILE_ENV = "HEXTECH_OVERLAY_READY_FILE"


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
        "follow_window_titles": ["League of Legends (TM) Client"],
        "top_offset": 132,
        "event_poll_ms": 120,
        "diagnostic_mode": False,
    }


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        logger.debug("设置 overlay DPI 感知失败。", exc_info=True)


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


def _poll_alt_h_hotkey(controller: HotkeyController, user32: Any) -> None:
    """全局热键被占用时，用按键边沿轮询保留关闭 overlay 的能力。"""

    was_pressed = False
    while not controller.stop_requested.is_set():
        alt_pressed = bool(int(user32.GetAsyncKeyState(VK_MENU)) & 0x8000)
        h_pressed = bool(int(user32.GetAsyncKeyState(ord("H"))) & 0x8000)
        pressed = alt_pressed and h_pressed
        if pressed and not was_pressed:
            controller.request_queue.put("toggle")
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


def _find_target_game_window(window_titles: list[str]) -> tuple[int, tuple[int, int, int, int]] | None:
    return find_lol_game_window(window_titles=window_titles)


def _find_target_game_rect(window_titles: list[str]) -> tuple[int, int, int, int] | None:
    target = _find_target_game_window(window_titles)
    return target[1] if target is not None else None


def _is_game_window_foreground(hwnd: int | None, *, overlay_hwnd: int | None = None) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    foreground = int(user32.GetForegroundWindow())
    if not foreground:
        return False
    foreground_root = root_window_hwnd(foreground)
    if overlay_hwnd and foreground_root == root_window_hwnd(overlay_hwnd):
        return False
    return foreground_root == root_window_hwnd(hwnd)


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

    return {
        "gate_state": str(source.get("gate_state") or "").strip(),
        "ready_slots": optional_number(source.get("ready_slots"), int) or 0,
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
    if ready_path:
        atomic_write_json(
            Path(ready_path),
            {"pid": os.getpid(), "ready_at": time.time()},
            ensure_ascii=False,
            indent=2,
        )

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


def _should_show_overlay(
    *,
    user_enabled: bool,
    event_visible: bool,
    game_foreground: bool,
    content_ready: bool,
    selection_window_active: bool | None,
    ready_slots: int | None = None,
    scoreboard_key_down: bool = False,
    event_fresh_after_tab: bool = True,
    event_error: str = "",
    blocking_modal: bool = False,
) -> bool:
    """统一显隐门：V2 场景中至少一个稳定槽即可显示。"""

    if not user_enabled or not game_foreground:
        return False
    if event_error or blocking_modal:
        return False
    if scoreboard_key_down or not event_fresh_after_tab:
        return False
    # sidecar 正常会保证 active 事件来自真实选择按钮；host 仍防御矛盾事件，
    # 避免旧/手写事件绕过“蓝色按钮是生命周期依据”的合同。
    if selection_window_active is False:
        return False
    resolved_ready_slots = (3 if content_ready else 0) if ready_slots is None else int(ready_slots)
    return bool(event_visible and resolved_ready_slots >= 1)


def _visibility_reason(
    *,
    user_enabled: bool,
    game_foreground: bool,
    event_visible: bool,
    content_ready: bool,
    ready_slots: int,
    selection_window_active: bool | None,
    scoreboard_key_down: bool,
    event_fresh_after_tab: bool,
    event_error: str,
    blocking_modal: bool,
    source_reason: str,
) -> str:
    if not user_enabled:
        return "user_disabled"
    if not game_foreground:
        return "game_not_foreground"
    if event_error:
        return event_error
    if blocking_modal:
        return "blocking_modal_present"
    if scoreboard_key_down:
        return "scoreboard_key_down"
    if not event_fresh_after_tab:
        return "awaiting_post_tab_event"
    if selection_window_active is False:
        return "selection_window_inactive"
    if not event_visible:
        return source_reason or "event_inactive"
    if ready_slots <= 0:
        return "content_not_ready"
    return "visible_ready" if content_ready else "visible_partial"


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
    game_foreground = _is_game_window_foreground(visibility.get("target_hwnd"), overlay_hwnd=overlay_hwnd)
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
    should_show = resolved_should_show
    if should_show is None:
        should_show = _should_show_overlay(
            user_enabled=user_enabled,
            event_visible=event_visible,
            game_foreground=game_foreground,
            content_ready=content_ready,
            selection_window_active=selection_window_active,
            ready_slots=ready_slots,
            scoreboard_key_down=scoreboard_key_down,
            event_fresh_after_tab=event_fresh_after_tab,
            event_error=event_error,
            blocking_modal=blocking_modal,
        )
    visibility["event_visible"] = event_visible
    visibility["game_foreground"] = game_foreground
    visibility["content_ready"] = content_ready
    visibility["ready_slots"] = ready_slots
    visibility["selection_window_active"] = selection_window_active
    visibility["event_error"] = event_error
    visibility["blocking_modal"] = blocking_modal
    visibility["visibility_reason"] = _visibility_reason(
        user_enabled=user_enabled,
        game_foreground=game_foreground,
        event_visible=event_visible,
        content_ready=content_ready,
        ready_slots=ready_slots,
        selection_window_active=selection_window_active,
        scoreboard_key_down=scoreboard_key_down,
        event_fresh_after_tab=event_fresh_after_tab,
        event_error=event_error,
        blocking_modal=blocking_modal,
        source_reason=_snapshot_source_reason(snapshot),
    )
    if not apply_window:
        return should_show
    if visibility.get("window_visible") is should_show:
        return should_show
    if should_show:
        _show_overlay_window(root, config, visibility)
    else:
        root.withdraw()
    visibility["window_visible"] = should_show
    logger.info(
        "game_overlay visibility=%s reason=%s event_visible=%s ready_slots=%s game_foreground=%s",
        "shown" if should_show else "hidden",
        visibility["visibility_reason"],
        event_visible,
        ready_slots,
        game_foreground,
    )
    return should_show


def _drain_hotkey_requests(request_queue: "queue.Queue[str]", visibility: dict[str, bool]) -> None:
    while True:
        try:
            request = request_queue.get_nowait()
        except queue.Empty:
            return
        if request == "toggle":
            visibility["user_enabled"] = not bool(visibility.get("user_enabled"))


def _refresh_target_window(root: tk.Tk, config: Mapping[str, Any], visibility: dict[str, Any]) -> None:
    """在事件 tick 内刷新 HWND 和 geometry，避免独立轮询产生节拍竞态。"""

    titles = [str(title) for title in config.get("follow_window_titles", [])]
    target = _find_target_game_window(titles)
    if target is None:
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
        visibility["applied_geometry"] = next_geometry


def _schedule_event_render(
    root: tk.Tk,
    canvas: tk.Canvas,
    config: dict[str, Any],
    visibility: dict[str, Any],
    hotkey_queue: "queue.Queue[str]",
    *,
    data_source: OverlayDataSource | None = None,
) -> None:
    """单一 tick 同步窗口、事件和显隐；隐藏时不加载 hint/context。"""

    poll_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    source = data_source or SharedOverlayDataSource()

    def render_once() -> None:
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
                return
            hint_cache = source.read_hint_cache()
            context = source.read_context()
            model = build_render_model(snapshot, hint_cache=hint_cache, context=context)
            draw_overlay_frame(canvas, model, perf_sink=visibility)
            _sync_event_visibility(
                root,
                config,
                visibility,
                snapshot,
                resolved_should_show=True,
            )
        except Exception:
            logger.exception("overlay 渲染轮询失败；下一 tick 将继续重试。")
        finally:
            canvas.after(poll_ms, render_once)

    render_once()


def run_overlay_host(*, diagnostic: bool = False) -> None:
    """启动独立 overlay 窗口；diagnostic 只增加去重日志。"""

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

    root.update_idletasks()
    _apply_overlay_window_styles(
        root,
        click_through=bool(config["click_through"]),
        no_activate=bool(config.get("no_activate", True)),
    )
    hotkey_controller = _start_hotkey_thread(hotkey_queue)
    _schedule_event_render(root, canvas, config, visibility, hotkey_queue, data_source=data_source)
    root.after_idle(_signal_overlay_ready)
    logger.info("game_overlay host 已启动：event_poll_ms=%s", config["event_poll_ms"])

    try:
        root.mainloop()
    finally:
        logger.info("game_overlay host 已停止")
        _stop_hotkey_thread(hotkey_controller)


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
    return {
        "ok": True,
        "title": config["title"],
        "event_poll_ms": config["event_poll_ms"],
        "no_activate": bool(config.get("no_activate")),
        "event_ok": bool(snapshot.get("ok")),
        "event_visible": bool(snapshot.get("visible")),
        "event_error": str(snapshot.get("error") or ""),
        "event_reason": str((snapshot.get("source") or {}).get("reason") or "") if isinstance(snapshot.get("source"), dict) else "",
        "schema_version": snapshot.get("schema_version"),
        "hint_cache_error": str(hint_cache.get("error") or ""),
        "hint_count": hint_count,
        "private_stats_enabled": _cache_allows_private_stats(hint_cache),
        "synergy_hint_count": synergy_hint_count,
        "context_champion_id": str(context.get("champion_id") or ""),
        "context_champion_name": str(context.get("champion_name") or ""),
        "context_synergy_hint_count": _count_current_context_synergy_hints(hints, context),
        "render_stats_count": sum(1 for row in model["stats"] if row["state"] == "ready"),
        "render_synergy_count": len(model["synergies"]),
        "context_ok": bool(context.get("ok")),
        "context_error": str(context.get("error") or ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech game overlay host。")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic", action="store_true", help="记录去重诊断日志，不绘制状态 UI。")
    parser.add_argument("--self-check", action="store_true", help="执行无 GUI overlay 入口自检后退出。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        print(json.dumps(run_self_check(), ensure_ascii=False, indent=2))
        return 0
    run_overlay_host(diagnostic=args.diagnostic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

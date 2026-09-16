"""Overlay host 的 Win32 窗口、热键与前台事件适配器。"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from typing import Any, Callable, Mapping

from hextech.interfaces.overlay.host_common import (
    EVENT_SYSTEM_FOREGROUND,
    FOREGROUND_EVENT_DRAIN_MS,
    GA_ROOT,
    GWL_EXSTYLE,
    GWL_WNDPROC,
    HOTKEY_FALLBACK_DEBOUNCE_SECONDS,
    HOTKEY_FALLBACK_POLL_SECONDS,
    HOTKEY_MODE_ID,
    HWND_TOPMOST,
    LRESULT,
    MA_NOACTIVATE,
    MOD_ALT,
    MSG,
    SWP_FRAMECHANGED,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    SW_SHOWNOACTIVATE,
    VK_MENU,
    WINEVENTPROC,
    WINEVENT_OUTOFCONTEXT,
    WINEVENT_SKIPOWNPROCESS,
    WM_HOTKEY,
    WM_MOUSEACTIVATE,
    WM_QUIT,
    WNDPROC,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    ForegroundEventHook,
    HotkeyController,
)
from hextech.modules.data.overlay_source import OverlayDataSource, SharedOverlayDataSource
from hextech.modules.vision.window import (
    configure_process_dpi_awareness,
    find_lol_game_window,
    is_window_foreground,
)
from hextech.modules.vision.window_titles import LOL_GAME_WINDOW_TITLE


logger = logging.getLogger(__name__)
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

def build_overlay_window_config() -> dict[str, Any]:
    """返回 overlay 的可验收窗口能力配置。"""

    return {
        "title": "Hextech Game Overlay",
        "width": 900,
        "height": 150,
        "topmost": True,
        "click_through": True,
        "no_activate": True,
        "mode_hotkey": "Alt+J",
        "default_display_mode": "compact",
        "alpha": 0.96,
        "transparent_color": "#010203",
        "badge_height": 72,
        "badge_width_ratio": 0.15,
        "show_missing_synergy_reason": True,
        "follow_window_titles": [LOL_GAME_WINDOW_TITLE],
        "top_offset": 132,
        # 两档轮询：无游戏窗口 250ms；游戏存在及选择期均只读内存邮箱 16ms。
        "event_poll_ms": 250,
        "game_event_poll_ms": 16,
        "fast_event_poll_ms": 16,
        "fast_event_hold_ms": 1200,
        "diagnostic_mode": False,
        "capture_exclusion_required": True,
    }


def _set_dpi_awareness() -> None:
    mode = configure_process_dpi_awareness()
    if mode == "unavailable":
        logger.debug("设置 overlay DPI 感知失败。")


def _prepare_host_hint_cache(data_source: OverlayDataSource | None = None) -> dict[str, Any]:
    """用 Host 的同一个数据源预热 generation，并返回首次 Hint 副本。"""

    source = data_source or SharedOverlayDataSource()
    try:
        return source.read_hint_cache()
    except Exception as exc:
        logger.warning("overlay host 启动前准备 hint cache 失败：%s", exc, exc_info=True)
        return {}


def _root_hwnd(root: tk.Tk) -> int:
    """解析 Tk 顶层窗口 HWND，避免把扩展样式写到内部子窗口。"""

    user32 = ctypes.windll.user32
    raw_hwnd = int(root.winfo_id())
    top_hwnd = int(user32.GetAncestor(raw_hwnd, GA_ROOT))
    return top_hwnd or raw_hwnd


def _query_overlay_capture_exclusion(
    hwnd: int,
    *,
    user32: Any | None = None,
) -> dict[str, Any]:
    """回读窗口捕获排除状态；API 缺失与调用失败保持可区分。"""

    if not hwnd:
        return {
            "status": "failed",
            "requested_affinity": WDA_EXCLUDEFROMCAPTURE,
            "applied_affinity": WDA_NONE,
            "set_ok": False,
            "query_ok": False,
            "error_code": 0,
            "reason": "overlay_hwnd_missing",
        }
    try:
        resolved = user32 if user32 is not None else ctypes.windll.user32
        query = getattr(resolved, "GetWindowDisplayAffinity")
    except (AttributeError, OSError):
        return {
            "status": "unsupported",
            "requested_affinity": WDA_EXCLUDEFROMCAPTURE,
            "applied_affinity": WDA_NONE,
            "set_ok": False,
            "query_ok": False,
            "error_code": 0,
            "reason": "display_affinity_api_unavailable",
        }
    affinity = wintypes.DWORD(WDA_NONE)
    try:
        query_ok = bool(query(int(hwnd), ctypes.byref(affinity)))
    except (OSError, TypeError, ValueError):
        query_ok = False
    applied = int(affinity.value)
    return {
        "status": "applied" if query_ok and applied == WDA_EXCLUDEFROMCAPTURE else "failed",
        "requested_affinity": WDA_EXCLUDEFROMCAPTURE,
        "applied_affinity": applied,
        "set_ok": False,
        "query_ok": query_ok,
        "error_code": 0,
        "reason": "" if query_ok and applied == WDA_EXCLUDEFROMCAPTURE else "display_affinity_readback_mismatch",
    }


def _ensure_overlay_capture_exclusion(
    root: tk.Tk,
    *,
    user32: Any | None = None,
) -> dict[str, Any]:
    """在首次映射前设置并回读 WDA_EXCLUDEFROMCAPTURE。"""

    hwnd = _root_hwnd(root)
    try:
        resolved = user32 if user32 is not None else ctypes.windll.user32
        setter = getattr(resolved, "SetWindowDisplayAffinity")
        getattr(resolved, "GetWindowDisplayAffinity")
    except (AttributeError, OSError):
        return _query_overlay_capture_exclusion(hwnd, user32=user32)
    before = _query_overlay_capture_exclusion(hwnd, user32=resolved)
    if before.get("status") == "applied":
        return before
    try:
        if hasattr(ctypes, "set_last_error"):
            ctypes.set_last_error(0)
        set_ok = bool(setter(int(hwnd), WDA_EXCLUDEFROMCAPTURE))
        error_code = int(ctypes.get_last_error()) if hasattr(ctypes, "get_last_error") else 0
    except (OSError, TypeError, ValueError):
        set_ok = False
        error_code = 0
    result = _query_overlay_capture_exclusion(hwnd, user32=resolved)
    result["set_ok"] = set_ok
    result["error_code"] = error_code
    if result.get("status") != "applied" and not result.get("reason"):
        result["reason"] = "display_affinity_set_failed"
    return result


def _apply_overlay_rect(
    root: tk.Tk,
    rect: tuple[int, int, int, int],
    *,
    show: bool = False,
) -> bool:
    """用整数虚拟屏坐标定位 client rect，兼容左侧或上方副屏。"""

    left, top, right, bottom = rect
    flags = SWP_NOACTIVATE | (SWP_SHOWWINDOW if show else 0)
    return bool(
        ctypes.windll.user32.SetWindowPos(
            _root_hwnd(root),
            HWND_TOPMOST,
            int(left),
            int(top),
            max(1, int(right) - int(left)),
            max(1, int(bottom) - int(top)),
            flags,
        )
    )


def _show_overlay_hwnd(root: tk.Tk, rect: tuple[int, int, int, int] | None) -> int:
    """以不抢焦点的方式映射 HWND，并显式携带 ``SWP_SHOWWINDOW``。"""

    hwnd = _root_hwnd(root)
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    if rect is not None:
        _apply_overlay_rect(root, rect, show=True)
    else:
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    return hwnd


def _window_client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """在当前进程 DPI awareness context 内返回窗口 client rect 的屏幕物理坐标。"""

    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    origin = wintypes.POINT(0, 0)
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = max(0, int(rect.right) - int(rect.left))
    height = max(0, int(rect.bottom) - int(rect.top))
    return (int(origin.x), int(origin.y), int(origin.x) + width, int(origin.y) + height)


def _query_window_cloaked(hwnd: int) -> tuple[bool | None, str]:
    """查询 DWM cloak；API 不可用时返回 degraded，而不是伪造失败。"""

    try:
        dwmapi = ctypes.windll.dwmapi
        value = wintypes.DWORD(0)
        result = int(
            dwmapi.DwmGetWindowAttribute(
                hwnd,
                14,  # DWMWA_CLOAKED
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
        )
    except (AttributeError, OSError):
        return None, "unavailable"
    if result != 0:
        return None, f"error_{result & 0xFFFFFFFF:08x}"
    return bool(value.value), "ok"


def _probe_overlay_hwnd(
    root: tk.Tk,
    config: Mapping[str, Any],
    expected_rect: tuple[int, int, int, int] | None,
    *,
    rect_tolerance: int = 2,
) -> dict[str, Any]:
    """读取 mapped/style/client-rect 事实；``WS_VISIBLE`` 只作为其中一个信号。"""

    try:
        hwnd = _root_hwnd(root)
        user32 = ctypes.windll.user32
        valid = bool(hwnd and user32.IsWindow(hwnd))
        style = _get_window_exstyle(hwnd) if valid else 0
        actual_rect = _window_client_rect_on_screen(hwnd) if valid else None
        cloaked, dwm_status = _query_window_cloaked(hwnd) if valid else (None, "not_queried")
        capture_exclusion = (
            _query_overlay_capture_exclusion(hwnd, user32=user32)
            if valid
            else _query_overlay_capture_exclusion(0, user32=user32)
        )
        style_checks = {
            "topmost": bool(style & WS_EX_TOPMOST),
            "layered": bool(style & WS_EX_LAYERED),
            "transparent": bool(style & WS_EX_TRANSPARENT)
            if bool(config.get("click_through", True))
            else True,
            "no_activate": bool(style & WS_EX_NOACTIVATE)
            if bool(config.get("no_activate", True))
            else True,
            "toolwindow": bool(style & WS_EX_TOOLWINDOW),
        }
        tolerance = max(0, int(rect_tolerance))
        rect_matches = bool(
            expected_rect is not None
            and actual_rect is not None
            and all(abs(int(actual) - int(expected)) <= tolerance for actual, expected in zip(actual_rect, expected_rect))
        )
        return {
            "overlay_hwnd": int(hwnd or 0),
            "valid": valid,
            "ws_visible": bool(valid and user32.IsWindowVisible(hwnd)),
            "iconic": bool(valid and user32.IsIconic(hwnd)),
            "cloaked": cloaked,
            "dwm_status": dwm_status,
            "expected_client_rect": list(expected_rect) if expected_rect is not None else [],
            "actual_rect": list(actual_rect) if actual_rect is not None else [],
            "rect_matches": rect_matches,
            "style_checks": style_checks,
            "style_ok": all(style_checks.values()),
            "capture_exclusion": capture_exclusion,
            "error": "",
        }
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        return {
            "overlay_hwnd": 0,
            "valid": False,
            "ws_visible": False,
            "iconic": False,
            "cloaked": None,
            "dwm_status": "unavailable",
            "expected_client_rect": list(expected_rect) if expected_rect is not None else [],
            "actual_rect": [],
            "rect_matches": False,
            "style_checks": {},
            "style_ok": False,
            "capture_exclusion": {
                "status": "failed",
                "requested_affinity": WDA_EXCLUDEFROMCAPTURE,
                "applied_affinity": WDA_NONE,
                "set_ok": False,
                "query_ok": False,
                "error_code": 0,
                "reason": type(exc).__name__,
            },
            "error": type(exc).__name__,
        }


def _sample_screen_pixels(points: list[tuple[int, int]]) -> list[tuple[int, int, int] | None]:
    """只读取调用方给定的有限屏幕点；不抓取、不保存任何周边像素。"""

    if not points:
        return []
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    if not hdc:
        return [None] * len(points)
    pixels: list[tuple[int, int, int] | None] = []
    try:
        for x, y in points:
            color = int(gdi32.GetPixel(hdc, int(x), int(y))) & 0xFFFFFFFF
            if color == 0xFFFFFFFF:
                pixels.append(None)
            else:
                pixels.append((color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF))
    finally:
        user32.ReleaseDC(0, hdc)
    return pixels


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

    changed = _apply_overlay_window_styles(
        root,
        click_through=bool(config.get("click_through", True)),
        no_activate=bool(config.get("no_activate", True)),
    )
    if bool(config.get("capture_exclusion_required", False)):
        _ensure_overlay_capture_exclusion(root)
    return changed


def _poll_mode_hotkey(controller: HotkeyController, user32: Any) -> None:
    """注册失败时只轮询公开的 Alt+J 显示模式切换。"""

    was_pressed = False
    last_toggle_at = 0.0
    while not controller.stop_requested.is_set():
        alt_pressed = bool(int(user32.GetAsyncKeyState(VK_MENU)) & 0x8000)
        now = time.monotonic()
        key_pressed = bool(int(user32.GetAsyncKeyState(ord("J"))) & 0x8000)
        pressed = alt_pressed and key_pressed
        if (
            pressed
            and not was_pressed
            and now - last_toggle_at >= HOTKEY_FALLBACK_DEBOUNCE_SECONDS
        ):
            controller.request_queue.put("toggle_mode")
            last_toggle_at = now
        was_pressed = pressed
        if controller.stop_requested.wait(HOTKEY_FALLBACK_POLL_SECONDS):
            break


def _start_hotkey_thread(request_queue: "queue.Queue[str]") -> HotkeyController:
    """用独立消息循环接收 Alt+J，避免 Tk WndProc 吃掉 WM_HOTKEY。"""

    controller = HotkeyController(request_queue)

    def hotkey_loop() -> None:
        user32 = ctypes.windll.user32
        controller.thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        registered_j = bool(user32.RegisterHotKey(None, HOTKEY_MODE_ID, MOD_ALT, ord("J")))
        if not registered_j:
            controller.mode = "poll"
            logger.warning("注册 Alt+J 全局热键失败，降级为按键轮询。")
            controller.ready.set()
            _poll_mode_hotkey(controller, user32)
            return

        controller.mode = "registered"
        controller.ready.set()
        try:
            while True:
                result = int(user32.GetMessageW(ctypes.byref(msg), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    logger.warning("读取 Alt+J 热键消息失败。")
                    break
                if int(msg.message) == WM_HOTKEY and int(msg.wParam) == HOTKEY_MODE_ID:
                    request_queue.put("toggle_mode")
        finally:
            try:
                user32.UnregisterHotKey(None, HOTKEY_MODE_ID)
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

"""Overlay host host_platform 职责模块。"""
# ruff: noqa: F403, F405

from hextech.interfaces.overlay.host_common import *

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



__all__ = [name for name in globals() if not name.startswith("__")]

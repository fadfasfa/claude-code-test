"""游戏内 overlay host。

本模块只负责透明置顶窗口、点击穿透、热键、游戏窗口跟随和本地事件渲染。
真实识别由 Vision sidecar 写入本地事件文件，host 不截图、不识别、不访问远端。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import queue
import threading
import tkinter as tk
from ctypes import wintypes
from typing import Any

from processing.overlay_event_channel import read_overlay_event


logger = logging.getLogger(__name__)

GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HOTKEY_ID = 0x4848
MOD_ALT = 0x0001
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


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
        self.thread: threading.Thread | None = None


def build_overlay_window_config() -> dict[str, Any]:
    """返回 overlay 的可验收窗口能力配置。"""

    return {
        "title": "Hextech Game Overlay",
        "width": 900,
        "height": 150,
        "topmost": True,
        "click_through": True,
        "hotkey": "Alt+H",
        "alpha": 0.9,
        "follow_window_titles": ["League of Legends (TM) Client"],
        "follow_poll_ms": 500,
        "top_offset": 132,
        "event_poll_ms": 250,
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


def _apply_overlay_window_styles(root: tk.Tk, *, click_through: bool) -> None:
    hwnd = _root_hwnd(root)
    user32 = ctypes.windll.user32
    current_style = _get_window_exstyle(hwnd)
    next_style = current_style | WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
    if click_through:
        next_style |= WS_EX_TRANSPARENT
    _set_window_exstyle(hwnd, next_style)
    if click_through and not (_get_window_exstyle(hwnd) & WS_EX_TRANSPARENT):
        # Tk/overrideredirect 在窗口初始化早期偶尔会覆盖扩展样式；读回失败时立即重试一次。
        _set_window_exstyle(hwnd, next_style)
    user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def _start_hotkey_thread(request_queue: "queue.Queue[str]") -> HotkeyController:
    """用独立消息循环接收 Alt+H，避免 Tk WndProc 吃掉 WM_HOTKEY。"""

    controller = HotkeyController(request_queue)

    def hotkey_loop() -> None:
        user32 = ctypes.windll.user32
        controller.thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        msg = MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_ALT, ord("H")):
            logger.warning("注册 Alt+H 全局热键失败。")
            controller.ready.set()
            return

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
    thread_id = int(controller.thread_id or 0)
    if thread_id:
        try:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        except Exception:
            logger.debug("发送 overlay 热键线程退出消息失败。", exc_info=True)
    controller.thread.join(timeout=1.0)


def _find_target_game_window(window_titles: list[str]) -> tuple[int, tuple[int, int, int, int]] | None:
    user32 = ctypes.windll.user32
    for title in window_titles:
        hwnd = user32.FindWindowW(None, str(title))
        if not hwnd or not user32.IsWindowVisible(hwnd):
            continue
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            continue
        return int(hwnd), (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    return None


def _find_target_game_rect(window_titles: list[str]) -> tuple[int, int, int, int] | None:
    target = _find_target_game_window(window_titles)
    return target[1] if target is not None else None


def _is_game_window_foreground(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    foreground = int(user32.GetForegroundWindow())
    if not foreground:
        return False
    foreground_root = int(user32.GetAncestor(foreground, GA_ROOT)) or foreground
    return foreground_root == int(hwnd)


def _target_overlay_geometry(rect: tuple[int, int, int, int], config: dict[str, Any]) -> str:
    left, top, right, bottom = rect
    game_width = max(1, right - left)
    overlay_width = int(config["width"])
    overlay_height = int(config["height"])
    x = left + max(0, (game_width - overlay_width) // 2)
    y = top + max(96, min(int(config.get("top_offset", 132)), (bottom - top) // 7))
    return f"{overlay_width}x{overlay_height}+{x}+{y}"


def _short_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _snapshot_status(snapshot: dict[str, Any], *, diagnostic_mode: bool) -> tuple[str, str]:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
    reason = _short_text(source.get("reason") or snapshot.get("error") or "", 32)
    if snapshot.get("visible"):
        selection_label = snapshot.get("selection_label") or "选择事件"
        label = f"{selection_label}: {source.get('tag', 'local')}"
        if diagnostic_mode and reason:
            label = f"{label} / {reason}"
        return label, "#a6e3a1"
    if diagnostic_mode:
        return f"诊断: {reason or 'inactive'}", "#f9e2af"
    if snapshot.get("ok"):
        return "无选择事件：隐藏", "#a6adc8"
    error = str(snapshot.get("error") or "event_missing")
    return ("等待本地数据通道事件" if error == "event_missing" else f"事件状态: {error}"), "#f9e2af"


def _draw_overlay_snapshot(canvas: tk.Canvas, config: dict[str, Any], snapshot: dict[str, Any]) -> None:
    width = int(config["width"])
    height = int(config["height"])
    canvas.delete("all")
    canvas.create_rectangle(2, 2, width - 3, height - 3, outline="#f5d076", width=2)
    canvas.create_text(
        20,
        20,
        text="Hextech 数据通道",
        fill="#f5d076",
        font=("Microsoft YaHei", 13, "bold"),
        anchor="w",
    )

    status_text, status_color = _snapshot_status(
        snapshot,
        diagnostic_mode=bool(config.get("diagnostic_mode")),
    )
    canvas.create_text(
        width - 20,
        20,
        text=status_text,
        fill=status_color,
        font=("Microsoft YaHei", 9),
        anchor="e",
    )

    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    slot_width = (width - 52) // 3
    for index in range(3):
        slot = slots[index] if index < len(slots) and isinstance(slots[index], dict) else {}
        x0 = 16 + index * (slot_width + 10)
        y0 = 44
        x1 = x0 + slot_width
        y1 = height - 14
        ready = bool(slot.get("name"))
        outline = "#89dceb" if ready else "#6c7086"
        fill = "#13222b" if ready else "#161923"
        canvas.create_rectangle(x0, y0, x1, y1, outline=outline, fill=fill, width=1)
        title = _short_text(slot.get("name") or f"槽位 {index + 1}", 18)
        tier = _short_text(slot.get("tier") or "待输入", 10)
        summary = _short_text(slot.get("summary") or "等待本地数据通道事件", 34)
        canvas.create_text(x0 + 12, y0 + 18, text=title, fill="#f3f0dd", font=("Microsoft YaHei", 10, "bold"), anchor="w")
        canvas.create_text(x1 - 10, y0 + 18, text=tier, fill="#f5d076", font=("Microsoft YaHei", 8), anchor="e")
        canvas.create_text(x0 + 12, y0 + 48, text=summary, fill="#cdd6f4", font=("Microsoft YaHei", 8), anchor="w")


def _show_overlay_window(root: tk.Tk, config: dict[str, Any]) -> None:
    root.deiconify()
    root.attributes("-topmost", True)
    _apply_overlay_window_styles(root, click_through=bool(config["click_through"]))


def _should_show_overlay(*, user_enabled: bool, event_visible: bool, game_foreground: bool) -> bool:
    """窗口显示必须同时满足用户开关、active 选择事件和游戏窗口前台三道门。"""

    return bool(user_enabled and event_visible and game_foreground)


def _sync_event_visibility(root: tk.Tk, config: dict[str, Any], visibility: dict[str, bool], snapshot: dict[str, Any]) -> None:
    event_visible = bool(snapshot.get("visible"))
    game_foreground = _is_game_window_foreground(visibility.get("target_hwnd"))
    user_enabled = bool(visibility.get("user_enabled"))
    if bool(config.get("diagnostic_mode")):
        should_show = user_enabled
    else:
        should_show = _should_show_overlay(
            user_enabled=user_enabled,
            event_visible=event_visible,
            game_foreground=game_foreground,
        )
    visibility["event_visible"] = event_visible
    visibility["game_foreground"] = game_foreground
    if visibility.get("window_visible") is should_show:
        return
    if should_show:
        _show_overlay_window(root, config)
    else:
        root.withdraw()
    visibility["window_visible"] = should_show


def _drain_hotkey_requests(request_queue: "queue.Queue[str]", visibility: dict[str, bool]) -> None:
    while True:
        try:
            request = request_queue.get_nowait()
        except queue.Empty:
            return
        if request == "toggle":
            visibility["user_enabled"] = not bool(visibility.get("user_enabled"))


def _schedule_event_render(
    root: tk.Tk,
    canvas: tk.Canvas,
    config: dict[str, Any],
    visibility: dict[str, bool],
    hotkey_queue: "queue.Queue[str]",
) -> None:
    """轮询本地事件文件并重绘三槽位；不触发识别或远端访问。"""

    poll_ms = max(100, int(config.get("event_poll_ms", 250) or 250))

    def render_once() -> None:
        _drain_hotkey_requests(hotkey_queue, visibility)
        snapshot = read_overlay_event()
        _draw_overlay_snapshot(canvas, config, snapshot)
        _sync_event_visibility(root, config, visibility, snapshot)
        canvas.after(poll_ms, render_once)

    render_once()


def _schedule_window_follow(root: tk.Tk, config: dict[str, Any], visibility: dict[str, bool]) -> None:
    """按目标游戏窗口位置移动 overlay；只读窗口矩形，不介入游戏进程。"""

    state = {"last_geometry": ""}
    titles = [str(title) for title in config.get("follow_window_titles", [])]
    poll_ms = max(250, int(config.get("follow_poll_ms", 500) or 500))

    def follow_once() -> None:
        target = _find_target_game_window(titles)
        if target is None:
            visibility["target_hwnd"] = None
            visibility["target_rect"] = None
        else:
            hwnd, rect = target
            visibility["target_hwnd"] = hwnd
            visibility["target_rect"] = rect
            next_geometry = _target_overlay_geometry(rect, config)
            if next_geometry != state["last_geometry"]:
                root.geometry(next_geometry)
                _apply_overlay_window_styles(root, click_through=bool(config["click_through"]))
                state["last_geometry"] = next_geometry
        root.after(poll_ms, follow_once)

    root.after(poll_ms, follow_once)


def run_overlay_host(*, diagnostic: bool = False) -> None:
    """启动 overlay 基础窗口，供阶段 0 人工验证。"""

    _set_dpi_awareness()
    config = build_overlay_window_config()
    config["diagnostic_mode"] = bool(diagnostic)
    root = tk.Tk()
    root.withdraw()
    root.title(config["title"])
    root.geometry(f"{config['width']}x{config['height']}+240+132")
    root.configure(bg="#10141f")
    root.attributes("-alpha", config["alpha"])
    root.attributes("-topmost", config["topmost"])
    root.overrideredirect(True)

    canvas = tk.Canvas(
        root,
        width=int(config["width"]),
        height=int(config["height"]),
        bg="#10141f",
        highlightthickness=0,
        bd=0,
    )
    canvas.pack(fill=tk.BOTH, expand=True)
    _draw_overlay_snapshot(canvas, config, read_overlay_event())

    visibility: dict[str, Any] = {
        "user_enabled": True,
        "event_visible": False,
        "game_foreground": False,
        "window_visible": False,
        "target_hwnd": None,
        "target_rect": None,
    }
    hotkey_queue: "queue.Queue[str]" = queue.Queue()
    hotkey_controller: HotkeyController | None = None

    root.update_idletasks()
    _apply_overlay_window_styles(root, click_through=bool(config["click_through"]))
    hotkey_controller = _start_hotkey_thread(hotkey_queue)
    _schedule_event_render(root, canvas, config, visibility, hotkey_queue)
    _schedule_window_follow(root, config, visibility)

    try:
        root.mainloop()
    finally:
        _stop_hotkey_thread(hotkey_controller)


def run_self_check() -> dict[str, Any]:
    """无 GUI 自检：验证冻结态入口、配置和事件读取链路可用。"""

    config = build_overlay_window_config()
    snapshot = read_overlay_event()
    return {
        "ok": True,
        "title": config["title"],
        "event_poll_ms": config["event_poll_ms"],
        "event_ok": bool(snapshot.get("ok")),
        "event_visible": bool(snapshot.get("visible")),
        "event_error": str(snapshot.get("error") or ""),
        "event_reason": str((snapshot.get("source") or {}).get("reason") or "") if isinstance(snapshot.get("source"), dict) else "",
        "schema_version": snapshot.get("schema_version"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech game overlay host。")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic", action="store_true", help="显式显示诊断面板，不改变正式 overlay 隐藏语义。")
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

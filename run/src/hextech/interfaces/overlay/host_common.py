# ruff: noqa: F401
"""独立游戏内 overlay 的 Tk/Win32 窗口宿主。

本模块只负责透明置顶窗口、点击穿透、热键、游戏窗口跟随和本地事件渲染。
真实识别由 Vision sidecar 写入本地事件文件，host 不截图、不识别、不访问远端。

调用方: hextech_ui、tests.test_overlay_host_visibility、collect_runtime_diagnostics; 关键依赖: support.python_runtime、overlay.window、overlay.window_titles。
"""

from __future__ import annotations

from hextech.modules.session.python_environment import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source(module_name="hextech.interfaces.overlay.host")

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

from hextech.modules.vision.window import (
    find_lol_game_window,
    is_scoreboard_key_down,
    is_window_foreground,
    is_window_renderable,
)
from hextech.modules.vision.window_titles import LOL_GAME_WINDOW_TITLE
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.interfaces.overlay.session_adapter import build_runtime_session
from hextech.contracts import GameSessionState, PresentationMode, VisionSlotState
from hextech.modules.session.evidence import build_evidence_bundle, write_evidence_bundle

from hextech.modules.data.overlay_source import OverlayDataSource, SharedOverlayDataSource, prepare_shared_overlay_data
from .gameflow import probe_gameflow_in_progress
from .renderer import build_render_model, build_render_model_from_session, draw_overlay_frame
from hextech.modules.vision.runtime_paths import overlay_runtime_state_path


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
                target = find_lol_game_window(self._window_titles)
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



__all__ = [name for name in globals() if not name.startswith("__")]

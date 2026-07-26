"""Overlay Host 的显式兼容聚合面。

生产入口只使用 ``main``；其余下划线符号为现有诊断和测试保留，禁止再次使用
动态 ``import *`` 扩散实现细节。
"""
# ruff: noqa: F401

from __future__ import annotations

import ctypes
import time

from hextech.interfaces.overlay.host_common import (
    EVENT_SYSTEM_FOREGROUND,
    OVERLAY_READY_FILE_ENV,
    OVERLAY_READY_TOKEN_ENV,
    RENDER_ERROR_BACKOFF_AFTER,
    SWP_FRAMECHANGED,
    SWP_NOACTIVATE,
    WS_EX_LAYERED,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    HotkeyController,
    WindowTargetPoller,
)
from hextech.interfaces.overlay.host_platform import (
    _apply_overlay_rect,
    _apply_overlay_window_styles,
    _find_target_game_window,
    _is_game_window_foreground,
    _poll_mode_hotkey,
    _register_foreground_event_hook,
    _schedule_foreground_event_drain,
    _start_hotkey_thread,
    _stop_hotkey_thread,
    build_overlay_window_config,
)
from hextech.interfaces.overlay.host_runner import (
    build_parser,
    main,
    render_acceptance_screenshot,
    resolve_event_render_retry_delay_ms,
    run_overlay_host,
    run_self_check,
    _schedule_event_render,
)
from hextech.interfaces.overlay.host_sync import (
    _drain_hotkey_requests,
    _refresh_target_window,
    _sync_event_visibility,
)
from hextech.interfaces.overlay.host_visibility import (
    _extract_event_status,
    _log_visibility_diagnostic,
    _query_gameflow_in_progress,
    _refresh_gameflow_in_progress,
    _schedule_exit_file_watch,
    _show_overlay_window,
    _signal_overlay_ready,
    _target_overlay_geometry,
    _write_host_visibility_status,
    decide_visibility,
    resolve_overlay_visibility,
)
from hextech.interfaces.overlay.host_visibility import logger
from hextech.modules.vision.window import is_scoreboard_key_down

__all__ = [
    "build_parser",
    "main",
    "render_acceptance_screenshot",
    "resolve_event_render_retry_delay_ms",
    "run_overlay_host",
    "run_self_check",
]

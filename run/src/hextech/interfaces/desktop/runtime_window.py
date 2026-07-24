"""桌面 UI 运行时辅助层。

文件职责：
- 承载桌面端后台线程、窗口联动和资源加载等非纯界面逻辑

核心输入：
- `HextechUI` 主类持有的状态、控件和会话对象
- Web live_state、LCU 本地接口和本地图片资源

核心输出：
- 桌面端后台刷新、英雄联动、图片缓存和窗口状态同步

主要依赖：
- `hextech.modules.data.ports.paths`

维护提醒：
- Tk 组件结构仍应留在 `hextech.interfaces.desktop.app`
- 新增后台线程、轮询或资源下载逻辑优先集中在本文件

调用方: dev_checks; 关键依赖: psutil、requests、display.web。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

import psutil
import requests
import urllib3
import win32gui
from PIL import Image, ImageDraw, ImageTk

from hextech.modules.game_context.client import ClientContextProvider, parse_client_context
from hextech.interfaces.overlay import context as overlay_context
from hextech.interfaces.overlay.gameflow import probe_lcu_gameflow_in_progress, probe_live_client_in_progress
from hextech.modules.vision.window import find_lol_game_window, is_window_renderable
from hextech.modules.vision.window_titles import LOL_CLIENT_WINDOW_TITLE
from hextech.modules.data.ports.paths import BASE_DIR, CHAMPION_ASSET_DIR, var_path
from hextech.modules.vision.image_validation import is_valid_png_bytes

if TYPE_CHECKING:
    from hextech.interfaces.desktop.app import HextechUI


logger = logging.getLogger(__name__)
_preload_status_executor: ThreadPoolExecutor | None = None
GAMEFLOW_VISIBILITY_POLL_SECONDS = 1.0
LCU_LOCAL_REQUEST_TIMEOUT_SECONDS = 1.0



# ruff: noqa: E402, F401, F403, F405
from hextech.interfaces.desktop.runtime_processes import (
    resolve_client_overlay_policy,
)
from hextech.interfaces.desktop.runtime_services import _web_frontend_available
from hextech.interfaces.desktop.runtime_interaction import (
    _drain_preload_pending,
    _fallback_live_state,
    _fetch_web_live_state,
    _sync_candidate_ids,
)

def lcu_polling_loop(ui: "HextechUI") -> None:
    """优先读取 Web live_state，失败时回退本地 LCU，持续同步可用英雄集合。"""
    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            time.sleep(1)
            continue

        available_ids = None
        payload = None
        if _web_frontend_available(ui):
            try:
                available_ids, payload = _fetch_web_live_state(ui)
            except Exception:
                available_ids = None
                payload = None

        if available_ids is None:
            available_ids = _fallback_live_state(ui)
            payload = None
            source = "lcu"
        else:
            source = "web"

        if available_ids is None:
            available_ids = set()

        _sync_candidate_ids(ui, available_ids, source=source, payload=payload)
        _drain_preload_pending(ui)
        time.sleep(1.5)


def _apply_rounded_corner(img: "Image.Image", radius: int = 8) -> "Image.Image":
    """为头像套一层圆角 alpha 遮罩，让头像在卡片背景上呈现柔和圆角。"""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0], img.size[1]), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def _write_champion_icon_cache(path: str, data: bytes) -> None:
    if not is_valid_png_bytes(data):
        raise ValueError("champion icon response is not a valid PNG")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_and_set_img(ui: "HextechUI", champ_id, label) -> None:
    """按运行缓存、只读 seed 顺序加载头像；远端结果只写 ``var``。"""
    try:
        if not label.winfo_exists():
            return

        def _publish_cached(photo) -> None:
            if label.winfo_exists():
                label.config(image=photo)

        if champ_id in ui.image_cache:
            cached_photo = ui.image_cache[champ_id]
            ui._run_on_ui_thread(lambda p=cached_photo: _publish_cached(p))
            return

        filename = f"{champ_id}.png"
        cache_path = var_path("cache", "assets", "champions", filename)
        seed_path = Path(CHAMPION_ASSET_DIR) / filename
        readable_path = next((path for path in (cache_path, seed_path) if path.is_file()), None)
        if readable_path is not None:
            with Image.open(readable_path) as raw_img:
                img = raw_img.resize((48, 48), Image.Resampling.LANCZOS)
        else:
            if champ_id in ui.downloading_imgs:
                return
            ui.downloading_imgs.add(champ_id)
            url = f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-icons/{champ_id}.png"
            try:
                res = ui.session.get(url, verify=True, timeout=10)
                if res.status_code != 200:
                    return
                with ui.img_write_lock:
                    _write_champion_icon_cache(os.fspath(cache_path), res.content)
                with Image.open(BytesIO(res.content)) as raw_img:
                    img = raw_img.resize((48, 48), Image.Resampling.LANCZOS)
            finally:
                ui.downloading_imgs.discard(champ_id)

        # 头像渲染前套圆角遮罩，削弱方框直角的生硬感
        img = _apply_rounded_corner(img, radius=8)
        safe_img = img.copy()

        def _publish_loaded(image_obj=safe_img) -> None:
            photo = ImageTk.PhotoImage(image_obj)
            ui.image_cache[champ_id] = photo
            if label.winfo_exists():
                label.config(image=photo)

        ui._run_on_ui_thread(_publish_loaded)
    except Exception:
        logger.exception("加载英雄头像失败：champ_id=%s", champ_id)


def window_sync_loop(ui: "HextechUI") -> None:
    """根据客户端和游戏窗口状态控制伴生窗口显隐、置顶与持续跟随。"""
    manual_follow_cooldown = 8.0
    hide_grace_seconds = 1.0
    follow_resume_distance = 32
    last_visible_at = 0.0
    last_client_interaction_at = 0.0
    last_client_hwnd = None
    last_gameflow_checked_at = 0.0
    cached_gameflow_in_progress = False
    cached_live_client_in_progress = False

    def _is_stale_window_handle_error(exc: BaseException) -> bool:
        if getattr(exc, "winerror", None) == 1400:
            return True
        return bool(getattr(exc, "args", ()) and exc.args[0] == 1400)

    def _foreground_belongs_to_client(hwnd: int | None, foreground_hwnd: int | None) -> bool:
        if not hwnd or not foreground_hwnd:
            return False
        if foreground_hwnd == hwnd:
            return True
        try:
            return win32gui.IsChild(hwnd, foreground_hwnd)
        except Exception:
            return False

    def _has_recent_client_context(now_ts: float) -> bool:
        return (now_ts - last_client_interaction_at) < hide_grace_seconds

    def _target_overlay_position(hwnd_client: int, client_rect: tuple[int, int, int, int]) -> tuple[int, int]:
        try:
            client_area = win32gui.GetClientRect(hwnd_client)
            target_x, target_y = win32gui.ClientToScreen(hwnd_client, (client_area[2], 0))
            return (int(target_x), int(target_y))
        except Exception:
            logger.debug("计算客户端内容区右侧坐标失败，回退到窗口外框。", exc_info=True)
            return (int(client_rect[2]), int(client_rect[1]))

    def _client_rect_jump_detected(current_rect: tuple[int, int, int, int], previous_rect: tuple[int, int, int, int] | None) -> bool:
        if not previous_rect:
            return False
        return (
            abs(current_rect[0] - previous_rect[0]) > follow_resume_distance
            or abs(current_rect[1] - previous_rect[1]) > follow_resume_distance
            or abs(current_rect[2] - previous_rect[2]) > follow_resume_distance
            or abs(current_rect[3] - previous_rect[3]) > follow_resume_distance
        )

    def _update_overlay_position(target_pos: tuple[int, int]) -> None:
        ui._run_on_ui_thread(lambda pos=target_pos: ui._move_overlay_to(pos[0], pos[1]))

    def _should_keep_overlay_visible(client_active: bool, overlay_active: bool, now_ts: float) -> bool:
        return client_active or overlay_active or _has_recent_client_context(now_ts)

    def _set_client_interaction(now_ts: float, hwnd: int | None) -> None:
        nonlocal last_client_interaction_at, last_client_hwnd
        last_client_interaction_at = now_ts
        last_client_hwnd = hwnd

    def _reset_client_tracking() -> None:
        nonlocal last_client_hwnd
        last_client_hwnd = None
        ui._last_client_rect = None
        # LCU 客户端消失或切走时重置"首次吸附完成"标志，
        # 下次再次出现时仍能立即吸附而不必等客户端窗口实际位移
        ui._overlay_position_initialized = False

    def _resume_follow_if_ready(client_rect: tuple[int, int, int, int], target_pos: tuple[int, int]) -> None:
        client_jump_detected = _client_rect_jump_detected(client_rect, ui._last_client_rect)
        if client_jump_detected and ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
            ui._resume_auto_follow()
        if ui._auto_follow_enabled:
            _update_overlay_position(target_pos)
        elif ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
            ui._resume_auto_follow()
            _update_overlay_position(target_pos)

    def _sync_overlay_follow(hwnd_client: int, client_rect: tuple[int, int, int, int], should_show_overlay: bool) -> None:
        if not should_show_overlay:
            ui._last_client_rect = client_rect
            return
        rect_changed = client_rect != ui._last_client_rect
        # 首次显示时即使客户端窗口没动也强制吸附一次，避免玩家"挪一下客户端才跟随"的体感
        first_show = not ui._overlay_position_initialized
        target_pos = _target_overlay_position(hwnd_client, client_rect)
        if rect_changed or first_show:
            _resume_follow_if_ready(client_rect, target_pos)
            if ui._auto_follow_enabled:
                ui._overlay_position_initialized = True
        ui._last_client_rect = client_rect

    def _set_overlay_visibility(should_show_overlay: bool, should_keep_topmost: bool, now_ts: float) -> None:
        if should_show_overlay:
            nonlocal last_visible_at
            last_visible_at = now_ts
            ui._show_overlay(topmost=should_keep_topmost)
            return
        if ui._window_visible and (now_ts - last_visible_at) < hide_grace_seconds:
            ui._set_window_topmost(False)
            return
        ui._hide_overlay()

    def _update_client_visibility(now_ts: float, hwnd_client: int | None, client_visible: bool, client_active: bool) -> None:
        if client_visible and client_active:
            _set_client_interaction(now_ts, hwnd_client)
        elif not client_visible:
            _reset_client_tracking()

    def _is_same_client_window(hwnd: int | None) -> bool:
        return bool(hwnd and last_client_hwnd and hwnd == last_client_hwnd)

    def _sync_for_client(hwnd_client: int | None, client_visible: bool, should_show_overlay: bool) -> None:
        if not client_visible or not hwnd_client:
            _reset_client_tracking()
            if ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
                ui._resume_auto_follow()
            return
        try:
            client_rect = win32gui.GetWindowRect(hwnd_client)
        except Exception as exc:
            if not _is_stale_window_handle_error(exc):
                raise
            logger.debug("客户端窗口句柄已失效，暂停伴生窗跟随并等待下一轮重扫。")
            _reset_client_tracking()
            ui._hide_overlay()
            return
        _sync_overlay_follow(hwnd_client, client_rect, should_show_overlay)

    def _client_active(is_client_fg: bool) -> bool:
        return is_client_fg

    def _client_or_overlay_active(is_client_fg: bool, is_self_fg_value: bool) -> tuple[bool, bool]:
        client_active = _client_active(is_client_fg)
        overlay_active = ui._window_visible and is_self_fg_value
        return client_active, overlay_active

    def _resolve_client_visibility(hwnd_client: int | None) -> bool:
        if not hwnd_client:
            return False
        try:
            return bool(win32gui.IsWindowVisible(hwnd_client) and not win32gui.IsIconic(hwnd_client))
        except Exception as exc:
            if not _is_stale_window_handle_error(exc):
                raise
            logger.debug("客户端窗口句柄已失效，按不可见处理并等待下一轮重扫。")
            _reset_client_tracking()
            return False

    def _resolve_game_visibility(hwnd_game: int | None) -> bool:
        return is_window_renderable(hwnd_game)

    def _resolve_gameflow_visibility(now_ts: float) -> tuple[bool, bool]:
        nonlocal last_gameflow_checked_at, cached_gameflow_in_progress, cached_live_client_in_progress
        if now_ts - last_gameflow_checked_at < GAMEFLOW_VISIBILITY_POLL_SECONDS:
            return cached_gameflow_in_progress, cached_live_client_in_progress
        last_gameflow_checked_at = now_ts
        try:
            live_state = probe_live_client_in_progress()
        except Exception:
            logger.debug("检查 Live Client 对局状态失败。", exc_info=True)
            live_state = None
        live_in_progress = live_state is True
        gameflow_in_progress = live_in_progress
        if not gameflow_in_progress:
            try:
                gameflow_state = probe_lcu_gameflow_in_progress()
            except Exception:
                logger.debug("检查 LCU gameflow 状态失败。", exc_info=True)
                gameflow_state = None
            gameflow_in_progress = gameflow_state is True
        cached_gameflow_in_progress = bool(gameflow_in_progress)
        cached_live_client_in_progress = bool(live_in_progress)
        return cached_gameflow_in_progress, cached_live_client_in_progress

    def _resolve_foreground_title(foreground_hwnd: int | None) -> str:
        return win32gui.GetWindowText(foreground_hwnd) if foreground_hwnd else ""

    def _resolve_self_fg(foreground_title: str) -> bool:
        return "Hextech" in foreground_title

    def _resolve_client_fg(hwnd_client: int | None, foreground_hwnd: int | None) -> bool:
        return _foreground_belongs_to_client(hwnd_client, foreground_hwnd)

    def _loop_once(now_ts: float) -> None:
        hwnd_client = win32gui.FindWindow(None, LOL_CLIENT_WINDOW_TITLE)
        game_target = find_lol_game_window()
        hwnd_game = game_target[0] if game_target is not None else None
        fg_window = win32gui.GetForegroundWindow()
        fg_title = _resolve_foreground_title(fg_window)
        is_client_fg = _resolve_client_fg(hwnd_client, fg_window)
        is_self_fg = _resolve_self_fg(fg_title)
        game_hwnd_renderable = _resolve_game_visibility(hwnd_game)
        gameflow_in_progress, live_client_in_progress = _resolve_gameflow_visibility(now_ts)
        client_visible = _resolve_client_visibility(hwnd_client)
        client_active, overlay_active = _client_or_overlay_active(is_client_fg, is_self_fg)
        _update_client_visibility(now_ts, hwnd_client, client_visible, client_active)
        should_show_overlay, should_keep_topmost = resolve_client_overlay_policy(
            client_visible=client_visible,
            game_hwnd_renderable=game_hwnd_renderable,
            gameflow_in_progress=gameflow_in_progress,
            live_client_in_progress=live_client_in_progress,
            client_active=client_active,
            overlay_active=overlay_active,
            recent_client_context=_has_recent_client_context(now_ts),
        )
        _set_overlay_visibility(should_show_overlay, should_keep_topmost, now_ts)
        _sync_for_client(hwnd_client, client_visible, should_show_overlay)


    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            time.sleep(1)
            continue
        try:
            now = time.time()
            _loop_once(now)
        except Exception:
            logger.exception("窗口同步循环异常。")
        time.sleep(0.2)

__all__ = [name for name in globals() if not name.startswith("__")]

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
import pywintypes
import requests
import urllib3
import win32gui
from PIL import Image, ImageDraw, ImageTk

from hextech.modules.game_context.client import ClientContextProvider, parse_client_context
from hextech.interfaces.overlay import context as overlay_context
from hextech.interfaces.overlay.gameflow import probe_lcu_gameflow_in_progress, probe_live_client_in_progress
from hextech.modules.vision.client_window import resolve_lol_client_window
from hextech.modules.vision.window import find_lol_game_window, is_window_renderable, window_display_context
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
    """复用本地 LCU 单在途读取，先发布轻量阶段，再更新英雄列表。"""
    previous_client_hwnd = 0
    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            ui.stop_event.wait(1.0)
            continue

        controller = ui._desktop_window_presentation
        try:
            client_probe = resolve_lol_client_window(previous_hwnd=previous_client_hwnd)
            requested_hwnd = int(client_probe.hwnd if client_probe.status == "found" else 0)
            if requested_hwnd:
                previous_client_hwnd = requested_hwnd
            groups = _fallback_live_state(ui) or {}
            if ui.stop_event.is_set():
                break
            controller.publish_phase(groups, client_hwnd=requested_hwnd, observed_at=time.time())
            controller.publish_candidates(groups)
        except Exception:
            logger.exception("备战席选人状态读取失败，保留有界上下文。")
        controller.wait_for_phase_poll(ui.stop_event)


def candidate_update_loop(ui: "HextechUI") -> None:
    """列表/预热独立串行消费最新候选；HTTP与统计准备不能阻塞轻量阶段轮询。"""
    controller = ui._desktop_window_presentation
    while not ui.stop_event.is_set():
        groups = controller.take_candidates()
        if groups is None or ui.stop_event.is_set():
            continue
        try:
            _sync_candidate_ids(ui, groups, source="lcu", payload=None)
            if not ui.stop_event.is_set():
                _drain_preload_pending(ui)
        except Exception:
            logger.exception("备战席列表更新失败，下一候选继续重试。")
        # 保留既有列表/预热的1.5s负载预算；只有轻量阶段加速到250ms，避免重复预热积压。
        ui.stop_event.wait(1.5)


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


class _Win32Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _Win32MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Win32Rect),
        ("rcWork", _Win32Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def _monitor_workarea(hwnd: int) -> tuple[int, int, int, int] | None:
    """返回窗口所在显示器的工作区 (left, top, right, bottom)；失败返回 None。"""

    try:
        import win32api

        # pywin32 保留完整 HMONITOR；ctypes 默认 c_int 会截断 64 位句柄。
        monitor = win32api.MonitorFromWindow(int(hwnd), 2)
        return tuple(int(value) for value in win32api.GetMonitorInfo(monitor)["Work"])
    except Exception:
        logger.debug("读取显示器工作区失败。", exc_info=True)
        return None


def _avatar_pixel_size(ui: "HextechUI") -> int:
    """头像边长随 UI 缩放推导；缩放当前锁 1.0，即固定 48px 的基线观感。"""

    return max(1, round(48 * float(getattr(ui, "_ui_scale", 1.0))))


def _monitor_dpi(monitor) -> float:
    from ctypes import wintypes
    query = ctypes.windll.shcore.GetDpiForMonitor
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
    query.restype = ctypes.c_long
    x, y = wintypes.UINT(), wintypes.UINT()
    if query(int(monitor), 0, ctypes.byref(x), ctypes.byref(y)) != 0 or not x.value or x.value != y.value:
        raise ValueError("monitor DPI unavailable")
    return x.value / 96.0


def _desktop_monitors():
    import win32api
    from .responsive_layout import DesktopMonitor
    result = []
    try:
        displays = win32api.EnumDisplayMonitors()
    except (AttributeError, OSError, TypeError, ValueError, pywintypes.error):
        logger.debug("显示器拓扑暂不可用。", exc_info=True)
        return None
    incomplete = False
    for monitor, _dc, _rect in displays:
        try:
            info = win32api.GetMonitorInfo(monitor)
            result.append(DesktopMonitor(str(info["Device"]),tuple(info["Work"]),_monitor_dpi(monitor),tuple(info["Monitor"])))
        except (AttributeError, OSError, TypeError, ValueError, pywintypes.error):
            incomplete = True
            logger.debug("单个显示器上下文暂不可用，保留上一份完整拓扑。", exc_info=True)
    return tuple(result) if result and not incomplete else None


def _select_desktop_monitor_topology(previous, observed):
    """探针失败保留上一份；成功的较小集合代表真实断开并立即采用。"""
    return previous if observed is None else observed


def _client_display_context(hwnd: int) -> dict[str, object]:
    """PMv2调用方读取目标显示器有效DPI，不继承外部客户端的DPI感知模式。"""
    import win32api
    result = window_display_context(hwnd, force=True)
    try:
        monitor = win32api.MonitorFromWindow(hwnd, 2)
        result["dpi_scale"] = _monitor_dpi(monitor)
        result["dpi_source"] = "monitor_effective"
    except (AttributeError, OSError, TypeError, ValueError, pywintypes.error):
        result.update(dpi_scale=0.0, status="unavailable")
    return result


def load_and_set_img(ui: "HextechUI", champ_id, label, *, request_key=None) -> None:
    """按运行缓存、只读 seed 顺序加载头像；远端结果只写 ``var``。"""
    try:
        if getattr(ui, "_closing", False):
            return
        avatar_px = request_key[1] if request_key is not None else _avatar_pixel_size(ui)
        revision = request_key[2] if request_key is not None else getattr(ui, "_avatar_revision", 0)
        def current_request():
            return (not getattr(ui, "_closing", False) and revision == getattr(ui, "_avatar_revision", 0)
                    and (request_key is None or getattr(label, "_hextech_avatar_request_key", None) == request_key))

        def _publish_cached(photo) -> None:
            if current_request() and label.winfo_exists():
                label.config(image=photo)
                label._hextech_avatar_photo = photo
                # 标记供 keyed 增量渲染判断是否还需补载头像。
                label._hextech_avatar_loaded = True
                label._hextech_avatar_loaded_key = request_key

        filename = f"{champ_id}.png"
        cache_path = var_path("cache", "assets", "champions", filename)
        seed_path = Path(CHAMPION_ASSET_DIR) / filename
        readable_path = next((path for path in (cache_path, seed_path) if path.is_file()), None)
        if readable_path is not None:
            with Image.open(readable_path) as raw_img:
                img = raw_img.resize((avatar_px, avatar_px), Image.Resampling.LANCZOS)
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
                    img = raw_img.resize((avatar_px, avatar_px), Image.Resampling.LANCZOS)
            finally:
                ui.downloading_imgs.discard(champ_id)

        # 头像渲染前套圆角遮罩，削弱方框直角的生硬感
        img = _apply_rounded_corner(img, radius=max(1, round(avatar_px / 6)))
        safe_img = img.copy()

        def _publish_loaded(image_obj=safe_img) -> None:
            if not current_request() or not label.winfo_exists():
                return
            key = (str(champ_id), avatar_px)
            photo = ui.image_cache.get(key)
            if photo is None:
                photo = ImageTk.PhotoImage(image_obj)
                if len(ui.image_cache) >= 64:
                    ui.image_cache.clear()
                ui.image_cache[key] = photo
            _publish_cached(photo)

        ui._run_on_ui_thread(_publish_loaded)
    except Exception:
        logger.exception("加载英雄头像失败：champ_id=%s", champ_id)


def window_sync_loop(ui: "HextechUI") -> None:
    """只读窗口探测器；HTTP 独立执行，任何 Tk 操作都交给呈现 owner。"""
    from hextech.interfaces.desktop.window_presentation import DesktopWindowObservation

    controller = ui._desktop_window_presentation
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-gameflow")
    pending = None
    pending_game_hwnd = 0
    pending_client_hwnd = 0
    flow_client_hwnd = 0
    last_flow_at = 0.0
    flow_active = False
    flow_known = False
    flow_observed_at = 0.0
    previous_game_hwnd = 0
    previous_client_hwnd = 0
    monitors = ()
    last_monitors_at = 0.0

    def read_gameflow() -> tuple[bool | None, float]:
        live = probe_live_client_in_progress()
        value = True if live is True else probe_lcu_gameflow_in_progress()
        return value, time.time()

    def belongs(parent: int, foreground: int) -> bool:
        return bool(parent and foreground and (parent == foreground or win32gui.IsChild(parent, foreground)))

    try:
        while not ui.stop_event.is_set():
            poll_delay = .1
            try:
                now = time.time()
                if now - last_monitors_at >= 1.0:
                    monitors = _select_desktop_monitor_topology(monitors, _desktop_monitors())
                    last_monitors_at = now
                if ui.pause_event.is_set():
                    controller.publish_window(DesktopWindowObservation(observed_at=now))
                    time.sleep(.2)
                    continue
                client_probe = resolve_lol_client_window(previous_hwnd=previous_client_hwnd)
                client = int(client_probe.hwnd if client_probe.status == "found" else 0)
                if client:
                    previous_client_hwnd = client
                game_target = find_lol_game_window(include_nonrenderable=True)
                game_hwnd = int(game_target[0]) if game_target is not None else 0
                if game_hwnd != previous_game_hwnd:
                    flow_active = False
                    flow_known = False
                    flow_observed_at = 0.0
                    previous_game_hwnd = game_hwnd
                    last_flow_at = 0.0
                if pending is not None and pending.done():
                    if pending_game_hwnd == game_hwnd and pending_client_hwnd == client:
                        try:
                            value, flow_observed_at = pending.result()
                            flow_active = value is True
                            flow_known = value is not None
                            flow_client_hwnd = client
                        except Exception:
                            logger.debug("备战席 gameflow 探测暂不可用。", exc_info=True)
                    pending = None
                if game_hwnd:
                    # 游戏窗口即使最小化仍属于实际对局；旧 HTTP 不能覆盖此硬门。
                    flow_active = False
                elif pending is None and now - last_flow_at >= GAMEFLOW_VISIBILITY_POLL_SECONDS:
                    last_flow_at = now
                    pending_game_hwnd = game_hwnd
                    pending_client_hwnd = client
                    pending = executor.submit(read_gameflow)
                foreground = int(win32gui.GetForegroundWindow() or 0)
                poll_delay = .05 if belongs(client, foreground) else .1
                visible = bool(client and win32gui.IsWindowVisible(client) and not win32gui.IsIconic(client))
                rect = client_probe.client_rect if client_probe.status == "found" else None
                area = None
                display = {}
                if visible:
                    area = _monitor_workarea(client)
                    display = _client_display_context(client)
                controller.publish_window(DesktopWindowObservation(
                    client_hwnd=client, foreground_hwnd=foreground, client_rect=rect, workarea=area, client_visible=visible,
                    client_active=belongs(client, foreground),
                    overlay_active=belongs(int(getattr(ui, "_desktop_hwnd", 0)), foreground),
                    game_visible=bool(game_hwnd), gameflow_in_progress=flow_active, observed_at=now,
                    gameflow_known=flow_known, gameflow_observed_at=flow_observed_at,
                    gameflow_client_hwnd=flow_client_hwnd,
                    dpi_scale=float(display.get("dpi_scale") or 0),
                    monitor_device=str(display.get("monitor_device") or ""),
                    monitors=monitors,
                    client_probe_status=client_probe.status, client_probe_reason=client_probe.reason,
                    client_process_id=client_probe.process_id, client_candidate_count=client_probe.candidate_count,
                ))
            except Exception as exc:
                # 失效句柄/权限等只发布不可见观察，不保留可能已失效的旧定位回调。
                controller.publish_window(DesktopWindowObservation(observed_at=time.time()))
                if getattr(exc, "winerror", 0) != 1400 and not (exc.args and exc.args[0] == 1400):
                    logger.exception("窗口同步循环异常。")
            ui.stop_event.wait(poll_delay)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

__all__ = [name for name in globals() if not name.startswith("__")]

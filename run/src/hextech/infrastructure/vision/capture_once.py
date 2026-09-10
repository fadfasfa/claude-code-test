"""显式单次诊断辅助；无 Sidecar、OCR、tracker、运行态写入或桌面捕获回退。"""
from __future__ import annotations

import ctypes
import hashlib
import io
import math
import time
from ctypes import wintypes
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import psutil
from PIL import Image

from hextech.modules.session.build_identity import get_build_identity
from hextech.modules.vision import window
from hextech.modules.vision.game_window_mode import read_game_window_mode
from hextech.modules.vision.layout import detect_selection_scene

MAX_PIXELS = 16_000_000
MAX_PNG_BYTES = 24 * 1024 * 1024
MAX_OBSERVATION_AGE = 0.5


class CaptureRejected(RuntimeError):
    """有限原因码；不得将进程路径、异常原文或其它应用内容写入诊断。"""


def probe_foreground_game() -> dict[str, Any]:
    """只检查前台 HWND，不枚举其它窗口、不读标题、不接受标题 fallback。"""
    hwnd = window.foreground_root_hwnd()
    if not hwnd or window._window_process_name(hwnd) not in window.LOL_GAME_PROCESS_NAMES:
        raise CaptureRejected("verified_foreground_game_missing")
    if not window.is_window_renderable(hwnd):
        raise CaptureRejected("game_not_renderable")
    identity = window.game_window_identity(hwnd)
    if identity["identity_quality"] != "process":
        raise CaptureRejected("game_process_identity_unverified")
    process = psutil.Process(int(identity["process_id"]))
    executable = process.exe()
    if (Path(executable).name.casefold() not in window.LOL_GAME_PROCESS_NAMES
            or process.create_time() != identity["process_started_at"]):
        raise CaptureRejected("game_process_identity_changed")
    rect = window._window_client_rect(hwnd, allow_window_fallback=False)
    if rect is None:
        raise CaptureRejected("game_client_rect_unavailable")
    mode = read_game_window_mode(executable, force=True)
    if not mode.supported:
        raise CaptureRejected(mode.reason or "game_window_mode_unknown")
    display = window.window_display_context(hwnd, force=True)
    get_dpi = ctypes.windll.user32.GetDpiForWindow
    get_dpi.argtypes = [wintypes.HWND]
    get_dpi.restype = wintypes.UINT
    dpi = int(get_dpi(hwnd))
    if dpi <= 0 or display.get("status") != "available":
        raise CaptureRejected("game_dpi_unavailable")
    foreground = window.foreground_root_hwnd()
    return {
        "hwnd": hwnd, "foreground_hwnd": foreground, "client_rect": list(rect),
        "process_id": identity["process_id"], "process_started_at": identity["process_started_at"],
        "game_instance_id": identity["game_instance_id"], "identity_quality": identity["identity_quality"],
        "process_name": "League of Legends.exe", "dpi": dpi, "display": display,
        "window_mode": mode.to_status(), "observed_at": time.time(),
    }


def capture_client(snapshot: dict[str, Any]) -> Image.Image:
    """一次 PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT)，绝不读取屏幕 DC 像素。

    使用 Pillow RGB 图像及既有 vision 分析路径。ImageGrab 的屏幕 bbox 可混入其它
    应用，故这里严格限定 HWND 和客户区；不支持 PrintWindow 的游戏返回失败/黑帧。
    原生调用可能阻塞，必须由 CLI 的独立进程硬截止监督。
    """
    import win32gui
    import win32ui

    hwnd = int(snapshot["hwnd"])
    left, top, right, bottom = snapshot["client_rect"]
    width, height = right - left, bottom - top
    source_handle = win32gui.GetDC(hwnd)
    if not source_handle:
        raise CaptureRejected("window_dc_unavailable")
    source = target = bitmap = None
    previous = None
    try:
        source = win32ui.CreateDCFromHandle(source_handle)
        target = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source, width, height)
        previous = target.SelectObject(bitmap)
        # 初始化为黑色；拒绝未绘制/全平帧，不让未初始化内存进入证据。
        target.PatBlt((0, 0), (width, height), 0x00000042)  # BLACKNESS
        print_window = ctypes.windll.user32.PrintWindow
        print_window.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        print_window.restype = wintypes.BOOL
        immediate = probe_foreground_game()
        validate_snapshot(immediate, time.time())
        if snapshot_key(immediate) != snapshot_key(snapshot):
            raise CaptureRejected("game_changed_before_native_capture")
        if not print_window(hwnd, target.GetSafeHdc(), 3):
            raise CaptureRejected("window_capture_failed")
        return Image.frombuffer("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        if target is not None:
            if previous is not None:
                target.SelectObject(previous)
            target.DeleteDC()
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if source is not None:
            source.DeleteDC()
        if source_handle:
            win32gui.ReleaseDC(hwnd, source_handle)


def validate_snapshot(snapshot: dict[str, Any], now: float) -> None:
    if (snapshot.get("identity_quality") != "process" or not snapshot.get("game_instance_id")
            or int(snapshot.get("process_id", 0)) <= 0
            or snapshot.get("process_name") != "League of Legends.exe"):
        raise CaptureRejected("game_process_identity_unverified")
    started = float(snapshot.get("process_started_at", 0))
    if not math.isfinite(started) or not 0 < started <= now:
        raise CaptureRejected("game_process_identity_unverified")
    if not snapshot.get("hwnd") or snapshot["hwnd"] != snapshot.get("foreground_hwnd"):
        raise CaptureRejected("game_not_foreground")
    age = now - float(snapshot.get("observed_at", 0))
    if not math.isfinite(age) or not 0 <= age <= MAX_OBSERVATION_AGE:
        raise CaptureRejected("game_observation_stale")
    mode = snapshot.get("window_mode", {})
    if mode.get("status") != "supported" or mode.get("mode") not in ("borderless", "windowed"):
        raise CaptureRejected("game_window_mode_unsupported")
    dpi = float(snapshot.get("dpi", 0))
    if not math.isfinite(dpi) or dpi <= 0:
        raise CaptureRejected("game_dpi_unavailable")
    left, top, right, bottom = snapshot["client_rect"]
    if (not all(isinstance(v, int) for v in (left, top, right, bottom))
            or right - left <= 1 or bottom - top <= 1 or (right - left) * (bottom - top) > MAX_PIXELS):
        raise CaptureRejected("client_size_budget_exceeded")


def snapshot_key(snapshot: dict[str, Any]) -> tuple[object, ...]:
    return tuple(snapshot[key] for key in (
        "hwnd", "foreground_hwnd", "client_rect", "process_id", "process_started_at",
        "game_instance_id", "identity_quality", "dpi", "display",
    )) + (snapshot["window_mode"]["mode"],)


def collect_once(*, enabled: bool, deadline: float,
                 probe: Callable = probe_foreground_game, capture: Callable = capture_client,
                 clock: Callable = time.monotonic, wall_clock: Callable = time.time,
                 build_reader: Callable = get_build_identity) -> tuple[bytes, dict[str, Any]]:
    """只有一次捕获；candidate/absent 均保留，不借诊断授权 READY。"""
    if not enabled:
        raise CaptureRejected("explicit_capture_required")

    def check_budget() -> None:
        if not math.isfinite(deadline) or clock() >= deadline:
            raise CaptureRejected("capture_budget_exceeded")

    check_budget()
    build = dict(build_reader())
    before = probe()
    validate_snapshot(before, wall_clock())
    check_budget()
    started = wall_clock()
    frame = capture(before)
    finished = wall_clock()
    try:
        after = probe()
        validate_snapshot(after, wall_clock())
        if snapshot_key(before) != snapshot_key(after):
            raise CaptureRejected("game_capture_identity_or_geometry_changed")
        check_budget()
        left, top, right, bottom = before["client_rect"]
        if frame.size != (right - left, bottom - top):
            raise CaptureRejected("capture_size_mismatch")
        original = frame
        frame = original.convert("RGB")
        original.close()
        if all(low == high for low, high in frame.getextrema()):
            raise CaptureRejected("capture_flat_or_black")
        # 只作单帧版式观察，不运行跨帧 scene/tracker，也不要求 active/READY。
        scene = detect_selection_scene(frame, layout_id="capture-once-client")
        output = io.BytesIO()
        frame.save(output, format="PNG")
        png = output.getvalue()
        if len(png) > MAX_PNG_BYTES:
            raise CaptureRejected("capture_byte_budget_exceeded")
        check_budget()
        return png, {
            "schema_version": 1, "capture_backend": "print_window_client_only", "capture_attempts": 1,
            "before": before, "after": after, "capture_started_at": started, "captured_at": finished,
            "build": build, "runtime_build_verified": False,
            "scene_observation": asdict(scene),
            "selection_confirmed": False, "automatic_exemplar_eligible": False, "requires_manual_truth": True,
            "image": {"file": "client.png", "size": list(frame.size), "bytes": len(png),
                      "sha256": hashlib.sha256(png).hexdigest()},
        }
    finally:
        frame.close()

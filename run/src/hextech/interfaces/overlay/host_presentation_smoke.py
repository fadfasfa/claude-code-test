"""冻结包 GUI 合成烟测。

该入口只在显式 packaged smoke 中创建已知底色的模拟游戏窗口，并用生产 renderer、
Tk/Win32 映射与有限像素探针验证最终合成；它不参与常驻 Host，也不保存截图。
"""

from __future__ import annotations

import time
import tkinter as tk
from typing import Any, cast

from hextech.interfaces.overlay.canvas_renderer import OverlayRenderModel
from hextech.interfaces.overlay.host_platform import (
    _root_hwnd,
    _set_dpi_awareness,
    _window_client_rect_on_screen,
    build_overlay_window_config,
)
from hextech.interfaces.overlay.host_presentation import (
    ensure_overlay_presentation,
    presentation_status,
)
from hextech.interfaces.overlay.host_render_state import present_overlay_model
from hextech.interfaces.overlay.host_visibility import _apply_transparent_background
from hextech.modules.session.build_identity import current_build_id
from hextech.modules.vision.screen_capture import MssCaptureBackend


SMOKE_WIDTH = 960
SMOKE_HEIGHT = 600
SMOKE_BACKGROUND = "#243447"
SMOKE_CAPTURE_MARKER = "#FF00FF"
SMOKE_CAPTURE_MARKER_BOX = (12, 12, 72, 72)
SMOKE_CAPTURE_POINTS = ((24, 24), (42, 42), (60, 60))
SMOKE_CAPTURE_TOLERANCE = 20


def _capture_exclusion_probe(
    actual_rect: tuple[int, int, int, int],
) -> dict[str, Any]:
    """用 Sidecar 同源 MSS 路径验证高对比探针未进入桌面捕获。"""

    left, top, _right, _bottom = actual_rect
    screen_points = [(left + x, top + y) for x, y in SMOKE_CAPTURE_POINTS]
    min_x = min(x for x, _y in screen_points)
    min_y = min(y for _x, y in screen_points)
    max_x = max(x for x, _y in screen_points) + 1
    max_y = max(y for _x, y in screen_points) + 1
    backend = MssCaptureBackend()
    try:
        captured = backend.capture_rgb((min_x, min_y, max_x, max_y))
    finally:
        backend.close()
    if captured is None:
        return {
            "state": "unavailable",
            "reason": "mss_capture_unavailable",
            "sample_count": 0,
            "matched_count": 0,
            "probe_contract": "mss_capture_exclusion_v1",
        }
    marker_rgb = (255, 0, 255)
    pixels = [captured.getpixel((x - min_x, y - min_y)) for x, y in screen_points]
    matched_count = sum(
        max(abs(actual - expected) for actual, expected in zip(pixel, marker_rgb, strict=True))
        <= SMOKE_CAPTURE_TOLERANCE
        for pixel in pixels
    )
    background_rgb = tuple(int(SMOKE_BACKGROUND[i:i+2], 16) for i in (1, 3, 5))
    background_matched_count = sum(
        max(abs(actual - expected) for actual, expected in zip(pixel, background_rgb, strict=True))
        <= SMOKE_CAPTURE_TOLERANCE for pixel in pixels
    )
    # 黑帧/错误窗口同样没有紫色marker；必须看到自有目标底色才证明捕获有效。
    excluded = matched_count == 0 and background_matched_count == len(pixels)
    return {
        "state": "excluded" if excluded else "leaked" if matched_count else "unavailable",
        "reason": "marker_excluded" if excluded else "marker_captured" if matched_count else "capture_background_mismatch",
        "sample_count": len(pixels),
        "matched_count": matched_count,
        "background_matched_count": background_matched_count,
        "tolerance": SMOKE_CAPTURE_TOLERANCE,
        "probe_contract": "mss_capture_exclusion_v1",
    }


def _ready_fixture_model() -> OverlayRenderModel:
    stats = [
        {
            "slot": slot,
            "state": "ready",
            "name": f"合成验收 {slot + 1}",
            "tier": "S" if slot == 0 else "A",
            "stats_text": f"胜率 {52.0 + slot:.1f}% · 出场 {11.0 + slot:.1f}%",
            "status_code": "READY",
            "winrate_text": f"{52.0 + slot:.1f}%",
            "pickrate_text": f"{11.0 + slot:.1f}%",
            "status_text": "",
            "synergy_status": "READY",
        }
        for slot in range(3)
    ]
    synergies = [
        {
            "slot": slot,
            "augment_name": f"合成验收 {slot + 1}",
            "tier": "S" if slot == 0 else "A",
            "hero_name": "模拟英雄",
            "rating": "S" if slot == 0 else "A",
            "tag": "packaged-smoke",
            "content": "用于验证真实 renderer、HWND 映射与屏幕像素合成。",
            "data_status": "READY",
            "status_text": "",
        }
        for slot in range(3)
    ]
    return {"stats": stats, "synergies": synergies}  # type: ignore[return-value]


def run_presentation_smoke(*, timeout_ms: int = 5000) -> dict[str, Any]:
    """在一个 Tk 事件循环中验证模拟游戏窗口上方的 Overlay 像素。"""

    _set_dpi_awareness()
    config = build_overlay_window_config()
    config["width"] = SMOKE_WIDTH
    config["height"] = SMOKE_HEIGHT
    target = tk.Tk()
    target.title("Hextech Presentation Smoke Target")
    target.overrideredirect(True)
    target.geometry(f"{SMOKE_WIDTH}x{SMOKE_HEIGHT}+80+80")
    target.configure(bg=SMOKE_BACKGROUND)
    target.attributes("-topmost", True)  # 排除上层后仍须捕获自有底色，而不是用户的其他窗口。

    overlay = tk.Toplevel(target)
    overlay.withdraw()
    overlay.title(str(config["title"]))
    overlay.overrideredirect(True)
    overlay.attributes("-alpha", config["alpha"])
    overlay.attributes("-topmost", True)
    canvas = tk.Canvas(
        overlay,
        width=SMOKE_WIDTH,
        height=SMOKE_HEIGHT,
        highlightthickness=0,
        bd=0,
    )
    _apply_transparent_background(cast(tk.Tk, overlay), canvas, config)
    canvas.pack(fill=tk.BOTH, expand=True)

    visibility: dict[str, Any] = {}
    finished = False
    timed_out = False
    last_error = ""
    canvas_item_count = 0
    draw_count = 0
    redraw_pending = False
    fullscreen_blocked_presentation: dict[str, Any] = {}
    capture_exclusion_probe: dict[str, Any] = {
        "state": "not_run",
        "reason": "presentation_pending",
    }

    def finish() -> None:
        nonlocal finished
        finished = True
        target.quit()

    def poll() -> None:
        nonlocal capture_exclusion_probe
        state = presentation_status(visibility)
        if str(state.get("state") or "") == "composed":
            actual = state.get("actual_rect")
            if isinstance(actual, list) and len(actual) == 4:
                capture_exclusion_probe = _capture_exclusion_probe(
                    tuple(int(value) for value in actual)
                )
            else:
                capture_exclusion_probe = {
                    "state": "unavailable",
                    "reason": "actual_rect_unavailable",
                }
            finish()
            return
        if str(state.get("state") or "") == "failed":
            finish()
            return
        composition = state.get("composition_probe")
        if isinstance(composition, dict) and composition.get("state") == "unavailable":
            if redraw_pending:
                target.after(10, poll)
                return
            finish()
            return
        target.after(10, poll)

    def draw_and_present() -> None:
        nonlocal canvas_item_count, draw_count, redraw_pending
        rect = cast(tuple[int, int, int, int], visibility["target_rect"])
        snapshot = {
            "source": {
                "layout_id": "packaged_presentation_smoke",
                "button_box": [0, rect[3] - rect[1] - 32, rect[2] - rect[0], rect[3] - rect[1]],
            }
        }
        present_overlay_model(
            canvas,
            config,
            visibility,
            snapshot,
            _ready_fixture_model(),
            ready_frame=True,
        )
        canvas.create_rectangle(
            *SMOKE_CAPTURE_MARKER_BOX,
            fill=SMOKE_CAPTURE_MARKER,
            outline=SMOKE_CAPTURE_MARKER,
            width=0,
        )
        canvas.update_idletasks()
        canvas_item_count = len(canvas.find_all())
        draw_count += 1
        redraw_pending = False
        ensure_overlay_presentation(cast(tk.Tk, overlay), canvas, config, visibility)

    def presentation_changed() -> None:
        nonlocal redraw_pending, fullscreen_blocked_presentation
        state = presentation_status(visibility)
        composition = state.get("composition_probe")
        if (
            not fullscreen_blocked_presentation
            and isinstance(composition, dict)
            and composition.get("reason") == "unsupported_game_window_mode"
        ):
            fullscreen_blocked_presentation = state
            visibility["game_window_mode_status"] = "supported"
            redraw_pending = True
            target.after_idle(draw_and_present)
            return
        if (
            draw_count < 2
            and isinstance(composition, dict)
            and composition.get("state") == "unavailable"
        ):
            redraw_pending = True
            target.after_idle(draw_and_present)

    def start() -> None:
        nonlocal last_error
        rect = _window_client_rect_on_screen(_root_hwnd(target))
        if rect is None or rect[2] - rect[0] < 100 or rect[3] - rect[1] < 100:
            last_error = "simulated_target_rect_unavailable"
            finish()
            return
        visibility.update(
            {
                "target_rect": rect,
                "pending_geometry": (
                    f"{rect[2] - rect[0]}x{rect[3] - rect[1]}"
                    f"{rect[0]:+d}{rect[1]:+d}"
                ),
                "window_visible": True,
                "game_window_mode_status": "unsupported",
                "presentation_state_changed": presentation_changed,
            }
        )
        draw_and_present()
        poll()

    def timeout() -> None:
        nonlocal timed_out
        if finished:
            return
        timed_out = True
        finish()

    started_at = time.monotonic()
    try:
        target.after(50, start)
        target.after(max(1000, int(timeout_ms)), timeout)
        target.mainloop()
    except Exception as exc:
        last_error = type(exc).__name__
    finally:
        try:
            overlay.destroy()
        finally:
            target.destroy()

    presentation = presentation_status(visibility)
    draw_completed_at = float(presentation.get("draw_completed_at") or 0.0)
    presented_at = float(presentation.get("presented_at") or 0.0)
    presentation_latency_ms = (
        round((presented_at - draw_completed_at) * 1000.0, 3)
        if presented_at >= draw_completed_at > 0.0
        else None
    )
    composition = presentation.get("composition_probe")
    composition_state = (
        str(composition.get("state") or "") if isinstance(composition, dict) else ""
    )
    style_checks = presentation.get("style_checks")
    capture_exclusion = presentation.get("capture_exclusion")
    host_surface_probe = presentation.get("host_surface_probe")
    checks = {
        "fullscreen_blocked": bool(fullscreen_blocked_presentation),
        "fullscreen_probe_not_run": (
            fullscreen_blocked_presentation.get("composition_probe", {}).get("reason")
            == "unsupported_game_window_mode"
            if isinstance(fullscreen_blocked_presentation.get("composition_probe"), dict)
            else False
        ),
        "fullscreen_presented_at_absent": float(
            fullscreen_blocked_presentation.get("presented_at") or 0.0
        )
        == 0.0,
        "borderless_resumed": visibility.get("game_window_mode_status") == "supported",
        "three_ready_rows": len(_ready_fixture_model()["stats"]) == 3,
        "canvas_items": canvas_item_count > 0,
        "hwnd_mapped": bool(presentation.get("overlay_hwnd"))
        and bool(presentation.get("ws_visible")),
        "rect_matches": bool(presentation.get("rect_matches")),
        "style_checks": bool(style_checks) and all(dict(style_checks).values()),
        "not_cloaked": presentation.get("cloaked") is not True,
        "capture_exclusion_applied": (
            isinstance(capture_exclusion, dict)
            and capture_exclusion.get("status") == "applied"
        ),
        "host_surface_content": (
            isinstance(host_surface_probe, dict)
            and host_surface_probe.get("state") == "matched"
        ),
        "composition_excluded": composition_state == "excluded",
        "desktop_capture_excluded": capture_exclusion_probe.get("state") == "excluded",
    }
    return {
        "ok": all(checks.values()) and presentation.get("state") == "composed" and not timed_out,
        "build_id": current_build_id(),
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "simulated_target_background": SMOKE_BACKGROUND,
        "checks": checks,
        "presentation": presentation,
        "capture_exclusion_probe": capture_exclusion_probe,
        "fullscreen_blocked_presentation": fullscreen_blocked_presentation,
        "draw_completed_to_presented_ms": presentation_latency_ms,
        "timed_out": timed_out,
        "last_error": last_error,
    }


__all__ = ["run_presentation_smoke"]

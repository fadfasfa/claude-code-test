"""Overlay HWND 映射与像素合成闭环。

本模块只证明 Host 已请求映射、窗口属性/物理矩形正确，以及有限的既有不透明
Canvas 像素是否出现在屏幕上；它不截图、不读取周边游戏内容，也不替代真实 League
中的人工可见性门。
"""

from __future__ import annotations

import logging
import time
import tkinter as tk
from collections.abc import Callable, Mapping
from typing import Any

from hextech.interfaces.overlay.host_platform import (
    _ensure_overlay_window_styles,
    _probe_overlay_hwnd,
    _sample_screen_pixels,
    _show_overlay_hwnd,
)


logger = logging.getLogger(__name__)

# after_idle 已先保证 Tk 提交 Canvas 命令。这里再跨过半帧后取屏幕像素；若
# DWM 尚未合成，既有 mismatch 重试仍会走第二个完整映射周期。
COMPOSITION_DELAY_MS = 8
COMPOSITION_MIN_SAMPLES = 3
COMPOSITION_MAX_SAMPLES = 5
COMPOSITION_COLOR_TOLERANCE = 20


def _new_presentation_state() -> dict[str, Any]:
    return {
        "state": "hidden",
        "overlay_hwnd": 0,
        "ws_visible": False,
        "iconic": False,
        "cloaked": None,
        "dwm_status": "not_queried",
        "expected_client_rect": [],
        "actual_rect": [],
        "rect_matches": False,
        "style_checks": {},
        "capture_exclusion": {"status": "unsupported", "reason": "not_initialized"},
        "host_surface_probe": {"state": "not_run", "reason": "hidden"},
        "composition_probe": {"state": "not_run", "reason": "hidden"},
        "probe_contract": "host_surface_and_capture_exclusion_v1",
        "failure_reason": "",
        "map_requested_at": 0.0,
        "mapped_at": 0.0,
        "composition_checked_at": 0.0,
        "presented_at": 0.0,
        "draw_completed_at": 0.0,
        "event_written_at": 0.0,
        "event_session_id": "",
        "event_selection_epoch": 0,
        "event_selection_revision": 0,
        "ready_frame": False,
        "_token": 0,
        "_scheduled": False,
        "_draw_sequence": 0,
        "_composition_checked_draw_sequence": 0,
        "_map_failures": 0,
        "_composition_failures": 0,
        "_expected_rect_key": (),
        "_last_failure_draw_sequence": 0,
    }


def _presentation_state(visibility: dict[str, Any]) -> dict[str, Any]:
    state = visibility.get("presentation")
    if isinstance(state, dict):
        capture_exclusion = visibility.get("capture_exclusion")
        if isinstance(capture_exclusion, Mapping):
            state["capture_exclusion"] = dict(capture_exclusion)
        return state
    state = _new_presentation_state()
    capture_exclusion = visibility.get("capture_exclusion")
    if isinstance(capture_exclusion, Mapping):
        state["capture_exclusion"] = dict(capture_exclusion)
    visibility["presentation"] = state
    return state


def presentation_status(visibility: Mapping[str, Any]) -> dict[str, Any]:
    """返回可写入 visibility/session report 的兼容公开字段。"""

    raw = visibility.get("presentation")
    state = raw if isinstance(raw, Mapping) else _new_presentation_state()
    composition = state.get("composition_probe")
    capture_exclusion = visibility.get("capture_exclusion")
    if not isinstance(capture_exclusion, Mapping):
        capture_exclusion = state.get("capture_exclusion")
    host_surface_probe = state.get("host_surface_probe")
    return {
        "state": str(state.get("state") or "hidden"),
        "overlay_hwnd": int(state.get("overlay_hwnd") or 0),
        "ws_visible": bool(state.get("ws_visible")),
        "iconic": bool(state.get("iconic")),
        "cloaked": state.get("cloaked"),
        "dwm_status": str(state.get("dwm_status") or "not_queried"),
        "expected_client_rect": list(state.get("expected_client_rect") or []),
        "actual_rect": list(state.get("actual_rect") or []),
        "rect_matches": bool(state.get("rect_matches")),
        "style_checks": dict(state.get("style_checks") or {}),
        "capture_exclusion": dict(capture_exclusion) if isinstance(capture_exclusion, Mapping) else {},
        "host_surface_probe": dict(host_surface_probe) if isinstance(host_surface_probe, Mapping) else {},
        "composition_probe": dict(composition) if isinstance(composition, Mapping) else {},
        "probe_contract": str(state.get("probe_contract") or "host_surface_and_capture_exclusion_v1"),
        "failure_reason": str(state.get("failure_reason") or ""),
        "map_requested_at": float(state.get("map_requested_at") or 0.0),
        "mapped_at": float(state.get("mapped_at") or 0.0),
        "composition_checked_at": float(state.get("composition_checked_at") or 0.0),
        "presented_at": float(state.get("presented_at") or 0.0),
        "draw_completed_at": float(state.get("draw_completed_at") or 0.0),
        "bound_timing": dict(state.get("bound_timing") or {}),
        "event_written_at": float(state.get("event_written_at") or 0.0),
        "event_session_id": str(state.get("event_session_id") or ""),
        "event_selection_epoch": int(state.get("event_selection_epoch") or 0),
        "event_selection_revision": int(state.get("event_selection_revision") or 0),
        "ready_frame": bool(state.get("ready_frame")),
        "desired_visible": bool(visibility.get("window_visible")),
        "actual_visible": bool(state.get("ws_visible") and not state.get("iconic") and state.get("cloaked") is not True),
        "geometry_version": int(visibility.get("geometry_version") or 0),
        "draw_version": int(state.get("_draw_sequence") or 0),
        "callback_version": int(state.get("_token") or 0),
    }


def mark_canvas_drawn(
    visibility: dict[str, Any],
    *,
    completed_at: float | None = None,
    ready_frame: bool = False,
    event: Mapping[str, Any] | None = None,
) -> None:
    """记录 Canvas 命令完成；这里绝不把它冒充为屏幕呈现完成。"""

    state = _presentation_state(visibility)
    # A queued map/composition callback belongs to the old Canvas, not the
    # newly rendered event (even when its HWND and client rect are unchanged).
    invalidate_overlay_presentation(visibility, reason="canvas_drawn")
    observed_at = time.time() if completed_at is None else float(completed_at)
    state["_draw_sequence"] = int(state.get("_draw_sequence") or 0) + 1
    state["_composition_checked_draw_sequence"] = 0
    state["_composition_failures"] = 0
    state["_last_failure_draw_sequence"] = 0
    state["state"] = "pending"
    state["draw_completed_at"] = observed_at
    state["mapped_at"] = 0.0
    state["composition_checked_at"] = 0.0
    state["presented_at"] = 0.0
    state["failure_reason"] = ""
    state["composition_probe"] = {"state": "pending", "reason": "canvas_drawn"}
    event_source = event.get("source") if isinstance(event, Mapping) else None
    source = event_source if isinstance(event_source, Mapping) else {}
    event_timing = event.get("timing") if isinstance(event, Mapping) else None
    timing = event_timing if isinstance(event_timing, Mapping) else {}
    state["event_written_at"] = float(timing.get("event_written_at") or 0.0)
    state["event_session_id"] = str(source.get("session_id") or "")
    state["event_selection_epoch"] = int(source.get("selection_epoch") or 0)
    state["event_selection_revision"] = int(source.get("selection_revision") or 0)
    state["ready_frame"] = bool(ready_frame)
    state["bound_timing"] = {
        **{key: float(timing.get(key) or 0.0) for key in (
            "capture_started_at", "captured_at", "recognition_completed_at", "event_written_at",
        )},
        "host_read_at": float(visibility.pop("render_event_host_read_at", 0.0) or visibility.get("host_read_at") or 0.0),
        "context_confirmed_at": float(visibility.get("context_confirmed_at") or 0.0),
        "draw_started_at": float(visibility.get("draw_started_at") or 0.0),
        "draw_completed_at": observed_at,
    }
    visibility["draw_completed_at"] = observed_at
    visibility["last_presented_at"] = 0.0
    visibility["last_draw_ready_frame"] = bool(ready_frame)


def invalidate_overlay_presentation(visibility: dict[str, Any], *, reason: str) -> None:
    """撤销旧呈现回调，不隐藏或清空仍可使用的同尺寸 Canvas。"""
    state = _presentation_state(visibility)
    state["_token"] = int(state.get("_token") or 0) + 1
    state["_scheduled"] = False
    state["_composition_checked_draw_sequence"] = -1
    state["_composition_failures"] = 0
    state["_map_failures"] = 0
    state["_last_failure_draw_sequence"] = 0
    state["state"] = "pending"
    state["failure_reason"] = ""
    state["presented_at"] = 0.0
    state["composition_checked_at"] = 0.0
    state["composition_probe"] = {"state": "not_run", "reason": reason}
    visibility["last_presented_at"] = 0.0


def hide_overlay_presentation(root: tk.Misc, visibility: dict[str, Any]) -> None:
    invalidate_overlay_presentation(visibility, reason="hidden")
    state = _presentation_state(visibility)
    root.withdraw()
    state.update(
        {
            "state": "hidden",
            "ws_visible": False,
            "iconic": False,
            "cloaked": None,
            "failure_reason": "",
            "composition_probe": {"state": "not_run", "reason": "hidden"},
            "host_surface_probe": {"state": "not_run", "reason": "hidden"},
            "map_requested_at": 0.0,
            "mapped_at": 0.0,
            "composition_checked_at": 0.0,
            "presented_at": 0.0,
        }
    )
    visibility["last_presented_at"] = 0.0


def _target_rect(visibility: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    value = visibility.get("target_rect")
    if not isinstance(value, tuple) or len(value) != 4:
        return None
    try:
        rect = tuple(int(item) for item in value)
    except (TypeError, ValueError):
        return None
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return None
    return rect


def _mapping_failure_reason(probe: Mapping[str, Any]) -> str:
    if str(probe.get("error") or ""):
        return "window_probe_error"
    if not bool(probe.get("valid")):
        return "overlay_hwnd_missing"
    if not bool(probe.get("ws_visible")):
        return "window_not_mapped"
    if bool(probe.get("iconic")):
        return "window_iconic"
    if probe.get("cloaked") is True:
        return "window_cloaked"
    if not bool(probe.get("style_ok")):
        return "window_style_mismatch"
    capture_exclusion = probe.get("capture_exclusion")
    if isinstance(capture_exclusion, Mapping) and str(capture_exclusion.get("status") or "") != "applied":
        return "capture_exclusion_unavailable"
    if not bool(probe.get("rect_matches")):
        return "window_rect_mismatch"
    return ""


def _apply_mapping_probe(state: dict[str, Any], probe: Mapping[str, Any]) -> None:
    for key in (
        "overlay_hwnd",
        "ws_visible",
        "iconic",
        "cloaked",
        "dwm_status",
        "expected_client_rect",
        "actual_rect",
        "rect_matches",
        "style_checks",
        "capture_exclusion",
    ):
        value = probe.get(key)
        state[key] = dict(value) if key == "style_checks" and isinstance(value, Mapping) else value


def _map_window(
    root: tk.Misc,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
    rect: tuple[int, int, int, int],
) -> None:
    pending_geometry = str(visibility.get("pending_geometry") or "")
    if pending_geometry:
        root.geometry(pending_geometry)
        visibility["applied_geometry"] = pending_geometry
    root.deiconify()
    root.attributes("-topmost", True)
    _ensure_overlay_window_styles(root, config)
    _show_overlay_hwnd(root, rect)


def _notify_state_change(visibility: Mapping[str, Any]) -> None:
    callback = visibility.get("presentation_state_changed")
    if not isinstance(callback, Callable):
        return
    try:
        callback()
    except Exception:
        logger.debug("调度 presentation 状态回读失败。", exc_info=True)


def _hex_rgb(canvas: tk.Canvas, value: str) -> tuple[int, int, int] | None:
    try:
        red, green, blue = canvas.winfo_rgb(value)
    except (tk.TclError, ValueError):
        return None
    return (int(red) // 257, int(green) // 257, int(blue) // 257)


def _canvas_probe_targets(
    canvas: tk.Canvas,
    actual_rect: tuple[int, int, int, int],
    *,
    transparent_color: str,
) -> list[tuple[int, int, tuple[int, int, int]]]:
    """从当前 Canvas 已有不透明图形中选择固定内点，不新增可见标记。"""

    targets: list[tuple[int, int, tuple[int, int, int]]] = []
    seen: set[tuple[int, int]] = set()
    transparent_rgb = _hex_rgb(canvas, transparent_color) if transparent_color else None
    try:
        items = list(canvas.find_all())
    except tk.TclError:
        return []
    for item in reversed(items):
        if len(targets) >= COMPOSITION_MAX_SAMPLES:
            break
        try:
            if canvas.type(item) not in {"rectangle", "polygon"}:
                continue
            fill = str(canvas.itemcget(item, "fill") or "")
            expected = _hex_rgb(canvas, fill) if fill else None
            bbox = canvas.bbox(item)
        except tk.TclError:
            continue
        if expected is None or expected == transparent_rgb or bbox is None:
            continue
        x0, y0, x1, y1 = (int(value) for value in bbox)
        if x1 - x0 < 28 or y1 - y0 < 28:
            continue
        inset = max(8, min(16, (y1 - y0) // 5))
        candidates = (
            (x0 + inset, y0 + inset),
            (x1 - inset, y0 + inset),
            (x0 + inset, y1 - inset),
            (x1 - inset, y1 - inset),
            ((x0 + x1) // 2, (y0 + y1) // 2),
        )
        for canvas_x, canvas_y in candidates:
            if len(targets) >= COMPOSITION_MAX_SAMPLES:
                break
            try:
                overlapping = canvas.find_overlapping(canvas_x, canvas_y, canvas_x, canvas_y)
            except tk.TclError:
                continue
            if not overlapping or int(overlapping[-1]) != int(item):
                continue
            screen_point = (actual_rect[0] + canvas_x, actual_rect[1] + canvas_y)
            if screen_point in seen:
                continue
            seen.add(screen_point)
            targets.append((screen_point[0], screen_point[1], expected))
    return targets


def _run_composition_probe(
    canvas: tk.Canvas | None,
    config: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any]:
    if str(config.get("game_window_mode_status") or "supported") != "supported":
        return {
            "state": "unavailable",
            "reason": "unsupported_game_window_mode",
            "sample_count": 0,
            "probe_contract": "dwm_desktop_dc",
        }
    actual = state.get("actual_rect")
    if canvas is None or not isinstance(actual, list) or len(actual) != 4:
        return {"state": "unavailable", "reason": "canvas_or_rect_unavailable", "sample_count": 0}
    actual_rect = tuple(int(value) for value in actual)
    targets = _canvas_probe_targets(
        canvas,
        actual_rect,
        transparent_color=str(config.get("transparent_color") or ""),
    )
    host_surface_probe = {
        "state": "matched" if len(targets) >= COMPOSITION_MIN_SAMPLES else "unavailable",
        "reason": "opaque_canvas_content_present"
        if len(targets) >= COMPOSITION_MIN_SAMPLES
        else "opaque_sample_points_unavailable",
        "sample_count": len(targets),
        "probe_contract": "tk_canvas_items_v1",
    }
    if len(targets) < COMPOSITION_MIN_SAMPLES:
        return {
            "state": "unavailable",
            "reason": "opaque_sample_points_unavailable",
            "sample_count": len(targets),
            "host_surface_probe": host_surface_probe,
        }
    capture_exclusion = state.get("capture_exclusion")
    capture_exclusion_applied = bool(
        isinstance(capture_exclusion, Mapping)
        and str(capture_exclusion.get("status") or "") == "applied"
    )
    if capture_exclusion_applied:
        # GetPixel/Desktop DC 返回的是人眼最终合成面，不是 Sidecar 使用的屏幕捕获
        # 语义；WDA_EXCLUDEFROMCAPTURE 生效时它在不同 Windows 构建上可能仍返回
        # Overlay 像素，也可能返回黑色。这里若继续拿它判断 leak，会把正确应用的
        # 捕获排除误判成绘制失败并重新制造显示/隐藏反馈环。真实 MSS 排除由
        # packaged presentation smoke 在已知底色和高对比探针上独立验证。
        return {
            "state": "excluded",
            "reason": "display_affinity_readback_applied",
            "sample_count": 0,
            "matched_count": 0,
            "tolerance": COMPOSITION_COLOR_TOLERANCE,
            "probe_contract": "window_display_affinity_readback_v1",
            "host_surface_probe": host_surface_probe,
        }

    sampled = _sample_screen_pixels([(x, y) for x, y, _expected in targets])
    valid_count = 0
    matched_count = 0
    for (_x, _y, expected), actual_rgb in zip(targets, sampled):
        if actual_rgb is None:
            continue
        valid_count += 1
        if max(abs(actual - wanted) for actual, wanted in zip(actual_rgb, expected)) <= COMPOSITION_COLOR_TOLERANCE:
            matched_count += 1
    if valid_count < COMPOSITION_MIN_SAMPLES:
        return {
            "state": "unavailable",
            "reason": "screen_pixel_api_unavailable",
            "sample_count": valid_count,
            "host_surface_probe": host_surface_probe,
        }
    matched = matched_count >= COMPOSITION_MIN_SAMPLES
    return {
        "state": "matched" if matched else "mismatched",
        "reason": "overlay_pixels_observed" if matched else "overlay_pixels_not_observed",
        "sample_count": valid_count,
        "matched_count": matched_count,
        "tolerance": COMPOSITION_COLOR_TOLERANCE,
        "probe_contract": "dwm_desktop_dc",
        "host_surface_probe": host_surface_probe,
    }


def _schedule_mapping_verification(
    root: tk.Misc,
    canvas: tk.Canvas | None,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
    rect: tuple[int, int, int, int],
    token: int,
) -> None:
    state = _presentation_state(visibility)
    state["_scheduled"] = True
    root.after_idle(
        lambda: _verify_mapping(root, canvas, config, visibility, rect, token)
    )


def _verify_mapping(
    root: tk.Misc,
    canvas: tk.Canvas | None,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
    rect: tuple[int, int, int, int],
    token: int,
) -> None:
    state = _presentation_state(visibility)
    if int(state.get("_token") or 0) != token:
        return
    probe = _probe_overlay_hwnd(root, config, rect)
    _apply_mapping_probe(state, probe)
    if isinstance(probe.get("capture_exclusion"), Mapping):
        visibility["capture_exclusion"] = dict(probe["capture_exclusion"])
    failure = _mapping_failure_reason(probe)
    if failure:
        if failure == "capture_exclusion_unavailable":
            root.withdraw()
            state["state"] = "failed"
            state["failure_reason"] = failure
            state["ws_visible"] = False
            state["_scheduled"] = False
            state["_last_failure_draw_sequence"] = int(state.get("_draw_sequence") or 0)
            visibility["last_presented_at"] = 0.0
            _notify_state_change(visibility)
            return
        failures = int(state.get("_map_failures") or 0) + 1
        state["_map_failures"] = failures
        if failures < 2:
            state["state"] = "pending"
            _map_window(root, config, visibility, rect)
            root.after(
                16,
                lambda: _verify_mapping(root, canvas, config, visibility, rect, token),
            )
            return
        state["state"] = "failed"
        state["failure_reason"] = failure
        state["_scheduled"] = False
        state["_last_failure_draw_sequence"] = int(state.get("_draw_sequence") or 0)
        _notify_state_change(visibility)
        return
    state["_map_failures"] = 0
    state["state"] = "mapped"
    state["failure_reason"] = ""
    state["mapped_at"] = time.time()
    root.after(
        COMPOSITION_DELAY_MS,
        lambda: _verify_composition(root, canvas, config, visibility, rect, token),
    )


def _verify_composition(
    root: tk.Misc,
    canvas: tk.Canvas | None,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
    rect: tuple[int, int, int, int],
    token: int,
) -> None:
    state = _presentation_state(visibility)
    if int(state.get("_token") or 0) != token:
        return
    probe_config = {
        **dict(config),
        "game_window_mode_status": str(
            visibility.get("game_window_mode_status") or "supported"
        ),
    }
    result = _run_composition_probe(canvas, probe_config, state)
    checked_at = time.time()
    host_surface_probe = result.get("host_surface_probe")
    if isinstance(host_surface_probe, Mapping):
        state["host_surface_probe"] = dict(host_surface_probe)
    state["composition_probe"] = {
        key: value for key, value in result.items() if key != "host_surface_probe"
    }
    state["composition_checked_at"] = checked_at
    state["_composition_checked_draw_sequence"] = int(state.get("_draw_sequence") or 0)
    if result.get("state") in {"matched", "excluded"}:
        state["state"] = "composed"
        state["presented_at"] = checked_at
        state["failure_reason"] = ""
        state["_composition_failures"] = 0
        state["_scheduled"] = False
        visibility["last_presented_at"] = checked_at
        if bool(visibility.get("last_draw_ready_frame")):
            visibility["last_ready_frame_at"] = checked_at
        _notify_state_change(visibility)
        return
    if result.get("state") == "unavailable":
        state["state"] = "mapped"
        state["presented_at"] = 0.0
        state["failure_reason"] = ""
        state["_scheduled"] = False
        visibility["last_presented_at"] = 0.0
        _notify_state_change(visibility)
        return
    failures = int(state.get("_composition_failures") or 0) + 1
    state["_composition_failures"] = failures
    if failures < 2:
        state["state"] = "pending"
        state["failure_reason"] = ""
        try:
            root.event_generate("<Expose>", when="tail")
        except tk.TclError:
            logger.debug("请求 Overlay Canvas 重绘失败。", exc_info=True)
        _map_window(root, config, visibility, rect)
        _schedule_mapping_verification(root, canvas, config, visibility, rect, token)
        return
    state["state"] = "failed"
    state["presented_at"] = 0.0
    state["failure_reason"] = (
        "capture_exclusion_leaked"
        if result.get("state") == "leaked"
        else "composition_not_observed"
    )
    state["_scheduled"] = False
    state["_last_failure_draw_sequence"] = int(state.get("_draw_sequence") or 0)
    visibility["last_presented_at"] = 0.0
    _notify_state_change(visibility)


def ensure_overlay_presentation(
    root: tk.Misc,
    canvas: tk.Canvas | None,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
) -> None:
    """复核实际 HWND；只有新绘制或窗口漂移才启动新的异步合成周期。"""

    state = _presentation_state(visibility)
    rect = _target_rect(visibility)
    if rect is None:
        state["state"] = "failed"
        state["failure_reason"] = "expected_client_rect_missing"
        state["_scheduled"] = False
        return
    rect_key = tuple(rect)
    draw_sequence = int(state.get("_draw_sequence") or 0)
    if bool(state.get("_scheduled")):
        return

    # 即便业务缓存仍为 True，也必须重新读取 mapped/style/rect 事实。
    probe = _probe_overlay_hwnd(root, config, rect)
    _apply_mapping_probe(state, probe)
    if isinstance(probe.get("capture_exclusion"), Mapping):
        visibility["capture_exclusion"] = dict(probe["capture_exclusion"])
    mapping_failure = _mapping_failure_reason(probe)
    if mapping_failure == "capture_exclusion_unavailable":
        root.withdraw()
        state["state"] = "failed"
        state["failure_reason"] = mapping_failure
        state["ws_visible"] = False
        state["_scheduled"] = False
        state["_last_failure_draw_sequence"] = draw_sequence
        visibility["last_presented_at"] = 0.0
        _notify_state_change(visibility)
        return
    checked_sequence = int(state.get("_composition_checked_draw_sequence") or 0)
    same_failed_draw = (
        str(state.get("state") or "") == "failed"
        and int(state.get("_last_failure_draw_sequence") or 0) == draw_sequence
        and str(state.get("failure_reason") or "")
        in {"composition_not_observed", "capture_exclusion_leaked"}
    )
    if not mapping_failure and checked_sequence == draw_sequence and not same_failed_draw:
        return
    if same_failed_draw and not mapping_failure:
        return

    state["_token"] = int(state.get("_token") or 0) + 1
    token = int(state["_token"])
    state["_expected_rect_key"] = rect_key
    state["_map_failures"] = 0
    state["state"] = "pending"
    state["failure_reason"] = ""
    state["map_requested_at"] = time.time()
    state["mapped_at"] = 0.0
    state["composition_checked_at"] = 0.0
    state["presented_at"] = 0.0
    visibility["last_presented_at"] = 0.0
    _map_window(root, config, visibility, rect)
    _schedule_mapping_verification(root, canvas, config, visibility, rect, token)


__all__ = [
    "ensure_overlay_presentation",
    "hide_overlay_presentation",
    "invalidate_overlay_presentation",
    "mark_canvas_drawn",
    "presentation_status",
]

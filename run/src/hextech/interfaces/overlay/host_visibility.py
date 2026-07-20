"""Overlay host 的平台无关显隐状态、诊断与窗口展示编排。"""

from __future__ import annotations

import json
import logging
import os
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Mapping

from hextech.interfaces.overlay.gameflow import GameflowState, probe_gameflow_state
from hextech.interfaces.overlay.host_common import (
    GAME_OVERLAY_VISIBILITY_FILE,
    HOST_VISIBILITY_STATUS_HEARTBEAT_SECONDS,
    OVERLAY_EXIT_FILE_ENV,
    OVERLAY_EXIT_POLL_MS,
    OVERLAY_READY_FILE_ENV,
    OVERLAY_READY_TOKEN_ENV,
    VISIBILITY_DIAGNOSTIC_LOG_SECONDS,
    GameflowPoller,
    OverlayVisibilitySnapshot,
)
from hextech.interfaces.overlay.host_platform import _apply_overlay_rect, _ensure_overlay_window_styles
from hextech.modules.data.ports.atomic import atomic_write_json


logger = logging.getLogger(__name__)

def resolve_overlay_visibility(
    *,
    user_enabled: bool,
    gameflow_in_progress: GameflowState | str | bool,
    game_hwnd: int | None,
    game_rect: tuple[int, int, int, int] | None,
    game_renderable: bool,
    game_foreground: bool,
    content_ready: bool,
    now: float | None = None,
) -> OverlayVisibilitySnapshot:
    """只按 host 自有信号决定窗口生命周期；视觉事件只影响内容。"""

    gameflow_state = _normalize_gameflow_state(gameflow_in_progress)
    normalized_hwnd = int(game_hwnd) if game_hwnd else None
    normalized_rect = tuple(int(value) for value in game_rect) if game_rect is not None else None
    if not user_enabled:
        visible, reason = False, "user_disabled"
    elif gameflow_state is GameflowState.NOT_IN_PROGRESS:
        visible, reason = False, "gameflow_not_in_progress"
    elif not normalized_hwnd:
        visible, reason = False, "game_window_missing"
    elif not game_renderable:
        visible, reason = False, "game_window_not_renderable"
    elif not game_foreground:
        visible, reason = False, "game_not_foreground"
    elif gameflow_state is GameflowState.UNKNOWN:
        visible, reason = True, "waiting_gameflow"
    elif content_ready:
        visible, reason = True, "visible_ready"
    else:
        visible, reason = True, "visible_detecting"
    return OverlayVisibilitySnapshot(
        user_enabled=bool(user_enabled),
        gameflow_in_progress=gameflow_state is GameflowState.IN_PROGRESS,
        gameflow_state=gameflow_state.value,
        game_hwnd=normalized_hwnd,
        game_rect=normalized_rect,
        game_renderable=bool(game_renderable),
        game_foreground=bool(game_foreground),
        visible=visible,
        reason=reason,
        updated_at=time.time() if now is None else float(now),
    )


def _normalize_gameflow_state(value: GameflowState | str | bool | None) -> GameflowState:
    if isinstance(value, GameflowState):
        return value
    if isinstance(value, bool):
        return GameflowState.IN_PROGRESS if value else GameflowState.NOT_IN_PROGRESS
    try:
        return GameflowState(str(value or "unknown"))
    except ValueError:
        return GameflowState.UNKNOWN


def _query_gameflow_in_progress() -> GameflowState:
    """优先用 2999 判断实际对局，再用 LCU gameflow 兜底；不读取凭据文件。"""

    return probe_gameflow_state()


def _refresh_gameflow_in_progress(visibility: dict[str, Any], *, now: float | None = None) -> GameflowState:
    poller = visibility.get("gameflow_poller")
    if isinstance(poller, GameflowPoller):
        state, checked_at = poller.current()
        visibility["gameflow_state"] = state.value
        visibility["gameflow_in_progress"] = state is GameflowState.IN_PROGRESS
        visibility["gameflow_checked_at"] = checked_at
        visibility["gameflow_probe"] = poller.status()
        return state
    raw_state = visibility.get("gameflow_state")
    if raw_state is None:
        raw_state = visibility.get("gameflow_in_progress", True)
    state = _normalize_gameflow_state(raw_state)
    visibility["gameflow_state"] = state.value
    visibility["gameflow_in_progress"] = state is GameflowState.IN_PROGRESS
    if "gameflow_checked_at" not in visibility:
        visibility["gameflow_checked_at"] = time.time() if now is None else float(now)
    return state


def _target_overlay_geometry(rect: tuple[int, int, int, int], config: dict[str, Any]) -> str:
    left, top, right, bottom = rect
    game_width = max(1, right - left)
    game_height = max(1, bottom - top)
    x_offset = f"+{left}" if left >= 0 else str(left)
    y_offset = f"+{top}" if top >= 0 else str(top)
    return f"{game_width}x{game_height}{x_offset}{y_offset}"


def _resolve_initial_overlay_viewport(
    initial_target: tuple[int, tuple[int, int, int, int]] | None,
    config: dict[str, Any],
) -> tuple[int, int, str]:
    """未发现游戏窗口时仅保留隐藏占位，避免 900x150 参与正式布局。"""

    if initial_target is None:
        return (1, 1, "1x1+0+0")
    _, rect = initial_target
    width = max(1, rect[2] - rect[0])
    height = max(1, rect[3] - rect[1])
    return (width, height, _target_overlay_geometry(rect, config))


def _snapshot_source_reason(snapshot: Mapping[str, Any]) -> str:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    return str(source.get("reason") or "").strip()


def _snapshot_selection_window_active(snapshot: Mapping[str, Any]) -> bool | None:
    """读取 sidecar 生命周期字段；旧事件返回 None 以启用短暂兼容 hold。"""

    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    value = source.get("selection_window_active")
    return value if isinstance(value, bool) else None


def _extract_event_status(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """提取仅用于去重日志的状态；正式与诊断画面均不显示状态 UI。"""

    if not isinstance(snapshot, Mapping):
        snapshot = {}
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}

    def optional_number(value: Any, converter: Any) -> Any:
        try:
            return converter(value)
        except (TypeError, ValueError):
            return None

    ready_slots = optional_number(source.get("ready_slots"), int)
    if ready_slots is None:
        ready_slots = _snapshot_ready_slot_count(snapshot)
    return {
        "gate_state": str(source.get("gate_state") or "").strip(),
        "ready_slots": max(0, min(3, int(ready_slots or 0))),
        "blocking_modal": bool(source.get("blocking_modal")),
        "latency_ms": optional_number(source.get("latency_ms"), float),
        "stable_frames": optional_number(source.get("stable_frames"), int),
        "selection_window_active": (
            source.get("selection_window_active")
            if isinstance(source.get("selection_window_active"), bool)
            else None
        ),
        "error": str(snapshot.get("error") or "").strip(),
    }


def _log_waiting_context_diagnostic(
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    context: Mapping[str, Any],
    model: Mapping[str, Any],
) -> None:
    statuses = [
        str(row.get("status_code") or "")
        for row in (model.get("stats") if isinstance(model.get("stats"), list) else [])
        if isinstance(row, Mapping)
    ]
    if not any(status in {"CONTEXT_MISSING", "CONTEXT_EXPIRED"} for status in statuses):
        return
    event_status = _extract_event_status(snapshot)
    diagnostic = {
        "context_status": "ok" if context.get("ok") else str(context.get("error") or "context_missing"),
        "context_source": str(context.get("source") or ""),
        "context_champion_id": str(context.get("champion_id") or ""),
        "context_champion_name": str(context.get("champion_name") or ""),
        "event_reason": _snapshot_source_reason(snapshot),
        "ready_slots": event_status.get("ready_slots"),
        "selection_window_active": event_status.get("selection_window_active"),
        "render_statuses": statuses,
    }
    diagnostic_key = tuple(diagnostic.items())
    if diagnostic_key == visibility.get("last_waiting_context_diagnostic"):
        return
    visibility["last_waiting_context_diagnostic"] = diagnostic_key
    logger.info("game_overlay waiting_context=%s", diagnostic)


def _snapshot_has_complete_ready_slots(snapshot: Mapping[str, Any]) -> bool:
    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    return len(slots) >= 3 and all(
        isinstance(slot, Mapping)
        and slot.get("state") == "ready"
        and bool(str(slot.get("augment_id") or slot.get("name") or "").strip())
        for slot in slots[:3]
    )


def _snapshot_ready_slot_count(snapshot: Mapping[str, Any]) -> int:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    try:
        return max(0, min(3, int(source.get("ready_slots"))))
    except (TypeError, ValueError):
        slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
        return sum(
            1
            for slot in slots[:3]
            if isinstance(slot, Mapping)
            and slot.get("state") == "ready"
            and bool(str(slot.get("augment_id") or slot.get("name") or "").strip())
        )


def _cache_allows_private_stats(hint_cache: Mapping[str, Any] | None) -> bool:
    source = hint_cache.get("source") if isinstance(hint_cache, Mapping) else None
    return bool(isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True)


def _count_current_context_synergy_hints(hints: Any, context: Mapping[str, Any] | None) -> int:
    if not isinstance(hints, Mapping) or not isinstance(context, Mapping):
        return 0
    champion_id = str(context.get("champion_id") or "").strip()
    champion_name = str(context.get("champion_name") or "").strip()
    return sum(
        1
        for hint in hints.values()
        if isinstance(hint, Mapping)
        and any(
            isinstance(item, Mapping)
            and (
                (champion_id and str(item.get("hero_id") or "").strip() == champion_id)
                or (champion_name and str(item.get("hero_name") or "").strip() == champion_name)
            )
            for item in (hint.get("synergies") if isinstance(hint.get("synergies"), list) else [])
        )
    )


def _signal_overlay_ready() -> bool:
    """首个健康 Host tick 后写 readiness；失败时允许下一 tick 重试。"""

    ready_path = str(os.environ.get(OVERLAY_READY_FILE_ENV) or "").strip()
    if not ready_path:
        return True
    try:
        ready_token = str(os.environ.get(OVERLAY_READY_TOKEN_ENV) or "").strip()
        atomic_write_json(
            Path(ready_path),
            {"pid": os.getpid(), "token": ready_token, "ready_at": time.time()},
            ensure_ascii=False,
            indent=2,
        )
        return True
    except Exception:
        logger.exception("写入 game_overlay host readiness 失败。")
        return False


def _schedule_exit_file_watch(root: tk.Tk, exit_path: str | Path | None = None) -> None:
    """监听 lifecycle 写入的退出信号，让 host 优先走 Tk 主循环正常收尾。"""

    raw_path = str(exit_path or os.environ.get(OVERLAY_EXIT_FILE_ENV) or "").strip()
    if not raw_path:
        return
    signal_path = Path(raw_path)

    def poll_exit_signal() -> None:
        try:
            should_exit = signal_path.exists()
        except OSError:
            logger.debug("检查 game_overlay host 退出信号失败：%s", signal_path, exc_info=True)
            should_exit = False
        if should_exit:
            logger.info("收到 game_overlay host 退出信号。")
            root.quit()
            return
        root.after(OVERLAY_EXIT_POLL_MS, poll_exit_signal)

    root.after(OVERLAY_EXIT_POLL_MS, poll_exit_signal)

def _show_overlay_window(root: tk.Tk, config: dict[str, Any], visibility: dict[str, Any]) -> None:
    pending_geometry = str(visibility.get("pending_geometry") or "")
    if pending_geometry:
        root.geometry(pending_geometry)
        visibility["applied_geometry"] = pending_geometry
    target_rect = visibility.get("target_rect")
    if isinstance(target_rect, tuple) and len(target_rect) == 4:
        _apply_overlay_rect(root, target_rect)
    root.deiconify()
    root.attributes("-topmost", True)
    _ensure_overlay_window_styles(root, config)


def _apply_transparent_background(root: tk.Tk, canvas: tk.Canvas, config: Mapping[str, Any]) -> None:
    transparent_color = str(config.get("transparent_color") or "").strip()
    if not transparent_color:
        return
    root.configure(bg=transparent_color)
    canvas.configure(bg=transparent_color)
    try:
        root.attributes("-transparentcolor", transparent_color)
    except tk.TclError:
        logger.debug("当前 Tk 环境不支持 transparentcolor。", exc_info=True)


def decide_visibility(
    *,
    user_enabled: bool,
    event_visible: bool,
    game_foreground: bool,
    content_ready: bool,
    selection_window_active: bool | None,
    gameflow_in_progress: GameflowState | str | bool = True,
    game_hwnd: int | None = None,
    game_rect: tuple[int, int, int, int] | None = None,
    game_renderable: bool = False,
    ready_slots: int | None = None,
    scoreboard_key_down: bool = False,
    event_fresh_after_tab: bool = True,
    event_error: str = "",
    blocking_modal: bool = False,
    diagnostic_mode: bool = False,
    stale_event_hold: bool = False,
) -> tuple[bool, str]:
    """统一显隐决策，避免显示结果和诊断原因分叉。"""

    resolved_ready_slots = (3 if content_ready else 0) if ready_slots is None else int(ready_slots)
    host_snapshot = resolve_overlay_visibility(
        user_enabled=user_enabled,
        gameflow_in_progress=gameflow_in_progress,
        game_hwnd=game_hwnd,
        game_rect=game_rect,
        game_renderable=game_renderable,
        game_foreground=game_foreground,
        content_ready=content_ready,
    )
    should_show, reason = host_snapshot.visible, host_snapshot.reason
    if should_show and blocking_modal:
        should_show, reason = False, "blocking_modal_present"
    elif should_show and scoreboard_key_down:
        should_show, reason = False, "scoreboard_key_down"
    elif should_show and not event_fresh_after_tab:
        should_show, reason = False, "event_stale_after_tab"
    elif should_show and selection_window_active is False:
        should_show, reason = False, "selection_inactive"
    elif should_show and selection_window_active is None and not (event_visible or stale_event_hold):
        should_show, reason = False, (event_error or "selection_state_unavailable")
    elif should_show and event_error and not stale_event_hold:
        should_show, reason = False, event_error
    elif should_show and stale_event_hold:
        reason = "visible_stale_hold"
    elif should_show and resolved_ready_slots > 0 and resolved_ready_slots < 3:
        reason = "visible_partial"

    del diagnostic_mode
    return should_show, reason


def _draw_diagnostic_status(canvas: tk.Canvas, reason: str, snapshot: Mapping[str, Any]) -> None:
    """诊断模式下只画一行 heartbeat，避免非选择态看起来像进程崩溃。"""

    status = _extract_event_status(snapshot)
    parts = [
        "Hextech overlay diagnostic",
        f"reason={reason}",
        f"gate={status.get('gate_state') or '-'}",
        f"ready={status.get('ready_slots')}",
        f"error={status.get('error') or '-'}",
    ]
    message = " · ".join(parts)
    canvas.delete("all")
    try:
        canvas.create_rectangle(8, 8, 560, 34, fill="#010A13", outline="#785A28")
    except AttributeError:
        pass
    canvas.create_text(
        18,
        21,
        text=message,
        fill="#F0E6D2",
        anchor="w",
        font=("Segoe UI", 11, "bold"),
    )


def _log_visibility_diagnostic(
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: float,
    should_show: bool,
    reason: str,
) -> None:
    """限频输出显隐决策面，便于真机从日志定位是哪一层挡住。"""

    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    diagnostic_key = (
        bool(should_show),
        str(reason or ""),
        bool(visibility.get("gameflow_in_progress")),
        int(visibility.get("target_hwnd") or 0),
        bool(visibility.get("game_renderable")),
        bool(visibility.get("game_foreground")),
        source.get("selection_window_active"),
        int(visibility.get("ready_slots") or 0),
        bool(visibility.get("blocking_modal")),
        bool(visibility.get("scoreboard_key_down")),
        str(visibility.get("event_error") or ""),
        bool(visibility.get("context_ok")),
        str(visibility.get("context_champion_id") or ""),
        str(visibility.get("context_source") or ""),
        str(visibility.get("context_error") or ""),
    )
    try:
        last_logged_at = float(visibility.get("last_visibility_diagnostic_logged_at") or 0.0)
    except (TypeError, ValueError):
        last_logged_at = 0.0
    if (
        diagnostic_key == visibility.get("last_visibility_diagnostic_key")
        and now - last_logged_at < VISIBILITY_DIAGNOSTIC_LOG_SECONDS
    ):
        return
    visibility["last_visibility_diagnostic_key"] = diagnostic_key
    visibility["last_visibility_diagnostic_logged_at"] = float(now)
    logger.info(
        "game_overlay visibility=%s",
        {
            "host": {
                "user_enabled": bool(visibility.get("user_enabled")),
                "gameflow": bool(visibility.get("gameflow_in_progress")),
                "hwnd": int(visibility.get("target_hwnd") or 0),
                "renderable": bool(visibility.get("game_renderable")),
                "foreground": bool(visibility.get("game_foreground")),
            },
            "scene": {
                "selection_window_active": source.get("selection_window_active"),
                "ready_slots": int(visibility.get("ready_slots") or 0),
                "blocking_modal": bool(visibility.get("blocking_modal")),
                "scoreboard": bool(visibility.get("scoreboard_key_down")),
                "event_error": str(visibility.get("event_error") or ""),
            },
            "context": {
                "context_ok": bool(visibility.get("context_ok")),
                "champion_id": str(visibility.get("context_champion_id") or ""),
                "source": str(visibility.get("context_source") or ""),
                "error": str(visibility.get("context_error") or ""),
            },
            "decision": {
                "window_visible": bool(should_show),
                "reason": str(reason or ""),
            },
        },
    )


def _build_visibility_status_payload(
    visibility: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: float,
    should_show: bool,
    reason: str,
) -> dict[str, Any]:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    probe = visibility.get("window_probe") if isinstance(visibility.get("window_probe"), Mapping) else {}
    render_failures = int(visibility.get("consecutive_render_failures") or 0)
    probe_failures = int(probe.get("consecutive_failures") or 0)
    try:
        missing_since = float(visibility.get("active_target_missing_since") or 0.0)
    except (TypeError, ValueError):
        missing_since = 0.0
    failure_reason = ""
    if render_failures >= 4:
        functional_status, failure_reason = "failed", "render_loop_failed"
    elif missing_since > 0.0 and now - missing_since >= 3.0:
        functional_status, failure_reason = "failed", "active_scene_without_window"
    elif probe_failures >= 3:
        functional_status, failure_reason = "degraded", "window_probe_error"
    elif bool(visibility.get("window_target_desync")):
        functional_status, failure_reason = "degraded", "window_target_desync"
    elif source.get("selection_window_active") is True and str(visibility.get("context_error") or ""):
        functional_status, failure_reason = "degraded", "context_unavailable"
    else:
        functional_status, failure_reason = "ready", ""
    return {
        "schema_version": 2,
        "updated_at": float(now),
        "functional_status": functional_status,
        "functional_reason": failure_reason,
        "host": {
            "user_enabled": bool(visibility.get("user_enabled")),
            "gameflow": bool(visibility.get("gameflow_in_progress")),
            "gameflow_state": str(visibility.get("gameflow_state") or "unknown"),
            "hwnd": int(visibility.get("target_hwnd") or 0),
            "renderable": bool(visibility.get("game_renderable")),
            "foreground": bool(visibility.get("game_foreground")),
        },
        "window": {
            "source": str(visibility.get("window_source") or "none"),
            "host_hwnd": int(visibility.get("host_scan_hwnd") or 0),
            "sidecar_hwnd": int(visibility.get("sidecar_hwnd") or 0),
            "desync": bool(visibility.get("window_target_desync")),
            "probe_status": str(probe.get("probe_status") or "unknown"),
            "last_probe_at": float(probe.get("last_probe_at") or 0.0),
            "last_success_at": float(probe.get("last_success_at") or 0.0),
            "last_error_type": str(probe.get("last_error_type") or ""),
            "consecutive_failures": probe_failures,
        },
        "scene": {
            "selection_window_active": source.get("selection_window_active"),
            "ready_slots": int(visibility.get("ready_slots") or 0),
            "blocking_modal": bool(visibility.get("blocking_modal")),
            "scoreboard": bool(visibility.get("scoreboard_key_down")),
            "event_error": str(visibility.get("event_error") or ""),
        },
        "context": {
            "context_ok": bool(visibility.get("context_ok")),
            "champion_id": str(visibility.get("context_champion_id") or ""),
            "source": str(visibility.get("context_source") or ""),
            "error": str(visibility.get("context_error") or ""),
        },
        "decision": {
            "window_visible": bool(should_show),
            "reason": str(reason or ""),
        },
        "render": {
            "last_tick_at": float(visibility.get("last_tick_at") or 0.0),
            "last_presented_at": float(visibility.get("last_presented_at") or 0.0),
            "last_ready_frame_at": float(visibility.get("last_ready_frame_at") or 0.0),
            "consecutive_failures": render_failures,
        },
    }


def _visibility_status_key(payload: Mapping[str, Any]) -> str:
    comparable = dict(payload)
    comparable.pop("updated_at", None)
    return json.dumps(comparable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_host_visibility_status(
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any],
    *,
    now: float,
    should_show: bool,
    reason: str,
) -> None:
    """把 host 最终显隐原因写入 state，供桌面 UI 和赛后诊断读取。"""

    payload = _build_visibility_status_payload(
        visibility,
        snapshot,
        now=now,
        should_show=should_show,
        reason=reason,
    )
    status_key = _visibility_status_key(payload)
    try:
        last_written_at = float(visibility.get("last_visibility_status_written_at") or 0.0)
    except (TypeError, ValueError):
        last_written_at = 0.0
    if (
        status_key == visibility.get("last_visibility_status_key")
        and now - last_written_at < HOST_VISIBILITY_STATUS_HEARTBEAT_SECONDS
    ):
        return
    try:
        atomic_write_json(GAME_OVERLAY_VISIBILITY_FILE, payload)
    except OSError:
        logger.debug("写入 game_overlay visibility 状态失败。", exc_info=True)
        return
    visibility["last_visibility_status_key"] = status_key
    visibility["last_visibility_status_written_at"] = float(now)

"""Overlay host 的事件轮询、窗口目标同步与渲染调度。"""

from __future__ import annotations

import logging
import json
import queue
import time
import tkinter as tk
import ctypes
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import GameSessionState, PresentationMode, VisionSlotState
from hextech.interfaces.overlay.gameflow import GameflowState
from hextech.interfaces.overlay.host_common import WindowTargetPoller
from hextech.interfaces.overlay.host_platform import (
    _apply_overlay_rect,
    _ensure_overlay_window_styles,
    _is_game_window_foreground,
    _root_hwnd,
)
from hextech.interfaces.overlay.host_visibility import (
    _log_visibility_diagnostic,
    _normalize_gameflow_state,
    _query_gameflow_in_progress,
    _refresh_gameflow_in_progress,
    _show_overlay_window,
    _snapshot_has_complete_ready_slots,
    _snapshot_ready_slot_count,
    _snapshot_selection_window_active,
    _target_overlay_geometry,
    _write_host_visibility_status,
    decide_visibility,
)
from hextech.modules.session.evidence import build_evidence_bundle, build_render_signature, write_evidence_bundle
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.vision.runtime_paths import overlay_runtime_state_path
from hextech.modules.vision.window import is_window_renderable


logger = logging.getLogger(__name__)

STALE_EVENT_HOLD_SECONDS = 1.0
OVERLAY_SESSION_REPORT_LIMIT = 20

# 兼容旧测试/诊断对该符号的 patch；render tick 只读取缓存，不会调用它。
__all__ = ["_query_gameflow_in_progress"]


def _string_key_mapping(value: object) -> dict[str, Any]:
    """将 JSON 边界的任意 Mapping 收窄为本模块可写的字符串键字典。"""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _sync_event_visibility(
    root: tk.Tk,
    config: dict[str, Any],
    visibility: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    apply_window: bool = True,
    resolved_should_show: bool | None = None,
) -> bool:
    now = time.time()
    event_visible = bool(snapshot.get("visible"))
    overlay_hwnd = _root_hwnd(root) if bool(config.get("no_activate", True)) else None
    game_hwnd = visibility.get("target_hwnd")
    game_rect = visibility.get("target_rect") if isinstance(visibility.get("target_rect"), tuple) else None
    game_renderable = bool(game_hwnd and is_window_renderable(game_hwnd))
    game_foreground = _is_game_window_foreground(game_hwnd, overlay_hwnd=overlay_hwnd) if game_renderable else False
    gameflow_state = _normalize_gameflow_state(_refresh_gameflow_in_progress(visibility))
    user_enabled = bool(visibility.get("user_enabled"))
    content_ready = _snapshot_has_complete_ready_slots(snapshot)
    ready_slots = _snapshot_ready_slot_count(snapshot)
    selection_window_active = _snapshot_selection_window_active(snapshot)
    event_error = str(snapshot.get("error") or "").strip()
    source = _string_key_mapping(snapshot.get("source"))
    blocking_modal = bool(source.get("blocking_modal"))
    stale_hold_active = bool(source.get("stale_event_hold_active"))

    if selection_window_active is True and not event_error and not blocking_modal:
        if not stale_hold_active:
            visibility["last_active_event"] = deepcopy(snapshot)
            visibility.pop("event_stale_hold_until", None)
    elif selection_window_active is False or blocking_modal:
        visibility.pop("last_active_event", None)
        visibility.pop("event_stale_hold_until", None)
        stale_hold_active = False
    elif event_error and not stale_hold_active:
        cached = visibility.get("last_active_event")
        try:
            hold_until = float(visibility.get("event_stale_hold_until") or 0.0)
        except (TypeError, ValueError):
            hold_until = 0.0
        if isinstance(cached, Mapping):
            if hold_until <= 0.0:
                hold_until = now + STALE_EVENT_HOLD_SECONDS
                visibility["event_stale_hold_until"] = hold_until
            if now <= hold_until:
                held_snapshot = deepcopy(dict(cached))
                held_source = _string_key_mapping(held_snapshot.get("source"))
                held_snapshot["source"] = {
                    **held_source,
                    "stale_event_hold_active": True,
                    "stale_event_error": event_error,
                    "reason": "stale_event_hold",
                }
                held_snapshot["ok"] = True
                held_snapshot["error"] = ""
                snapshot.clear()
                snapshot.update(held_snapshot)
                event_visible = bool(snapshot.get("visible"))
                selection_window_active = _snapshot_selection_window_active(snapshot)
                event_error = ""
                source = _string_key_mapping(snapshot.get("source"))
                stale_hold_active = True
            else:
                visibility.pop("last_active_event", None)
                visibility.pop("event_stale_hold_until", None)
        else:
            visibility.pop("event_stale_hold_until", None)
    scoreboard_key_down = bool(visibility.get("scoreboard_key_down"))
    try:
        generated_at = float(snapshot.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        generated_at = 0.0
    tab_released_at = float(visibility.get("tab_released_at") or 0.0)
    event_fresh_after_tab = not tab_released_at or generated_at >= tab_released_at
    should_show = resolved_should_show
    reason = str(visibility.get("visibility_reason") or "")
    if should_show is None:
        should_show, reason = decide_visibility(
            user_enabled=user_enabled,
            event_visible=event_visible,
            game_foreground=game_foreground,
            content_ready=content_ready,
            selection_window_active=selection_window_active,
            gameflow_in_progress=gameflow_state,
            game_hwnd=game_hwnd,
            game_rect=game_rect,
            game_renderable=game_renderable,
            ready_slots=ready_slots,
            scoreboard_key_down=scoreboard_key_down,
            event_fresh_after_tab=event_fresh_after_tab,
            event_error=event_error,
            blocking_modal=blocking_modal,
            diagnostic_mode=bool(config.get("diagnostic_mode")),
            stale_event_hold=stale_hold_active,
        )
    visibility["event_visible"] = event_visible
    visibility["gameflow_state"] = gameflow_state.value
    visibility["gameflow_in_progress"] = gameflow_state is GameflowState.IN_PROGRESS
    visibility["game_renderable"] = game_renderable
    visibility["game_foreground"] = game_foreground
    visibility["content_ready"] = content_ready
    visibility["ready_slots"] = ready_slots
    visibility["selection_window_active"] = selection_window_active
    visibility["event_error"] = event_error
    visibility["blocking_modal"] = blocking_modal
    visibility["event_stale_hold_active"] = stale_hold_active
    visibility["visibility_reason"] = reason
    if selection_window_active is True and not game_hwnd:
        visibility.setdefault("active_target_missing_since", time.time())
    else:
        visibility.pop("active_target_missing_since", None)
    visibility["render_full_overlay"] = bool(should_show)
    _write_host_visibility_status(visibility, snapshot, now=now, should_show=should_show, reason=reason)
    if not apply_window:
        _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
        return should_show
    if visibility.get("window_visible") is should_show:
        _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
        return should_show
    if should_show:
        _show_overlay_window(root, config, visibility)
    else:
        root.withdraw()
    visibility["window_visible"] = should_show
    _log_visibility_diagnostic(visibility, snapshot, now=now, should_show=should_show, reason=reason)
    return should_show


def _draw_waiting_status(canvas: tk.Canvas, reason: str) -> None:
    """非选择态只显示轻量提示，不能把空事件重新解释为三张检测中卡片。"""

    message = {
        "selection_completed": "海克斯选择已完成，等待下一次选择",
        "waiting_context": "等待英雄上下文",
        "waiting_gameflow": "等待游戏状态",
    }.get(reason, "等待海克斯选择")
    canvas.delete("all")
    canvas.create_rectangle(8, 8, 420, 42, fill="#010A13", outline="#785A28")
    canvas.create_text(
        20,
        25,
        text=message,
        fill="#F0E6D2",
        anchor="w",
        font=("Microsoft YaHei UI", 11, "bold"),
    )


def _write_real_session_evidence(
    root: tk.Tk,
    state: GameSessionState,
    snapshot: Mapping[str, Any],
    model: Mapping[str, Any],
    visibility: dict[str, Any],
    *,
    diagnostic: bool = False,
) -> None:
    """三槽渲染连续稳定两个 tick 后，延迟抓取同 revision 的真实证据。"""

    vision = state.vision
    if (
        state.visibility.presentation_mode is not PresentationMode.CONTENT
        or state.context is None
        or state.context.local_champion_id is None
        or state.recommendation is None
        or vision is None
        or len(vision.slots) != 3
        or any(slot.state is not VisionSlotState.READY for slot in vision.slots)
    ):
        return
    revision = max(1, int(vision.selection_revision))
    rows = [dict(item) for item in model.get("stats", []) if isinstance(item, Mapping)]
    render_signature = build_render_signature(state, rows)
    if visibility.get("current_render_signature") == render_signature:
        visibility["stable_render_ticks"] = int(visibility.get("stable_render_ticks") or 0) + 1
    else:
        visibility["current_render_signature"] = render_signature
        visibility["stable_render_ticks"] = 1
    if int(visibility.get("stable_render_ticks") or 0) < 2:
        return
    key = (str(state.session_id), str(state.generation_id), int(vision.epoch), revision, render_signature)
    if visibility.get("last_evidence_key") == key or visibility.get("evidence_attempt_key") == key:
        return
    source = _string_key_mapping(snapshot.get("source"))
    client_rect = source.get("client_rect") if isinstance(source.get("client_rect"), list) else []
    capture_size = source.get("capture_size") if isinstance(source.get("capture_size"), list) else []
    if len(client_rect) != 4 or len(capture_size) != 2:
        return
    visibility["evidence_attempt_key"] = key
    snapshot_copy = deepcopy(dict(snapshot))
    source_copy = deepcopy(source)
    model_copy = deepcopy(dict(model))

    def capture() -> None:
        try:
            if (
                not bool(visibility.get("window_visible"))
                or visibility.get("current_render_signature") != render_signature
                or visibility.get("evidence_attempt_key") != key
            ):
                if visibility.get("evidence_attempt_key") == key:
                    visibility.pop("evidence_attempt_key", None)
                return
            root.update_idletasks()
            try:
                ctypes.windll.dwmapi.DwmFlush()
            except (AttributeError, OSError):
                pass
            evidence_dir = Path(overlay_runtime_state_path("session_evidence")).resolve()
            evidence_dir.mkdir(parents=True, exist_ok=True)
            context = state.context
            safe_session = "".join(char for char in str(state.session_id) if char.isalnum())[:32] or "session"
            context_revision = max(1, int(context.context_revision or 0))
            signature_prefix = render_signature[:12]
            stem = (
                f"overlay-{safe_session}-e{int(vision.epoch)}-r{revision}"
                f"-c{context_revision}-{signature_prefix}"
            )
            screenshot_name = ""
            if diagnostic:
                screenshot_path = evidence_dir / f"{stem}.png"
                from PIL import ImageGrab

                left, top = root.winfo_rootx(), root.winfo_rooty()
                right, bottom = left + root.winfo_width(), top + root.winfo_height()
                ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB").save(screenshot_path)
                screenshot_name = screenshot_path.name
            client_size = [int(client_rect[2]) - int(client_rect[0]), int(client_rect[3]) - int(client_rect[1])]
            event_slots = snapshot_copy.get("slots") if isinstance(snapshot_copy.get("slots"), list) else []
            render_rows = [dict(item) for item in model_copy.get("stats", []) if isinstance(item, Mapping)]
            bundle = build_evidence_bundle(
                state,
                lcu_summary={
                    "local_champion_id": str(context.local_champion_id),
                    "teammate_champion_ids": [str(value) for value in context.teammate_champion_ids],
                    "bench_champion_ids": [str(value) for value in context.bench_champion_ids],
                    "source": str(context.source or ""),
                    "game_instance_id": str(context.game_instance_id or ""),
                    "window_hwnd": int(context.window_hwnd or 0),
                    "context_revision": int(context.context_revision or 0),
                    "publication_seq": int(context.publication_seq or 0),
                    "publisher_instance_id": str(context.publisher_instance_id or ""),
                },
                window_summary={
                    "hwnd": int(source_copy.get("window_hwnd") or 0),
                    "client_size": client_size,
                    "capture_size": [int(value) for value in capture_size],
                    "dpi_scale": float(source_copy.get("dpi_scale") or 0.0),
                },
                screenshot=screenshot_name,
                render_summary={
                    "rows": render_rows,
                    "synergies": [
                        dict(item) for item in model_copy.get("synergies", []) if isinstance(item, Mapping)
                    ],
                    "event_slots": [dict(item) for item in event_slots if isinstance(item, Mapping)],
                    "acceptance_rules": [
                        str(item)
                        for item in (
                            snapshot_copy.get("_acceptance_rules")
                            if isinstance(snapshot_copy.get("_acceptance_rules"), list)
                            else []
                        )
                    ],
                },
                render_signature=render_signature,
            )
            evidence_path = write_evidence_bundle(bundle, evidence_dir / f"{stem}.v2.json")
            write_evidence_bundle(bundle, evidence_dir / "latest_real_session.v2.json")
            visibility["last_evidence_key"] = key
            logger.info("game_overlay real_session_evidence=%s", evidence_path)
        except Exception:
            logger.warning("延迟写入真实会话验收证据失败。", exc_info=True)
        finally:
            if visibility.get("evidence_attempt_key") == key:
                visibility.pop("evidence_attempt_key", None)

    root.after(100, capture)


def _write_overlay_session_report(
    snapshot: Mapping[str, Any],
    model: Mapping[str, Any] | None,
    visibility: dict[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    state: GameSessionState | None = None,
    diagnostic: bool = False,
) -> Path | None:
    """记录每种真实 Overlay 会话结果，包含缺失原因而不默认保存截图。

    同一事件签名只落盘一次；`latest.json` 始终指向最新结构化结果，历史严格保留
    最近 20 条，便于真机问题复现而不无限增长运行态。
    """

    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    rows = model.get("stats") if isinstance(model, Mapping) and isinstance(model.get("stats"), list) else []
    def _safe_slot_number(value: object, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    safe_slots = [
        {
            "slot": _safe_slot_number(item.get("slot"), index),
            "state": str(item.get("state") or ""),
            "name": str(item.get("name") or "")[:80],
            "data_status": str(item.get("data_status") or ""),
            "data_reason": str(item.get("data_reason") or ""),
            "generation_id": str(item.get("generation_id") or ""),
            "vision_id": str(item.get("vision_id") or ""),
            "canonical_id": str(item.get("canonical_id") or ""),
            "champion_id": str(item.get("champion_id") or ""),
        }
        for index, item in enumerate(slots[:3])
        if isinstance(item, Mapping)
    ]
    safe_rows = [
        {
            "slot": _safe_slot_number(item.get("slot"), index),
            "status_code": str(item.get("status_code") or ""),
            "status_text": str(item.get("status_text") or "")[:80],
        }
        for index, item in enumerate(rows[:3])
        if isinstance(item, Mapping)
    ]
    session_id = str(source.get("session_id") or (state.session_id if state is not None else "") or "")
    generation_id = str(
        source.get("generation_id") or (state.generation_id if state is not None else "") or ""
    )
    signature_payload = {
        "session_id": session_id,
        "generation_id": generation_id,
        "visible_reason": str(visibility.get("visibility_reason") or ""),
        "event_error": str(snapshot.get("error") or ""),
        "slots": safe_slots,
        "rows": safe_rows,
        "context": str((context or {}).get("error") or "") if isinstance(context, Mapping) else "",
    }
    signature = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if visibility.get("last_overlay_session_report_signature") == signature:
        return None
    report = {
        "schema_version": 1,
        "recorded_at": time.time(),
        "diagnostic": bool(diagnostic),
        "session_id": session_id,
        "generation_id": generation_id,
        "event": {
            "schema_version": int(snapshot.get("schema_version") or 0),
            "ok": bool(snapshot.get("ok")),
            "visible": bool(snapshot.get("visible")),
            "active": bool(snapshot.get("active")),
            "error": str(snapshot.get("error") or ""),
            "selection_type": str(snapshot.get("selection_type") or ""),
        },
        "source": {
            "tag": str(source.get("tag") or ""),
            "reason": str(source.get("reason") or ""),
            "generation_id": generation_id,
        },
        "visibility": {
            "reason": str(visibility.get("visibility_reason") or ""),
            "context_gate_state": str(visibility.get("context_gate_state") or ""),
            "context_gate_reason": str(visibility.get("context_gate_reason") or ""),
        },
        "context": {
            "ok": bool((context or {}).get("ok")) if isinstance(context, Mapping) else False,
            "champion_id": str((context or {}).get("champion_id") or "") if isinstance(context, Mapping) else "",
            "error": str((context or {}).get("error") or "") if isinstance(context, Mapping) else "",
        },
        "slots": safe_slots,
        "render": {"rows": safe_rows},
        "screenshot": "",
    }
    report_dir = (get_var_dir() / "reports" / "overlay_sessions").resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    # Windows 的 system clock 粒度可能低于纳秒；只截取 time_ns 会在同一 UI tick
    # 内复用文件名并静默覆盖前一份诊断。每个 host 会话维护单调序号，mtime 相同
    # 时路径名仍能保留“最近 20 条”的正确顺序；随机 token 则覆盖重启时的极端碰撞。
    try:
        sequence = int(visibility.get("session_report_sequence") or 0) + 1
    except (TypeError, ValueError):
        sequence = 1
    visibility["session_report_sequence"] = sequence
    target = report_dir / f"overlay-session-{stamp}-{time.time_ns():020d}-{sequence:08d}-{uuid.uuid4().hex}.json"
    atomic_write_json(target, report, ensure_ascii=False, indent=2)
    atomic_write_json(report_dir / "latest.json", report, ensure_ascii=False, indent=2)
    reports = sorted(
        (path for path in report_dir.glob("overlay-session-*.json") if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for stale in reports[OVERLAY_SESSION_REPORT_LIMIT:]:
        if stale.parent.resolve() == report_dir:
            stale.unlink(missing_ok=True)
    visibility["last_overlay_session_report_signature"] = signature
    return target


def _drain_hotkey_requests(request_queue: "queue.Queue[str]", visibility: dict[str, bool]) -> None:
    while True:
        try:
            request = request_queue.get_nowait()
        except queue.Empty:
            return
        if request == "toggle":
            visibility["user_enabled"] = not bool(visibility.get("user_enabled"))
        elif request == "toggle_mode":
            visibility["display_mode"] = (
                "compact" if visibility.get("display_mode") == "expanded" else "expanded"
            )


def _snapshot_window_target(snapshot: Mapping[str, Any]) -> tuple[int, tuple[int, int, int, int]] | None:
    """只接受 canonical reader 判定为新鲜的 Sidecar 窗口观察。"""

    if snapshot.get("ok") is not True:
        return None
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    raw_hwnd = source.get("window_hwnd")
    raw_rect = source.get("client_rect")
    if not raw_hwnd or not isinstance(raw_rect, list) or len(raw_rect) != 4:
        return None
    try:
        hwnd = int(raw_hwnd)
        rect = tuple(int(value) for value in raw_rect)
    except (TypeError, ValueError):
        return None
    if rect[2] <= rect[0] or rect[3] <= rect[1] or not is_window_renderable(hwnd):
        return None
    return hwnd, rect


def _refresh_target_window(
    root: tk.Tk,
    config: Mapping[str, Any],
    visibility: dict[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> None:
    """Sidecar observation 优先，本地 Poller 仅作启动与失联 fallback。"""

    poller = visibility.get("window_target_poller")
    if isinstance(poller, WindowTargetPoller):
        host_target = poller.current()
        poller_status = poller.status()
    else:
        hwnd = visibility.get("target_hwnd")
        rect = visibility.get("target_rect")
        host_target = (int(hwnd), rect) if hwnd and isinstance(rect, tuple) and len(rect) == 4 else None
        poller_status = {}
    sidecar_target = _snapshot_window_target(snapshot or {})
    sidecar_hwnd = int(sidecar_target[0]) if sidecar_target is not None else 0
    host_hwnd = int(host_target[0]) if host_target is not None else 0
    target = sidecar_target or host_target
    visibility["sidecar_hwnd"] = sidecar_hwnd
    visibility["host_scan_hwnd"] = host_hwnd
    visibility["window_target_desync"] = bool(sidecar_hwnd and host_hwnd and sidecar_hwnd != host_hwnd)
    visibility["window_source"] = "sidecar" if sidecar_target is not None else (
        "host_scan" if host_target is not None else "none"
    )
    visibility["window_probe"] = dict(poller_status)
    snapshot_source = (snapshot or {}).get("source") if isinstance((snapshot or {}).get("source"), Mapping) else {}
    sidecar_game_instance = str(snapshot_source.get("game_instance_id") or "")
    host_game_instance = str(poller_status.get("game_instance_id") or "")
    visibility["sidecar_game_instance_id"] = sidecar_game_instance
    visibility["host_game_instance_id"] = host_game_instance
    visibility["game_instance_id"] = sidecar_game_instance or host_game_instance
    visibility["game_identity_desync"] = bool(
        sidecar_game_instance and host_game_instance and sidecar_game_instance != host_game_instance
    )
    if target is None:
        if isinstance(poller, WindowTargetPoller):
            visibility["target_hwnd"] = None
            visibility["target_rect"] = None
            visibility["pending_geometry"] = ""
        return
    hwnd, rect = target
    visibility["target_hwnd"] = hwnd
    visibility["target_rect"] = rect
    next_geometry = _target_overlay_geometry(rect, dict(config))
    visibility["pending_geometry"] = next_geometry
    if visibility.get("window_visible") and next_geometry != visibility.get("applied_geometry"):
        root.geometry(next_geometry)
        _apply_overlay_rect(root, rect)
        _ensure_overlay_window_styles(root, config)
        visibility["applied_geometry"] = next_geometry

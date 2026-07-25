"""Overlay host 的进程入口与 Tk 主循环组装。"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.interfaces.overlay.gameflow import GameflowState, lcu_scanner_configured
from hextech.interfaces.overlay.generation_pin import SelectionGenerationPin
from hextech.interfaces.overlay.context_gate import ContextRenderGate
from hextech.interfaces.overlay.host_common import (
    RENDER_ERROR_BACKOFF_AFTER,
    RENDER_ERROR_BACKOFF_MAX_MS,
    ForegroundEventHook,
    GameflowPoller,
    HotkeyController,
    WindowTargetPoller,
)
from hextech.interfaces.overlay.host_platform import (
    _ensure_overlay_window_styles,
    _find_target_game_window,
    _prepare_host_hint_cache,
    _register_foreground_event_hook,
    _schedule_foreground_event_drain,
    _set_dpi_awareness,
    _start_hotkey_thread,
    _stop_foreground_event_hook,
    _stop_hotkey_thread,
    build_overlay_window_config,
)
from hextech.interfaces.overlay.host_sync import (
    _drain_hotkey_requests,
    _draw_waiting_status,
    _refresh_target_window,
    _sync_event_visibility,
    _write_overlay_session_report,
    _write_real_session_evidence,
)
from hextech.interfaces.overlay.host_visibility import (
    _apply_transparent_background,
    _cache_allows_private_stats,
    _count_current_context_synergy_hints,
    _draw_diagnostic_status,
    _extract_event_status,
    _log_waiting_context_diagnostic,
    _resolve_initial_overlay_viewport,
    _schedule_exit_file_watch,
    _signal_overlay_ready,
    _snapshot_has_complete_ready_slots,
    _snapshot_ready_slot_count,
    _snapshot_selection_window_active,
    _write_host_visibility_status,
    decide_visibility,
)
from hextech.interfaces.overlay.renderer import build_render_model, build_render_model_from_session, draw_overlay_frame
from hextech.interfaces.overlay.report_writer import OverlayReportWriter
from hextech.interfaces.overlay.session_adapter import build_runtime_session
from hextech.modules.data.overlay_source import (
    OverlayDataSource,
    SharedOverlayDataSource,
    apply_overlay_display_policy,
    source_has_private_stats,
)
from hextech.modules.vision.window import WindowProbeResult, is_scoreboard_key_down
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.session import build_identity
from hextech.modules.vision.runtime_paths import overlay_runtime_state_path


logger = logging.getLogger(__name__)


def _resolve_overlay_render_options(
    snapshot: Mapping[str, Any],
    *,
    viewport_width: int,
    display_mode: str,
) -> dict[str, Any]:
    """生产与验收共用显示模式和游戏控件禁入区解析。"""

    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    raw_button_box = source.get("button_box")
    exclusion_zones: list[tuple[int, int, int, int]] = []
    if isinstance(raw_button_box, list) and len(raw_button_box) == 4:
        try:
            left, top, right, bottom = (int(value) for value in raw_button_box)
            margin = max(12, int(max(1, viewport_width) * 0.008))
            exclusion_zones.append((left - margin, top - margin, right + margin, bottom + margin))
        except (TypeError, ValueError):
            exclusion_zones = []
    return {
        "expanded": display_mode == "expanded",
        "show_synergy": bool(str(source.get("layout_id") or "") and exclusion_zones),
        "exclusion_zones": tuple(exclusion_zones),
    }

def resolve_event_render_delay_ms(config: Mapping[str, Any], visibility: Mapping[str, Any]) -> int:
    """根据最近选择态事件选择 overlay host 下一帧轮询间隔。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    fast_ms = max(50, int(visibility.get("fast_event_poll_ms") or config.get("fast_event_poll_ms", 60) or 60))
    if bool(visibility.get("render_full_overlay")) or bool(visibility.get("selection_window_active")):
        return min(base_ms, fast_ms)
    try:
        if int(visibility.get("ready_slots") or 0) > 0:
            return min(base_ms, fast_ms)
    except (TypeError, ValueError):
        pass
    try:
        fast_until = float(visibility.get("fast_event_until") or 0.0)
    except (TypeError, ValueError):
        fast_until = 0.0
    return min(base_ms, fast_ms) if time.monotonic() < fast_until else base_ms


def resolve_event_render_retry_delay_ms(config: Mapping[str, Any], failure_count: int) -> int:
    """渲染失败后的下一次重试间隔；失败路径不进入 fast poll。"""

    base_ms = max(50, int(config.get("event_poll_ms", 250) or 250))
    if int(failure_count or 0) <= RENDER_ERROR_BACKOFF_AFTER:
        return base_ms
    exponent = min(8, int(failure_count) - RENDER_ERROR_BACKOFF_AFTER)
    return min(RENDER_ERROR_BACKOFF_MAX_MS, base_ms * (2 ** exponent))


def _schedule_event_render(
    root: tk.Tk,
    canvas: tk.Canvas,
    config: dict[str, Any],
    visibility: dict[str, Any],
    hotkey_queue: "queue.Queue[str]",
    *,
    data_source: OverlayDataSource | None = None,
) -> Callable[[], None]:
    """单一 tick 同步窗口、事件和显隐；隐藏时不加载 hint/context。"""

    fast_poll_ms = max(50, int(config.get("fast_event_poll_ms", 60) or 60))
    fast_hold_seconds = max(0.0, float(config.get("fast_event_hold_ms", 1200) or 1200) / 1000.0)
    source = data_source or SharedOverlayDataSource()
    generation_pin = SelectionGenerationPin()
    context_gate = ContextRenderGate()
    failure_count = 0
    render_after_id: str | None = None

    def retry_delay_ms() -> int:
        if failure_count <= 0:
            return resolve_event_render_delay_ms(config, visibility)
        return resolve_event_render_retry_delay_ms(config, failure_count)

    def note_fast_event(snapshot: Mapping[str, Any]) -> None:
        event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
        slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
        ready_slots = sum(
            1
            for slot in slots
            if isinstance(slot, Mapping)
            and (str(slot.get("state") or "") == "ready" or bool(str(slot.get("augment_id") or "").strip()))
        )
        if (
            bool(snapshot.get("active"))
            or bool(snapshot.get("visible"))
            or event_source.get("selection_window_active") is True
            or ready_slots > 0
        ):
            visibility["fast_event_until"] = max(
                float(visibility.get("fast_event_until") or 0.0),
                time.monotonic() + fast_hold_seconds,
            )
        visibility["fast_event_poll_ms"] = fast_poll_ms

    def schedule_render(delay_ms: int, *, replace: bool = False) -> None:
        nonlocal render_after_id
        if render_after_id is not None:
            if not replace:
                return
            try:
                canvas.after_cancel(render_after_id)
            except Exception:
                logger.debug("取消 overlay render after 失败。", exc_info=True)
            render_after_id = None
        render_after_id = canvas.after(max(0, int(delay_ms)), render_once)

    def request_render() -> None:
        schedule_render(0, replace=True)

    def render_once() -> None:
        nonlocal failure_count, render_after_id
        render_after_id = None
        success = False
        snapshot: Mapping[str, Any] = {}
        try:
            _drain_hotkey_requests(hotkey_queue, visibility)
            snapshot = source.read_event()
            visibility["host_read_at"] = time.time()
            note_fast_event(snapshot)
            _refresh_target_window(root, config, visibility, snapshot)
            tab_down = is_scoreboard_key_down()
            previous_tab_down = bool(visibility.get("scoreboard_key_down"))
            if tab_down:
                visibility["scoreboard_key_down"] = True
            else:
                visibility["scoreboard_key_down"] = False
                if previous_tab_down:
                    visibility["tab_released_at"] = time.time()
            if bool(config.get("diagnostic_mode")):
                status = _extract_event_status(snapshot)
                diagnostic_key = tuple(status.get(key) for key in (
                    "gate_state", "ready_slots", "blocking_modal", "selection_window_active", "error"
                ))
                if diagnostic_key != visibility.get("last_diagnostic_key"):
                    logger.info("game_overlay diagnostic=%s", status)
                    visibility["last_diagnostic_key"] = diagnostic_key
            should_show = _sync_event_visibility(root, config, visibility, snapshot, apply_window=False)
            if not should_show:
                generation_pin.reset()
                visibility["pinned_generation"] = generation_pin.status()
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=False,
                )
                _write_overlay_session_report(
                    snapshot,
                    None,
                    visibility,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
                success = True
                return
            if bool(config.get("diagnostic_mode")) and not bool(visibility.get("render_full_overlay")):
                visibility["draw_started_at"] = time.time()
                _draw_diagnostic_status(canvas, str(visibility.get("visibility_reason") or ""), snapshot)
                visibility["draw_completed_at"] = time.time()
                visibility["last_presented_at"] = time.time()
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                )
                _write_overlay_session_report(
                    snapshot,
                    None,
                    visibility,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
                success = True
                return
            if not bool(visibility.get("render_full_overlay")):
                event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
                visibility_reason = str(visibility.get("visibility_reason") or "")
                waiting_reason = (
                    visibility_reason
                    if visibility_reason == "waiting_gameflow"
                    else str(event_source.get("reason") or visibility_reason)
                )
                visibility["draw_started_at"] = time.time()
                _draw_waiting_status(canvas, waiting_reason)
                visibility["draw_completed_at"] = time.time()
                visibility["last_presented_at"] = time.time()
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                )
                _write_overlay_session_report(
                    snapshot,
                    None,
                    visibility,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
                success = True
                return
            snapshot_view = generation_pin.resolve(snapshot, source.open_view)
            visibility["pinned_generation"] = generation_pin.status()
            context = source.read_context()
            visibility["context_ok"] = bool(isinstance(context, Mapping) and context.get("ok"))
            visibility["context_champion_id"] = str(context.get("champion_id") or "") if isinstance(context, Mapping) else ""
            visibility["context_source"] = str(context.get("source") or "") if isinstance(context, Mapping) else ""
            visibility["context_error"] = str(context.get("error") or "") if isinstance(context, Mapping) else "context_missing"
            event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
            gate_decision = context_gate.evaluate(
                context if isinstance(context, Mapping) else {},
                game_instance_id=str(visibility.get("game_instance_id") or ""),
                window_hwnd=int(visibility.get("target_hwnd") or 0),
                vision_game_instance_id=str(event_source.get("game_instance_id") or ""),
                vision_window_hwnd=int(event_source.get("window_hwnd") or 0),
                active=bool(event_source.get("selection_window_active") is True),
            )
            visibility["context_gate_state"] = gate_decision.state
            visibility["context_gate_reason"] = gate_decision.reason
            visibility["context_revision"] = gate_decision.context_revision
            visibility["context_held"] = gate_decision.held
            visibility["context_confirmed_at"] = time.time()
            effective_context = gate_decision.payload
            if snapshot_view is not None:
                hint_cache = snapshot_view.get_overlay_hints()
                hint_cache.setdefault("snapshot", {}).update(snapshot_view.status())
                hint_cache = apply_overlay_display_policy(hint_cache)
            else:
                hint_cache = source.read_hint_cache()
            session_state = build_runtime_session(
                event=snapshot,
                context_payload=effective_context,
                snapshot_view=snapshot_view,
                user_enabled=bool(visibility.get("user_enabled")),
                game_present=bool(visibility.get("target_hwnd")),
                private_stats_enabled=source_has_private_stats(hint_cache),
            )
            model = build_render_model_from_session(session_state, hint_cache=hint_cache)
            visibility["session_state"] = session_state
            _log_waiting_context_diagnostic(visibility, snapshot, context, model)
            render_options = _resolve_overlay_render_options(
                snapshot,
                viewport_width=canvas.winfo_width(),
                display_mode=str(visibility.get("display_mode") or "compact"),
            )
            visibility["draw_started_at"] = time.time()
            draw_overlay_frame(
                canvas,
                model,
                perf_sink=visibility,
                **render_options,
            )
            visibility["draw_completed_at"] = time.time()
            visibility["last_presented_at"] = time.time()
            if _snapshot_has_complete_ready_slots(snapshot):
                visibility["last_ready_frame_at"] = visibility["last_presented_at"]
            _sync_event_visibility(
                root,
                config,
                visibility,
                snapshot,
                resolved_should_show=True,
            )
            _write_overlay_session_report(
                snapshot,
                model,
                visibility,
                context=context if isinstance(context, Mapping) else None,
                state=session_state,
                diagnostic=bool(config.get("diagnostic_mode")),
            )
            try:
                _write_real_session_evidence(
                    root,
                    session_state,
                    snapshot,
                    model,
                    visibility,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
            except Exception:
                logger.warning("写入真实会话验收证据失败。", exc_info=True)
            success = True
        except Exception:
            failure_count += 1
            visibility["consecutive_render_failures"] = failure_count
            # 即使本 tick 在上下文、快照或 Tk 绘制阶段失败，也保留最小结构化
            # 会话结果。真机排查不应依赖“成功渲染过一次”这一前提。
            try:
                _write_overlay_session_report(
                    snapshot,
                    None,
                    visibility,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
            except Exception:
                logger.debug("写入失败 Overlay 会话报告失败。", exc_info=True)
            if failure_count <= RENDER_ERROR_BACKOFF_AFTER:
                logger.exception("overlay 渲染轮询失败；下一 tick 将继续重试。")
            elif failure_count == RENDER_ERROR_BACKOFF_AFTER + 1:
                logger.exception("overlay 渲染轮询连续失败，开始退避。")
            else:
                logger.warning("overlay 渲染轮询仍在失败：连续失败=%s，退避中。", failure_count)
            try:
                _write_host_visibility_status(
                    visibility,
                    snapshot,
                    now=time.time(),
                    should_show=bool(visibility.get("window_visible")),
                    reason="render_loop_error",
                )
            except Exception:
                logger.debug("写入 overlay render failure 状态失败。", exc_info=True)
        finally:
            if success:
                failure_count = 0
                visibility["consecutive_render_failures"] = 0
                visibility["last_tick_at"] = time.time()
                poller = visibility.get("window_target_poller")
                poller_status = poller.status() if isinstance(poller, WindowTargetPoller) else {}
                if (
                    not visibility.get("readiness_signaled")
                    and float(poller_status.get("last_probe_at") or 0.0) > 0.0
                    and str(poller_status.get("probe_status") or "") != "error"
                ):
                    visibility["readiness_signaled"] = bool(_signal_overlay_ready())
            schedule_render(retry_delay_ms())

    render_once()
    return request_render


def run_overlay_host(*, diagnostic: bool = False) -> None:
    """启动独立 overlay 窗口；diagnostic 只增加去重日志。"""

    _prepare_host_hint_cache()
    _set_dpi_awareness()
    config = build_overlay_window_config()
    config["diagnostic_mode"] = bool(diagnostic)
    titles = [str(title) for title in config.get("follow_window_titles", [])]
    initial_target = _find_target_game_window(titles)
    initial_width, initial_height, initial_geometry = _resolve_initial_overlay_viewport(initial_target, config)

    root = tk.Tk()
    root.withdraw()
    root.title(config["title"])
    root.geometry(initial_geometry)
    root.attributes("-alpha", config["alpha"])
    root.attributes("-topmost", config["topmost"])
    root.overrideredirect(True)

    canvas = tk.Canvas(
        root,
        width=initial_width,
        height=initial_height,
        highlightthickness=0,
        bd=0,
    )
    _apply_transparent_background(root, canvas, config)
    canvas.pack(fill=tk.BOTH, expand=True)

    visibility: dict[str, Any] = {
        "user_enabled": True,
        "event_visible": False,
        "game_foreground": False,
        "window_visible": False,
        "target_hwnd": initial_target[0] if initial_target is not None else None,
        "target_rect": initial_target[1] if initial_target is not None else None,
        "pending_geometry": initial_geometry if initial_target is not None else "",
        "applied_geometry": initial_geometry if initial_target is not None else "",
        "scoreboard_key_down": False,
        "tab_released_at": 0.0,
        "display_mode": str(config.get("default_display_mode") or "compact"),
    }
    report_writer = OverlayReportWriter(
        get_var_dir() / "reports" / "overlay_sessions",
        Path(overlay_runtime_state_path("session_evidence")),
    )
    report_writer.start()
    visibility["report_writer"] = report_writer
    hotkey_queue: "queue.Queue[str]" = queue.Queue()
    hotkey_controller: HotkeyController | None = None
    data_source = SharedOverlayDataSource()
    gameflow_poller = GameflowPoller()
    window_target_poller = WindowTargetPoller(titles, initial_target=initial_target)
    foreground_event = threading.Event()
    foreground_hook: ForegroundEventHook | None = None
    visibility["gameflow_poller"] = gameflow_poller
    visibility["window_target_poller"] = window_target_poller

    root.update_idletasks()
    _ensure_overlay_window_styles(root, config)
    gameflow_poller.start()
    window_target_poller.start()
    hotkey_controller = _start_hotkey_thread(hotkey_queue)
    request_overlay_render = _schedule_event_render(root, canvas, config, visibility, hotkey_queue, data_source=data_source)
    foreground_hook = _register_foreground_event_hook(foreground_event)
    _schedule_foreground_event_drain(root, foreground_event, request_overlay_render)
    _schedule_exit_file_watch(root)
    logger.info("game_overlay host 已启动：event_poll_ms=%s", config["event_poll_ms"])

    try:
        root.mainloop()
    finally:
        logger.info("game_overlay host 已停止")
        _stop_foreground_event_hook(foreground_hook)
        _stop_hotkey_thread(hotkey_controller)
        window_target_poller.stop()
        gameflow_poller.stop()
        report_writer.close(timeout=5.0)


def run_self_check() -> dict[str, Any]:
    """无 GUI 自检：覆盖真实 Poller、composition root 和规范化契约。"""

    first_target = (101, (10, 20, 1930, 1100))
    recovered_target = (202, (0, 0, 2560, 1600))
    error_gate = threading.Event()
    allow_recovery = threading.Event()
    recovered = threading.Event()
    calls = 0

    def fake_finder(*, window_titles: list[str]) -> WindowProbeResult | None:
        nonlocal calls
        del window_titles
        calls += 1
        if calls == 1:
            return None
        if calls == 2:
            return WindowProbeResult(status="found", hwnd=first_target[0], client_rect=first_target[1], observed_at=time.time())
        if calls == 3:
            error_gate.set()
            raise RuntimeError("self_check_probe_error")
        allow_recovery.wait(timeout=1.0)
        recovered.set()
        return WindowProbeResult(
            status="found",
            hwnd=recovered_target[0],
            client_rect=recovered_target[1],
            observed_at=time.time(),
        )

    poller = WindowTargetPoller([], finder=fake_finder, interval_seconds=0.1)
    poller.start()
    try:
        error_seen = error_gate.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while error_seen and poller.status()["probe_status"] != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        retained_last_good = poller.current() == first_target
        error_status = poller.status()
        allow_recovery.set()
        recovered.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while poller.current() != recovered_target and time.monotonic() < deadline:
            time.sleep(0.01)
        poller_recovered = poller.current() == recovered_target and poller.status()["probe_status"] == "found"
    finally:
        allow_recovery.set()
        poller.stop()
    window_probe_ok = bool(
        error_seen
        and retained_last_good
        and error_status.get("last_error_type") == "RuntimeError"
        and poller_recovered
    )

    config = build_overlay_window_config()
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    hints = hint_cache.get("hints") if isinstance(hint_cache, Mapping) else {}
    hint_count = len(hints) if isinstance(hints, Mapping) else 0
    synergy_hint_count = sum(
        1
        for hint in (hints.values() if isinstance(hints, Mapping) else [])
        if isinstance(hint, Mapping) and isinstance(hint.get("synergies"), list) and hint.get("synergies")
    )
    model = build_render_model(snapshot, hint_cache=hint_cache, context=context)
    status_counts: dict[str, int] = {}
    for row in model["stats"]:
        status_code = str(row.get("status_code") or "")
        status_counts[status_code] = status_counts.get(status_code, 0) + 1
    event_status = _extract_event_status(snapshot)
    context_has_champion = bool(str(context.get("champion_id") or "").strip())
    context_contract_ok = bool(context.get("ok")) if context_has_champion else bool(context.get("error"))
    visibility_contract_ok = decide_visibility(
        user_enabled=True,
        event_visible=True,
        game_foreground=True,
        content_ready=True,
        selection_window_active=True,
        gameflow_in_progress=GameflowState.UNKNOWN,
        game_hwnd=101,
        game_rect=(0, 0, 1920, 1080),
        game_renderable=True,
        ready_slots=3,
    ) == (True, "waiting_gameflow")
    scanner_configured = lcu_scanner_configured()
    contract_checks = {
        "window_probe_ok": window_probe_ok,
        "context_contract_ok": context_contract_ok,
        "visibility_contract_ok": visibility_contract_ok,
        "lcu_scanner_configured": scanner_configured,
        "overlay_event_contract_ok": build_identity.OVERLAY_EVENT_SCHEMA_VERSION == 3,
        "sidecar_status_contract_ok": build_identity.SIDECAR_STATUS_SCHEMA_VERSION == 2,
        "session_report_contract_ok": build_identity.OVERLAY_SESSION_REPORT_SCHEMA_VERSION == 2,
    }
    try:
        state_age_ms = int(max(0.0, time.time() - float(snapshot.get("generated_at") or 0.0)) * 1000)
    except (TypeError, ValueError):
        state_age_ms = None
    return {
        "ok": all(contract_checks.values()),
        "build_id": build_identity.current_build_id(),
        "runtime_contracts": dict(build_identity.RUNTIME_CONTRACT_VERSIONS),
        "title": config["title"],
        "process_health": {
            "host": "self-check-passed" if all(contract_checks.values()) else "self-check-failed",
            "sidecar": "not-inspected",
        },
        **contract_checks,
        "state_age_ms": state_age_ms,
        "event_poll_ms": config["event_poll_ms"],
        "no_activate": bool(config.get("no_activate")),
        "event_ok": bool(snapshot.get("ok")),
        "event_visible": bool(snapshot.get("visible")),
        "event_error": str(snapshot.get("error") or ""),
        "event_reason": str((snapshot.get("source") or {}).get("reason") or "") if isinstance(snapshot.get("source"), dict) else "",
        "ready_slots": event_status["ready_slots"],
        "selection_window_active": event_status["selection_window_active"],
        "schema_version": snapshot.get("schema_version"),
        "cache_ok": not bool(hint_cache.get("error")) if isinstance(hint_cache, Mapping) else False,
        "hint_cache_error": str(hint_cache.get("error") or ""),
        "hint_count": hint_count,
        "private_stats_enabled": _cache_allows_private_stats(hint_cache),
        "synergy_hint_count": synergy_hint_count,
        "context_champion_id": str(context.get("champion_id") or ""),
        "context_champion_name": str(context.get("champion_name") or ""),
        "context_synergy_hint_count": _count_current_context_synergy_hints(hints, context),
        "render_stats_count": sum(1 for row in model["stats"] if row["status_code"] == "READY"),
        "render_synergy_count": len(model["synergies"]),
        "render_status_counts": status_counts,
        "context_status": "ok" if context.get("ok") else str(context.get("error") or "context_missing"),
        "context_source": str(context.get("source") or ""),
        "context_ok": bool(context.get("ok")),
        "context_error": str(context.get("error") or ""),
    }


def render_acceptance_screenshot(
    output_path: str | Path,
    *,
    width: int = 1280,
    height: int = 720,
    display_mode: str | None = None,
) -> dict[str, Any]:
    """用生产显隐与渲染参数生成当前事件的验收截图。"""

    from PIL import ImageGrab

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    source = SharedOverlayDataSource()
    snapshot = source.read_event()
    hint_cache = source.read_hint_cache()
    context = source.read_context()
    event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    game_instance_id = str(event_source.get("game_instance_id") or context.get("game_instance_id") or "")
    window_hwnd = int(event_source.get("window_hwnd") or context.get("window_hwnd") or 0)
    acceptance_gate = ContextRenderGate()
    gate_decision = acceptance_gate.evaluate(
        context,
        game_instance_id=game_instance_id,
        window_hwnd=window_hwnd,
        vision_game_instance_id=str(event_source.get("game_instance_id") or ""),
        vision_window_hwnd=int(event_source.get("window_hwnd") or 0),
        active=bool(event_source.get("selection_window_active") is True),
    )
    if gate_decision.state == "pending" and gate_decision.reason == "context_confirming":
        gate_decision = acceptance_gate.evaluate(
            context,
            game_instance_id=game_instance_id,
            window_hwnd=window_hwnd,
            vision_game_instance_id=str(event_source.get("game_instance_id") or ""),
            vision_window_hwnd=int(event_source.get("window_hwnd") or 0),
            active=bool(event_source.get("selection_window_active") is True),
        )
    snapshot_view = source.open_view()
    session_state = build_runtime_session(
        event=snapshot,
        context_payload=gate_decision.payload,
        snapshot_view=snapshot_view,
        user_enabled=True,
        game_present=bool(window_hwnd),
        private_stats_enabled=source_has_private_stats(hint_cache),
    )
    model = build_render_model_from_session(session_state, hint_cache=hint_cache)
    config = build_overlay_window_config()
    resolved_display_mode = str(display_mode or config.get("default_display_mode") or "compact")
    if resolved_display_mode not in {"compact", "expanded"}:
        raise ValueError(f"未知 Overlay 显示模式：{resolved_display_mode}")
    should_show, visibility_reason = decide_visibility(
        user_enabled=True,
        event_visible=bool(snapshot.get("visible")),
        game_foreground=True,
        content_ready=_snapshot_has_complete_ready_slots(snapshot),
        selection_window_active=_snapshot_selection_window_active(snapshot),
        gameflow_in_progress=True,
        game_hwnd=1,
        game_rect=(0, 0, max(640, int(width)), max(360, int(height))),
        game_renderable=True,
        ready_slots=_snapshot_ready_slot_count(snapshot),
        event_error=str(snapshot.get("error") or ""),
        blocking_modal=bool(event_source.get("blocking_modal")),
    )
    render_options = _resolve_overlay_render_options(
        snapshot,
        viewport_width=max(640, int(width)),
        display_mode=resolved_display_mode,
    )

    _set_dpi_awareness()
    root = tk.Tk()
    root.title("Hextech Overlay Acceptance")
    # 验收窗口模拟生产无边框 overlay；否则 Windows 标题栏会吃掉约 30px，
    # 生成的 2560×1600 请求实际只剩 2560×1570，布局证据失真。
    root.overrideredirect(True)
    root.geometry(f"{max(640, int(width))}x{max(360, int(height))}+0+0")
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, bg="#10131A", highlightthickness=0, bd=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    try:
        root.update_idletasks()
        root.deiconify()
        root.lift()
        root.update()
        if should_show:
            draw_overlay_frame(canvas, model, **render_options)
        else:
            canvas.delete("all")
        root.update_idletasks()
        root.update()
        time.sleep(0.2)
        left = root.winfo_rootx()
        top = root.winfo_rooty()
        right = left + root.winfo_width()
        bottom = top + root.winfo_height()
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
        image.save(target)
    finally:
        root.destroy()

    status_counts: dict[str, int] = {}
    for row in model.get("stats", []):
        code = str(row.get("status_code") or "")
        status_counts[code] = status_counts.get(code, 0) + 1
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache.get("snapshot"), Mapping) else {}
    return {
        "ok": target.is_file() and target.stat().st_size > 0,
        "path": str(target),
        "width": image.width,
        "height": image.height,
        "generation_id": str(snapshot_status.get("generation_id") or ""),
        "status_counts": status_counts,
        "context_champion_id": str(context.get("champion_id") or ""),
        "display_mode": resolved_display_mode,
        "context_gate_state": gate_decision.state,
        "context_gate_reason": gate_decision.reason,
        "overlay_visible": should_show,
        "visibility_reason": visibility_reason,
        "show_synergy": bool(render_options["show_synergy"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech game overlay host。")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic", action="store_true", help="记录去重诊断日志，不绘制状态 UI。")
    parser.add_argument("--self-check", action="store_true", help="执行无 GUI overlay 入口自检后退出。")
    parser.add_argument("--acceptance-screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-width", type=int, default=1280, help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-height", type=int, default=720, help=argparse.SUPPRESS)
    parser.add_argument(
        "--acceptance-display-mode",
        choices=("compact", "expanded"),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_check:
        result = run_self_check()
        from hextech.modules.session.process_bootstrap import publish_process_bootstrap

        # 冻结 GUI 进程没有可靠 stdout；文件结果是 packaged smoke 的权威通道。
        publish_process_bootstrap(result)
        return 0
    if args.acceptance_screenshot is not None:
        result = render_acceptance_screenshot(
            args.acceptance_screenshot,
            width=args.acceptance_width,
            height=args.acceptance_height,
            display_mode=args.acceptance_display_mode,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    run_overlay_host(diagnostic=args.diagnostic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

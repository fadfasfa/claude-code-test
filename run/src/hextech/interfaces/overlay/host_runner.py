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

from hextech.contracts import GameSessionState
from hextech.interfaces.overlay.generation_pin import selection_key
from hextech.interfaces.overlay.host_data_preparation import OverlayDataPreparation
from hextech.interfaces.overlay.host_input import HostInputObserver
from hextech.interfaces.overlay.display_contract import DisplaySelectionGate
from hextech.interfaces.overlay.context_gate import ContextRenderGate
from hextech.interfaces.overlay.host_common import (
    RENDER_ERROR_BACKOFF_AFTER,
    GAME_OVERLAY_VISIBILITY_FILE,
    ForegroundEventHook,
    GameflowPoller,
    HotkeyController,
    WindowTargetPoller,
)
from hextech.interfaces.overlay.host_platform import (
    _ensure_overlay_capture_exclusion,
    _ensure_overlay_window_styles,
    _find_target_game_window,
    _register_foreground_event_hook,
    _schedule_foreground_event_drain,
    _set_dpi_awareness,
    _start_hotkey_thread,
    _stop_foreground_event_hook,
    _stop_hotkey_thread,
    build_overlay_window_config,
)
from hextech.interfaces.overlay.host_acceptance import render_acceptance_screenshot
from hextech.interfaces.overlay.host_presentation import mark_canvas_drawn
from hextech.interfaces.overlay.host_presentation_smoke import run_presentation_smoke
from hextech.interfaces.overlay.host_render_state import (
    SlotRenderCache,
    canvas_viewport,
    note_fast_event,
    present_overlay_model,
    render_semantic_key,
    resolve_event_render_delay_ms,
    resolve_event_render_retry_delay_ms,
    snapshot_slot_generations,
)
from hextech.interfaces.overlay.host_stage_stats import (
    OverlayStageRuntime,
    preserve_selection_pins_while_hidden as _preserve_selection_pins_while_hidden,
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
    _draw_diagnostic_status,
    _extract_event_status,
    _log_waiting_context_diagnostic,
    _resolve_initial_overlay_viewport,
    _schedule_exit_file_watch,
    _signal_overlay_ready,
    _snapshot_has_complete_ready_slots,
    _write_host_visibility_status,
)
from hextech.interfaces.overlay.report_writer import OverlayReportWriter
from hextech.modules.data.overlay_source import (
    OverlayDataSource,
    SharedOverlayDataSource,
)
from hextech.modules.vision.window import is_scoreboard_key_down, window_display_context
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.vision.instance_lock import overlay_instance_lock
from hextech.modules.vision.runtime_paths import overlay_runtime_state_path
from hextech.interfaces.overlay.host_geometry import refresh_display_geometry


logger = logging.getLogger(__name__)
HOST_INSTANCE_LOCK_FILE = get_var_dir() / "locks" / "game_overlay_host.lock"
def _schedule_event_render(
    root: tk.Tk,
    canvas: tk.Canvas,
    config: dict[str, Any],
    visibility: dict[str, Any],
    hotkey_queue: "queue.Queue[str]",
    *,
    data_source: OverlayDataSource | None = None,
    initial_hint_cache: Mapping[str, Any] | None = None,
    data_preparation: OverlayDataPreparation | None = None,
    input_observer: HostInputObserver | None = None,
) -> Callable[[], None]:
    """Tk tick 只读输入邮箱；事件/context 磁盘读取由 Host 观察器拥有。"""
    fast_poll_ms = max(16, int(config.get("fast_event_poll_ms", 16) or 16))
    fast_hold_seconds = max(0.0, float(config.get("fast_event_hold_ms", 1200) or 1200) / 1000.0)
    source = data_source or SharedOverlayDataSource()
    preparation = data_preparation or OverlayDataPreparation(source, initial_hints=initial_hint_cache)
    visibility["data_preparation"] = preparation
    observer = input_observer or HostInputObserver(
        source, config=config, on_event=getattr(preparation, "observe_input", None),
    )
    visibility["input_observer"] = observer
    observer.start()
    display_gate = DisplaySelectionGate()
    slot_render_cache = SlotRenderCache()
    context_gate = ContextRenderGate()
    stage_runtime = OverlayStageRuntime()
    visibility["pinned_stats_scope"] = stage_runtime.pin.status()
    failure_count = 0
    render_after_id: str | None = None

    def retry_delay_ms() -> int:
        if failure_count <= 0:
            return resolve_event_render_delay_ms(config, visibility)
        return resolve_event_render_retry_delay_ms(config, failure_count)

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
            input_sample = observer.snapshot()
            snapshot = display_gate.filter(input_sample.event)
            visibility["host_read_at"] = input_sample.host_read_at
            visibility["input_sequence"] = input_sample.sequence
            visibility["input_age_seconds"] = max(0.0, time.monotonic() - input_sample.observed_at) if input_sample.sequence else None
            visibility["input_error"] = input_sample.error
            visibility["event_read_started_at"] = input_sample.event_read_started_at
            visibility["event_read_completed_at"] = input_sample.event_read_completed_at
            visibility["context_requested_at"] = input_sample.context_requested_at
            visibility["context_read_started_at"] = input_sample.context_read_started_at
            visibility["context_read_completed_at"] = input_sample.context_read_completed_at
            visibility["context_input_sequence"] = input_sample.context_sequence
            visibility["context_input_error"] = input_sample.context_error
            visibility["context_input_game_instance_id"] = input_sample.context_game_instance_id
            visibility["context_input_age_seconds"] = input_sample.context_age_seconds
            note_fast_event(
                snapshot,
                visibility,
                fast_poll_ms=fast_poll_ms,
                fast_hold_seconds=fast_hold_seconds,
            )
            _refresh_target_window(root, config, visibility, snapshot)
            if visibility.get("target_hwnd"):
                visibility["display_context"] = window_display_context(
                    int(visibility["target_hwnd"]), force=bool(visibility.get("display_context_refresh"))
                )
                refresh_display_geometry(root, visibility, visibility["display_context"])
            event_source_value = snapshot.get("source")
            event_source = event_source_value if isinstance(event_source_value, Mapping) else {}
            event_vision_pool_generation_id = str(
                event_source.get("vision_pool_generation_id")
                or event_source.get("data_generation_id")
                or ""
            )
            if event_vision_pool_generation_id:
                # 即使当前事件 inactive/expired，Host visibility 也必须记录实际
                # Sidecar pool；否则迁移场景会把“尚无可显示选择”误报成代际缺失。
                visibility["vision_pool_generation_id"] = event_vision_pool_generation_id
            visibility["selection_completion_tracker"] = stage_runtime.observe(
                snapshot,
                game_instance_id=str(
                    event_source.get("game_instance_id")
                    or visibility.get("game_instance_id")
                    or ""
                ),
            )
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
            context = input_sample.context if should_show or str(event_source.get("session_id") or "") else {}
            visibility["context_ok"] = bool(isinstance(context, Mapping) and context.get("ok"))
            visibility["context_champion_id"] = str(context.get("champion_id") or "") if isinstance(context, Mapping) else ""
            visibility["context_source"] = str(context.get("source") or "") if isinstance(context, Mapping) else ""
            visibility["context_error"] = (
                str(context.get("error") or input_sample.context_error or "context_missing")
                if isinstance(context, Mapping)
                else str(input_sample.context_error or "context_missing")
            )
            previous_context_revision = int(visibility.get("context_revision") or 0)
            context_gate_evaluated_at = time.time()
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
            visibility["context_gate_evaluated_at"] = context_gate_evaluated_at
            if (
                gate_decision.state == "confirmed"
                and (
                    float(visibility.get("context_confirmed_at") or 0.0) <= 0.0
                    or int(visibility.get("context_confirmed_input_sequence") or 0)
                    != input_sample.context_sequence
                )
            ):
                visibility["context_confirmed_at"] = context_gate_evaluated_at
                visibility["context_confirmed_input_sequence"] = input_sample.context_sequence
            elif gate_decision.state != "holding" and gate_decision.context_revision <= 0:
                visibility["context_confirmed_at"] = 0.0
                visibility["context_confirmed_input_sequence"] = 0
            effective_context = gate_decision.payload
            if (
                previous_context_revision > 0
                and gate_decision.context_revision <= 0
                and not gate_decision.held
            ):
                # Context 信任门硬拒绝后，异步准备尚未产出等待模型的短窗口内也不能
                # 继续展示或上报上一英雄的 READY 数字。清空当前呈现态并让下方首帧
                # shell 立即覆盖旧 Canvas；同身份的短暂可信 hold 仍保留 last-good。
                preparation.invalidate()
                stage_runtime.reset_scope()
                slot_render_cache.reset()
                visibility["pinned_stats_scope"] = stage_runtime.pin.status()
                for key in (
                    "prepared_shell_key",
                    "render_semantic_key",
                    "rendered_selection_key",
                    "waiting_render_key",
                    "last_render_model",
                    "last_report_context",
                    "session_state",
                ):
                    visibility.pop(key, None)
            current_selection_key = selection_key(snapshot)
            shell_key = (current_selection_key, snapshot_slot_generations(snapshot))
            shell_drawn = False
            if (should_show and visibility.get("render_full_overlay")
                and event_source.get("selection_window_active") is True
                and shell_key != visibility.get("prepared_shell_key")):
                # 首帧先排入映射；后台准备的复制/锁和状态读取不能挡住轻量反馈。
                shell_model = slot_render_cache.build_shell(snapshot, current_selection_key=current_selection_key)
                present_overlay_model(canvas, config, visibility, snapshot, shell_model, shell=True)
                visibility["prepared_shell_key"] = shell_key
                _sync_event_visibility(root, config, visibility, snapshot,
                                       resolved_should_show=True, canvas=canvas)
                schedule_render(fast_poll_ms)
                shell_drawn = True
            request_key = None
            if should_show or str(event_source.get("session_id") or ""):
                try:
                    request_viewport = canvas_viewport(canvas, config, target_rect=visibility.get("target_rect"))
                except ValueError:
                    # 无窗口/暂停期不为1x1占位生成排版；真实显示路径仍严格拒绝坏几何。
                    if should_show:
                        raise
                else:
                    request_key = preparation.request(
                        snapshot, {**effective_context, "context_revision": gate_decision.context_revision},
                        completed=stage_runtime.completion_tracker.resolved_completed_count,
                        host_read_at=float(visibility["host_read_at"]),
                        viewport_size=request_viewport,
                        display_mode=str(visibility.get("display_mode") or "compact"),
                        input_timing={
                            **{name: float(visibility.get(name) or 0.0) for name in (
                                "host_read_at", "event_read_started_at", "event_read_completed_at",
                                "context_requested_at", "context_read_started_at", "context_read_completed_at",
                                "context_gate_evaluated_at", "context_confirmed_at",
                            )},
                            "first_event_read_started_at": input_sample.first_event_read_started_at,
                            "first_event_read_completed_at": input_sample.first_event_read_completed_at,
                        },
                    )
            else:
                preparation.invalidate()
                idle_refresh = getattr(preparation, "request_idle_refresh", None)
                if callable(idle_refresh):
                    idle_refresh()
            if shell_drawn:
                # The shell is mapped first. Queue cheap background work now, not
                # one tick later, but never replace this shell in the same callback.
                success = True
                return
            visibility["data_preparation_status"] = preparation.status()
            preparation_status = visibility["data_preparation_status"]
            metadata = preparation_status.get("generation") or {}
            session = str(event_source.get("session_id") or "")
            if session and metadata.get("game_session_id") == session:
                visibility["pinned_generation"] = dict(metadata)
                visibility["stats_generation_id"] = str(metadata.get("generation_id") or "")
                visibility["data_generation_id"] = visibility["stats_generation_id"]
            elif not session and preparation_status.get("bootstrap_generation_id"):
                visibility["stats_generation_id"] = preparation_status["bootstrap_generation_id"]
                visibility["data_generation_id"] = visibility["stats_generation_id"]
            if not should_show:
                # 数据准备已异步提交；隐藏 tick 不打开磁盘快照。
                preserve_selection = _preserve_selection_pins_while_hidden(snapshot)
                if not preserve_selection:
                    stage_runtime.reset_scope()
                    slot_render_cache.reset()
                    visibility["pinned_stats_scope"] = stage_runtime.pin.status()
                visibility.pop("prepared_shell_key", None)
                visibility.pop("render_semantic_key", None)
                visibility.pop("rendered_selection_key", None)
                visibility.pop("waiting_render_key", None)
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
                mark_canvas_drawn(visibility, event=snapshot)
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                    canvas=canvas,
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
                waiting_key = (waiting_reason, visibility.get("target_hwnd"),
                               tuple(visibility.get("target_rect") or ()), visibility.get("geometry_version"))
                if waiting_key != visibility.get("waiting_render_key"):
                    visibility["draw_started_at"] = time.time()
                    _draw_waiting_status(canvas, waiting_reason)
                    mark_canvas_drawn(visibility, event=snapshot)
                    visibility["waiting_render_key"] = waiting_key
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                    canvas=canvas,
                )
                _write_overlay_session_report(
                    snapshot,
                    None,
                    visibility,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
                success = True
                return
            visibility.pop("waiting_render_key", None)
            prepared = preparation.poll(request_key)
            if prepared is None:
                _sync_event_visibility(root, config, visibility, snapshot,
                                       resolved_should_show=True, canvas=canvas)
                _write_overlay_session_report(snapshot, visibility.get("last_render_model"), visibility,
                                              context=context, diagnostic=bool(config.get("diagnostic_mode")))
                success = True
                return
            pinned_generation = dict(prepared.generation)
            pinned_stats_status = dict(prepared.scope)
            stats_scope_key = prepared.scope_key
            visibility["pinned_generation"] = pinned_generation
            visibility["pinned_stats_scope"] = pinned_stats_status
            stats_generation_id = str(pinned_generation.get("stats_generation_id") or "")
            visibility["stats_generation_id"] = stats_generation_id
            visibility["data_generation_id"] = stats_generation_id
            data_notice_key = prepared.content_key
            viewport = request_viewport
            display_context = visibility.get("display_context")
            display_context = display_context if isinstance(display_context, Mapping) else {}
            semantic_key = render_semantic_key(
                snapshot,
                context_revision=gate_decision.context_revision,
                generation_id=stats_generation_id,
                display_mode=str(visibility.get("display_mode") or "compact"),
                viewport=viewport,
                stats_scope_key=stats_scope_key,
                data_notice_key=data_notice_key,
                display_context_key=(
                    int(visibility.get("target_hwnd") or 0),
                    tuple(visibility.get("target_rect") or ()),
                    str(display_context.get("monitor_device") or ""),
                    display_context.get("dpi_scale", 1.0),
                ),
            )
            if semantic_key == visibility.get("render_semantic_key"):
                _sync_event_visibility(
                    root,
                    config,
                    visibility,
                    snapshot,
                    resolved_should_show=True,
                    canvas=canvas,
                )
                cached_model = visibility.get("last_render_model")
                cached_state = visibility.get("session_state")
                cached_context = visibility.get("last_report_context")
                _write_overlay_session_report(
                    snapshot,
                    cached_model if isinstance(cached_model, Mapping) else None,
                    visibility,
                    context=cached_context if isinstance(cached_context, Mapping) else None,
                    state=cached_state if isinstance(cached_state, GameSessionState) else None,
                    diagnostic=bool(config.get("diagnostic_mode")),
                )
                if (
                    isinstance(cached_model, Mapping)
                    and isinstance(cached_state, GameSessionState)
                ):
                    try:
                        _write_real_session_evidence(
                            root,
                            cached_state,
                            snapshot,
                            cached_model,
                            visibility,
                            diagnostic=bool(config.get("diagnostic_mode")),
                        )
                    except Exception:
                        logger.warning("写入真实会话验收证据失败。", exc_info=True)
                success = True
                return

            session_state = prepared.state
            model = prepared.model
            visibility["render_event_host_read_at"] = prepared.host_read_at
            visibility["render_event_input_timing"] = {
                **dict(getattr(prepared, "input_timing", None) or {}), "preparation_consumed_at": time.time(),
            }
            model = slot_render_cache.merge(
                snapshot,
                model,
                current_selection_key=current_selection_key,
                context_revision=gate_decision.context_revision,
                generation_id=stats_generation_id,
                stats_scope_key=stats_scope_key,
            )
            visibility["session_state"] = session_state
            _log_waiting_context_diagnostic(visibility, snapshot, context, model)
            present_overlay_model(
                canvas,
                config,
                visibility,
                prepared.event,
                model,
                ready_frame=prepared.phase == "ready" and _snapshot_has_complete_ready_slots(snapshot),
            )
            visibility["rendered_selection_key"] = current_selection_key
            visibility["render_semantic_key"] = semantic_key
            visibility["last_render_model"] = model
            visibility["last_report_context"] = dict(context) if isinstance(context, Mapping) else {}
            _sync_event_visibility(
                root,
                config,
                visibility,
                snapshot,
                    resolved_should_show=True,
                    canvas=canvas,
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
            delay_ms = retry_delay_ms()
            observer.set_poll_ms(delay_ms)
            schedule_render(delay_ms)

    visibility["presentation_state_changed"] = request_render
    render_once()
    return request_render


def run_overlay_host(*, diagnostic: bool = False) -> None:
    """持有共享运行态独占锁后启动 overlay，重复实例不创建 Tk 窗口。"""
    with overlay_instance_lock(HOST_INSTANCE_LOCK_FILE) as acquired:
        if not acquired:
            logger.warning("game_overlay host 已有运行实例，本实例退出。")
            return
        _run_overlay_host_locked(diagnostic=diagnostic)


def _run_overlay_host_locked(*, diagnostic: bool = False) -> None:
    data_source = SharedOverlayDataSource()
    initial_hint_cache = None
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
        "data_generation_id": str(
            (
                initial_hint_cache.get("snapshot")
                if isinstance(initial_hint_cache, Mapping)
                and isinstance(initial_hint_cache.get("snapshot"), Mapping)
                else {}
            ).get("generation_id")
            or ""
        ),
        "stats_generation_id": str(
            (
                initial_hint_cache.get("snapshot")
                if isinstance(initial_hint_cache, Mapping)
                and isinstance(initial_hint_cache.get("snapshot"), Mapping)
                else {}
            ).get("generation_id")
            or ""
        ),
        "vision_pool_generation_id": "",
    }
    report_writer = OverlayReportWriter(
        get_var_dir() / "reports" / "overlay_sessions",
        Path(overlay_runtime_state_path("session_evidence")),
        visibility_path=GAME_OVERLAY_VISIBILITY_FILE,
    )
    report_writer.start()
    visibility["report_writer"] = report_writer
    hotkey_queue: "queue.Queue[str]" = queue.Queue()
    hotkey_controller: HotkeyController | None = None
    gameflow_poller = GameflowPoller()
    window_target_poller = WindowTargetPoller(titles, initial_target=initial_target)
    foreground_event = threading.Event()
    foreground_hook: ForegroundEventHook | None = None
    visibility["gameflow_poller"] = gameflow_poller
    visibility["window_target_poller"] = window_target_poller

    root.update_idletasks()
    _ensure_overlay_window_styles(root, config)
    visibility["capture_exclusion"] = _ensure_overlay_capture_exclusion(root)
    if visibility["capture_exclusion"].get("status") != "applied":
        logger.error(
            "Overlay 捕获排除未生效，保持 fail-closed：%s",
            visibility["capture_exclusion"].get("reason") or "unknown",
        )
    gameflow_poller.start()
    window_target_poller.start()
    hotkey_controller = _start_hotkey_thread(hotkey_queue)
    preparation = OverlayDataPreparation(data_source)
    preparation.warmup()
    request_overlay_render = _schedule_event_render(
        root,
        canvas,
        config,
        visibility,
        hotkey_queue,
        data_source=data_source,
        initial_hint_cache=initial_hint_cache,
        data_preparation=preparation,
    )
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
        observer = visibility.get("input_observer")
        if isinstance(observer, HostInputObserver):
            observer.close(timeout=2.0)
        preparation = visibility.get("data_preparation")
        if isinstance(preparation, OverlayDataPreparation):
            preparation.close(timeout=2.0)
        report_writer.close(timeout=5.0)


def run_self_check() -> dict[str, Any]:
    from hextech.interfaces.overlay.host_self_check import run_self_check as _run_self_check

    return _run_self_check()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech game overlay host。")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic", action="store_true", help="记录去重诊断日志，不绘制状态 UI。")
    parser.add_argument("--self-check", action="store_true", help="执行无 GUI overlay 入口自检后退出。")
    parser.add_argument("--presentation-smoke", action="store_true", help=argparse.SUPPRESS)
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
    if args.presentation_smoke:
        result = run_presentation_smoke()
        from hextech.modules.session.process_bootstrap import publish_process_bootstrap

        publish_process_bootstrap(result)
        return 0 if result["ok"] else 1
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

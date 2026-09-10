"""Overlay Vision Sidecar 运行器。"""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.vision.events import write_overlay_event
from hextech.modules.vision import instance_lock as _instance_lock
from hextech.modules.data.overlay_source import SharedOverlayDataSource
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.infrastructure.vision.failure_evidence import FailureEvidenceCollector, FailureEvidenceWriter
from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter
from hextech.infrastructure.vision.roi_diagnostic_writer import RoiDiagnosticWriter
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.vision.window import game_window_identity, window_display_context
from hextech.modules.session.build_identity import current_build_id
from hextech.modules.vision.gameflow import probe_gameflow_state
from hextech.infrastructure.vision.template_runtime import load_or_build_default_template_runtime
from hextech.infrastructure.vision.gameflow_pause import PausedGameflowProbe, pause_identity, resolve_game_visibility_pause
from hextech.infrastructure.vision.sidecar_diagnostics import DiagnosticEpochSampler as _DiagnosticEpochSampler
from hextech.infrastructure.vision.sidecar_matching import VisionComputeMemoryError, prepare_compute_rank_matrices
from hextech.infrastructure.vision.ocr_shadow import OcrShadowRuntime
from hextech.infrastructure.vision.mouse_transition import MouseTransitionObserver
from hextech.infrastructure.vision.held_scene import HeldSceneEvidence
from hextech.infrastructure.vision.frame_pipeline import process_captured_frame
from hextech.infrastructure.vision.diagnostic_capture_control import ExplicitCaptureControl
from hextech.infrastructure.vision.scene_recovery import SceneRecoveryReference
from hextech.infrastructure.vision import sidecar_status as _status
from hextech.infrastructure.vision.runner_lifecycle import run_loop  # noqa: F401 - public facade
from hextech.infrastructure.vision.runner_helpers import (
    stable_slot_fingerprints as _stable_slot_fingerprints,  # noqa: F401 - compatibility
    captured_window_still_current,
    held_request_for_frame,
    roi_dump_signature,
    next_held_scene_evidence,
    recovery_capture_for_frame,
    advance_recovery_event,
    current_snapshot_generation_id as _current_snapshot_generation_id,
    game_window_mode_pause_reason,
    game_window_mode_payload,
    mutable_string_key_mapping as _mutable_string_key_mapping,
    sanitize_bootstrap_error_message as _sanitize_bootstrap_error_message,  # noqa: F401 - public facade
)

logger = logging.getLogger(__name__)
DEFAULT_MIN_CONFIDENCE = 0.80
DEFAULT_LOOP_FRAME_INTERVAL_MS = 80
DEFAULT_LOOP_SCAN_FRAME_INTERVAL_MS = 160
DEFAULT_LOOP_IDLE_INTERVAL_SECONDS = 0.25
DEFAULT_LOOP_FAST_HOLD_SECONDS = 1.2
DEFAULT_LOOP_HEARTBEAT_SECONDS = 1.0
PERSISTENT_CAPTURE_FAILURE_SECONDS = 3.0

SIDECAR_STATUS_FILE = _status.STATUS_FILE
SIDECAR_READY_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_FILE"
SIDECAR_READY_TOKEN_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_TOKEN"
SIDECAR_BOOTSTRAP_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_BOOTSTRAP_FILE"
SIDECAR_GENERATION_ENV = _status.GENERATION_ENV
SIDECAR_EXIT_FILE_ENV = "HEXTECH_OVERLAY_EXIT_FILE"
VISION_TARGET_GENERATION_ENV = "HEXTECH_VISION_TARGET_GENERATION_ID"
SIDECAR_INSTANCE_LOCK_FILE = get_var_dir() / "locks" / "game_overlay_sidecar.lock"
_SIDECAR_DEBUG_DUMP_ENABLED = False
overlay_instance_lock = _instance_lock.overlay_instance_lock
_publish_runtime_fields = _status.publish_runtime_fields
SIDECAR_PID_STARTED_AT = _status.PID_STARTED_AT


def _write_sidecar_status(status: str, **fields: Any) -> None:
    """写入 sidecar 分阶段状态；失败只影响诊断，不阻断识别循环。"""
    try:
        _status.debug_dump_enabled = _SIDECAR_DEBUG_DUMP_ENABLED
        _status.STATUS_FILE = SIDECAR_STATUS_FILE
        _status.write_status(status, **fields)
    except OSError:
        logger.debug("写入 Vision sidecar 状态失败。", exc_info=True)


def _write_sidecar_bootstrap_from_env(state: str, **fields: Any) -> None:
    target_value = str(os.environ.get(SIDECAR_BOOTSTRAP_FILE_ENV) or "").strip()
    if not target_value:
        return
    payload = {
        "schema_version": 1,
        "build_id": current_build_id(),
        "state": str(state),
        "phase": str(fields.pop("phase", state) or state),
        "pid": os.getpid(),
        "token": str(os.environ.get(SIDECAR_READY_TOKEN_ENV) or ""),
        "generation": str(os.environ.get(SIDECAR_GENERATION_ENV) or ""),
        "updated_at": time.time(),
    }
    payload.update(fields)
    try:
        atomic_write_json(Path(target_value), payload, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("写入 Vision sidecar bootstrap 状态失败。", exc_info=True)


def _write_sidecar_ready_from_env(
    *,
    template_count: int,
    started_at: float,
    startup_profile: Mapping[str, Any] | None = None,
) -> None:
    """向父进程报告冷启动完成；ready 前 watchdog 不应按 trace stale 杀进程。"""

    startup_seconds = round(max(0.0, time.perf_counter() - started_at), 3)
    profile = dict(startup_profile or {})
    _publish_runtime_fields(profile)
    _write_sidecar_status(
        "running",
        phase="ready",
        template_count=int(template_count),
        startup_seconds=startup_seconds,
        startup_profile=profile,
        compute_profile=str(profile.get("compute_profile") or ""),
        compute_matrix_bytes=int(profile.get("compute_matrix_bytes") or 0),
        compute_warmup_seconds=float(profile.get("compute_warmup_seconds") or 0.0),
    )
    _write_sidecar_bootstrap_from_env(
        "ready",
        phase="ready",
        template_count=int(template_count),
        startup_seconds=startup_seconds,
        startup_profile=profile,
    )
    ready_path = str(os.environ.get(SIDECAR_READY_FILE_ENV) or "").strip()
    if not ready_path:
        return
    payload = {
        "pid": os.getpid(),
        "token": str(os.environ.get(SIDECAR_READY_TOKEN_ENV) or ""),
        "generation": str(os.environ.get("HEXTECH_OVERLAY_GENERATION") or ""),
        "template_count": int(template_count),
        "ready_at": time.time(),
        "startup_seconds": startup_seconds,
        "startup_profile": profile,
    }
    atomic_write_json(Path(ready_path), payload, ensure_ascii=False, indent=2)


def _sidecar_exit_requested() -> bool:
    """检查父进程写入的 graceful exit 文件。"""

    exit_path = str(os.environ.get(SIDECAR_EXIT_FILE_ENV) or "").strip()
    if not exit_path:
        return False
    return Path(exit_path).exists()


def _prepare_compute_runtime(runtime: Any) -> None:
    """ready 前建立 FP32 热计算镜像；内存失败必须显式终止 Sidecar。"""

    _write_sidecar_status("starting", phase="compute_matrix_warmup")
    try:
        profile = prepare_compute_rank_matrices(runtime.template_index)
    except VisionComputeMemoryError:
        _write_sidecar_status(
            "failed",
            phase="compute_matrix_warmup",
            error_code="vision_compute_memory_unavailable",
        )
        _write_sidecar_bootstrap_from_env(
            "failed",
            phase="compute_matrix_warmup",
            error_code="vision_compute_memory_unavailable",
        )
        raise
    if isinstance(runtime.stats, dict):
        runtime.stats.update(profile)


def run_once(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_frames: int = 2,
    frame_interval_ms: int = 80,
    debug_dump_dir: str | Path | None = None,
) -> dict[str, Any]:
    """执行一次短窗口识别；无 LoL 窗口时写入 inactive 诊断事件。"""

    from hextech.infrastructure.vision.runner_once import run_once_impl

    return run_once_impl(
        preset=preset,
        write_event=write_event,
        event_path=event_path,
        min_confidence=min_confidence,
        required_frames=required_frames,
        frame_interval_ms=frame_interval_ms,
        debug_dump_dir=debug_dump_dir,
        write_status=_write_sidecar_status,
        publish_runtime_fields=_publish_runtime_fields,
        prepare_compute_runtime=_prepare_compute_runtime,
    )


def _run_loop_impl(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    required_frames: int = 2,
    frame_interval_ms: int = DEFAULT_LOOP_FRAME_INTERVAL_MS,
    idle_interval_seconds: float = DEFAULT_LOOP_IDLE_INTERVAL_SECONDS,
    heartbeat_seconds: float = DEFAULT_LOOP_HEARTBEAT_SECONDS,
    debug_dump_dir: str | Path | None = None,
    scan_frame_interval_ms: int = DEFAULT_LOOP_SCAN_FRAME_INTERVAL_MS,
    fast_hold_seconds: float = DEFAULT_LOOP_FAST_HOLD_SECONDS,
    failure_writer: FailureEvidenceWriter | None = None,
    diagnostic_writer: VisionDiagnosticWriter | None = None,
    roi_diagnostic_writer: RoiDiagnosticWriter | None = None,
    retention_worker: Any | None = None,
    ocr_shadow_sink: list[OcrShadowRuntime] | None = None,
    mouse_transition_observer: MouseTransitionObserver | None = None,
) -> dict[str, Any] | None:
    """常驻 V2 视觉循环；场景和槽位分别稳定，非前台时低频待机。"""
    from hextech.infrastructure.vision import sidecar as vision_sidecar_module

    vision_sidecar: Any = vision_sidecar_module

    started_at = time.perf_counter()
    vision_sidecar._set_dpi_awareness()
    _write_sidecar_status("starting", phase="hint_cache_load")
    data_source = SharedOverlayDataSource(
        generation_id=str(os.environ.get(VISION_TARGET_GENERATION_ENV) or "")
    )
    hint_cache = data_source.read_hint_cache()
    runtime = load_or_build_default_template_runtime(
        hint_cache=hint_cache,
        require_production_pool=True,
        status_callback=lambda phase, fields: _write_sidecar_status(
            "starting",
            phase=phase,
            startup_seconds=round(time.perf_counter() - started_at, 3),
            **dict(fields),
        ),
    )
    _publish_runtime_fields(runtime.stats)
    template_index = runtime.template_index
    try:
        _prepare_compute_runtime(runtime)
    except VisionComputeMemoryError:
        event = vision_sidecar._build_loop_inactive_event(
            "vision_compute_memory_unavailable",
            poll_mode="idle",
        )
        if write_event:
            write_overlay_event(vision_sidecar._public_event_payload(event), event_path)
        return event
    trace_path = vision_sidecar._vision_trace_path_for_event(event_path)
    tracker = SelectionTracker(scene_enter_frames=max(1, int(required_frames)))
    mouse_observer = mouse_transition_observer
    failure_collector = (
        FailureEvidenceCollector(
            failure_writer,
            pool_id=str(runtime.stats.get("production_pool_id") or "production-pool-unavailable"),
        )
        if failure_writer is not None
        else None
    )

    if not template_index:
        event = vision_sidecar._build_loop_inactive_event("template_missing", poll_mode="idle")
        if write_event:
            write_overlay_event(vision_sidecar._public_event_payload(event), event_path)
        if diagnostic_writer is not None:
            diagnostic_writer.submit(event, trace_path, write_trace=write_event)
        logger.error("Vision sidecar 模板缺失，已退出。")
        return event

    ocr_shadow = OcrShadowRuntime.from_environment(template_index)
    if ocr_shadow_sink is not None:
        ocr_shadow_sink.append(ocr_shadow)

    last_signature: tuple[str, ...] | None = None
    last_write_at = 0.0
    last_status_heartbeat_at = 0.0
    idle_sleep_seconds = max(0.12, float(idle_interval_seconds))
    scan_frame_interval_ms = max(int(frame_interval_ms), int(scan_frame_interval_ms))
    fast_hold_seconds = max(0.0, float(fast_hold_seconds))
    fast_poll_until = 0.0
    dump_root = Path(debug_dump_dir) if debug_dump_dir else None
    last_dump_signature: tuple[str, ...] | None = None
    diagnostic_sampler = _DiagnosticEpochSampler()
    tab_was_down = False
    left_mouse_was_down = False
    active_hwnd = 0
    frame_id = 0
    game_session_id = ""
    current_game_identity: dict[str, object] = {}
    capture_unavailable_started_at = 0.0
    paused_gameflow = PausedGameflowProbe(probe=probe_gameflow_state)
    held_scene: HeldSceneEvidence | None = None
    scene_recovery: SceneRecoveryReference | None = None
    ocr_evidence_not_before = 0.0
    explicit_capture = ExplicitCaptureControl(get_var_dir(), roi_diagnostic_writer, current_build_id())
    def commit_event(event_payload: dict[str, Any], *, poll_mode: str, scene_only: bool = False) -> None:
        nonlocal last_signature, last_write_at, last_status_heartbeat_at, held_scene, scene_recovery
        source = _mutable_string_key_mapping(event_payload.get("source"))
        if (not tracker.scene_active or tracker.body_shard_latched
            or source.get("scene_state") in {"paused", "absent", "blocked"}
            or source.get("transient_pause")):
            held_scene = None
            scene_recovery = None
        source["poll_mode"] = poll_mode
        vision_pool_generation_id = str(
            runtime.stats.get("vision_pool_generation_id")
            or runtime.stats.get("data_generation_id")
            or ""
        )
        vision_pool_fingerprint = str(runtime.stats.get("vision_pool_fingerprint") or "")
        observed_data_generation_id = _current_snapshot_generation_id() or str(
            runtime.stats.get("observed_data_generation_id") or vision_pool_generation_id
        )
        source["vision_pool_generation_id"] = vision_pool_generation_id
        source["vision_pool_origin_generation_id"] = vision_pool_generation_id
        source["vision_pool_fingerprint"] = vision_pool_fingerprint
        source["observed_data_generation_id"] = observed_data_generation_id
        source.setdefault("data_generation_id", vision_pool_generation_id)
        source["generation_roles"] = {
            "vision_pool_generation_id": "sidecar_template_runtime",
            "stats_generation_id": "host_game_session",
            "data_generation_id": "legacy_vision_pool_compat",
        }
        source["build_id"] = current_build_id()
        source["sidecar_pid"] = os.getpid()
        source["sidecar_instance_id"] = _status.SIDECAR_INSTANCE_ID
        event_payload["source"] = source
        now = time.time()
        if write_event and vision_sidecar.should_write_loop_event(
            event_payload,
            last_signature=last_signature,
            last_write_at=last_write_at,
            now=now,
            heartbeat_seconds=heartbeat_seconds,
        ):
            write_overlay_event(vision_sidecar._public_event_payload(event_payload), event_path)
            last_signature = vision_sidecar._loop_event_signature(event_payload)
            last_write_at = now
        if scene_only:
            return
        explicit_capture.observe(None, event_payload, event_payload)
        if diagnostic_writer is not None:
            diagnostic_writer.submit(event_payload, trace_path, write_trace=write_event)
        if failure_writer is not None:
            for notification in failure_writer.drain_journal_events():
                logger.warning(
                    "failure evidence writer=%s slot=%s",
                    notification.get("detail"),
                    notification.get("slot_key"),
                )
        if now - last_status_heartbeat_at >= max(0.2, float(heartbeat_seconds)):
            diagnostic_status = (
                diagnostic_writer.status() if diagnostic_writer is not None else {}
            )
            _write_sidecar_status(
                "running",
                phase="loop",
                poll_mode=poll_mode,
                event_vision_pool_generation_id=vision_pool_generation_id,
                vision_pool_origin_generation_id=vision_pool_generation_id,
                vision_pool_fingerprint=vision_pool_fingerprint,
                observed_data_generation_id=observed_data_generation_id,
                compute_profile=str(source.get("compute_profile") or "float32_batched"),
                compute_matrix_bytes=int(runtime.stats.get("compute_matrix_bytes") or 0),
                compute_warmup_seconds=float(runtime.stats.get("compute_warmup_seconds") or 0.0),
                matching_timing=source.get("matching_timing")
                if isinstance(source.get("matching_timing"), Mapping)
                else {},
                ocr_shadow=ocr_shadow.status(),
                explicit_capture=explicit_capture.status(),
                mouse_transition=mouse_observer.status() if mouse_observer is not None else {},
                diagnostic_writer=diagnostic_status,
                current_timeline_path_hash=str(
                    diagnostic_status.get("current_timeline_path_hash") or ""
                ),
                timeline_last_written_at=float(
                    diagnostic_status.get("timeline_last_written_at") or 0.0
                ),
                timeline_epoch=int(diagnostic_status.get("timeline_epoch") or 0),
                timeline_failed_count=int(
                    diagnostic_status.get("timeline_failed_count") or 0
                ),
                roi_dump_writer=roi_diagnostic_writer.status()
                if roi_diagnostic_writer is not None
                else {},
                failure_evidence_writer=failure_writer.status()
                if failure_writer is not None
                else {},
                diagnostic_retention=retention_worker.status()
                if retention_worker is not None
                else {},
            )
            last_status_heartbeat_at = now
    def attach_window_observation(
        event_payload: dict[str, Any],
        *,
        hwnd: int,
        rect: tuple[int, int, int, int],
        capture_size: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """状态机只决定业务状态；runner 统一补回本帧真实窗口观测。"""

        source = _mutable_string_key_mapping(event_payload.get("source"))
        source.update(
            {
                "session_id": game_session_id,
                "game_instance_id": str(current_game_identity.get("game_instance_id") or game_session_id),
                "window_hwnd": int(hwnd),
                "window_process_id": int(current_game_identity.get("process_id") or 0),
                "window_process_started_at": float(current_game_identity.get("process_started_at") or 0.0),
                "identity_quality": str(current_game_identity.get("identity_quality") or "unavailable"),
                "game_window_mode": game_window_mode_payload(current_game_identity),
                "client_rect": [int(value) for value in rect],
                "capture_size": [int(value) for value in capture_size] if capture_size else [],
                "dpi_scale": vision_sidecar._window_dpi_scale(hwnd),
            }
        )
        event_payload["source"] = source
        return event_payload
    def foreground_sleep_seconds(event_payload: Mapping[str, Any], *, elapsed_seconds: float) -> tuple[str, float]:
        nonlocal fast_poll_until
        now = time.monotonic()
        if vision_sidecar.loop_event_needs_fast_poll(event_payload):
            fast_poll_until = max(fast_poll_until, now + fast_hold_seconds)
        poll_mode = vision_sidecar.resolve_loop_poll_mode(event_payload, fast_until=fast_poll_until, now=now)
        interval_ms = frame_interval_ms if poll_mode == "fast" else scan_frame_interval_ms
        return poll_mode, vision_sidecar.remaining_frame_sleep_seconds(interval_ms, elapsed_seconds=elapsed_seconds)

    def maybe_dump(frame, event_payload: Mapping[str, Any]) -> None:
        nonlocal last_dump_signature
        if dump_root is None:
            return
        source = _mutable_string_key_mapping(event_payload.get("source"))
        signature = roi_dump_signature(event_payload)
        observation_seq = diagnostic_sampler.next_observation_seq(source)
        sequential_observation = observation_seq is not None
        should_dump = bool(
            sequential_observation
            or signature != last_dump_signature
            and (
                source.get("scoreboard_key_down")
                or source.get("scene_state") in {"active", "candidate"}
                or int(source.get("ready_slots") or 0) < vision_sidecar.SLOT_COUNT
            )
        )
        if should_dump and roi_diagnostic_writer is not None:
            roi_diagnostic_writer.submit(
                dump_root,
                frame,
                event_payload,
                observation_seq=observation_seq,
            )
            last_dump_signature = signature

    logger.info(
        "Vision sidecar V2 已启动：fast_frame_interval_ms=%s scan_frame_interval_ms=%s heartbeat_seconds=%.1f",
        int(frame_interval_ms),
        int(scan_frame_interval_ms),
        float(heartbeat_seconds),
    )
    _write_sidecar_ready_from_env(
        template_count=len(template_index),
        started_at=started_at,
        startup_profile=runtime.stats,
    )

    while True:
        if _sidecar_exit_requested():
            logger.info("Vision sidecar 收到 graceful exit 信号，准备退出。")
            return None
        frame_started_at = time.perf_counter()
        explicit_capture.poll()
        target = vision_sidecar._find_lol_game_window()
        if target is None:
            capture_unavailable_started_at = 0.0
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
            if mouse_observer is not None:
                mouse_observer.update_context(
                    window_hwnd=0,
                    game_instance_id="",
                    selection_epoch=0,
                    eligible=False,
                )
            event, gameflow_ended = resolve_game_visibility_pause(
                tracker,
                reason="game_window_missing",
                should_probe=bool(tracker.scene_active or active_hwnd or game_session_id),
                now=time.monotonic(),
                gameflow_probe=paused_gameflow,
                identity_source=pause_identity(game_session_id, current_game_identity),
            )
            if gameflow_ended:
                active_hwnd = 0
                game_session_id = ""
                current_game_identity = {}
                paused_gameflow.reset()
            vision_sidecar.attach_visibility_probe_timing(event)
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue

        hwnd, rect = target
        observed_identity = game_window_identity(int(hwnd))
        observed_game_instance = str(observed_identity.get("game_instance_id") or "")
        if active_hwnd == 0 or (
            observed_game_instance
            and game_session_id
            and observed_game_instance != game_session_id
        ):
            tracker.reset()
            if mouse_observer is not None:
                mouse_observer.clear()
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
        active_hwnd = int(hwnd)
        if observed_game_instance:
            game_session_id = observed_game_instance
        current_game_identity = observed_identity
        window_mode_pause_reason = game_window_mode_pause_reason(observed_identity)
        if window_mode_pause_reason:
            capture_unavailable_started_at = 0.0
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
            if mouse_observer is not None:
                mouse_observer.update_context(
                    window_hwnd=int(hwnd),
                    game_instance_id=game_session_id,
                    selection_epoch=tracker.epoch,
                    eligible=False,
                )
            event = attach_window_observation(
                tracker.pause(window_mode_pause_reason),
                hwnd=hwnd,
                rect=rect,
            )
            vision_sidecar.attach_visibility_probe_timing(event)
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue
        if not vision_sidecar._is_lol_game_foreground(hwnd):
            capture_unavailable_started_at = 0.0
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
            if mouse_observer is not None:
                mouse_observer.update_context(
                    window_hwnd=int(hwnd),
                    game_instance_id=game_session_id,
                    selection_epoch=tracker.epoch,
                    eligible=False,
                )
            event, gameflow_ended = resolve_game_visibility_pause(
                tracker,
                reason="game_not_foreground",
                should_probe=bool(tracker.scene_active or active_hwnd or game_session_id),
                now=time.monotonic(),
                gameflow_probe=paused_gameflow,
                identity_source=pause_identity(game_session_id, current_game_identity),
            )
            event = attach_window_observation(event, hwnd=hwnd, rect=rect)
            if gameflow_ended:
                active_hwnd = 0
                game_session_id = ""
                current_game_identity = {}
                paused_gameflow.reset()
            vision_sidecar.attach_visibility_probe_timing(event)
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue

        paused_gameflow.reset()

        tab_down = vision_sidecar.is_scoreboard_key_down()
        if tab_down:
            capture_unavailable_started_at = 0.0
            if mouse_observer is not None:
                mouse_observer.update_context(
                    window_hwnd=int(hwnd),
                    game_instance_id=game_session_id,
                    selection_epoch=tracker.epoch,
                    eligible=False,
                )
            event = attach_window_observation(
                tracker.pause("scoreboard_key_down", scoreboard_key_down=True), hwnd=hwnd, rect=rect
            )
            if dump_root is not None and not tab_was_down:
                frame = vision_sidecar._capture_lol_game_rect(rect)
                if frame is not None:
                    maybe_dump(frame, event)
            tab_was_down = True
            vision_sidecar.attach_visibility_probe_timing(event)
            poll_mode, sleep_seconds = foreground_sleep_seconds(
                event,
                elapsed_seconds=time.perf_counter() - frame_started_at,
            )
            commit_event(event, poll_mode=poll_mode)
            time.sleep(sleep_seconds)
            continue
        tab_was_down = False
        if mouse_observer is not None:
            mouse_observer.update_context(
                window_hwnd=int(hwnd),
                game_instance_id=game_session_id,
                selection_epoch=tracker.epoch,
                eligible=bool(tracker.scene_active and tracker.epoch > 0),
            )

        dpi_scale = vision_sidecar._window_dpi_scale(hwnd)
        capture_binding, scene_recovery, recovery_full_capture = recovery_capture_for_frame(
            scene_recovery, tracker, game_session_id, hwnd, rect, dpi_scale, now=time.monotonic()
        )
        capture_started_at = time.time()
        capture_display = window_display_context(int(hwnd)) if explicit_capture.session is not None else {}
        frame = vision_sidecar._capture_lol_game_rect(
            rect,
            preset_name=preset,
            force_full_client=recovery_full_capture or explicit_capture.wants_full_client(),
        )
        captured_at = time.time()
        if frame is not None and capture_display:
            frame.info["hextech_monitor_device"] = str(capture_display.get("monitor_device") or "")
        if frame is not None and not captured_window_still_current(
            frame, hwnd=int(hwnd), client_rect=tuple(rect), game_instance_id=game_session_id,
        ):
            held_scene = None
            event = attach_window_observation(tracker.pause("capture_binding_changed"), hwnd=hwnd, rect=rect)
            event["timing"] = {"observation_kind": "capture_failure", "capture_status": "binding_changed",
                               "capture_started_at": capture_started_at, "captured_at": captured_at,
                               "recognition_completed_at": captured_at}
            poll_mode, sleep_seconds = foreground_sleep_seconds(event, elapsed_seconds=time.perf_counter()-frame_started_at)
            commit_event(event, poll_mode=poll_mode)
            time.sleep(sleep_seconds)
            continue
        if frame is None:
            if capture_unavailable_started_at <= 0.0:
                capture_unavailable_started_at = captured_at
            if captured_at - capture_unavailable_started_at >= PERSISTENT_CAPTURE_FAILURE_SECONDS:
                event = tracker.block("capture_unavailable")
                event["error"] = "capture_unavailable"
                event["source"]["failure_kind"] = "capture_persistent"
            else:
                event = tracker.pause("capture_unavailable")
            event = attach_window_observation(event, hwnd=hwnd, rect=rect)
            event["timing"] = {
                "observation_kind": "capture_failure",
                "capture_status": "unavailable",
            }
        else:
            expected_size = (int(rect[2] - rect[0]), int(rect[3] - rect[1]))
            if tuple(frame.size) != expected_size:
                if capture_unavailable_started_at <= 0.0:
                    capture_unavailable_started_at = captured_at
                if captured_at - capture_unavailable_started_at >= PERSISTENT_CAPTURE_FAILURE_SECONDS:
                    event = tracker.block("capture_client_size_mismatch")
                    event["error"] = "capture_client_size_mismatch"
                    event["source"]["failure_kind"] = "capture_client_size_mismatch_persistent"
                else:
                    event = tracker.pause("capture_client_size_mismatch")
                event = attach_window_observation(event, hwnd=hwnd, rect=rect, capture_size=frame.size)
                event["timing"] = {
                    "observation_kind": "capture_failure",
                    "capture_status": "invalid_size",
                    "capture_started_at": capture_started_at,
                    "captured_at": captured_at,
                    "recognition_completed_at": captured_at,
                }
                poll_mode, sleep_seconds = foreground_sleep_seconds(
                    event,
                    elapsed_seconds=time.perf_counter() - frame_started_at,
                )
                commit_event(event, poll_mode=poll_mode)
                time.sleep(sleep_seconds)
                continue
            capture_unavailable_started_at = 0.0
            frame_id += 1
            hold_request = held_request_for_frame(held_scene, tracker, capture_binding, frame.size,
                                                  vision_sidecar._cursor_over_card_slots)
            if hold_request is None:
                held_scene = None
            ocr_shadow.set_production_context(
                session_id=game_session_id,
                selection_epoch=capture_binding.selection_epoch,
                slot_generations=[max(1, track.slot_generation) for track in tracker.slots],
                captured_frame_id=frame_id,
                captured_at=captured_at,
            )
            raw_event, event, left_mouse_was_down = process_captured_frame(
                frame, template_index, sidecar=vision_sidecar, tracker=tracker, ocr=ocr_shadow,
                binding=capture_binding, frame_id=frame_id, capture_started_at=capture_started_at,
                captured_at=captured_at, preset=preset, min_confidence=min_confidence, held_scene=hold_request,
                mouse_observer=mouse_observer, left_mouse_was_down=left_mouse_was_down,
                minimum_captured_at=ocr_evidence_not_before,
                publish_scene=lambda feedback: commit_event(feedback, poll_mode="fast", scene_only=True))
            recognition_completed_at = float(raw_event["timing"]["recognition_completed_at"])
            scene_recovery, allow_held, cutoff = advance_recovery_event(
                scene_recovery, hold_request, raw_event, event, tracker, capture_binding,
                now=time.monotonic(), full_capture_used=recovery_full_capture)
            ocr_evidence_not_before = max(ocr_evidence_not_before, cutoff)
            held_scene = next_held_scene_evidence(
                held_scene, raw_event, event, tracker, capture_binding, allow_confirmed=allow_held)
            attach_window_observation(event, hwnd=hwnd, rect=rect, capture_size=frame.size)
            explicit_capture.observe(frame, raw_event, event)
            if mouse_observer is not None:
                event_source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
                event_scene_state = str(event_source.get("scene_state") or "")
                mouse_observer.update_context(
                    window_hwnd=int(hwnd),
                    game_instance_id=game_session_id,
                    selection_epoch=int(event_source.get("selection_epoch") or tracker.epoch),
                    eligible=bool(
                        int(event_source.get("selection_epoch") or tracker.epoch) > 0
                        and event_scene_state in {"candidate", "active"}
                    ),
                )
            if failure_collector is not None:
                failure_collector.observe(
                    frame,
                    raw_event,
                    event,
                    slot_generations=[track.slot_generation for track in tracker.slots],
                )
            maybe_dump(frame, event)

        timing = event.get("timing") if isinstance(event.get("timing"), Mapping) else {}
        event["timing"] = {
            **{str(key): value for key, value in timing.items()},
            "capture_started_at": capture_started_at,
            "captured_at": captured_at,
            "recognition_completed_at": (
                recognition_completed_at if frame is not None else captured_at
            ),
        }

        poll_mode, sleep_seconds = foreground_sleep_seconds(
            event,
            elapsed_seconds=time.perf_counter() - frame_started_at,
        )
        commit_event(event, poll_mode=poll_mode)
        time.sleep(sleep_seconds)


def build_parser():
    from hextech.infrastructure.vision.runner_cli import build_parser as _build_parser
    return _build_parser()


def main(argv: list[str] | None = None) -> int:
    from hextech.infrastructure.vision.runner_cli import main as _main
    return _main(argv)

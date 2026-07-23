"""overlay vision sidecar 运行器。

本模块收口 CLI 参数解析、一次性/常驻运行、sidecar 状态写入和 bootstrap 诊断。
调用方: hextech.infrastructure.vision.sidecar 兼容入口。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, cast

import psutil

from hextech.modules.data.catalog.runtime_store import build_runtime_state_path
from hextech.modules.vision.events import build_overlay_event, write_overlay_event
from hextech.modules.data.overlay_source import SharedOverlayDataSource
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.vision.window import game_window_identity

from hextech.infrastructure.vision.template_runtime import load_or_build_default_template_runtime

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONFIDENCE = 0.80
DEFAULT_LOOP_FRAME_INTERVAL_MS = 80
DEFAULT_LOOP_SCAN_FRAME_INTERVAL_MS = 160
DEFAULT_LOOP_IDLE_INTERVAL_SECONDS = 0.25
DEFAULT_LOOP_FAST_HOLD_SECONDS = 1.2
DEFAULT_LOOP_HEARTBEAT_SECONDS = 1.0

SIDECAR_STATUS_FILE = Path(build_runtime_state_path("game_overlay_sidecar_status.json"))
SIDECAR_READY_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_FILE"
SIDECAR_READY_TOKEN_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_TOKEN"
SIDECAR_BOOTSTRAP_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_BOOTSTRAP_FILE"
SIDECAR_GENERATION_ENV = "HEXTECH_OVERLAY_GENERATION"
SIDECAR_EXIT_FILE_ENV = "HEXTECH_OVERLAY_EXIT_FILE"


def _sidecar_pid_started_at() -> float:
    """使用 OS 创建时间防止 Supervisor 把 PID 复用误判为原 sidecar。"""

    try:
        return float(psutil.Process(os.getpid()).create_time())
    except (psutil.Error, OSError):
        return time.time()


SIDECAR_PID_STARTED_AT = _sidecar_pid_started_at()


def _mutable_string_key_mapping(value: object) -> dict[str, Any]:
    """把外部事件字典收窄为可写 source，避免跨模块 Mapping 类型泄漏。"""

    if not isinstance(value, Mapping):
        return {}
    source = cast(Mapping[object, Any], value)
    return {str(key): item for key, item in source.items()}


def _write_sidecar_status(status: str, **fields: Any) -> None:
    """写入 sidecar 分阶段状态；失败只影响诊断，不阻断识别循环。"""

    payload = {
        "schema_version": 2,
        "status": status,
        "pid": os.getpid(),
        "pid_started_at": SIDECAR_PID_STARTED_AT,
        "heartbeat_at": time.time(),
        "generation": str(os.environ.get(SIDECAR_GENERATION_ENV) or ""),
        "updated_at": time.time(),
    }
    payload.update(fields)
    try:
        atomic_write_json(SIDECAR_STATUS_FILE, payload, ensure_ascii=False, indent=2)
    except OSError:
        logger.debug("写入 Vision sidecar 状态失败。", exc_info=True)


def _sanitize_bootstrap_error_message(value: object) -> str:
    """bootstrap 仅保留可诊断错误，不暴露本机用户目录。"""

    text = str(value or "").strip()
    home = str(Path.home())
    if home:
        text = text.replace(home, "<home>")
        text = text.replace(home.replace("\\", "/"), "<home>")
    return re.sub(r"(?i)\b[a-z]:[\\/][^\s；;,]+", "<path>", text)


def _write_sidecar_bootstrap_from_env(state: str, **fields: Any) -> None:
    target_value = str(os.environ.get(SIDECAR_BOOTSTRAP_FILE_ENV) or "").strip()
    if not target_value:
        return
    payload = {
        "schema_version": 1,
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
    _write_sidecar_status(
        "running",
        phase="ready",
        template_count=int(template_count),
        startup_seconds=startup_seconds,
        startup_profile=profile,
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

    from hextech.infrastructure.vision import sidecar as vision_sidecar_module

    vision_sidecar: Any = vision_sidecar_module

    started_at = time.perf_counter()
    vision_sidecar._set_dpi_awareness()
    _write_sidecar_status("starting", phase="hint_cache_load")
    data_source = SharedOverlayDataSource()
    hint_cache = data_source.read_hint_cache()
    runtime = load_or_build_default_template_runtime(
        hint_cache=hint_cache,
        status_callback=lambda phase, fields: _write_sidecar_status(
            "starting",
            phase=phase,
            startup_seconds=round(time.perf_counter() - started_at, 3),
            **dict(fields),
        ),
    )
    template_index = runtime.template_index
    tracker = SelectionTracker(scene_enter_frames=max(1, int(required_frames)))
    if not template_index:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "template_missing"})
    else:
        event = tracker.block("warming_up")
        for index in range(max(1, int(required_frames))):
            if vision_sidecar.is_scoreboard_key_down():
                event = tracker.block("scoreboard_key_down", scoreboard_key_down=True)
                break
            frame = vision_sidecar.capture_lol_game_frame()
            if frame is None:
                event = tracker.block("capture_unavailable")
                break
            raw_event = vision_sidecar.detect_overlay_choices(
                frame,
                template_index,
                preset_name=preset,
                min_confidence=min_confidence,
            )
            event = tracker.update(raw_event)
            if debug_dump_dir and index == 0:
                vision_sidecar._write_roi_diagnostic_dump(debug_dump_dir, frame, event)
            if index + 1 < max(1, int(required_frames)):
                time.sleep(max(0, int(frame_interval_ms)) / 1000.0)

    if write_event:
        write_overlay_event(vision_sidecar._public_event_payload(event), event_path)
        try:
            vision_sidecar.write_vision_trace_if_changed(event, vision_sidecar._vision_trace_path_for_event(event_path))
        except OSError:
            logger.debug("写入 Vision trace 失败。", exc_info=True)
    return event


def run_loop(
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
) -> dict[str, Any] | None:
    """常驻 V2 视觉循环；场景和槽位分别稳定，非前台时低频待机。"""

    from hextech.infrastructure.vision import sidecar as vision_sidecar_module

    vision_sidecar: Any = vision_sidecar_module

    started_at = time.perf_counter()
    vision_sidecar._set_dpi_awareness()
    _write_sidecar_status("starting", phase="hint_cache_load")
    data_source = SharedOverlayDataSource()
    hint_cache = data_source.read_hint_cache()
    runtime = load_or_build_default_template_runtime(
        hint_cache=hint_cache,
        status_callback=lambda phase, fields: _write_sidecar_status(
            "starting",
            phase=phase,
            startup_seconds=round(time.perf_counter() - started_at, 3),
            **dict(fields),
        ),
    )
    template_index = runtime.template_index
    trace_path = vision_sidecar._vision_trace_path_for_event(event_path)
    tracker = SelectionTracker(scene_enter_frames=max(1, int(required_frames)))

    def write_runtime_trace(event_payload: Mapping[str, Any]) -> None:
        if not write_event:
            return
        try:
            vision_sidecar.write_vision_trace_if_changed(event_payload, trace_path)
        except OSError:
            logger.debug("写入 Vision trace 失败。", exc_info=True)

    if not template_index:
        event = vision_sidecar._build_loop_inactive_event("template_missing", poll_mode="idle")
        if write_event:
            write_overlay_event(vision_sidecar._public_event_payload(event), event_path)
            write_runtime_trace(event)
        logger.error("Vision sidecar 模板缺失，已退出。")
        return event

    last_signature: tuple[str, ...] | None = None
    last_write_at = 0.0
    last_status_heartbeat_at = 0.0
    idle_sleep_seconds = max(0.12, float(idle_interval_seconds))
    scan_frame_interval_ms = max(int(frame_interval_ms), int(scan_frame_interval_ms))
    fast_hold_seconds = max(0.0, float(fast_hold_seconds))
    fast_poll_until = 0.0
    dump_root = Path(debug_dump_dir) if debug_dump_dir else None
    last_dump_signature: tuple[str, ...] | None = None
    tab_was_down = False
    left_mouse_was_down = False
    active_hwnd = 0
    game_session_id = ""
    current_game_identity: dict[str, object] = {}

    def commit_event(event_payload: dict[str, Any], *, poll_mode: str) -> None:
        nonlocal last_signature, last_write_at, last_status_heartbeat_at
        source = _mutable_string_key_mapping(event_payload.get("source"))
        source["poll_mode"] = poll_mode
        event_payload["source"] = source
        write_runtime_trace(event_payload)
        now = time.time()
        if now - last_status_heartbeat_at >= max(0.2, float(heartbeat_seconds)):
            _write_sidecar_status(
                "running",
                phase="loop",
                poll_mode=poll_mode,
                event_generation_id=str(source.get("generation_id") or ""),
            )
            last_status_heartbeat_at = now
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
        raw_slots_value = event_payload.get("_raw_slots")
        raw_slots = raw_slots_value if isinstance(raw_slots_value, list) else []
        top_ids: list[str] = []
        for slot in raw_slots[:vision_sidecar.SLOT_COUNT]:
            candidates = slot.get("top_candidates") if isinstance(slot, Mapping) and isinstance(slot.get("top_candidates"), list) else []
            top = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
            top_ids.append(str(top.get("augment_id") or top.get("name") or ""))
        signature = (
            str(source.get("scene_state") or ""),
            str(source.get("reason") or ""),
            str(source.get("ready_slots") or 0),
            str(source.get("scoreboard_key_down") or False),
            *top_ids,
        )
        should_dump = bool(
            signature != last_dump_signature
            and (
                source.get("scoreboard_key_down")
                or source.get("scene_state") in {"active", "candidate"}
                or int(source.get("ready_slots") or 0) < vision_sidecar.SLOT_COUNT
            )
        )
        if should_dump:
            try:
                vision_sidecar._write_roi_diagnostic_dump(dump_root, frame, event_payload)
            except OSError:
                logger.debug("V2 ROI 诊断转储失败。", exc_info=True)
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
        target = vision_sidecar._find_lol_game_window()
        if target is None:
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
            event = tracker.block("game_window_missing")
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue

        hwnd, rect = target
        observed_identity = game_window_identity(int(hwnd))
        observed_game_instance = str(observed_identity.get("game_instance_id") or "")
        if int(hwnd) != active_hwnd or observed_game_instance != game_session_id:
            tracker.reset()
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
            active_hwnd = int(hwnd)
            game_session_id = observed_game_instance
        current_game_identity = observed_identity
        if not vision_sidecar._is_lol_game_foreground(hwnd):
            left_mouse_was_down = vision_sidecar.is_left_mouse_button_down()
            event = attach_window_observation(tracker.block("game_not_foreground"), hwnd=hwnd, rect=rect)
            commit_event(event, poll_mode="idle")
            time.sleep(idle_sleep_seconds)
            continue

        tab_down = vision_sidecar.is_scoreboard_key_down()
        if tab_down:
            event = attach_window_observation(
                tracker.block("scoreboard_key_down", scoreboard_key_down=True), hwnd=hwnd, rect=rect
            )
            if dump_root is not None and not tab_was_down:
                frame = vision_sidecar._capture_lol_game_rect(rect)
                if frame is not None:
                    maybe_dump(frame, event)
            tab_was_down = True
            poll_mode, sleep_seconds = foreground_sleep_seconds(
                event,
                elapsed_seconds=time.perf_counter() - frame_started_at,
            )
            commit_event(event, poll_mode=poll_mode)
            time.sleep(sleep_seconds)
            continue
        tab_was_down = False

        frame = vision_sidecar._capture_lol_game_rect(rect)
        if frame is None:
            event = attach_window_observation(tracker.block("capture_unavailable"), hwnd=hwnd, rect=rect)
        else:
            expected_size = (int(rect[2] - rect[0]), int(rect[3] - rect[1]))
            if tuple(frame.size) != expected_size:
                event = tracker.block("capture_client_size_mismatch")
                attach_window_observation(event, hwnd=hwnd, rect=rect, capture_size=frame.size)
                poll_mode, sleep_seconds = foreground_sleep_seconds(
                    event,
                    elapsed_seconds=time.perf_counter() - frame_started_at,
                )
                commit_event(event, poll_mode=poll_mode)
                time.sleep(sleep_seconds)
                continue
            raw_event = vision_sidecar.detect_overlay_choices(
                frame,
                template_index,
                preset_name=preset,
                min_confidence=min_confidence,
            )
            raw_source = _mutable_string_key_mapping(raw_event.get("source"))
            raw_source.update(
                {
                    "session_id": game_session_id,
                    "window_hwnd": int(hwnd),
                    "client_rect": [int(value) for value in rect],
                    "capture_size": [int(value) for value in frame.size],
                    "dpi_scale": vision_sidecar._window_dpi_scale(hwnd),
                }
            )
            raw_source["cursor_over_cards"] = vision_sidecar._cursor_over_card_panels(
                rect,
                frame.size,
                raw_source,
            )
            left_mouse_down = vision_sidecar.is_left_mouse_button_down()
            raw_source["selection_click"] = bool(
                left_mouse_down
                and not left_mouse_was_down
                and raw_source["cursor_over_cards"]
            )
            left_mouse_was_down = left_mouse_down
            raw_event["source"] = raw_source
            event = tracker.update(raw_event)
            attach_window_observation(event, hwnd=hwnd, rect=rect, capture_size=frame.size)
            maybe_dump(frame, event)

        poll_mode, sleep_seconds = foreground_sleep_seconds(
            event,
            elapsed_seconds=time.perf_counter() - frame_started_at,
        )
        commit_event(event, poll_mode=poll_mode)
        time.sleep(sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    from hextech.infrastructure.vision import sidecar as vision_sidecar_module

    vision_sidecar: Any = vision_sidecar_module

    parser = argparse.ArgumentParser(description="Hextech overlay Vision sidecar。")
    parser.add_argument("--once", action="store_true", help="执行一次短窗口识别后退出。")
    parser.add_argument("--loop", action="store_true", help="常驻自门控识别循环；未指定 --once 时默认启用。")
    parser.add_argument("--preset", default="auto", help="ROI preset: auto, 1920x1080, 2560x1440, 2560x1600。")
    parser.add_argument("--write-event", action="store_true", help="把识别结果写入 overlay 事件文件。")
    parser.add_argument("--event-path", default="", help="调试用事件文件路径；默认写运行态 state。")
    parser.add_argument("--min-confidence", type=float, default=vision_sidecar.DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--required-frames", type=int, default=2)
    parser.add_argument("--frame-interval-ms", type=int, default=vision_sidecar.DEFAULT_LOOP_FRAME_INTERVAL_MS)
    parser.add_argument("--scan-frame-interval-ms", type=int, default=vision_sidecar.DEFAULT_LOOP_SCAN_FRAME_INTERVAL_MS)
    parser.add_argument("--idle-interval-ms", type=int, default=int(vision_sidecar.DEFAULT_LOOP_IDLE_INTERVAL_SECONDS * 1000))
    parser.add_argument("--fast-hold-ms", type=int, default=int(vision_sidecar.DEFAULT_LOOP_FAST_HOLD_SECONDS * 1000))
    parser.add_argument("--heartbeat-seconds", type=float, default=vision_sidecar.DEFAULT_LOOP_HEARTBEAT_SECONDS)
    parser.add_argument(
        "--debug-dump",
        default="",
        help="把单帧、ROI crop 和 top3 候选分数转储到该目录用于校准；--once 转储首帧，--loop 在每个选择窗口首帧自动转储。",
    )
    return parser


def _record_template_missing_failure(event: Mapping[str, Any] | None) -> bool:
    if not isinstance(event, Mapping):
        return False
    source = event.get("source")
    if not isinstance(source, Mapping) or source.get("reason") != "template_missing":
        return False
    _write_sidecar_bootstrap_from_env(
        "failed",
        phase="template_load",
        error_type="FileNotFoundError",
        error_message_sanitized="Vision sidecar 模板缺失：template_missing",
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _write_sidecar_bootstrap_from_env("starting", phase="argument_parsed")
    if args.once:
        try:
            event = run_once(
                preset=args.preset,
                write_event=args.write_event,
                event_path=args.event_path or None,
                min_confidence=args.min_confidence,
                required_frames=args.required_frames,
                frame_interval_ms=args.frame_interval_ms,
                debug_dump_dir=args.debug_dump or None,
            )
            print(json.dumps(event, ensure_ascii=False, indent=2))
            if _record_template_missing_failure(event):
                return 1
            return 0
        except Exception as exc:
            _write_sidecar_bootstrap_from_env(
                "failed",
                phase="run_once",
                error_type=exc.__class__.__name__,
                error_message_sanitized=_sanitize_bootstrap_error_message(exc),
            )
            return 1

    try:
        event = run_loop(
            preset=args.preset,
            write_event=args.write_event,
            event_path=args.event_path or None,
            min_confidence=args.min_confidence,
            required_frames=args.required_frames,
            frame_interval_ms=args.frame_interval_ms,
            idle_interval_seconds=max(0, int(args.idle_interval_ms)) / 1000.0,
            heartbeat_seconds=args.heartbeat_seconds,
            debug_dump_dir=args.debug_dump or None,
            scan_frame_interval_ms=args.scan_frame_interval_ms,
            fast_hold_seconds=max(0, int(args.fast_hold_ms)) / 1000.0,
        )
    except Exception as exc:
        _write_sidecar_bootstrap_from_env(
            "failed",
            phase="run_loop",
            error_type=exc.__class__.__name__,
            error_message_sanitized=_sanitize_bootstrap_error_message(exc),
        )
        return 1
    if event is None:
        return 0
    print(json.dumps(event, ensure_ascii=False, indent=2))
    if _record_template_missing_failure(event):
        return 1
    return 0

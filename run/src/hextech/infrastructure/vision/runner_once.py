"""Vision Sidecar 的一次性短窗口入口。"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.infrastructure.vision.sidecar_matching import VisionComputeMemoryError
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.infrastructure.vision.template_runtime import load_or_build_default_template_runtime
from hextech.modules.data.overlay_source import SharedOverlayDataSource
from hextech.modules.vision.events import build_overlay_event, write_overlay_event


logger = logging.getLogger(__name__)
VISION_TARGET_GENERATION_ENV = "HEXTECH_VISION_TARGET_GENERATION_ID"


def run_once_impl(
    *,
    preset: str,
    write_event: bool,
    event_path: str | Path | None,
    min_confidence: float,
    required_frames: int,
    frame_interval_ms: int,
    debug_dump_dir: str | Path | None,
    write_status: Callable[..., None],
    publish_runtime_fields: Callable[[Mapping[str, Any]], None],
    prepare_compute_runtime: Callable[[Any], None],
) -> dict[str, Any]:
    from hextech.infrastructure.vision import sidecar as vision_sidecar

    started_at = time.perf_counter()
    vision_sidecar._set_dpi_awareness()
    write_status("starting", phase="hint_cache_load")
    data_source = SharedOverlayDataSource(
        generation_id=str(os.environ.get(VISION_TARGET_GENERATION_ENV) or "")
    )
    hint_cache = data_source.read_hint_cache()
    runtime = load_or_build_default_template_runtime(
        hint_cache=hint_cache,
        require_production_pool=True,
        status_callback=lambda phase, fields: write_status(
            "starting",
            phase=phase,
            startup_seconds=round(time.perf_counter() - started_at, 3),
            **dict(fields),
        ),
    )
    publish_runtime_fields(runtime.stats)
    template_index = runtime.template_index
    try:
        prepare_compute_runtime(runtime)
    except VisionComputeMemoryError:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update({"reason": "vision_compute_memory_unavailable"})
        if write_event:
            write_overlay_event(vision_sidecar._public_event_payload(event), event_path)
        return event
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
            event = tracker.update(
                vision_sidecar.detect_overlay_choices(
                    frame,
                    template_index,
                    preset_name=preset,
                    min_confidence=min_confidence,
                )
            )
            if debug_dump_dir and index == 0:
                vision_sidecar._write_roi_diagnostic_dump(debug_dump_dir, frame, event)
            if index + 1 < max(1, int(required_frames)):
                time.sleep(max(0, int(frame_interval_ms)) / 1000.0)
    if write_event:
        write_overlay_event(vision_sidecar._public_event_payload(event), event_path)
        try:
            vision_sidecar.write_vision_trace_if_changed(
                event,
                vision_sidecar._vision_trace_path_for_event(event_path),
            )
        except OSError:
            logger.debug("写入 Vision trace 失败。", exc_info=True)
    write_status("stopped", phase="once_complete")
    return event


__all__ = ["run_once_impl"]

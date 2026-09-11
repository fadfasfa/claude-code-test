"""Vision Sidecar 常驻 writer 的异常安全生命周期。

本模块只组装 failure/timeline/ROI/OCR Shadow 后台资源并保证五秒内收口；具体识别
循环仍由 ``runner._run_loop_impl`` 拥有，便于测试继续注入窗口和时钟。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hextech.infrastructure.persistence.diagnostic_retention import get_diagnostic_retention_worker


def run_loop(
    *,
    preset: str = "auto",
    write_event: bool = False,
    event_path: str | Path | None = None,
    min_confidence: float = 0.80,
    required_frames: int = 2,
    frame_interval_ms: int = 80,
    idle_interval_seconds: float = 0.25,
    heartbeat_seconds: float = 1.0,
    debug_dump_dir: str | Path | None = None,
    scan_frame_interval_ms: int = 160,
    fast_hold_seconds: float = 1.2,
) -> dict[str, Any] | None:
    from hextech.infrastructure.vision import runner
    from hextech.infrastructure.vision import sidecar as vision_sidecar
    from hextech.infrastructure.vision.mouse_transition import MouseTransitionObserver

    retention_worker = get_diagnostic_retention_worker()
    retention_worker.request()
    failure_writer = (
        runner.FailureEvidenceWriter(retention_worker=retention_worker)
        if write_event
        else None
    )
    diagnostic_writer = runner.VisionDiagnosticWriter(
        trace_writer=vision_sidecar.write_vision_trace_if_changed,
        timeline_writer=vision_sidecar.write_selection_timeline_observation,
        retention_worker=retention_worker,
    )
    roi_writer = (
        runner.RoiDiagnosticWriter(
            dump_writer=vision_sidecar._write_roi_diagnostic_dump,
            retention_worker=retention_worker,
        )
    )
    ocr_runtimes: list[Any] = []
    mouse_observer = MouseTransitionObserver()
    mouse_observer.start()
    try:
        return runner._run_loop_impl(
            preset=preset,
            write_event=write_event,
            event_path=event_path,
            min_confidence=min_confidence,
            required_frames=required_frames,
            frame_interval_ms=frame_interval_ms,
            idle_interval_seconds=idle_interval_seconds,
            heartbeat_seconds=heartbeat_seconds,
            debug_dump_dir=debug_dump_dir,
            scan_frame_interval_ms=scan_frame_interval_ms,
            fast_hold_seconds=fast_hold_seconds,
            failure_writer=failure_writer,
            diagnostic_writer=diagnostic_writer,
            roi_diagnostic_writer=roi_writer,
            retention_worker=retention_worker,
            ocr_shadow_sink=ocr_runtimes,
            mouse_transition_observer=mouse_observer,
        )
    finally:
        from hextech.infrastructure.vision.sidecar_capture import close_capture_backend
        close_capture_backend()
        mouse_observer.close(timeout=1.0)
        for runtime in ocr_runtimes:
            runtime.close(timeout=5.0)
        if failure_writer is not None:
            failure_writer.close(timeout=5.0)
        if roi_writer is not None:
            roi_writer.close(timeout=5.0)
        diagnostic_writer.close(timeout=5.0)


__all__ = ["run_loop"]

"""Vision Sidecar 的命令行入口。

本模块只负责参数解析、单实例锁和错误码；识别与状态机仍由 ``runner`` 负责。
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, Mapping

from hextech.infrastructure.vision import runner
from hextech.infrastructure.vision.sidecar_diagnostics import emit_cli_event


logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    from hextech.infrastructure.vision import sidecar as vision_sidecar_module

    vision_sidecar: Any = vision_sidecar_module
    parser = argparse.ArgumentParser(description="Hextech overlay Vision sidecar。")
    parser.add_argument("--once", action="store_true", help="执行一次短窗口识别后退出。")
    parser.add_argument("--loop", action="store_true", help="常驻自门控识别循环；未指定 --once 时默认启用。")
    parser.add_argument("--diagnostic-retention-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--preset", default="auto", help="ROI preset: auto, 1920x1080, 2560x1440, 2560x1600。")
    parser.add_argument("--write-event", action="store_true", help="把识别结果写入 overlay 事件文件。")
    parser.add_argument("--event-path", default="", help="调试用事件文件路径；默认写运行态 state。")
    parser.add_argument("--min-confidence", type=float, default=vision_sidecar.DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--required-frames", type=int, default=2)
    parser.add_argument("--frame-interval-ms", type=int, default=vision_sidecar.DEFAULT_LOOP_FRAME_INTERVAL_MS)
    parser.add_argument("--scan-frame-interval-ms", type=int, default=vision_sidecar.DEFAULT_LOOP_SCAN_FRAME_INTERVAL_MS)
    parser.add_argument(
        "--idle-interval-ms",
        type=int,
        default=int(vision_sidecar.DEFAULT_LOOP_IDLE_INTERVAL_SECONDS * 1000),
    )
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
    runner._write_sidecar_bootstrap_from_env(
        "failed",
        phase="template_load",
        error_type="FileNotFoundError",
        error_message_sanitized="Vision sidecar 模板缺失：template_missing",
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 经 runner 访问锁函数，保留既有测试与嵌入调用的窄注入点。
    with runner.overlay_instance_lock(runner.SIDECAR_INSTANCE_LOCK_FILE) as acquired:
        if not acquired:
            logger.warning("Vision sidecar 已有运行实例，本实例退出。")
            return 0
        return _run_main_locked(args)


def _run_main_locked(args: argparse.Namespace) -> int:
    """执行已取得单实例锁的 Sidecar CLI。"""

    runner._SIDECAR_DEBUG_DUMP_ENABLED = bool(args.debug_dump)
    runner._write_sidecar_bootstrap_from_env("starting", phase="argument_parsed")
    if args.diagnostic_retention_smoke:
        from hextech.infrastructure.vision.diagnostic_retention_smoke import (
            run_diagnostic_retention_smoke,
        )
        from hextech.modules.session.process_bootstrap import publish_process_bootstrap

        result = run_diagnostic_retention_smoke()
        publish_process_bootstrap(result)
        return 0 if result["ok"] else 1
    if args.once:
        try:
            event = runner.run_once(
                preset=args.preset,
                write_event=args.write_event,
                event_path=args.event_path or None,
                min_confidence=args.min_confidence,
                required_frames=args.required_frames,
                frame_interval_ms=args.frame_interval_ms,
                debug_dump_dir=args.debug_dump or None,
            )
            if _record_template_missing_failure(event):
                emit_cli_event(event)
                return 1
            emit_cli_event(event)
            return 0
        except Exception as exc:
            runner._write_sidecar_bootstrap_from_env(
                "failed",
                phase="run_once",
                error_type=exc.__class__.__name__,
                error_message_sanitized=runner._sanitize_bootstrap_error_message(exc),
            )
            return 1

    try:
        event = runner.run_loop(
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
        runner._write_sidecar_bootstrap_from_env(
            "failed",
            phase="run_loop",
            error_type=exc.__class__.__name__,
            error_message_sanitized=runner._sanitize_bootstrap_error_message(exc),
        )
        return 1
    if event is None:
        return 0
    if _record_template_missing_failure(event):
        emit_cli_event(event)
        return 1
    emit_cli_event(event)
    return 0


__all__ = ["build_parser", "main"]

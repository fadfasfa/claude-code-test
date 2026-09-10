"""Vision trace/timeline 的低优先级有界写入线程。

生产识别完成后必须先发布 Overlay event；trace 与 timeline 只用于复盘，磁盘慢写
不能扩大下一帧采集间隔。队列满或写入失败只记状态，不参与候选、READY 或显隐。
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread
from typing import Any


TraceWriter = Callable[[Mapping[str, Any], Path], object]
TimelineWriter = Callable[[Mapping[str, Any], Path], object]
DIAGNOSTIC_WRITER_MAX_QUEUE = 64


@dataclass(frozen=True)
class VisionDiagnosticTask:
    event: dict[str, Any]
    trace_path: Path
    write_trace: bool
    write_timeline: bool


class VisionDiagnosticWriter:
    """保持 observation 顺序的单线程 writer；满载时拒绝新诊断而不阻塞识别。"""

    def __init__(
        self,
        *,
        trace_writer: TraceWriter,
        timeline_writer: TimelineWriter,
        max_queue: int = DIAGNOSTIC_WRITER_MAX_QUEUE,
        retention_worker: Any | None = None,
    ) -> None:
        self._trace_writer = trace_writer
        self._timeline_writer = timeline_writer
        self._max_queue = max(1, int(max_queue))
        self._retention_worker = retention_worker
        self._condition = Condition()
        self._tasks: deque[VisionDiagnosticTask] = deque()
        self._thread: Thread | None = None
        self._stopping = False
        self._active = False
        self._submitted_count = 0
        self._completed_count = 0
        self._dropped_count = 0
        self._failed_count = 0
        self._last_error = ""
        self._last_error_stage = ""
        self._trace_failed_count = 0
        self._timeline_failed_count = 0
        self._timeline_truncated_count = 0
        self._timeline_skipped_count = 0
        self._current_timeline_path_hash = ""
        self._timeline_last_written_at = 0.0
        self._timeline_epoch = 0
        self._ineligible_count = 0
        self._last_probe_signature: dict[tuple[str, int], tuple[tuple[str, ...], float]] = {}
        self._terminal_keys: set[tuple[str, int, str]] = set()

    @staticmethod
    def _source(event: Mapping[str, Any]) -> Mapping[str, Any]:
        source = event.get("source")
        return source if isinstance(source, Mapping) else {}

    @staticmethod
    def _probe_signature(event: Mapping[str, Any]) -> tuple[str, ...]:
        source = VisionDiagnosticWriter._source(event)
        slots = event.get("slots") if isinstance(event.get("slots"), list) else []
        return (
            str(source.get("reason") or ""),
            str(source.get("scene_state") or ""),
            str(source.get("scene_temporal_state") or ""),
            str(source.get("selection_window_active")),
            str(bool(source.get("transient_pause"))),
            str(bool(event.get("active"))),
            *(
                f"{slot.get('state') or ''}:{slot.get('augment_id') or ''}"
                for slot in slots[:3]
                if isinstance(slot, Mapping)
            ),
        )

    def _eligible(self, event: Mapping[str, Any]) -> bool:
        """在 deepcopy 前裁掉空闲噪声，并对暂停探针做变化/30 秒节流。"""

        if str(event.get("selection_type") or "") not in {"hextech", "body_shard"}:
            return False
        source = self._source(event)
        session_id = str(source.get("session_id") or "")
        game_instance_id = str(source.get("game_instance_id") or "")
        try:
            epoch = int(source.get("selection_epoch") or 0)
        except (TypeError, ValueError):
            epoch = 0
        if (not session_id and not game_instance_id) or epoch <= 0:
            return False
        timing = event.get("timing") if isinstance(event.get("timing"), Mapping) else {}
        kind = str(timing.get("observation_kind") or "recognition")
        if kind == "recognition" and str(timing.get("capture_status") or "captured") == "captured":
            return True
        reason = str(source.get("reason") or "")
        if reason in {"selection_completed", "scene_loss_confirmed", "gameflow_ended"}:
            terminal_key = (session_id or game_instance_id, epoch, reason)
            if terminal_key in self._terminal_keys:
                return False
            self._terminal_keys.add(terminal_key)
            return True
        if kind not in {"visibility_probe", "capture_failure"} and not bool(source.get("transient_pause")):
            return False
        key = (session_id or game_instance_id, epoch)
        signature = self._probe_signature(event)
        now = time.monotonic()
        previous = self._last_probe_signature.get(key)
        if previous is not None and signature == previous[0] and now - previous[1] < 30.0:
            return False
        self._last_probe_signature[key] = (signature, now)
        return True

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = Thread(
                target=self._run,
                name="overlay-vision-diagnostics",
                daemon=True,
            )
            self._thread.start()

    def submit(
        self,
        event: Mapping[str, Any],
        trace_path: str | Path,
        *,
        write_trace: bool,
    ) -> bool:
        with self._condition:
            if self._stopping or len(self._tasks) >= self._max_queue:
                self._dropped_count += 1
                return False
            if not self._eligible(event):
                self._ineligible_count += 1
                return False
            task = VisionDiagnosticTask(
                event=deepcopy(dict(event)),
                trace_path=Path(trace_path),
                write_trace=bool(write_trace),
                write_timeline=True,
            )
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name="overlay-vision-diagnostics",
                    daemon=True,
                )
                self._thread.start()
            self._tasks.append(task)
            self._submitted_count += 1
            self._condition.notify_all()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._tasks and not self._stopping:
                    self._condition.wait()
                if not self._tasks and self._stopping:
                    return
                task = self._tasks.popleft()
                self._active = True
            try:
                if task.write_trace:
                    try:
                        self._trace_writer(task.event, task.trace_path)
                    except Exception as exc:
                        with self._condition:
                            self._failed_count += 1
                            self._trace_failed_count += 1
                            self._last_error = exc.__class__.__name__
                            self._last_error_stage = "trace"
                if task.write_timeline:
                    try:
                        target = self._timeline_writer(task.event, task.trace_path)
                        source = self._source(task.event)
                        if target is not None:
                            target_text = str(Path(target).resolve())
                            disposition = str(
                                task.event.get("_timeline_write_disposition") or "appended"
                            )
                            with self._condition:
                                if disposition in {"appended", "truncated"}:
                                    self._current_timeline_path_hash = hashlib.sha256(
                                        target_text.encode("utf-8", errors="replace")
                                    ).hexdigest()
                                    self._timeline_last_written_at = time.time()
                                    self._timeline_epoch = int(source.get("selection_epoch") or 0)
                                if disposition == "truncated":
                                    self._timeline_truncated_count += 1
                                elif disposition in {"already_terminal", "already_truncated"}:
                                    self._timeline_skipped_count += 1
                    except Exception as exc:
                        with self._condition:
                            self._failed_count += 1
                            self._timeline_failed_count += 1
                            self._last_error = exc.__class__.__name__
                            self._last_error_stage = "timeline"
                request = getattr(self._retention_worker, "request", None)
                if callable(request):
                    try:
                        request()
                    except Exception as exc:
                        with self._condition:
                            self._failed_count += 1
                            self._last_error = exc.__class__.__name__
                            self._last_error_stage = "retention"
            finally:
                with self._condition:
                    self._completed_count += 1
                    self._active = False
                    self._condition.notify_all()

    def wait_empty(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while (self._tasks or self._active) and time.monotonic() < deadline:
                self._condition.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            return not self._tasks and not self._active

    def close(self, timeout: float = 5.0) -> None:
        self.wait_empty(timeout)
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "queue_depth": len(self._tasks),
                "active": self._active,
                "submitted_count": self._submitted_count,
                "completed_count": self._completed_count,
                "dropped_count": self._dropped_count,
                "failed_count": self._failed_count,
                "last_error": self._last_error,
                "last_error_stage": self._last_error_stage,
                "trace_failed_count": self._trace_failed_count,
                "timeline_failed_count": self._timeline_failed_count,
                "timeline_truncated_count": self._timeline_truncated_count,
                "timeline_skipped_count": self._timeline_skipped_count,
                "current_timeline_path_hash": self._current_timeline_path_hash,
                "timeline_last_written_at": self._timeline_last_written_at,
                "timeline_epoch": self._timeline_epoch,
                "ineligible_count": self._ineligible_count,
            }


__all__ = ["VisionDiagnosticWriter"]

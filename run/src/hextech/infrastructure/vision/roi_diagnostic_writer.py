"""显式 ROI PNG 诊断的低优先级有界写入线程。

常驻识别循环只提交当前帧引用和事件快照；裁剪、PNG 编码、JSON 与轮转全部在
后台完成。队列满或磁盘失败只进入状态诊断，不得阻塞或改变生产识别结果。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread
from typing import Any

from PIL import Image


ROI_DIAGNOSTIC_WRITER_MAX_QUEUE = 8
RoiDumpWriter = Callable[..., object]


@dataclass(frozen=True)
class RoiDiagnosticTask:
    dump_root: Path
    frame: Image.Image
    event: dict[str, Any]
    observation_seq: int | None


@dataclass(frozen=True)
class CaptureSessionTask:
    session: Any
    draft: Any


class RoiDiagnosticWriter:
    """后台持有待写帧；满载时拒绝新诊断而不拖慢下一帧。"""

    def __init__(
        self,
        *,
        dump_writer: RoiDumpWriter,
        max_queue: int = ROI_DIAGNOSTIC_WRITER_MAX_QUEUE,
        retention_worker: Any | None = None,
    ) -> None:
        self._dump_writer = dump_writer
        self._max_queue = max(1, int(max_queue))
        self._retention_worker = retention_worker
        self._condition = Condition()
        self._tasks: deque[RoiDiagnosticTask] = deque()
        self._thread: Thread | None = None
        self._stopping = False
        self._active = False
        self._submitted_count = 0
        self._completed_count = 0
        self._dropped_count = 0
        self._failed_count = 0
        self._last_error = ""
        self._capture_session = None

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = Thread(
                target=self._run,
                name="overlay-roi-diagnostics",
                daemon=True,
            )
            self._thread.start()

    def submit(
        self,
        dump_root: str | Path,
        frame: Image.Image,
        event: Mapping[str, Any],
        *,
        observation_seq: int | None,
    ) -> bool:
        """转移当前 PIL 帧的只读持有权；调用方提交后不得修改该对象。"""

        with self._condition:
            if self._stopping or len(self._tasks) >= self._max_queue:
                self._dropped_count += 1
                return False
            task = RoiDiagnosticTask(
                dump_root=Path(dump_root),
                frame=frame,
                event=deepcopy(dict(event)),
                observation_seq=observation_seq,
            )
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name="overlay-roi-diagnostics",
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
                    if self._capture_session is not None:
                        self._capture_session.finalize()
                    return
                task = self._tasks.popleft()
                self._active = True
            try:
                if isinstance(task, CaptureSessionTask):
                    task.session.write(task.draft)
                else:
                    self._dump_writer(task.dump_root, task.frame, task.event, observation_seq=task.observation_seq)
                request = getattr(self._retention_worker, "request", None)
                if callable(request):
                    request()
            except Exception as exc:
                with self._condition:
                    self._failed_count += 1
                    self._last_error = exc.__class__.__name__
            finally:
                with self._condition:
                    self._completed_count += 1
                    self._active = False
                    self._condition.notify_all()

    def submit_capture(self, session: Any, draft: Any) -> bool:
        """显式有界采集复用同一低优先级线程，不创建第二个writer。"""
        with self._condition:
            self._capture_session = session
            if self._stopping or len(self._tasks) >= self._max_queue:
                self._dropped_count += 1
                session.record_drop(draft)
                return False
            self._tasks.append(CaptureSessionTask(session, draft))
            self._submitted_count += 1
            if self._thread is None:
                self._thread = Thread(target=self._run, name="overlay-roi-diagnostics", daemon=True)
                self._thread.start()
            self._condition.notify_all()
            return True

    def close(self, timeout: float = 5.0) -> None:
        """请求排空并在总 timeout 内返回；后台线程是 daemon，不阻塞退出。"""

        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

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
            }


__all__ = ["ROI_DIAGNOSTIC_WRITER_MAX_QUEUE", "RoiDiagnosticWriter"]

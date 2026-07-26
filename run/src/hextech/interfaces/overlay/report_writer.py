"""Overlay 结构化报告与诊断截图的异步写入器。

Tk 主线程只提交已经脱敏的不可变任务；单个后台线程负责 JSON、latest、轮转和
可选截图。队列有界且优先保留最新状态，磁盘慢时不会拖住 Overlay 呈现。
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Thread
from typing import Any, Mapping

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.session.evidence import SessionEvidenceBundle, write_evidence_bundle


# 一局实测约 58 份报告；20 条会把整局前半段证据冲掉（真机复现：
# 出问题的 epoch 1-4 报告全部丢失）。200 可完整留住至少 3 局。
OVERLAY_SESSION_REPORT_LIMIT = 200


@dataclass(frozen=True)
class _WriteTask:
    kind: str
    key: str
    payload: Any


class OverlayReportWriter:
    """单线程有界写入器；相同 key 的等待任务会被最新内容替换。"""

    def __init__(self, report_dir: Path, evidence_dir: Path, *, max_queue: int = 8) -> None:
        self.report_dir = Path(report_dir)
        self.evidence_dir = Path(evidence_dir)
        self.max_queue = max(1, int(max_queue))
        self._condition = Condition()
        self._tasks: deque[_WriteTask] = deque()
        self._stopping = False
        self._active = False
        self._thread: Thread | None = None
        self._dropped_count = 0
        self._coalesced_count = 0
        self._written_count = 0
        self._error_count = 0
        self._sequence = 0

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = Thread(target=self._run, name="hextech-overlay-report-writer", daemon=True)
            self._thread.start()

    def submit_session(self, report: Mapping[str, Any], *, key: str) -> bool:
        return self._enqueue(_WriteTask("session", str(key), deepcopy(dict(report))))

    def submit_evidence(
        self,
        bundle: SessionEvidenceBundle,
        *,
        key: str,
        target_name: str,
        screenshot_name: str = "",
        screenshot_bbox: tuple[int, int, int, int] | None = None,
    ) -> bool:
        payload = {
            "bundle": bundle,
            "target_name": str(target_name),
            "screenshot_name": str(screenshot_name),
            "screenshot_bbox": tuple(screenshot_bbox) if screenshot_bbox is not None else None,
        }
        return self._enqueue(_WriteTask("evidence", str(key), payload))

    def _enqueue(self, task: _WriteTask) -> bool:
        with self._condition:
            if self._stopping:
                return False
            for index, queued in enumerate(self._tasks):
                if queued.kind == task.kind and queued.key == task.key:
                    self._tasks[index] = task
                    self._coalesced_count += 1
                    self._condition.notify()
                    return True
            if len(self._tasks) >= self.max_queue:
                self._tasks.popleft()
                self._dropped_count += 1
            self._tasks.append(task)
            self._condition.notify()
            return True

    def status(self) -> dict[str, int]:
        with self._condition:
            return {
                "queue_depth": len(self._tasks),
                "dropped_count": self._dropped_count,
                "coalesced_count": self._coalesced_count,
                "written_count": self._written_count,
                "error_count": self._error_count,
            }

    def wait_empty(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._tasks or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(0.1, remaining))
            return True

    def close(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))

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
                if task.kind == "session":
                    self._write_session(task.payload)
                else:
                    self._write_evidence(task.payload)
                with self._condition:
                    self._written_count += 1
            except Exception:
                # 报告失败只能影响诊断；状态计数由 Host 暴露，不能反向拖垮渲染。
                with self._condition:
                    self._error_count += 1
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def _write_session(self, raw_report: Mapping[str, Any]) -> None:
        report = deepcopy(dict(raw_report))
        timing = report.get("timing") if isinstance(report.get("timing"), Mapping) else {}
        report["timing"] = {
            **{str(key): value for key, value in timing.items()},
            "report_written_at": time.time(),
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        target = self.report_dir / (
            f"overlay-session-{stamp}-{time.time_ns():020d}-{self._sequence:08d}-{uuid.uuid4().hex}.json"
        )
        atomic_write_json(target, report, ensure_ascii=False, indent=2)
        atomic_write_json(self.report_dir / "latest.json", report, ensure_ascii=False, indent=2)
        reports = sorted(
            (path for path in self.report_dir.glob("overlay-session-*.json") if path.is_file()),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for stale in reports[OVERLAY_SESSION_REPORT_LIMIT:]:
            if stale.parent.resolve() == self.report_dir.resolve():
                stale.unlink(missing_ok=True)

    def _write_evidence(self, payload: Mapping[str, Any]) -> None:
        bundle = payload.get("bundle")
        if not isinstance(bundle, SessionEvidenceBundle):
            raise TypeError("evidence bundle invalid")
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        screenshot_name = str(payload.get("screenshot_name") or "")
        bbox = payload.get("screenshot_bbox")
        if screenshot_name and isinstance(bbox, tuple) and len(bbox) == 4:
            from PIL import ImageGrab

            ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB").save(self.evidence_dir / screenshot_name)
            bundle = replace(bundle, screenshot=screenshot_name)
        target_name = str(payload.get("target_name") or "")
        if not target_name:
            raise ValueError("evidence target missing")
        write_evidence_bundle(bundle, self.evidence_dir / target_name)
        write_evidence_bundle(bundle, self.evidence_dir / "latest_real_session.v2.json")


__all__ = ["OVERLAY_SESSION_REPORT_LIMIT", "OverlayReportWriter"]

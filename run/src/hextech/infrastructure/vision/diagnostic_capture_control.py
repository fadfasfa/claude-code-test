"""一次性显式请求的生产采集接线；不启动线程、不影响识别判定。"""
from __future__ import annotations

import json
import os
from collections import deque
import time
from pathlib import Path
from uuid import UUID

from .diagnostic_capture_session import DiagnosticCaptureSession


class ExplicitCaptureControl:
    def __init__(self, var_dir: Path, writer, build_id: str):
        self.var_dir, self.writer, self.build_id = Path(var_dir), writer, build_id
        from .sidecar_status import SIDECAR_INSTANCE_ID
        self.sidecar_instance_id = SIDECAR_INSTANCE_ID
        self.session = None
        self._last_poll = 0.0
        self._last_request = ""
        self._seen_requests = deque(maxlen=64)
        self._started = 0.0
        self._selection = None
        self._last_full = float("-inf")
        self._last_roi = float("-inf")
        self._selection_seen = False
        self._full_count = self._roi_count = 0
        self._done = False

    def poll(self) -> None:
        now = time.monotonic()
        if now-self._last_poll < 1.0 or self.writer is None:
            return
        self._last_poll = now
        path = self.var_dir / "state" / "diagnostic_capture_request.v1.json"
        try:
            if path.is_symlink() or (path.stat().st_file_attributes & 0x400) or path.stat().st_size > 4096:
                return
            payload = json.loads(path.read_text(encoding="utf-8"))
            request_id = str(UUID(payload["request_id"]))
            age = time.time()-float(payload["requested_at"])
            if payload.get("schema_version") != 1 or not 0 <= age <= 30:
                return
            if (payload.get("expected_build_id") != self.build_id or request_id in self._seen_requests
                or payload.get("expected_sidecar_instance_id") != self.sidecar_instance_id):
                return
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return
        self._last_request = request_id
        self._seen_requests.append(request_id)
        if self.session is not None and not self._done:
            return
        self.session = DiagnosticCaptureSession(self.var_dir, enabled=True)
        self._started, self._selection, self._selection_seen = now, None, False
        self._last_full = self._last_roi = float("-inf")
        self._full_count = self._roi_count = 0
        self._done = False

    def wants_full_client(self) -> bool:
        return bool(self.session and not self._done and self._selection_seen
                    and self._full_count < 3 and time.monotonic()-self._last_full >= 1.0)

    def observe(self, frame, raw_event, event) -> None:
        if self.session is None or self._done:
            return
        now = time.monotonic()
        source = {**dict(raw_event.get("source") or {}), **dict(event.get("source") or {})}
        source["build_id"] = self.build_id
        from .sidecar_status import SIDECAR_INSTANCE_ID
        source["sidecar_instance_id"] = SIDECAR_INSTANCE_ID
        source["sidecar_pid"] = os.getpid()
        payload = {**event, "source": source, "timing": dict(raw_event.get("timing") or event.get("timing") or {})}
        for field in ("_raw_slots", "_completed_ocr_evidence"):
            if field in raw_event:
                payload[field] = raw_event[field]
        terminal = str(source.get("reason") or "")
        if now-self._started > 120:
            terminal = "user_stopped"
        if terminal in {"selection_completed", "scene_loss_confirmed", "gameflow_ended", "user_stopped"}:
            if self._selection is not None or terminal == "user_stopped":
                self._submit(self.session.plan(None, payload, terminal_reason=terminal))
                self._done = True
            return
        valid_scene = bool(source.get("scene_present") or source.get("selection_button_present")
                           or source.get("scene_kind") == "body_shard" or source.get("reason") == "scene_type_conflict")
        if frame is None or not valid_scene:
            return
        if self._selection is None:
            self._selection = (source.get("session_id"), source.get("selection_epoch"))
        self._selection_seen = True
        full = (self._full_count < 3 and frame.info.get("hextech_capture_mode") in {"client_full", "client_full_recovery"}
                and now-self._last_full >= 1.0)
        roi = self._roi_count < 12 and now-self._last_roi >= .1
        draft = self.session.plan(frame, payload, include_full_client=full, include_roi_set=roi)
        if draft is not None:
            if full:
                self._full_count += 1
                self._last_full = now
            if roi:
                self._roi_count += 1
                self._last_roi = now
            self._submit(draft)
        if self._full_count >= 3 and self._roi_count >= 12:
            self._submit(self.session.plan(None, payload, terminal_reason="budget_exhausted"))
            self._done = True

    def _submit(self, draft):
        if draft is not None:
            self.writer.submit_capture(self.session, draft)

    def status(self):
        return self.session.status() if self.session is not None else {"enabled": False}

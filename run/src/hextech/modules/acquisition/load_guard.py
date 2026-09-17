"""局中后台领取保护；采样只用于降速，绝不充当识别正确性/真机验收。"""
from __future__ import annotations

from collections import deque
import math
from typing import Any, Mapping


class BackgroundLoadGuard:
    def __init__(self) -> None:
        self._samples: deque[float] = deque(maxlen=10)
        self._last_capture = 0.0
        self._instance = ""
        self.paused = False

    def observe(self, event: Mapping[str, Any]) -> bool:
        source = event.get("source") or {}
        timing = event.get("timing") or {}
        if not isinstance(source, Mapping) or not isinstance(timing, Mapping):
            return self.paused
        if timing.get("observation_kind") != "recognition" or timing.get("capture_status") != "captured":
            return self.paused
        try:
            captured = float(timing["captured_at"])
            started = float(timing["capture_started_at"])
            completed = float(timing["recognition_completed_at"])
        except (KeyError, ValueError, TypeError):
            return self.paused
        if not all(math.isfinite(t) for t in (captured, started, completed)) or not 0 < started <= captured <= completed:
            return self.paused
        instance = str(source.get("game_instance_id") or source.get("session_id") or "")
        if not instance:
            return self.paused
        if instance != self._instance:
            self._instance, self._last_capture = instance, 0.0
            self._samples.clear()
            self.paused = False
        if captured <= self._last_capture:
            return self.paused
        self._last_capture = captured
        self._samples.append((completed - started) * 1000)
        if len(self._samples) >= 5 and sum(value > 180 for value in self._samples) >= 5:
            self.paused = True
        elif len(self._samples) >= 10 and all(value <= 150 for value in list(self._samples)[-10:]):
            self.paused = False
        return self.paused

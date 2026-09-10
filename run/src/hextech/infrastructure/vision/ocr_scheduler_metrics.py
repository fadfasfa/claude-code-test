"""OCR worker 调度与推理耗时的有界聚合。"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[position], 3)


class BoundedOcrSchedulerTiming:
    """由 OCR runtime 锁保护的固定容量耗时窗口。"""

    _NAMES = (
        "production_queue_wait",
        "diagnostic_queue_wait",
        "production_inference",
        "diagnostic_inference",
    )

    def __init__(self, capacity: int) -> None:
        self._samples = {
            name: deque[float](maxlen=max(1, int(capacity)))
            for name in self._NAMES
        }

    def record_queue_wait(self, *, production: bool, seconds: float) -> None:
        name = "production_queue_wait" if production else "diagnostic_queue_wait"
        self._samples[name].append(round(max(0.0, float(seconds)) * 1000.0, 3))

    def record_inference(self, *, production: bool, elapsed_ms: float) -> None:
        name = "production_inference" if production else "diagnostic_inference"
        self._samples[name].append(round(max(0.0, float(elapsed_ms)), 3))

    def snapshot(self) -> dict[str, float]:
        payload: dict[str, float] = {}
        for name, samples in self._samples.items():
            values = tuple(samples)
            payload[f"{name}_p50_ms"] = percentile(values, 0.50)
            payload[f"{name}_p95_ms"] = percentile(values, 0.95)
        return payload


__all__ = ["BoundedOcrSchedulerTiming", "percentile"]

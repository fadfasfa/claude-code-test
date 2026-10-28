"""Bounded post-selection sampling, owned by the existing context poll thread."""
from collections.abc import Mapping
from typing import Any
import math


def fresh_selection_key(event: Mapping[str, Any], context: Mapping[str, Any], wall_now: float) -> tuple | None:
    source = event.get("source") or {}
    try:
        age = wall_now - float(event.get("generated_at") or 0)
        epoch = int(source.get("selection_epoch") or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(age) or not 0 <= age <= 2.5 or epoch <= 0
            or not source.get("session_id") or not source.get("game_instance_id")
            or source.get("game_instance_id") != context.get("game_instance_id")
            or not source.get("window_hwnd") or source.get("window_hwnd") != context.get("window_hwnd")
            or not source.get("selection_window_active")
            or str(event.get("selection_type") or source.get("scene_kind")) != "hextech"):
        return None
    # 缺省值保留旧事件读取兼容；生产事件用 Sidecar 实例隔离重启后的 epoch 回退。
    return (source["game_instance_id"], source["session_id"], epoch, str(source.get("sidecar_instance_id") or ""))


class SelectionContextCadence:
    def __init__(self, interval: float):
        self.interval = interval
        self.next_poll = 0.0
        self.fast_until = 0.0
        self.identity = None
        self.max_epoch = 0

    def due(self, now: float, key: tuple | None) -> bool:
        if key is not None:
            identity, epoch = (*key[:2], key[3] if len(key) > 3 else ""), key[2]
            if identity != self.identity:
                self.identity, self.max_epoch = identity, 0
            if epoch > self.max_epoch:
                self.max_epoch = epoch
                self.fast_until, self.next_poll = now + 2.0, now
        if now < self.next_poll:
            return False
        return True

    def completed(self, now: float) -> None:
        # Completion based, so a slow request never creates catch-up work.
        self.next_poll = now + (.25 if now < self.fast_until else self.interval)

"""Overlay 单轮选择的 generation 固定器。

选择轮由 ``session_id + selection_epoch`` 唯一标识。同一轮只使用首次打开的
``DataSnapshotView``；current 中途变化只记状态，下一轮才采用新 generation。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from hextech.modules.data import SnapshotViewPort


SelectionKey = tuple[str, int]


def selection_key(event: Mapping[str, Any]) -> SelectionKey | None:
    source_value = event.get("source")
    source = source_value if isinstance(source_value, Mapping) else {}
    session_id = str(source.get("session_id") or "").strip()
    try:
        epoch = int(source.get("selection_epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0
    return (session_id, epoch) if session_id and epoch > 0 else None


def _generation_id(view: SnapshotViewPort | None) -> str:
    if view is None:
        return ""
    try:
        return str(view.status().get("generation_id") or "")
    except Exception:
        return ""


class SelectionGenerationPin:
    """持有当前选择轮的 immutable snapshot view。"""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        latest_probe_interval_seconds: float = 1.0,
    ) -> None:
        self._key: SelectionKey | None = None
        self._view: SnapshotViewPort | None = None
        self._generation_id = ""
        self._new_generation_id = ""
        self._now = now
        self._latest_probe_interval_seconds = max(0.1, float(latest_probe_interval_seconds))
        self._last_latest_probe_at = 0.0

    def reset(self) -> None:
        self._key = None
        self._view = None
        self._generation_id = ""
        self._new_generation_id = ""
        self._last_latest_probe_at = 0.0

    def resolve(
        self,
        event: Mapping[str, Any],
        open_latest: Callable[[], SnapshotViewPort | None],
    ) -> SnapshotViewPort | None:
        key = selection_key(event)
        if key is None:
            self.reset()
            return None
        if key != self._key:
            self._key = key
            self._new_generation_id = ""
            self._last_latest_probe_at = self._now()
            try:
                self._view = open_latest()
            except Exception:
                self._view = None
            self._generation_id = _generation_id(self._view)
            return self._view
        if self._view is None:
            return None

        now = self._now()
        if now - self._last_latest_probe_at < self._latest_probe_interval_seconds:
            return self._view
        self._last_latest_probe_at = now

        try:
            latest = open_latest()
        except Exception:
            latest = None
        latest_id = _generation_id(latest)
        if latest_id and latest_id != self._generation_id:
            self._new_generation_id = latest_id
        return self._view

    def status(self) -> dict[str, Any]:
        return {
            "selection_key": list(self._key) if self._key is not None else [],
            "generation_id": self._generation_id,
            "new_generation_available": bool(self._new_generation_id),
            "new_generation_id": self._new_generation_id,
            "available": self._view is not None and bool(self._generation_id),
        }


__all__ = ["SelectionGenerationPin", "selection_key"]

"""显示入口共用的场景硬门与固定屏幕规格；不消费逐帧识别校准。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def fragment_selection(event: Mapping[str, Any]) -> bool:
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    return bool(
        str(event.get("selection_type") or "") == "body_shard"
        or str(source.get("scene_kind") or "") == "body_shard"
        or source.get("body_shard_latched")
        or str(source.get("reason") or "") == "body_shard_only"
    )


def ordinary_selection(event: Mapping[str, Any]) -> bool:
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    return bool(
        not fragment_selection(event)
        and str(event.get("selection_type") or source.get("scene_kind") or "") == "hextech"
        and source.get("selection_window_active") is True
        and str(source.get("scene_state") or "") == "active"
    )


def display_profile(width: int, height: int) -> str:
    ratio = max(1, width) / max(1, height)
    if abs(ratio - 16 / 9) < 0.015:
        return "fixed-16:9-v2"
    if abs(ratio - 16 / 10) < 0.015:
        return "fixed-16:10-v2"
    return "unqualified-aspect"


class DisplaySelectionGate:
    """第二道碎片门：同轮的旧事件、缓存或异步结果不能恢复统计。"""

    def __init__(self) -> None:
        self._blocked: tuple[str, str, int] | None = None

    def filter(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
        try:
            epoch = int(source.get("selection_epoch") or 0)
        except (TypeError, ValueError):
            epoch = 0
        key = (str(source.get("session_id") or ""), str(source.get("game_instance_id") or ""), epoch)
        if fragment_selection(event):
            self._blocked = key
        elif self._blocked is not None and (key[:2] != self._blocked[:2] or epoch > self._blocked[2]):
            self._blocked = None
        blocked = self._blocked is not None and key[:2] == self._blocked[:2] and epoch <= self._blocked[2]
        if blocked:
            return {
                **event, "selection_type": "body_shard", "active": False, "visible": False, "slots": [],
                "source": {**source, "reason": "body_shard_only", "body_shard_latched": True,
                           "selection_window_active": False, "ready_slots": 0, "scene_state": "blocked"},
            }
        if not ordinary_selection(event) and source.get("selection_window_active") is True and not source.get("transient_pause"):
            return {**event, "active": False, "visible": False, "slots": [],
                    "source": {**source, "selection_window_active": False, "ready_slots": 0}}
        return event

"""Overlay 单局游戏的统计 generation 固定器。

首个可信选择场景之前允许采用当前英雄完整的新统计；场景出现后整局固定，
不等待任何槽位 READY。统计切代不改变 Vision 的启动绑定。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from typing import Any, ContextManager

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


def game_session_key(event: Mapping[str, Any]) -> str:
    source_value = event.get("source")
    source = source_value if isinstance(source_value, Mapping) else {}
    return str(source.get("session_id") or "").strip()


def first_selection_started(event: Mapping[str, Any]) -> bool:
    """确认的选择窗口或有明确场景证据的 candidate 即为截止，不用卡片身份。"""
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    return bool(
        source.get("selection_window_active") is True
        or (
            source.get("scene_present") is True
            and source.get("scene_state") in {"candidate", "active", "blocked"}
            and str(event.get("selection_type") or source.get("scene_kind") or "")
            in {"hextech", "body_shard"}
        )
    )


def _champion_complete(view: SnapshotViewPort, champion_id: str) -> bool:
    check = getattr(view, "is_champion_complete", None)
    # 旧 full snapshot/测试适配器没有此能力时保持原合同；partial 必须显式证明完整。
    return bool(check(champion_id)) if callable(check) else True


def _generation_id(view: SnapshotViewPort | None) -> str:
    if view is None:
        return ""
    try:
        return str(view.status().get("generation_id") or "")
    except Exception:
        return ""


class SelectionGenerationPin:
    """持有当前游戏局的 immutable stats snapshot view。"""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        latest_probe_interval_seconds: float = 1.0,
        is_complete: Callable[[SnapshotViewPort, str], bool] = _champion_complete,
    ) -> None:
        self._session_id = ""
        self._selection_key: SelectionKey | None = None
        self._view: SnapshotViewPort | None = None
        self._generation_id = ""
        self._new_generation_id = ""
        self._now = now
        self._latest_probe_interval_seconds = max(0.1, float(latest_probe_interval_seconds))
        self._last_latest_probe_at = 0.0
        self._frozen = False
        self._is_complete = is_complete

    def reset(self) -> None:
        self._session_id = ""
        self._selection_key = None
        self._view = None
        self._generation_id = ""
        self._new_generation_id = ""
        self._last_latest_probe_at = 0.0
        self._frozen = False

    def resolve(
        self,
        event: Mapping[str, Any],
        open_latest: Callable[[], SnapshotViewPort | None],
        *,
        champion_id: str = "",
        selection_started: bool = False,
        can_adopt: Callable[[], bool] | None = None,
        initial_view: SnapshotViewPort | None = None,
        adoption_lock: ContextManager[Any] | None = None,
    ) -> SnapshotViewPort | None:
        session_id = game_session_key(event)
        current_selection_key = selection_key(event)
        if not session_id:
            self.reset()
            return None
        new_session = session_id != self._session_id
        if new_session:
            known_view = initial_view if initial_view is not None else self._view
            self.reset()
            self._session_id = session_id
            if known_view is not None:
                try:
                    if not champion_id or self._is_complete(known_view, champion_id):
                        self._view = known_view
                        self._generation_id = _generation_id(known_view)
                except Exception:
                    pass
        self._selection_key = current_selection_key
        self._frozen = self._frozen or selection_started or first_selection_started(event)
        now = self._now()
        if not new_session and now - self._last_latest_probe_at < self._latest_probe_interval_seconds:
            return self._view
        self._last_latest_probe_at = now

        try:
            latest = open_latest()
        except Exception:
            latest = None
        latest_id = _generation_id(latest)
        # callback 在耗时 open 后复核请求身份/截止，防止旧后台结果跨门回流。
        # 冷启动若已过截止且没有预先验证的 view，本局保持 unavailable。
        if latest is not None and latest_id and not self._frozen:
            try:
                complete = self._is_complete(latest, champion_id)
            except Exception:
                complete = False
            # 检查与实际换代共用 Host 截止锁；不能在 predicate 返回后被首场景插入。
            with adoption_lock if adoption_lock is not None else nullcontext():
                if complete and (can_adopt is None or can_adopt()):
                    self._view = latest
                    self._generation_id = latest_id
                    self._new_generation_id = ""
        if latest_id and latest_id != self._generation_id:
            self._new_generation_id = latest_id
        return self._view

    def status(self) -> dict[str, Any]:
        return {
            "selection_key": (
                list(self._selection_key) if self._selection_key is not None else []
            ),
            "game_session_id": self._session_id,
            "stats_frozen": self._frozen,
            "stats_generation_id": self._generation_id,
            "new_stats_generation_id": self._new_generation_id,
            "generation_role": "stats_game_session",
            # 兼容旧 Host/test 读取；新写入与诊断必须优先使用上面的角色字段。
            "generation_id": self._generation_id,
            "new_generation_available": bool(self._new_generation_id),
            "new_generation_id": self._new_generation_id,
            "available": self._view is not None and bool(self._generation_id),
        }


__all__ = ["SelectionGenerationPin", "first_selection_started", "game_session_key", "selection_key"]

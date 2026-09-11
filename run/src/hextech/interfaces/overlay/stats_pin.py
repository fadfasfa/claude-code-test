"""Overlay selection epoch 的 champion、Stage 与 scoped stats 固定器。

Generation 仍由 ``SelectionGenerationPin`` 按游戏局决定；本模块在同一 selection key 上
最多等待两秒获取等级，然后把 champion、Stage、ARAMKit run 和单英雄 view 一次
固定。固定后 API 恢复、等级变化或后台 generation 更新都只能影响下一轮。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hextech.interfaces.overlay.generation_pin import SelectionKey, selection_key
from hextech.modules.data.ports import SnapshotViewPort
from hextech.modules.data.scoped_stats import ScopedStatsCache, ScopedStatsView
from hextech.modules.game_context.stage_context import StageContextV1, resolve_stage_context


DEFAULT_STAGE_CONTEXT_WAIT_SECONDS = 2.0


def _generation_id(view: SnapshotViewPort | None) -> str:
    if view is None:
        return ""
    try:
        status = view.status()
    except Exception:
        return ""
    return str(status.get("generation_id") or "") if isinstance(status, Mapping) else ""


@dataclass(frozen=True)
class PinnedStatsScope:
    selection_key: SelectionKey | None
    generation_id: str
    stage_context: StageContextV1
    scoped_view: ScopedStatsView | None = None
    aramkit_run_id: str = ""
    status: str = "unavailable"
    reason: str = ""
    frozen: bool = False

    @property
    def champion_id(self) -> str:
        return self.stage_context.champion_id

    @property
    def stage(self) -> int | None:
        return self.stage_context.stage

    def semantic_key(self) -> tuple[Any, ...]:
        return (
            self.selection_key,
            self.generation_id,
            self.champion_id,
            self.stage,
            self.aramkit_run_id,
            self.status,
            self.reason,
            self.frozen,
        )

    def to_status(self) -> dict[str, Any]:
        return {
            "selection_key": list(self.selection_key) if self.selection_key is not None else [],
            "stats_generation_id": self.generation_id,
            "generation_role": "stats_game_session",
            "generation_id": self.generation_id,
            "champion_id": self.champion_id,
            "stage": self.stage,
            "aramkit_run_id": self.aramkit_run_id,
            "status": self.status,
            "reason": self.reason,
            "frozen": self.frozen,
            "stage_context": self.stage_context.to_dict(),
            "scoped_view": self.scoped_view.status() if self.scoped_view is not None else {},
        }


class SelectionStatsPin:
    """为一个 selection epoch 固定阶段统计范围。"""

    def __init__(
        self,
        *,
        cache: ScopedStatsCache | None = None,
        now: Callable[[], float] = time.monotonic,
        wait_seconds: float = DEFAULT_STAGE_CONTEXT_WAIT_SECONDS,
    ) -> None:
        self.cache = cache or ScopedStatsCache()
        self._now = now
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._key: SelectionKey | None = None
        self._started_at = 0.0
        self._pinned: PinnedStatsScope | None = None
        self._latest = PinnedStatsScope(None, "", StageContextV1())

    def reset(self) -> None:
        self._key = None
        self._started_at = 0.0
        self._pinned = None
        self._latest = PinnedStatsScope(None, "", StageContextV1())

    def resolve(
        self,
        event: Mapping[str, Any],
        context: Mapping[str, Any],
        snapshot_view: SnapshotViewPort | None,
        *,
        completed_selection_count: int | None,
    ) -> PinnedStatsScope:
        key = selection_key(event)
        if key is None:
            self.reset()
            return self._latest
        now = self._now()
        if key != self._key:
            self._key = key
            self._started_at = now
            self._pinned = None
        if self._pinned is not None:
            self._latest = self._pinned
            return self._pinned

        generation_id = _generation_id(snapshot_view)
        stage_context = resolve_stage_context(
            context,
            completed_selection_count=completed_selection_count,
        )
        elapsed = max(0.0, now - self._started_at)
        if stage_context.status != "ready" and elapsed < self.wait_seconds:
            self._latest = PinnedStatsScope(
                selection_key=key,
                generation_id=generation_id,
                stage_context=stage_context,
                status="preparing",
                reason="stage_context_waiting",
                frozen=False,
            )
            return self._latest

        if not stage_context.champion_id:
            self._pinned = PinnedStatsScope(
                selection_key=key,
                generation_id=generation_id,
                stage_context=stage_context,
                status="unavailable",
                reason=stage_context.reason or "context_missing",
                frozen=True,
            )
        elif snapshot_view is None:
            self._pinned = PinnedStatsScope(
                selection_key=key,
                generation_id=generation_id,
                stage_context=stage_context,
                status="fallback",
                reason="snapshot_unavailable",
                frozen=True,
            )
        else:
            loaded = self.cache.load(snapshot_view, stage_context.champion_id)
            self._pinned = PinnedStatsScope(
                selection_key=key,
                generation_id=generation_id,
                stage_context=stage_context,
                scoped_view=loaded.view,
                aramkit_run_id=loaded.run_id,
                status="ready" if loaded.available else "fallback",
                reason=loaded.reason,
                frozen=True,
            )
        self._latest = self._pinned
        return self._pinned

    def status(self) -> dict[str, Any]:
        return self._latest.to_status()


__all__ = [
    "DEFAULT_STAGE_CONTEXT_WAIT_SECONDS",
    "PinnedStatsScope",
    "SelectionStatsPin",
]

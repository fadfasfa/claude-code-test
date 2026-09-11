"""Overlay Host 的阶段统计编排。

本模块集中持有 selection completion、Stage pin、单英雄 LRU 与 packaged scoped
自检；它不创建窗口、不读取事件文件，也不改变 Vision revision。Host 主循环只传入
已读取的 event/context/snapshot view，避免把阶段生命周期继续堆进 GUI 入口。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hextech.contracts import GameSessionState
from hextech.interfaces.overlay.generation_pin import selection_key
from hextech.interfaces.overlay.stats_pin import PinnedStatsScope, SelectionStatsPin
from hextech.modules.data.ports import SnapshotViewPort
from hextech.modules.data.scoped_stats import ScopedStatsCache
from hextech.modules.game_context import SelectionCompletionTracker
from hextech.modules.recommendation import apply_scoped_stage_stats


def preserve_selection_pins_while_hidden(snapshot: Mapping[str, Any]) -> bool:
    source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    return selection_key(snapshot) is not None and source.get("selection_window_active") is True


class OverlayStageRuntime:
    """跨 Host tick 保存 completion tracker 和当前 epoch 的统计固定范围。"""

    def __init__(self, *, wait_seconds: float = 2.0, cache: ScopedStatsCache | None = None) -> None:
        self.pin = SelectionStatsPin(wait_seconds=wait_seconds, cache=cache)
        self.completion_tracker = SelectionCompletionTracker()

    def observe(self, event: Mapping[str, Any], *, game_instance_id: str = "") -> dict[str, Any]:
        self.completion_tracker.observe(event, game_instance_id=game_instance_id)
        return self.completion_tracker.status()

    def reset_scope(self) -> None:
        self.pin.reset()

    def resolve(
        self,
        event: Mapping[str, Any],
        context: Mapping[str, Any],
        snapshot_view: SnapshotViewPort | None,
    ) -> PinnedStatsScope:
        return self.pin.resolve(
            event,
            context,
            snapshot_view,
            completed_selection_count=self.completion_tracker.resolved_completed_count,
        )

    @staticmethod
    def project(
        state: GameSessionState,
        scope: PinnedStatsScope,
        snapshot_view: SnapshotViewPort | None,
    ) -> GameSessionState:
        return apply_scoped_stage_stats(
            state,
            stage_context=scope.stage_context,
            scoped_view=scope.scoped_view,
            scope_status=scope.status,
            scope_reason=scope.reason,
            snapshot_status=snapshot_view.status() if snapshot_view is not None else {},
        )


def verify_packaged_scoped_stats(source: Any) -> dict[str, Any]:
    """从已安装 cohort 懒加载一个有效英雄，证明包内 source run 可达。"""

    result_status: dict[str, Any] = {"available": False, "reason": "snapshot_unavailable"}
    try:
        snapshot_view = source.open_view()
        champions = snapshot_view.get_champions() if snapshot_view is not None else []
        cache = ScopedStatsCache()
        if snapshot_view is not None:
            for champion in champions[:8]:
                champion_id = str(champion.get("id") or "")
                if not champion_id:
                    continue
                loaded = cache.load(snapshot_view, champion_id)
                result_status = {
                    "available": loaded.available,
                    "reason": loaded.reason,
                    "generation_id": loaded.generation_id,
                    "run_id": loaded.run_id,
                    "champion_id": champion_id,
                }
                if loaded.available:
                    break
    except Exception as exc:
        result_status = {"available": False, "reason": type(exc).__name__}
    return result_status


__all__ = [
    "OverlayStageRuntime",
    "preserve_selection_pins_while_hidden",
    "verify_packaged_scoped_stats",
]

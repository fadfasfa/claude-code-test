"""Overlay selection epoch 的 champion、Stage 与 scoped stats 固定器。

Generation 仍由 ``SelectionGenerationPin`` 按游戏局决定；本模块在同一 selection key 上
最多等待两秒获取等级，然后把 champion、Stage、ARAMKit run 和单英雄 view 一次
固定。固定后 API 恢复、等级变化或后台 generation 更新都只能影响下一轮。
"""

from __future__ import annotations

import time
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from hextech.interfaces.overlay.generation_pin import SelectionKey, selection_key
from hextech.modules.data.ports import SnapshotViewPort
from hextech.modules.data.scoped_stats import ScopedStatsCache, ScopedStatsView
from hextech.modules.game_context.stage_context import StageContextV1, resolve_stage_context


DEFAULT_STAGE_CONTEXT_WAIT_SECONDS = 2.0


@dataclass(frozen=True)
class StageConfirmation:
    """仅在输入到达时生成的有界内存凭据；不携带数据代或统计数值。"""

    selection_key: SelectionKey
    game_instance_id: str
    window_hwnd: int
    sidecar_instance_id: str
    stage_context: StageContextV1
    boundary_at: float
    confirmed_at: float
    details: Mapping[str, Any]


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
    confirmation: Mapping[str, Any] = field(default_factory=dict)

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
            "confirmation": dict(self.confirmation),
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
        wall_now: Callable[[], float] = time.time,
        wait_seconds: float = DEFAULT_STAGE_CONTEXT_WAIT_SECONDS,
    ) -> None:
        self.cache = cache or ScopedStatsCache()
        self._now = now
        self._wall_now = wall_now
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._key: SelectionKey | None = None
        self._started_at = 0.0
        self._pinned: PinnedStatsScope | None = None
        self._latest = PinnedStatsScope(None, "", StageContextV1())
        self._boundary = 0.0
        self._candidate: tuple[Any, ...] | None = None
        self._last_seq = 0
        self._last_sample = 0.0
        self._confirmation: dict[str, Any] = {}
        self._input_confirmation: StageConfirmation | None = None
        self._identity: tuple[Any, ...] | None = None

    def reset(self) -> None:
        self._key = None
        self._started_at = 0.0
        self._pinned = None
        self._latest = PinnedStatsScope(None, "", StageContextV1())
        self._candidate = None
        self._last_seq = 0
        self._last_sample = 0.0
        self._confirmation = {}
        self._input_confirmation = None
        self._identity = None

    def _begin(self, event: Mapping[str, Any], boundary: float | None) -> SelectionKey | None:
        key = selection_key(event)
        source = event.get("source") or {}
        identity = (key, source.get("game_instance_id"), source.get("window_hwnd"), source.get("sidecar_instance_id"))
        if key is None:
            self.reset()
            return None
        if identity != self._identity:
            self.reset()
            self._identity, self._key = identity, key
            self._boundary = boundary if boundary is not None else self._wall_now()
            self._started_at = self._now() - max(0.0, self._wall_now() - self._boundary)
        return key

    def observe(
        self, event: Mapping[str, Any], context: Mapping[str, Any], *,
        completed_selection_count: int | None, confirmation_started_at: float | None = None,
    ) -> StageConfirmation | None:
        """纯内存取证；调用者串行化输入，不等待 snapshot 或 scoped 文件。"""

        key = self._begin(event, confirmation_started_at)
        if key is None:
            return None
        if self._input_confirmation is not None:
            if self._input_confirmation.stage_context.champion_id == str(context.get("champion_id") or ""):
                return self._input_confirmation
            # 英雄身份改变也不能复用第一份样本，更不能重新获得两秒预算。
            self._input_confirmation = None
            self._candidate = None
        elapsed = max(0.0, self._now() - self._started_at)
        stage = resolve_stage_context(context, completed_selection_count=completed_selection_count)
        if elapsed > self.wait_seconds or not self._confirmed(event, context, stage):
            return None
        source = event.get("source") or {}
        self._input_confirmation = StageConfirmation(
            key, str(source.get("game_instance_id") or ""), int(source.get("window_hwnd") or 0),
            str(source.get("sidecar_instance_id") or ""),
            stage, self._boundary, self._wall_now(), dict(self._confirmation),
        )
        return self._input_confirmation

    def _confirmed(self, event: Mapping[str, Any], context: Mapping[str, Any], stage: StageContextV1) -> bool:
        source = event.get("source") or {}
        game = str(source.get("game_instance_id") or "")
        identity_ok = bool(context.get("ok") and not context.get("source_conflict") and game
                           and game == context.get("game_instance_id")
                           and source.get("window_hwnd")
                           and source.get("window_hwnd") == context.get("window_hwnd"))
        if identity_ok and stage.resolution_source == "selection_count":
            self._confirmation = {"method": "selection_count", "boundary_at": self._boundary}
            return True
        try:
            start, end, published = (float(context.get(k) or 0) for k in
                                     ("sample_started_at", "sample_completed_at", "published_at"))
            seq = int(context.get("publication_seq") or 0)
        except (TypeError, ValueError, OverflowError):
            start = end = published = 0.0
            seq = 0
        publisher = str(context.get("publisher_instance_id") or "")
        valid = (identity_ok and stage.status == "ready" and context.get("source") == "live-client-data"
                 and publisher and seq > 0 and all(math.isfinite(v) for v in (start, end, published))
                 and self._boundary < start <= end <= published <= self._wall_now()
                 and self._wall_now() - end <= 3.0)
        if not valid:
            self._candidate = None
            return False
        candidate = (publisher, game, stage.champion_id, stage.stage)
        if candidate != self._candidate:
            self._candidate, self._last_seq, self._last_sample = candidate, seq, start
            return False
        if seq <= self._last_seq or start <= self._last_sample:
            return False
        self._confirmation = {"method": "post_selection_live_samples", "boundary_at": self._boundary,
                              "publisher_instance_id": publisher, "publication_seq": seq,
                              "previous_publication_seq": self._last_seq,
                              "sample_started_at": start, "sample_completed_at": end}
        return True

    def resolve(
        self,
        event: Mapping[str, Any],
        context: Mapping[str, Any],
        snapshot_view: SnapshotViewPort | None,
        *,
        completed_selection_count: int | None,
        confirmation_started_at: float | None = None,
        input_confirmation: StageConfirmation | None = None,
    ) -> PinnedStatsScope:
        key = self._begin(event, confirmation_started_at)
        if key is None:
            self.reset()
            return self._latest
        if self._pinned is not None:
            self._latest = self._pinned
            return self._pinned

        generation_id = _generation_id(snapshot_view)
        stage_context = resolve_stage_context(
            context,
            completed_selection_count=completed_selection_count,
        )
        source = event.get("source") or {}
        receipt = input_confirmation
        receipt_matches = bool(
            isinstance(receipt, StageConfirmation) and receipt.selection_key == key
            and receipt.game_instance_id == str(source.get("game_instance_id") or "")
            and receipt.window_hwnd == source.get("window_hwnd")
            and receipt.sidecar_instance_id == str(source.get("sidecar_instance_id") or "")
            and context.get("ok") and not context.get("source_conflict")
            and context.get("game_instance_id") == receipt.game_instance_id
            and context.get("window_hwnd") == receipt.window_hwnd
            and receipt.stage_context.champion_id == stage_context.champion_id
            and receipt.boundary_at == self._boundary
            and self._boundary <= receipt.confirmed_at <= self._boundary + self.wait_seconds
        )
        elapsed = max(0.0, self._now() - self._started_at)
        confirmed = receipt_matches or elapsed <= self.wait_seconds and self._confirmed(event, context, stage_context)
        if receipt_matches and receipt is not None:
            stage_context = receipt.stage_context
            self._confirmation = dict(receipt.details)
        if not confirmed and elapsed < self.wait_seconds:
            self._latest = PinnedStatsScope(
                selection_key=key,
                generation_id=generation_id,
                stage_context=replace(stage_context, stage=None, status="unknown"),
                status="preparing",
                reason="stage_context_waiting",
                frozen=False,
            )
            return self._latest

        if not confirmed:
            self._pinned = PinnedStatsScope(
                key, generation_id, replace(stage_context, stage=None, status="unknown", reason="stage_unconfirmed"),
                status="unavailable", reason="stage_unconfirmed", frozen=True,
                confirmation={"boundary_at": self._boundary, "method": "timeout"},
            )
            self._latest = self._pinned
            return self._pinned

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
                confirmation=dict(self._confirmation),
            )
        self._latest = self._pinned
        return self._pinned

    def status(self) -> dict[str, Any]:
        return self._latest.to_status()


__all__ = [
    "DEFAULT_STAGE_CONTEXT_WAIT_SECONDS",
    "PinnedStatsScope",
    "StageConfirmation",
    "SelectionStatsPin",
]

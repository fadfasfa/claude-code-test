"""ARAM Overlay 的独立阶段上下文。

本模块把 Live Client 等级和 Vision 已确认选择次数收敛为 Stage 1–4；它不扩展
共享 ``GameContext``，也不读取 Live Client、数据文件或 UI 状态。Host 只在新的
selection epoch 固定一次结果，因而同一轮的等级更新和重掷不会切换统计范围。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


STAGE_CONTEXT_SCHEMA_VERSION = 1


def coerce_player_level(value: object) -> int | None:
    """只接受 LoL 合法的整数等级，拒绝布尔值、非整数和越界值。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric.is_integer():
        return None
    level = int(numeric)
    return level if 1 <= level <= 18 else None


def stage_from_level(value: object) -> int | None:
    level = coerce_player_level(value)
    if level is None or level < 3:
        return None
    if level <= 6:
        return 1
    if level <= 10:
        return 2
    if level <= 14:
        return 3
    return 4


def stage_from_completed_selections(value: object) -> int | None:
    """只有已确认的 0–3 次选择能推导下一阶段；其他值保持 unknown。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not numeric.is_integer():
        return None
    completed = int(numeric)
    return completed + 1 if 0 <= completed <= 3 else None


def _float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else default


@dataclass(frozen=True)
class StageContextV1:
    """一次 observation 的阶段事实；固定生命周期由 Overlay pin 管理。"""

    game_instance_id: str = ""
    champion_id: str = ""
    player_level: int | None = None
    stage: int | None = None
    observed_at: float = 0.0
    source: str = ""
    status: str = "unknown"
    resolution_source: str = "unknown"
    reason: str = ""
    conflict: bool = False
    completed_selection_count: int | None = None
    schema_version: int = STAGE_CONTEXT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_stage_context(
    payload: Mapping[str, Any],
    *,
    completed_selection_count: int | None,
    now: float | None = None,
) -> StageContextV1:
    """等级优先；等级缺失时才使用本局已确认选择次数。"""

    timestamp = time.time() if now is None else float(now)
    champion_id = str(payload.get("champion_id") or "").strip()
    game_instance_id = str(payload.get("game_instance_id") or payload.get("session_id") or "").strip()
    observed_at = _float(payload.get("generated_at"), timestamp)
    player_level = coerce_player_level(payload.get("player_level"))
    level_stage = stage_from_level(player_level)
    count_stage = stage_from_completed_selections(completed_selection_count)
    conflict = level_stage is not None and count_stage is not None and level_stage != count_stage

    if not champion_id:
        return StageContextV1(
            game_instance_id=game_instance_id,
            observed_at=observed_at,
            source=str(payload.get("source") or ""),
            status="unavailable",
            reason="context_missing",
            completed_selection_count=completed_selection_count,
        )
    if level_stage is not None:
        return StageContextV1(
            game_instance_id=game_instance_id,
            champion_id=champion_id,
            player_level=player_level,
            stage=level_stage,
            observed_at=observed_at,
            source=str(payload.get("source") or ""),
            status="ready",
            resolution_source="player_level",
            reason="stage_context_conflict" if conflict else "",
            conflict=conflict,
            completed_selection_count=completed_selection_count,
        )
    if count_stage is not None:
        return StageContextV1(
            game_instance_id=game_instance_id,
            champion_id=champion_id,
            player_level=player_level,
            stage=count_stage,
            observed_at=observed_at,
            source=str(payload.get("source") or ""),
            status="ready",
            resolution_source="selection_count",
            reason="player_level_missing_fallback_selection_count",
            completed_selection_count=completed_selection_count,
        )
    return StageContextV1(
        game_instance_id=game_instance_id,
        champion_id=champion_id,
        player_level=player_level,
        observed_at=observed_at,
        source=str(payload.get("source") or ""),
        status="unknown",
        reason="stage_unknown",
        completed_selection_count=completed_selection_count,
    )


class SelectionCompletionTracker:
    """只统计 Vision 明确确认结束的 epoch；重掷和场景误消失都不计数。"""

    def __init__(self) -> None:
        self._game_instance_id = ""
        self._confirmed: set[tuple[str, int]] = set()
        self._baseline_observed = False

    @property
    def completed_count(self) -> int:
        return len(self._confirmed)

    @property
    def game_instance_id(self) -> str:
        return self._game_instance_id

    @property
    def resolved_completed_count(self) -> int | None:
        """只有见过 epoch 0 或真实完成事件时，零值才是可信事实。"""

        return self.completed_count if self._baseline_observed else None

    def reset(self, game_instance_id: str = "") -> None:
        self._game_instance_id = str(game_instance_id or "").strip()
        self._confirmed.clear()
        self._baseline_observed = False

    def observe(self, event: Mapping[str, Any], *, game_instance_id: str = "") -> int:
        source_value = event.get("source")
        source = source_value if isinstance(source_value, Mapping) else {}
        observed_game = str(
            game_instance_id
            or source.get("game_instance_id")
            or source.get("session_id")
            or ""
        ).strip()
        if observed_game and self._game_instance_id and observed_game != self._game_instance_id:
            self.reset(observed_game)
        elif observed_game and not self._game_instance_id:
            self._game_instance_id = observed_game

        raw_epoch = source.get("selection_epoch")
        try:
            epoch = int(raw_epoch or 0)
        except (TypeError, ValueError):
            epoch = 0
        if observed_game and raw_epoch is not None and epoch == 0:
            self._baseline_observed = True
        if source.get("selection_confirmed") is not True:
            return self.completed_count
        if epoch <= 0:
            return self.completed_count
        session_id = str(source.get("session_id") or observed_game or "").strip()
        if session_id:
            self._confirmed.add((session_id, epoch))
            self._baseline_observed = True
        return self.completed_count

    def status(self) -> dict[str, Any]:
        return {
            "game_instance_id": self._game_instance_id,
            "completed_selection_count": self.completed_count,
            "completed_selection_count_known": self._baseline_observed,
            "confirmed_epochs": [list(item) for item in sorted(self._confirmed)],
        }


__all__ = [
    "STAGE_CONTEXT_SCHEMA_VERSION",
    "SelectionCompletionTracker",
    "StageContextV1",
    "coerce_player_level",
    "resolve_stage_context",
    "stage_from_completed_selections",
    "stage_from_level",
]

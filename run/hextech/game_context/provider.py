"""把兼容期 ClientContext 转换为稳定 GameContext DTO。"""

from __future__ import annotations

import time
from typing import Any, Mapping

from hextech.client_context import ClientContextProvider
from hextech.contracts import ChampionId, GameContext, GameSessionId, HealthState


class TypedGameContextProvider:
    def __init__(self, *, ttl_seconds: float = 8.0) -> None:
        self._legacy = ClientContextProvider(ttl_seconds=ttl_seconds)

    @property
    def session_id(self) -> str:
        return self._legacy.session_id

    def update(self, payload: Mapping[str, Any], *, now: float | None = None, source: str = "lcu") -> GameContext:
        return self._convert(self._legacy.update(payload, now=now, source=source))

    def unavailable(self, error_code: str, *, now: float | None = None, source: str = "lcu") -> GameContext:
        return self._convert(self._legacy.unavailable(error_code, now=now, source=source))

    def not_in_champ_select(self, *, now: float | None = None, source: str = "lcu") -> GameContext:
        return self._convert(self._legacy.not_in_champ_select(now=now, source=source))

    def _convert(self, context: Any) -> GameContext:
        health = HealthState.READY
        if context.connection_state == "degraded":
            health = HealthState.DEGRADED
        elif context.connection_state == "disconnected":
            health = HealthState.UNAVAILABLE
        return GameContext(
            session_id=GameSessionId(context.session_id),
            observed_at=float(context.updated_at or time.time()),
            local_champion_id=ChampionId(context.local_champion_id) if context.local_champion_id else None,
            teammate_champion_ids=tuple(ChampionId(value) for value in context.teammate_champion_ids),
            bench_champion_ids=tuple(ChampionId(value) for value in context.bench_champion_ids),
            phase=context.phase,
            source=context.source,
            health=health,
            error_code=context.error_code,
        )

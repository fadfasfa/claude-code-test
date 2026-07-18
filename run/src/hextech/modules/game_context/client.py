"""League Client 选人上下文的统一解析与短暂断连保留。

桌面、Web 和 Overlay 应复用本模块产生的角色语义；本模块不访问 LCU、不负责 UI，
也不会改变英雄列表排序。短暂断连只保留最近一次未过期上下文，过期后明确清空。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from hextech.contracts.identifiers import optional_champion_id


CLIENT_CONTEXT_SCHEMA_VERSION = 1
DEFAULT_CONTEXT_TTL_SECONDS = 8.0


def _champion_id(value: object) -> str:
    normalized = optional_champion_id(value)
    return str(normalized or "")


def _cell_id(value: object) -> str:
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True)
class ClientContext:
    schema_version: int = CLIENT_CONTEXT_SCHEMA_VERSION
    phase: str = "not_in_champ_select"
    connection_state: str = "connected"
    local_cell_id: int | str | None = None
    local_champion_id: str = ""
    team_champion_ids: tuple[str, ...] = ()
    teammate_champion_ids: tuple[str, ...] = ()
    bench_champion_ids: tuple[str, ...] = ()
    updated_at: float = 0.0
    source: str = "lcu"
    error_code: str = ""
    session_id: str = ""

    @property
    def selected_champion_ids(self) -> tuple[str, ...]:
        return self.team_champion_ids

    def candidate_groups(self, *, include_roles: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "selected_champion_ids": list(self.selected_champion_ids),
            "bench_champion_ids": list(self.bench_champion_ids),
        }
        if include_roles:
            payload.update(
                {
                    "local_champion_id": self.local_champion_id,
                    "teammate_champion_ids": list(self.teammate_champion_ids),
                    "context_phase": self.phase,
                    "context_connection_state": self.connection_state,
                    "context_error_code": self.error_code,
                }
            )
        return payload


def parse_client_context(payload: Mapping[str, Any], *, now: float | None = None, source: str = "lcu") -> ClientContext:
    """把一次 champ-select session 解析成稳定角色上下文。"""

    timestamp = time.time() if now is None else float(now)
    if not isinstance(payload, Mapping):
        return ClientContext(connection_state="disconnected", updated_at=timestamp, source=source, error_code="invalid_payload")
    team = payload.get("myTeam", [])
    bench = payload.get("benchChampions", [])
    if not isinstance(team, list) or not isinstance(bench, list):
        return ClientContext(connection_state="disconnected", updated_at=timestamp, source=source, error_code="invalid_payload")
    local_cell_id = payload.get("localPlayerCellId")
    local_champion_id = ""
    team_ids: list[str] = []
    teammate_ids: list[str] = []
    for player in team:
        if not isinstance(player, Mapping):
            continue
        champion_id = _champion_id(player.get("championId"))
        if not champion_id:
            continue
        if champion_id not in team_ids:
            team_ids.append(champion_id)
        if _cell_id(player.get("cellId")) == _cell_id(local_cell_id):
            local_champion_id = champion_id
        elif champion_id not in teammate_ids:
            teammate_ids.append(champion_id)
    selected = set(teammate_ids)
    if local_champion_id:
        selected.add(local_champion_id)
    bench_ids: list[str] = []
    for champion in bench:
        if not isinstance(champion, Mapping):
            continue
        champion_id = _champion_id(champion.get("championId"))
        if champion_id and champion_id not in selected and champion_id not in bench_ids:
            bench_ids.append(champion_id)
    return ClientContext(
        phase="champ_select",
        connection_state="connected",
        local_cell_id=local_cell_id,
        local_champion_id=local_champion_id,
        team_champion_ids=tuple(team_ids),
        teammate_champion_ids=tuple(teammate_ids),
        bench_champion_ids=tuple(bench_ids),
        updated_at=timestamp,
        source=source,
    )


class ClientContextProvider:
    """保存最近一次 LCU 上下文，并对短暂断连提供有界降级。"""

    def __init__(self, *, ttl_seconds: float = DEFAULT_CONTEXT_TTL_SECONDS) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._context: ClientContext | None = None
        self._session_id = uuid.uuid4().hex
        self._in_champ_select = False

    @property
    def session_id(self) -> str:
        return self._session_id

    def update(self, payload: Mapping[str, Any], *, now: float | None = None, source: str = "lcu") -> ClientContext:
        context = parse_client_context(payload, now=now, source=source)
        if context.connection_state == "disconnected":
            return self.unavailable(
                context.error_code or "invalid_payload",
                now=context.updated_at,
                source=source,
            )
        if not self._in_champ_select:
            self._session_id = uuid.uuid4().hex
        self._in_champ_select = True
        self._context = replace(context, session_id=self._session_id)
        return self._context

    def unavailable(self, error_code: str, *, now: float | None = None, source: str = "lcu") -> ClientContext:
        timestamp = time.time() if now is None else float(now)
        if (
            self._context is not None
            and self._context.connection_state != "disconnected"
            and timestamp - self._context.updated_at <= self.ttl_seconds
        ):
            return replace(
                self._context,
                connection_state="degraded",
                source=source,
                error_code=str(error_code or "lcu_unavailable"),
            )
        # 断连占位不参与 TTL 保留，否则下一次轮询会把空上下文重新标成 degraded。
        self._context = None
        return ClientContext(
            connection_state="disconnected",
            updated_at=timestamp,
            source=source,
            error_code=str(error_code or "lcu_unavailable"),
            session_id=self._session_id,
        )

    def not_in_champ_select(self, *, now: float | None = None, source: str = "lcu") -> ClientContext:
        timestamp = time.time() if now is None else float(now)
        self._in_champ_select = False
        self._context = ClientContext(
            phase="not_in_champ_select",
            connection_state="connected",
            updated_at=timestamp,
            source=source,
            session_id=self._session_id,
        )
        return self._context

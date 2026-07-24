"""Overlay Context 的单一生产发布者与来源仲裁。

桌面 Web/LCU 状态仍可服务主界面，但不得直接覆盖 canonical Context。本模块只保存
当前英雄的脱敏身份，不记录 LCU token、认证头或原始响应。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeGuard

from hextech.interfaces.overlay.context import (
    read_current_lcu_context_once,
    read_current_live_client_context_once,
    write_overlay_context,
)
from hextech.modules.game_context import TypedGameContextProvider
from hextech.modules.game_context.overlay_context import empty_overlay_context
from hextech.modules.vision.window import WindowProbeResult, probe_lol_game_window


CONTEXT_BROKER_PUBLISHER = "overlay-context-broker"
LIVE_CLIENT_PRIORITY = 300
LCU_FALLBACK_PRIORITY = 200
LCU_FALLBACK_TICKET_SECONDS = 120.0


class OverlayContextBroker:
    """把多来源 observation 收口为唯一 canonical publication。"""

    def __init__(
        self,
        *,
        context_path: str | Path | None = None,
        window_probe: Callable[[], WindowProbeResult] = probe_lol_game_window,
        live_reader: Callable[[], tuple[dict[str, Any] | None, str]] = read_current_live_client_context_once,
        lcu_reader: Callable[..., tuple[dict[str, Any] | None, str]] = read_current_lcu_context_once,
        now: Callable[[], float] = time.time,
        fallback_ticket_seconds: float = LCU_FALLBACK_TICKET_SECONDS,
    ) -> None:
        self.context_path = context_path
        self.window_probe = window_probe
        self.live_reader = live_reader
        self.lcu_reader = lcu_reader
        self.now = now
        self.fallback_ticket_seconds = max(0.0, float(fallback_ticket_seconds))
        self.publisher_instance_id = uuid.uuid4().hex
        self.context_provider = TypedGameContextProvider()
        self.publication_seq = 0
        self.context_revision = 0
        self._canonical_key: tuple[object, ...] | None = None
        self._selection_ticket: dict[str, Any] | None = None
        self._selection_ticket_seen_at = 0.0
        self._ticket_bound_game_instance = ""
        self._game_instance_id = ""

    @staticmethod
    def _valid_context(payload: Mapping[str, Any] | None) -> TypeGuard[dict[str, Any]]:
        """仅在可安全复制为 canonical payload 时收窄为 dict。"""

        return isinstance(payload, dict) and bool(str(payload.get("champion_id") or "").strip())

    def _read_lcu(self) -> tuple[dict[str, Any] | None, str]:
        return self.lcu_reader(context_provider=self.context_provider)

    def _fresh_selection_ticket(self, now: float) -> dict[str, Any] | None:
        if self._selection_ticket is None:
            return None
        if now - self._selection_ticket_seen_at > self.fallback_ticket_seconds:
            return None
        return deepcopy(self._selection_ticket)

    def _decorate(
        self,
        payload: Mapping[str, Any],
        *,
        now: float,
        probe: WindowProbeResult,
        source_priority: int,
        conflict: bool,
    ) -> dict[str, Any]:
        game_instance_id = str(probe.game_instance_id or "") if probe.status == "found" else ""
        window_hwnd = int(probe.hwnd or 0) if probe.status == "found" else 0
        result = dict(payload)
        result.setdefault("schema_version", 1)
        result.setdefault("error", "")
        result.update(
            {
                "generated_at": now,
                "published_at": now,
                "publisher": CONTEXT_BROKER_PUBLISHER,
                "publisher_instance_id": self.publisher_instance_id,
                "publication_seq": self.publication_seq,
                "game_instance_id": game_instance_id,
                "session_id": game_instance_id,
                "window_hwnd": window_hwnd,
                "window_process_id": int(probe.process_id or 0),
                "window_process_started_at": float(probe.process_started_at or 0.0),
                "identity_quality": str(probe.identity_quality or "unavailable"),
                "source_priority": int(source_priority),
                "source_conflict": bool(conflict),
            }
        )
        canonical_key = (
            game_instance_id,
            window_hwnd,
            str(result.get("champion_id") or ""),
            str(result.get("source") or ""),
            str(result.get("error") or ""),
            bool(conflict),
        )
        if canonical_key != self._canonical_key:
            self.context_revision += 1
            self._canonical_key = canonical_key
        result["context_revision"] = self.context_revision
        return result

    def poll_once(self, *, should_write: Callable[[], bool] | None = None) -> bool:
        """采集并发布一次；先仲裁后单点写入，禁止 observation 自行抢写。"""

        now = float(self.now())
        probe = self.window_probe()
        current_game_instance = str(probe.game_instance_id or "") if probe.status == "found" else ""
        if current_game_instance != self._game_instance_id:
            if (
                current_game_instance
                and self._ticket_bound_game_instance
                and self._ticket_bound_game_instance != current_game_instance
            ):
                self._selection_ticket = None
                self._selection_ticket_seen_at = 0.0
                self._ticket_bound_game_instance = ""
            self._game_instance_id = current_game_instance

        lcu_payload, lcu_error = self._read_lcu()
        if self._valid_context(lcu_payload):
            self._selection_ticket = deepcopy(lcu_payload)
            self._selection_ticket_seen_at = now
            self._ticket_bound_game_instance = current_game_instance

        live_payload: dict[str, Any] | None = None
        live_error = "game-window-missing"
        if probe.status == "found":
            live_payload, live_error = self.live_reader()

        conflict = bool(
            self._valid_context(live_payload)
            and self._valid_context(lcu_payload)
            and str(live_payload.get("champion_id")) != str(lcu_payload.get("champion_id"))
        )
        selected: dict[str, Any]
        priority = 0
        if self._valid_context(live_payload):
            selected = deepcopy(live_payload)
            priority = LIVE_CLIENT_PRIORITY
            if self._selection_ticket is not None:
                self._ticket_bound_game_instance = current_game_instance
        elif probe.status == "found":
            fallback = deepcopy(lcu_payload) if self._valid_context(lcu_payload) else self._fresh_selection_ticket(now)
            if self._valid_context(fallback):
                selected = dict(fallback)
                selected["source"] = "lcu-champ-select"
                selected["fallback_reason"] = live_error
                priority = LCU_FALLBACK_PRIORITY
                self._ticket_bound_game_instance = current_game_instance
            else:
                selected = empty_overlay_context(live_error or lcu_error or "context_missing")
                selected["source"] = "context-broker"
        elif self._valid_context(lcu_payload):
            selected = deepcopy(lcu_payload)
            priority = LCU_FALLBACK_PRIORITY
        else:
            selected = empty_overlay_context(lcu_error or "game-window-missing")
            selected["source"] = "context-broker"

        self.publication_seq += 1
        publication = self._decorate(
            selected,
            now=now,
            probe=probe,
            source_priority=priority,
            conflict=conflict,
        )
        if should_write is not None and not should_write():
            return False
        write_overlay_context(publication, self.context_path)
        return bool(publication.get("champion_id"))


__all__ = [
    "CONTEXT_BROKER_PUBLISHER",
    "LCU_FALLBACK_PRIORITY",
    "LIVE_CLIENT_PRIORITY",
    "OverlayContextBroker",
]

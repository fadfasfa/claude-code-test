"""Host 生产渲染前的英雄 Context 信任门控。

Parser 继续兼容旧 DTO；只有本门控决定某个 publication 是否能生成数字统计与联动。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

from hextech.interfaces.overlay.context_broker import CONTEXT_BROKER_PUBLISHER
from hextech.modules.game_context.overlay_context import empty_overlay_context


CONFIRM_RENDER_TICKS = 2
CONFIRMED_CONTEXT_HOLD_SECONDS = 8.0
TRUSTED_IDENTITY_QUALITIES = frozenset({"process", "window_fallback"})


@dataclass(frozen=True)
class ContextGateDecision:
    payload: dict[str, Any]
    state: str
    reason: str
    context_revision: int = 0
    held: bool = False


class ContextRenderGate:
    """按游戏身份、窗口、来源和 publication revision 过滤 Context。"""

    def __init__(self, *, confirm_ticks: int = CONFIRM_RENDER_TICKS, hold_seconds: float = CONFIRMED_CONTEXT_HOLD_SECONDS) -> None:
        self.confirm_ticks = max(1, int(confirm_ticks))
        self.hold_seconds = max(0.0, float(hold_seconds))
        self._identity_key: tuple[str, int] | None = None
        self._pending_key: tuple[object, ...] | None = None
        self._pending_ticks = 0
        self._confirmed_payload: dict[str, Any] | None = None
        self._confirmed_at = 0.0
        self._highest_confirmed_priority = 0
        self._highest_priority_champion = ""
        self._publisher_instance_id = ""
        self._last_publication_seq = 0

    def reset(self) -> None:
        self._identity_key = None
        self._pending_key = None
        self._pending_ticks = 0
        self._confirmed_payload = None
        self._confirmed_at = 0.0
        self._highest_confirmed_priority = 0
        self._highest_priority_champion = ""
        self._publisher_instance_id = ""
        self._last_publication_seq = 0

    @staticmethod
    def _waiting(reason: str, *, game_instance_id: str = "") -> ContextGateDecision:
        payload = empty_overlay_context(reason)
        payload["session_id"] = game_instance_id
        payload["game_instance_id"] = game_instance_id
        return ContextGateDecision(payload=payload, state="pending", reason=reason)

    def _clear_render_context(self, *, clear_priority: bool = False) -> None:
        """撤下可见数字并清空确认计数，避免拒绝前的 tick 被继续复用。"""

        self._pending_key = None
        self._pending_ticks = 0
        self._confirmed_payload = None
        self._confirmed_at = 0.0
        if clear_priority:
            self._highest_confirmed_priority = 0
            self._highest_priority_champion = ""

    def _hard_reject(self, reason: str, *, game_instance_id: str) -> ContextGateDecision:
        self._clear_render_context()
        return self._waiting(reason, game_instance_id=game_instance_id)

    def _hold_or_wait(
        self,
        reason: str,
        *,
        now: float,
        identity_key: tuple[str, int],
        publication: Mapping[str, Any],
    ) -> ContextGateDecision:
        same_identity = self._identity_key == identity_key
        broker_publication = str(publication.get("publisher") or "") == CONTEXT_BROKER_PUBLISHER
        publication_identity = (
            str(publication.get("game_instance_id") or ""),
            int(publication.get("window_hwnd") or 0),
        )
        if (
            same_identity
            and broker_publication
            and publication_identity == identity_key
            and self._confirmed_payload is not None
            and now - self._confirmed_at <= self.hold_seconds
        ):
            held_payload = dict(self._confirmed_payload)
            return ContextGateDecision(
                payload=held_payload,
                state="holding",
                reason=reason,
                context_revision=int(held_payload.get("context_revision") or 0),
                held=True,
            )
        self._clear_render_context()
        return self._waiting(reason, game_instance_id=identity_key[0])

    def evaluate(
        self,
        publication: Mapping[str, Any],
        *,
        game_instance_id: str,
        window_hwnd: int,
        vision_game_instance_id: str = "",
        vision_window_hwnd: int = 0,
        active: bool = True,
        now: float | None = None,
    ) -> ContextGateDecision:
        timestamp = time.time() if now is None else float(now)
        host_identity = (str(game_instance_id or ""), int(window_hwnd or 0))
        if not active or not host_identity[0] or host_identity[1] <= 0:
            self.reset()
            return self._waiting("context_game_identity_missing")
        if self._identity_key is not None and self._identity_key != host_identity:
            self.reset()
        self._identity_key = host_identity

        if str(publication.get("publisher") or "") != CONTEXT_BROKER_PUBLISHER:
            return self._hard_reject("context_untrusted_publisher", game_instance_id=host_identity[0])
        publisher_instance_id = str(publication.get("publisher_instance_id") or "").strip()
        try:
            publication_seq = int(publication.get("publication_seq") or 0)
            revision = int(publication.get("context_revision") or 0)
        except (TypeError, ValueError):
            publication_seq = 0
            revision = 0
        if not publisher_instance_id or publication_seq <= 0 or revision <= 0:
            return self._hard_reject("context_publication_metadata_missing", game_instance_id=host_identity[0])
        publication_identity = (
            str(publication.get("game_instance_id") or ""),
            int(publication.get("window_hwnd") or 0),
        )
        if publication_identity != host_identity:
            return self._hard_reject("context_game_identity_mismatch", game_instance_id=host_identity[0])
        if vision_game_instance_id and vision_game_instance_id != host_identity[0]:
            return self._hard_reject("context_vision_identity_mismatch", game_instance_id=host_identity[0])
        if vision_window_hwnd and int(vision_window_hwnd) != host_identity[1]:
            return self._hard_reject("context_vision_hwnd_mismatch", game_instance_id=host_identity[0])
        if str(publication.get("identity_quality") or "") not in TRUSTED_IDENTITY_QUALITIES:
            return self._hard_reject("context_identity_untrusted", game_instance_id=host_identity[0])

        try:
            published_at = float(publication.get("published_at") or publication.get("generated_at") or 0.0)
            process_started_at = float(publication.get("window_process_started_at") or 0.0)
        except (TypeError, ValueError):
            published_at = 0.0
            process_started_at = 0.0
        if published_at <= 0 or (process_started_at > 0 and published_at < process_started_at):
            return self._hard_reject("context_publication_stale", game_instance_id=host_identity[0])

        if self._publisher_instance_id and publisher_instance_id == self._publisher_instance_id:
            if publication_seq < self._last_publication_seq:
                return self._hard_reject("context_publication_replayed", game_instance_id=host_identity[0])
            self._last_publication_seq = max(self._last_publication_seq, publication_seq)
        else:
            # Broker 重启会生成新的 instance；旧确认和来源优先级不能跨 publisher 继承。
            self._clear_render_context(clear_priority=True)
            self._publisher_instance_id = publisher_instance_id
            self._last_publication_seq = publication_seq

        champion_id = str(publication.get("champion_id") or "").strip()
        if not publication.get("ok") or not champion_id:
            return self._hold_or_wait(
                str(publication.get("error") or "context_missing"),
                now=timestamp,
                identity_key=host_identity,
                publication=publication,
            )

        try:
            source_priority = int(publication.get("source_priority") or 0)
        except (TypeError, ValueError):
            return self._hard_reject("context_source_priority_invalid", game_instance_id=host_identity[0])
        if (
            self._highest_confirmed_priority > source_priority
            and self._highest_priority_champion
            and self._highest_priority_champion != champion_id
        ):
            return self._hard_reject("context_lower_priority_conflict", game_instance_id=host_identity[0])
        candidate_key = (
            host_identity[0],
            host_identity[1],
            publisher_instance_id,
            champion_id,
            str(publication.get("source") or ""),
            revision,
            source_priority,
        )
        confirmed_champion = str((self._confirmed_payload or {}).get("champion_id") or "")
        if confirmed_champion and confirmed_champion != champion_id:
            self._confirmed_payload = None
            self._confirmed_at = 0.0
        if candidate_key != self._pending_key:
            self._pending_key = candidate_key
            self._pending_ticks = 1
        else:
            self._pending_ticks += 1
        if self._pending_ticks < self.confirm_ticks:
            return self._waiting("context_confirming", game_instance_id=host_identity[0])

        self._confirmed_payload = dict(publication)
        self._confirmed_at = timestamp
        if source_priority >= self._highest_confirmed_priority:
            self._highest_confirmed_priority = source_priority
            self._highest_priority_champion = champion_id
        return ContextGateDecision(
            payload=dict(publication),
            state="confirmed",
            reason="context_confirmed",
            context_revision=revision,
        )


__all__ = ["ContextGateDecision", "ContextRenderGate"]

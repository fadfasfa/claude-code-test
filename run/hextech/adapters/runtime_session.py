"""把迁移期运行态 JSON 转为核心 DTO，并组合唯一推荐与会话状态。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from hextech.contracts import (
    AugmentId,
    ChampionId,
    GameContext,
    GameSessionId,
    GenerationId,
    HealthState,
    VisionEpoch,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)
from hextech.contracts.identifiers import optional_champion_id
from hextech.recommendation import RecommendationService
from hextech.session import SessionCoordinator


def game_context_from_runtime(payload: Mapping[str, Any], *, fallback_session_id: str = "") -> GameContext | None:
    champion_id = optional_champion_id(payload.get("champion_id"))
    session_id = str(payload.get("session_id") or fallback_session_id).strip()
    if not session_id:
        return None
    def normalized_ids(values: object) -> tuple[ChampionId, ...]:
        if not isinstance(values, (list, tuple)):
            return ()
        result: list[ChampionId] = []
        for item in values:
            normalized = optional_champion_id(item)
            if normalized is not None:
                result.append(normalized)
        return tuple(result)

    teammates = normalized_ids(payload.get("teammate_champion_ids", ()))
    bench = normalized_ids(payload.get("bench_champion_ids", ()))
    error = str(payload.get("error") or "")
    health = HealthState.READY if payload.get("ok") and champion_id else HealthState.DEGRADED
    return GameContext(
        session_id=GameSessionId(session_id),
        observed_at=float(payload.get("generated_at") or time.time()),
        local_champion_id=champion_id,
        teammate_champion_ids=teammates,
        bench_champion_ids=bench,
        phase=str(payload.get("phase") or "in_progress"),
        source=str(payload.get("source") or "runtime_context"),
        health=health,
        error_code=error or ("" if champion_id else "context_missing"),
    )


def vision_selection_from_runtime(payload: Mapping[str, Any], *, fallback_session_id: str = "") -> VisionSelection | None:
    source_value = payload.get("source")
    source: Mapping[str, Any] = source_value if isinstance(source_value, Mapping) else {}
    session_id = str(source.get("session_id") or fallback_session_id).strip()
    epoch = int(source.get("selection_epoch") or 0)
    if not session_id or epoch <= 0:
        return None
    try:
        scene_state = VisionSceneState(str(source.get("scene_state") or "absent"))
    except ValueError:
        scene_state = VisionSceneState.ABSENT
    raw_slots_value = payload.get("slots")
    slots_value: list[Any] = raw_slots_value if isinstance(raw_slots_value, list) else []
    slots: list[VisionSlot] = []
    for index in range(3):
        raw_value = slots_value[index] if index < len(slots_value) else None
        raw: Mapping[str, Any] = raw_value if isinstance(raw_value, Mapping) else {}
        try:
            state = VisionSlotState(str(raw.get("state") or "detecting"))
        except ValueError:
            state = VisionSlotState.DETECTING
        raw_id = str(raw.get("augment_id") or "").strip()
        slots.append(
            VisionSlot(
                index=index,
                state=state,
                augment_id=AugmentId(raw_id) if raw_id else None,
                name=str(raw.get("name") or ""),
                confidence=float(raw["confidence"]) if isinstance(raw.get("confidence"), (int, float)) else None,
                error_code=str(raw.get("diagnostic") or raw.get("error_code") or ""),
            )
        )
    return VisionSelection(
        session_id=GameSessionId(session_id),
        epoch=VisionEpoch(epoch),
        observed_at=float(payload.get("generated_at") or time.time()),
        scene_state=scene_state,
        slots=tuple(slots),
        health=HealthState.DEGRADED if any(slot.state is VisionSlotState.FAILED for slot in slots) else HealthState.READY,
        error_code=str(source.get("reason") or ""),
    )


def build_runtime_session(
    *,
    event: Mapping[str, Any],
    context_payload: Mapping[str, Any],
    snapshot_view: Any | None,
    user_enabled: bool,
    game_present: bool,
):
    source_value = event.get("source")
    source: Mapping[str, Any] = source_value if isinstance(source_value, Mapping) else {}
    event_session_id = str(source.get("session_id") or "")
    context = game_context_from_runtime(context_payload, fallback_session_id=event_session_id)
    context_session_id = str(context.session_id) if context else event_session_id
    vision = vision_selection_from_runtime(event, fallback_session_id=context_session_id)
    if context is not None and vision is not None and context.session_id != vision.session_id:
        vision = None
    generation_id = GenerationId("")
    recommendation = None
    if snapshot_view is not None:
        generation_id = GenerationId(str(snapshot_view.status().get("generation_id") or ""))
        if context is not None:
            recommendation = RecommendationService().build(context, snapshot_view, vision=vision)
    return SessionCoordinator().reduce(
        user_enabled=user_enabled,
        game_present=game_present,
        generation_id=generation_id,
        context=context,
        vision=vision,
        recommendation=recommendation,
    )

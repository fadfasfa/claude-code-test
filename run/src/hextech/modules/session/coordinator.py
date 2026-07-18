"""组合游戏、上下文、Vision 和推荐事实，不依赖具体 UI。"""

from __future__ import annotations

import time

from hextech.contracts import (
    GameContext,
    GameSessionId,
    GameSessionState,
    GenerationId,
    HealthState,
    OverlayVisibility,
    PresentationMode,
    RecommendationModel,
    SessionPhase,
    VisionSceneState,
    VisionSelection,
    VisionSlotState,
)


class SessionCoordinator:
    def reduce(
        self,
        *,
        user_enabled: bool,
        game_present: bool,
        generation_id: GenerationId,
        context: GameContext | None,
        vision: VisionSelection | None,
        recommendation: RecommendationModel | None,
    ) -> GameSessionState:
        session_id = (
            context.session_id
            if context is not None
            else vision.session_id
            if vision is not None
            else GameSessionId("unbound")
        )
        phase, mode, reason = self._resolve_phase(game_present, context, vision, recommendation)
        should_show = bool(user_enabled and game_present)
        if not user_enabled:
            mode, reason = PresentationMode.HIDDEN, "user_disabled"
        elif not game_present:
            mode, reason = PresentationMode.HIDDEN, "waiting_game"
        health_inputs = [
            HealthState.UNAVAILABLE
            if phase is SessionPhase.FAILED
            else HealthState.DEGRADED
            if phase is SessionPhase.DEGRADED
            else HealthState.READY,
            *(item.health for item in (context, vision, recommendation) if item is not None),
        ]
        severity = {HealthState.READY: 0, HealthState.DEGRADED: 1, HealthState.UNAVAILABLE: 2}
        health = max(health_inputs, key=severity.__getitem__)
        error_code = ""
        if health is not HealthState.READY:
            error_code = next(
                (
                    item.error_code
                    for item in (recommendation, context, vision)
                    if item is not None and item.error_code
                ),
                reason,
            )
        return GameSessionState(
            session_id=session_id,
            generation_id=generation_id,
            observed_at=time.time(),
            phase=phase,
            visibility=OverlayVisibility(user_enabled, game_present, should_show, mode, reason),
            context=context,
            vision=vision,
            recommendation=recommendation,
            health=health,
            error_code=error_code,
        )

    @staticmethod
    def _resolve_phase(
        game_present: bool,
        context: GameContext | None,
        vision: VisionSelection | None,
        recommendation: RecommendationModel | None,
    ) -> tuple[SessionPhase, PresentationMode, str]:
        if not game_present:
            return SessionPhase.WAITING_GAME, PresentationMode.HIDDEN, "waiting_game"
        if context is None or context.local_champion_id is None:
            return SessionPhase.WAITING_CONTEXT, PresentationMode.WAITING, "waiting_context"
        if vision is None or vision.scene_state is VisionSceneState.ABSENT:
            return SessionPhase.WAITING_SELECTION, PresentationMode.WAITING, "waiting_selection"
        if vision.scene_state is VisionSceneState.BLOCKED:
            return SessionPhase.DEGRADED, PresentationMode.DEGRADED, vision.error_code or "vision_blocked"
        if any(slot.state is VisionSlotState.FAILED for slot in vision.slots):
            return SessionPhase.FAILED, PresentationMode.FAILED, "detection_failed"
        if vision.scene_state is VisionSceneState.CANDIDATE or not recommendation:
            return SessionPhase.DETECTING, PresentationMode.WAITING, "detecting"
        return SessionPhase.READY, PresentationMode.CONTENT, "ready"

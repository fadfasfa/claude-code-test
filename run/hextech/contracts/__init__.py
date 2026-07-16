"""Hextech 核心模块共享的稳定契约。"""

from .identifiers import AugmentId, ChampionId, GenerationId, GameSessionId, ItemId, VisionEpoch
from .models import (
    GameContext,
    GameSessionState,
    HealthState,
    OverlayVisibility,
    PresentationMode,
    RecommendationModel,
    SessionPhase,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)

__all__ = [
    "AugmentId",
    "ChampionId",
    "GameContext",
    "GameSessionId",
    "GameSessionState",
    "GenerationId",
    "HealthState",
    "ItemId",
    "OverlayVisibility",
    "PresentationMode",
    "RecommendationModel",
    "SessionPhase",
    "VisionEpoch",
    "VisionSceneState",
    "VisionSelection",
    "VisionSlot",
    "VisionSlotState",
]

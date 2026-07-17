"""Hextech 核心模块共享的稳定契约。"""

from .identifiers import AugmentId, ChampionId, GenerationId, GameSessionId, ItemId, VisionEpoch
from .models import (
    GameContext,
    GameSessionState,
    FailureKind,
    HealthState,
    OverlayVisibility,
    PresentationMode,
    RecommendationModel,
    SessionPhase,
    SourceHealth,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)

__all__ = [
    "AugmentId",
    "ChampionId",
    "GameContext",
    "FailureKind",
    "GameSessionId",
    "GameSessionState",
    "GenerationId",
    "HealthState",
    "ItemId",
    "OverlayVisibility",
    "PresentationMode",
    "RecommendationModel",
    "SessionPhase",
    "SourceHealth",
    "VisionEpoch",
    "VisionSceneState",
    "VisionSelection",
    "VisionSlot",
    "VisionSlotState",
]

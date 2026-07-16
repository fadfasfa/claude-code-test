"""UI、数据、LCU 与 Vision 之间的版本化不可变 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .identifiers import AugmentId, ChampionId, GenerationId, GameSessionId, VisionEpoch


CONTRACT_SCHEMA_VERSION = 1


class HealthState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class VisionSceneState(StrEnum):
    ABSENT = "absent"
    CANDIDATE = "candidate"
    ACTIVE = "active"
    BLOCKED = "blocked"


class VisionSlotState(StrEnum):
    DETECTING = "detecting"
    READY = "ready"
    FAILED = "failed"


class SessionPhase(StrEnum):
    WAITING_GAME = "waiting_game"
    WAITING_CONTEXT = "waiting_context"
    WAITING_SELECTION = "waiting_selection"
    DETECTING = "detecting"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class PresentationMode(StrEnum):
    HIDDEN = "hidden"
    WAITING = "waiting"
    CONTENT = "content"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class GameContext:
    session_id: GameSessionId
    observed_at: float
    local_champion_id: ChampionId | None = None
    teammate_champion_ids: tuple[ChampionId, ...] = ()
    bench_champion_ids: tuple[ChampionId, ...] = ()
    phase: str = "not_in_champ_select"
    source: str = "lcu"
    health: HealthState = HealthState.READY
    error_code: str = ""
    schema_version: int = CONTRACT_SCHEMA_VERSION


@dataclass(frozen=True)
class VisionSlot:
    index: int
    state: VisionSlotState
    augment_id: AugmentId | None = None
    name: str = ""
    confidence: float | None = None
    error_code: str = ""


@dataclass(frozen=True)
class VisionSelection:
    session_id: GameSessionId
    epoch: VisionEpoch
    observed_at: float
    scene_state: VisionSceneState
    slots: tuple[VisionSlot, ...] = ()
    source: str = "vision"
    health: HealthState = HealthState.READY
    error_code: str = ""
    schema_version: int = CONTRACT_SCHEMA_VERSION


@dataclass(frozen=True)
class RecommendationModel:
    generation_id: GenerationId
    session_id: GameSessionId
    observed_at: float
    champion_candidates: tuple[dict[str, object], ...] = ()
    augment_slots: tuple[dict[str, object], ...] = ()
    item_recommendations: tuple[dict[str, object], ...] = ()
    champion_select_augments: tuple[dict[str, object], ...] = ()
    item_recommendations_status: str = "NOT_IMPLEMENTED"
    champion_select_augments_status: str = "NOT_IMPLEMENTED"
    health: HealthState = HealthState.READY
    error_code: str = ""
    schema_version: int = CONTRACT_SCHEMA_VERSION


@dataclass(frozen=True)
class OverlayVisibility:
    user_enabled: bool
    game_present: bool
    should_show: bool
    presentation_mode: PresentationMode
    reason_code: str


@dataclass(frozen=True)
class GameSessionState:
    session_id: GameSessionId
    generation_id: GenerationId
    observed_at: float
    phase: SessionPhase
    visibility: OverlayVisibility
    context: GameContext | None = None
    vision: VisionSelection | None = None
    recommendation: RecommendationModel | None = None
    health: HealthState = HealthState.READY
    error_code: str = ""
    diagnostics: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    schema_version: int = CONTRACT_SCHEMA_VERSION

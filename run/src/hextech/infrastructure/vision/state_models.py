"""Overlay 状态机的逐帧证据与单槽跟踪数据。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hextech.infrastructure.vision.matcher import SlotCandidate, unknown_slot


EVIDENCE_STARVED_OBSERVATIONS = 5
EVIDENCE_STARVED_SECONDS = 2.0


@dataclass(frozen=True)
class CandidateEvidence:
    """单次原始观察；miss 也占据窗口，frame_id 用于拒绝重复帧。"""

    observed_at: float
    candidate: SlotCandidate | None
    frame_id: int = 0
    fingerprint: str = ""
    rgb_sha256: str = ""


@dataclass
class SlotTrack:
    """单槽数据；状态转移由 ``slot_reducer.update_slot`` 唯一负责。"""

    candidate_identity: str = ""
    candidate_frames: int = 0
    stable_slot: dict[str, Any] | None = None
    weak_miss_frames: int = 0
    observations: list[CandidateEvidence] = field(default_factory=list)
    raw_observation_count: int = 0
    pending_started_at: float = 0.0
    slot_generation: int = 0
    last_frame_id: int = 0
    last_transition_frame_id: int = 0
    baseline_fingerprints: list[str] = field(default_factory=list)
    pending_replacement_fingerprint: str = ""
    pending_replacement_started_at: float = 0.0
    pending_replacement_observations: list[CandidateEvidence] = field(default_factory=list)
    replacement_in_progress: bool = False
    replacement_transition_reason: str = ""
    replacement_transition_started_at: float = 0.0
    replacement_previous_slot: dict[str, Any] | None = None
    transition_absent_frames: list[int] = field(default_factory=list)

    def clear(self) -> None:
        self.candidate_identity = ""
        self.candidate_frames = 0
        self.stable_slot = None
        self.weak_miss_frames = 0
        self.observations.clear()
        self.raw_observation_count = 0
        self.pending_started_at = 0.0
        self.slot_generation = 0
        self.last_frame_id = 0
        self.last_transition_frame_id = 0
        self.baseline_fingerprints.clear()
        self.pending_replacement_fingerprint = ""
        self.pending_replacement_started_at = 0.0
        self.pending_replacement_observations.clear()
        self.replacement_in_progress = False
        self.replacement_transition_reason = ""
        self.replacement_transition_started_at = 0.0
        self.replacement_previous_slot = None
        self.transition_absent_frames.clear()


def rendered_slot(index: int, track: SlotTrack) -> dict[str, Any]:
    if track.stable_slot is not None:
        return dict(track.stable_slot)
    pending = unknown_slot(index)
    pending["slot_generation"] = track.slot_generation
    return pending


def pending_temporal_state(track: SlotTrack, observed_at: float, *, window_size: int) -> str:
    counts: dict[str, int] = {}
    for item in track.observations[-window_size:]:
        if item.candidate is not None:
            counts[item.candidate.identity] = counts.get(item.candidate.identity, 0) + 1
    starved = bool(
        track.raw_observation_count >= EVIDENCE_STARVED_OBSERVATIONS
        and track.pending_started_at > 0.0
        and observed_at - track.pending_started_at >= EVIDENCE_STARVED_SECONDS
        and max(counts.values(), default=0) < 2
    )
    return "evidence_starved" if starved else "evidence_pending"


__all__ = ["CandidateEvidence", "SlotTrack", "pending_temporal_state", "rendered_slot"]

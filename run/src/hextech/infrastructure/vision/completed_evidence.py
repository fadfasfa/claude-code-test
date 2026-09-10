"""把异步 OCR 结论原位升级到它所属的原始 observation。"""

from __future__ import annotations

from typing import Any, Mapping, MutableSequence, Sequence
from dataclasses import replace

from hextech.infrastructure.vision.matcher import candidate_from_ocr_evidence
from hextech.infrastructure.vision.state_models import CandidateEvidence, SlotTrack


def _identity(candidate: Any) -> str:
    return str(getattr(candidate, "identity", "") or "")


def reconcile_ocr_owners(slots, candidates, reasons, *, observed_at):
    """Current/cache and delayed OCR share the same cross-slot ownership gate."""
    owners: dict[str, set[int]] = {}
    strong: dict[str, set[int]] = {}
    for index, track in enumerate(slots):
        for item in track.observations:
            if item.candidate is not None and item.candidate.rule == "ocr_exact_fallback" and 0 <= observed_at-item.observed_at <= 6:
                owners.setdefault(item.candidate.identity, set()).add(index)
        candidate = candidates[index]
        if candidate is not None:
            owners.setdefault(candidate.identity, set()).add(index)
            if candidate.evidence_grade == "strong":
                strong.setdefault(candidate.identity, set()).add(index)
        if track.stable_slot is not None:
            identity = str(track.stable_slot.get("recognition_key") or track.stable_slot.get("name") or track.stable_slot.get("augment_id") or "")
            owners.setdefault(identity, set()).add(index)
            strong.setdefault(identity, set()).add(index)
    for identity, indexes in owners.items():
        if len(indexes) < 2:
            continue
        permitted = strong.get(identity, set())
        kept = next(iter(permitted)) if len(permitted) == 1 else None
        for index in indexes:
            if index == kept:
                continue
            candidate = candidates[index]
            if candidate is not None and candidate.identity == identity:
                candidates[index] = None
                reasons[index] = "cross_slot_identity_conflict"
            track = slots[index]
            track.observations = [replace(item, candidate=None) if item.candidate is not None
                and item.candidate.rule == "ocr_exact_fallback" and item.candidate.identity == identity else item
                for item in track.observations]


def apply_completed_ocr_evidence(
    slots: Sequence[SlotTrack],
    completed: Sequence[Mapping[str, Any]],
    outcomes: MutableSequence[str],
    *,
    session_id: str,
    selection_epoch: int,
    current_candidates: Sequence[Any] = (),
) -> None:
    """只替换最近窗口中的同一帧票，不确认 READY 或改变 revision。"""

    candidates: list[Any | None] = []
    for item in completed:
        evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        try:
            candidate = candidate_from_ocr_evidence(int(evidence.get("slot_index", -1)), evidence)
        except (TypeError, ValueError):
            candidate = None
        candidates.append(candidate)

    for item, candidate in zip(completed, candidates, strict=True):
        evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
        try:
            index = int(evidence.get("slot_index", -1))
            generation = int(evidence.get("slot_generation") or 0)
            frame_id = int(evidence.get("captured_frame_id") or 0)
            valid = bool(
                str(evidence.get("session_id") or "") == session_id
                and int(evidence.get("selection_epoch") or 0) == selection_epoch
                and 0 <= index < len(slots)
                and generation == max(1, slots[index].slot_generation)
                and frame_id > 0
                and len(str(evidence.get("rgb_sha256") or "")) == 64
            )
        except (TypeError, ValueError):
            valid = False
            index = -1
            frame_id = 0
        if not valid:
            outcomes.append("ocr_result_stale")
            continue
        scene_negative = (
            evidence.get("scene_negative")
            if isinstance(evidence.get("scene_negative"), Mapping)
            else {}
        )
        if scene_negative.get("state") == "admitted":
            outcomes.append("scene_negative_consumed")
            continue
        if candidate is None:
            outcomes.append(str(evidence.get("reason") or "ocr_exact_gate_rejected"))
            continue
        identity = _identity(candidate)
        track = slots[index]
        current = current_candidates[index] if index < len(current_candidates) else None
        if current is not None and current.evidence_grade == "strong" and _identity(current) != identity:
            outcomes.append("ocr_template_conflict")
            continue
        fingerprint = str(evidence.get("perceptual_fingerprint") or "")
        observation_index = next(
            (
                position
                for position, observation in enumerate(track.observations)
                if observation.frame_id == frame_id
            ),
            -1,
        )
        if observation_index < 0:
            outcomes.append("evidence_window_expired")
            continue
        original = track.observations[observation_index]
        if original.fingerprint != fingerprint or original.rgb_sha256 != str(
            evidence.get("rgb_sha256") or ""
        ):
            outcomes.append("ocr_result_stale")
            continue
        if (original.candidate is not None and _identity(original.candidate) == identity
            and (original.candidate.rule == "ocr_exact_fallback" or original.candidate.evidence_grade == "strong")):
            outcomes.append("duplicate")
            continue
        original_template_id = str(item.get("template_candidate_id") or "")
        original_template_grade = str(item.get("template_evidence_grade") or "").casefold()
        window_strong_conflict = any(
            observation.candidate is not None
            and str(observation.candidate.evidence_grade).casefold() == "strong"
            and _identity(observation.candidate) != identity
            for observation in track.observations
        )
        if (
            original_template_grade == "strong"
            and original_template_id
            and original_template_id not in {identity, candidate.augment_id}
        ) or window_strong_conflict:
            outcomes.append("ocr_template_conflict")
            continue
        stable_conflict = any(
            owner != index
            and other.stable_slot is not None
            and str(
                other.stable_slot.get("recognition_key")
                or other.stable_slot.get("augment_id")
                or ""
            )
            == identity
            for owner, other in enumerate(slots)
        )
        if stable_conflict:
            outcomes.append("cross_slot_identity_conflict")
            continue
        observation_conflicts = [
            (other, position, observation)
            for owner, other in enumerate(slots)
            if owner != index
            for position, observation in enumerate(other.observations)
            if observation.candidate is not None
            and observation.candidate.rule == "ocr_exact_fallback"
            and _identity(observation.candidate) == identity
        ]
        if observation_conflicts:
            for other, position, observation in observation_conflicts:
                other.observations[position] = CandidateEvidence(
                    observation.observed_at,
                    None,
                    observation.frame_id,
                    observation.fingerprint,
                    observation.rgb_sha256,
                )
            outcomes.append("cross_slot_identity_conflict")
            continue
        track.observations[observation_index] = CandidateEvidence(
            original.observed_at,
            candidate,
            original.frame_id,
            original.fingerprint,
            original.rgb_sha256,
        )
        outcomes.append("upgraded")


__all__ = ["apply_completed_ocr_evidence"]

"""单帧多槽归约：先登记观察，再合并异步证据，最后统一发布。"""
from __future__ import annotations
from typing import Any, Mapping
from .matcher import arbitrate_slot_candidates, candidate_from_ocr_evidence, unknown_slot
from .slot_reducer import begin_explicit_transition, record_slot_observation, update_slot
from .completed_evidence import apply_completed_ocr_evidence, reconcile_ocr_owners
from .state_models import pending_temporal_state

SLOT_COUNT = 3


def reduce_slots(
    self,
    raw_event: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    observed_at: float,
    pending_only: bool = False,
) -> tuple[list[dict[str, Any]], set[int], list[Any], int]:
    """累计本帧逐槽证据；grace 期间只推进仍未确认且名称仍可见的槽。"""

    completed = raw_event.get("_completed_ocr_evidence")
    outcomes = raw_event.get("_completed_ocr_outcomes")
    raw_slots = raw_event.get("_raw_slots") if isinstance(raw_event.get("_raw_slots"), list) else []
    cursor_over_slots = {
        int(value)
        for value in (
            source.get("cursor_over_slots")
            if isinstance(source.get("cursor_over_slots"), list)
            else []
        )
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < SLOT_COUNT
    }
    candidates, rejection_reasons = arbitrate_slot_candidates(
        raw_slots,
        [track.stable_slot for track in self.slots],
        cursor_over_slots=cursor_over_slots,
        slot_count=SLOT_COUNT,
    )
    template_candidates = tuple(candidates)
    try:
        frame_id = int(source.get("frame_id") or 0)
    except (TypeError, ValueError):
        frame_id = 0
    session_id = str(source.get("session_id") or "")
    try:
        transition_slot = int(source.get("transition_slot"))
    except (TypeError, ValueError):
        transition_slot = -1
    transition_source = str(source.get("transition_source") or "")
    transition_kind = str(source.get("transition_kind") or "")
    transitioned = set()
    for index, track in enumerate(self.slots):
        if (source.get("selection_click") and transition_kind != "card"
            and (index in cursor_over_slots or (transition_source == "async_mouse_down" and transition_slot == index))):
            if begin_explicit_transition(track, observed_at=observed_at, frame_id=frame_id,
                                        reason="slot_click_async" if transition_source == "async_mouse_down" else "slot_click"):
                transitioned.add(index)
            elif (track.last_transition_frame_id == frame_id and track.replacement_transition_reason):
                # begin_frame已推进这次明确重随；finish只沿用待确认状态，不再推进代际。
                transitioned.add(index)
    for index in range(SLOT_COUNT):
        raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
        evidence = raw_slot.get("ocr_production") if isinstance(raw_slot.get("ocr_production"), Mapping) else None
        if evidence is None:
            continue
        track = self.slots[index]
        expected_generation = max(1, track.slot_generation)
        fingerprint = str(raw_slot.get("evidence_fingerprint") or "")
        try:
            binding_valid = bool(
                str(evidence.get("session_id") or "") == session_id
                and int(evidence.get("selection_epoch") or 0) == self.epoch
                and int(evidence.get("slot_index", -1)) == index
                and int(evidence.get("slot_generation") or 0) == expected_generation
                and int(evidence.get("captured_frame_id") or 0) == frame_id
                and str(evidence.get("perceptual_fingerprint") or "") == fingerprint
                and len(str(evidence.get("rgb_sha256") or "")) == 64
            )
        except (TypeError, ValueError):
            binding_valid = False
        if not binding_valid:
            if candidates[index] is None:
                rejection_reasons[index] = "ocr_result_stale"
            continue
        scene_negative = (
            evidence.get("scene_negative")
            if isinstance(evidence.get("scene_negative"), Mapping)
            else {}
        )
        if scene_negative.get("state") == "admitted":
            candidates[index] = None
            rejection_reasons[index] = "scene_negative_conflict"
            continue
        ocr_candidate = candidate_from_ocr_evidence(index, evidence)
        if ocr_candidate is None:
            if candidates[index] is None:
                rejection_reasons[index] = str(evidence.get("reason") or "ocr_exact_gate_rejected")
            continue
        template_candidate = candidates[index]
        if (
            template_candidate is not None
            and template_candidate.evidence_grade == "strong"
            and template_candidate.identity != ocr_candidate.identity
        ):
            candidates[index] = None
            rejection_reasons[index] = "ocr_template_conflict"
            continue
        stable_owner_conflict = any(
            owner != index
            and self.slots[owner].stable_slot is not None
            and str(
                self.slots[owner].stable_slot.get("recognition_key")
                or self.slots[owner].stable_slot.get("augment_id")
                or ""
            )
            == ocr_candidate.identity
            for owner in range(SLOT_COUNT)
        )
        if stable_owner_conflict:
            candidates[index] = None
            rejection_reasons[index] = f"cross_slot_stable_identity_conflict:{ocr_candidate.identity}"
            continue
        if template_candidate is None or template_candidate.evidence_grade != "strong":
            candidates[index] = ocr_candidate
            rejection_reasons[index] = ""
    name_residue = source.get("name_residue") if isinstance(source.get("name_residue"), list) else []
    recorded = []
    for index, track in enumerate(self.slots):
        raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
        frozen = index in cursor_over_slots or index in transitioned or (
            pending_only and (track.stable_slot is not None or index >= len(name_residue) or not name_residue[index]))
        recorded.append(record_slot_observation(track, raw_slot, observed_at=observed_at, frame_id=frame_id,
                                                candidate=None if frozen else candidates[index]))
    if isinstance(completed, list) and isinstance(outcomes, list):
        apply_completed_ocr_evidence(self.slots, completed, outcomes, session_id=session_id,
                                     selection_epoch=self.epoch, current_candidates=template_candidates)
    for index, track in enumerate(self.slots):
        if rejection_reasons[index] == "ocr_template_conflict":
            from dataclasses import replace
            track.observations = [replace(item, candidate=None) if item.candidate is not None
                                  and item.candidate.rule == "ocr_exact_fallback" else item
                                  for item in track.observations]
        elif recorded[index] and track.observations and track.observations[-1].frame_id == frame_id:
            candidates[index] = track.observations[-1].candidate
    reconcile_ocr_owners(self.slots, candidates, rejection_reasons, observed_at=observed_at)
    rendered_slots: list[dict[str, Any]] = []
    for index in range(SLOT_COUNT):
        track = self.slots[index]
        explicit_transition = bool(
            source.get("selection_click")
            and transition_kind != "card"
            and (
                index in cursor_over_slots
                or (transition_source == "async_mouse_down" and transition_slot == index)
            )
        )
        if index in cursor_over_slots or explicit_transition:
            if index in transitioned:
                pending = unknown_slot(index, diagnostic="replacement_transition")
                pending.update(
                    {
                        "slot_generation": track.slot_generation,
                        "temporal_state": "replacement_transition",
                        "replacement_reason": (
                            "slot_click_async" if transition_source == "async_mouse_down" else "slot_click"
                        ),
                        "rejection_reason": (
                            "slot_click_async" if transition_source == "async_mouse_down" else "slot_click"
                        ),
                        "observed_at": observed_at,
                    }
                )
                rendered_slots.append(pending)
                continue
            stable = track.stable_slot
            rendered_slots.append(dict(stable) if stable is not None else unknown_slot(index))
            continue
        if pending_only:
            if track.stable_slot is not None:
                rendered_slots.append(dict(track.stable_slot))
                continue
            name_still_visible = index < len(name_residue) and bool(name_residue[index])
            if not name_still_visible or candidates[index] is None:
                pending = unknown_slot(index)
                if pending_temporal_state(track, observed_at, window_size=5) == "evidence_starved":
                    pending.update(diagnostic="evidence_starved", temporal_state="evidence_starved")
                rendered_slots.append(pending)
                continue
        raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
        rendered, generation_changed = update_slot(
            index,
            track,
            raw_slot,
            observed_at=observed_at,
            frame_id=frame_id,
            candidate=candidates[index],
            rejection_reason=rejection_reasons[index],
            observation_recorded=recorded[index],
        )
        rendered_slots.append(rendered)
        if generation_changed:
            self._revision_changed = True
    if self._revision_changed:
        self.selection_revision = max(1, self.selection_revision + 1)
        self._revision_changed = False
    return rendered_slots, cursor_over_slots, raw_slots, frame_id

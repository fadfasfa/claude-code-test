"""Overlay 单槽识别与视觉代际 Reducer。

场景生命周期由 ``state.SelectionTracker`` 管理。本模块只处理已经通过场景门的
单槽观察：确认初始身份、保留 last-good，并以明确点击、内容消失或可信视觉证据确认换卡。
无法确认始终返回 detecting，不产生普通识别失败终态。
"""

from __future__ import annotations

from typing import Any, Mapping

from hextech.infrastructure.vision.matcher import SlotCandidate, unknown_slot
from hextech.infrastructure.vision.state_models import CandidateEvidence, SlotTrack, pending_temporal_state
from hextech.modules.recommendation.hints import normalize_augment_id


STRONG_WINDOW_SIZE = 3
STRONG_REQUIRED_HITS = 2
MEDIUM_WINDOW_SIZE = 5
MEDIUM_REQUIRED_HITS = 3
EVIDENCE_MAX_AGE_SECONDS = 6.0
BASELINE_FINGERPRINT_LIMIT = 8
TRANSITION_WINDOW_SECONDS = 2.0


def _clear_replacement_candidate(track: SlotTrack) -> None:
    track.pending_replacement_fingerprint = ""
    track.pending_replacement_started_at = 0.0
    track.pending_replacement_observations.clear()


def _clear_transition(track: SlotTrack) -> None:
    track.replacement_in_progress = False
    track.replacement_transition_reason = ""
    track.replacement_transition_started_at = 0.0
    track.replacement_previous_slot = None
    track.transition_absent_frames.clear()


def _remember_baseline_hash(track: SlotTrack, value: str) -> None:
    if len(value) == 64:
        if value in track.baseline_input_hashes:
            track.baseline_input_hashes.remove(value)
        track.baseline_input_hashes.append(value)
        del track.baseline_input_hashes[:-BASELINE_FINGERPRINT_LIMIT]


def _remember_baseline_fingerprint(track: SlotTrack, fingerprint: str) -> None:
    if not fingerprint:
        return
    if fingerprint in track.baseline_fingerprints:
        track.baseline_fingerprints.remove(fingerprint)
    track.baseline_fingerprints.append(fingerprint)
    del track.baseline_fingerprints[:-BASELINE_FINGERPRINT_LIMIT]


def _stable_identity(track: SlotTrack) -> str:
    stable = track.stable_slot or {}
    return str(
        stable.get("recognition_key")
        or normalize_augment_id(stable.get("name"))
        or stable.get("augment_id")
        or ""
    )


def _update_same_identity(
    track: SlotTrack,
    candidate: SlotCandidate,
    *,
    fingerprint: str,
    observed_at: float,
    rgb_sha256: str = "",
    update_baseline: bool = True,
) -> dict[str, Any]:
    if update_baseline:
        _remember_baseline_fingerprint(track, fingerprint)
        _remember_baseline_hash(track, rgb_sha256)
    _clear_replacement_candidate(track)
    track.transition_absent_frames.clear()
    stable = track.stable_slot or {}
    stable_variant = str(stable.get("visual_variant_id") or stable.get("augment_id") or "")
    candidate_variant = str(candidate.visual_variant_id or candidate.augment_id or "")
    if candidate_variant and candidate.evidence_grade == "strong" and candidate_variant != stable_variant:
        enriched = candidate.ready_slot()
        enriched.update(
            {
                "slot_generation": track.slot_generation,
                "temporal_state": "confirmed",
                "evidence_hits": max(1, track.candidate_frames),
                "evidence_window": len(track.observations),
                "observed_at": observed_at,
            }
        )
        track.stable_slot = enriched
    return dict(track.stable_slot or {})


def _begin_replacement_generation(
    track: SlotTrack,
    evidence: list[CandidateEvidence],
    *,
    reason: str,
    observed_at: float,
) -> None:
    previous = dict(track.stable_slot) if track.stable_slot is not None else track.replacement_previous_slot
    track.slot_generation = max(1, track.slot_generation) + 1
    track.candidate_identity = ""
    track.candidate_frames = 0
    track.stable_slot = None
    track.replacement_previous_slot = previous
    track.weak_miss_frames = sum(item.candidate is None for item in evidence)
    track.observations = list(evidence[-MEDIUM_WINDOW_SIZE:])
    track.raw_observation_count = len(track.observations)
    track.pending_started_at = evidence[0].observed_at if evidence else 0.0
    track.baseline_fingerprints.clear()
    track.baseline_input_hashes.clear()
    track.replacement_in_progress = True
    track.replacement_transition_reason = reason
    track.replacement_transition_started_at = observed_at
    track.transition_absent_frames.clear()


def begin_explicit_transition(
    track: SlotTrack,
    *,
    observed_at: float,
    frame_id: int,
    reason: str,
) -> bool:
    """明确逐槽点击触发 detecting；返回是否真的启动了新 slot generation。"""

    if track.replacement_in_progress or (frame_id > 0 and frame_id <= track.last_transition_frame_id):
        return False
    track.last_transition_frame_id = frame_id
    _clear_replacement_candidate(track)
    _begin_replacement_generation(
        track,
        [CandidateEvidence(observed_at, None, frame_id, "")],
        reason=reason,
        observed_at=observed_at,
    )
    track.last_frame_id = max(track.last_frame_id, frame_id)
    return True


def _observe_content_absence(
    track: SlotTrack,
    *,
    observed_at: float,
    frame_id: int,
) -> bool:
    """两次独立 flat content 才确认动画/消失 transition。"""

    if frame_id <= 0:
        return False
    if (
        track.replacement_transition_started_at > 0.0
        and observed_at - track.replacement_transition_started_at > TRANSITION_WINDOW_SECONDS
    ):
        track.transition_absent_frames.clear()
    track.replacement_transition_started_at = observed_at
    if frame_id not in track.transition_absent_frames:
        track.transition_absent_frames.append(frame_id)
        track.transition_absent_frames = track.transition_absent_frames[-2:]
    return len(track.transition_absent_frames) >= 2


def _confirm_ocr_replacement(
    track: SlotTrack,
    candidate: SlotCandidate,
    *,
    fingerprint: str,
    observed_at: float,
    frame_id: int,
    rgb_sha256: str,
) -> dict[str, Any] | None:
    """不同 OCR exact 先在 last-good 背后累计 3/5，确认时原子替换。"""

    window = track.observations[-MEDIUM_WINDOW_SIZE:]
    hits = sum(
        item.candidate is not None
        and item.candidate.identity == candidate.identity
        and item.candidate.rule == "ocr_exact_fallback"
        for item in window
    )
    first_ocr_at = min((item.observed_at for item in window if item.candidate is not None
                        and item.candidate.identity == candidate.identity
                        and item.candidate.rule == "ocr_exact_fallback"), default=observed_at)
    if hits < MEDIUM_REQUIRED_HITS or any(
        item.candidate is not None and item.candidate.evidence_grade == "strong"
        and item.candidate.identity != candidate.identity and item.observed_at >= first_ocr_at for item in window
    ):
        return None
    track.slot_generation = max(1, track.slot_generation) + 1
    stable_slot = candidate.ready_slot()
    stable_slot.update(
        {
            "required_frames": MEDIUM_REQUIRED_HITS,
            "slot_generation": track.slot_generation,
            "observed_frames": hits,
            "replacement_reason": "ocr_exact_transition",
            "temporal_state": "confirmed",
            "evidence_hits": hits,
            "evidence_window": len(window),
            "observed_at": observed_at,
        }
    )
    track.stable_slot = stable_slot
    track.pending_started_at = 0.0
    _clear_replacement_candidate(track)
    _clear_transition(track)
    track.observations.clear()
    track.baseline_fingerprints.clear()
    track.baseline_input_hashes.clear()
    for item in window:
        if (item.candidate is not None and item.candidate.identity == candidate.identity
            and item.candidate.rule == "ocr_exact_fallback"):
            _remember_baseline_fingerprint(track, item.fingerprint)
            _remember_baseline_hash(track, item.rgb_sha256)
    return dict(stable_slot)


def _append_observation(
    track: SlotTrack,
    *,
    observed_at: float,
    frame_id: int,
    fingerprint: str,
    candidate: SlotCandidate | None,
    rgb_sha256: str = "",
) -> None:
    track.raw_observation_count += 1
    if track.pending_started_at <= 0.0:
        track.pending_started_at = observed_at
    track.observations = [
        item for item in track.observations if observed_at - item.observed_at <= EVIDENCE_MAX_AGE_SECONDS
    ][-MEDIUM_WINDOW_SIZE:]
    track.observations.append(
        CandidateEvidence(observed_at, candidate, frame_id, fingerprint, rgb_sha256)
    )
    track.observations = track.observations[-MEDIUM_WINDOW_SIZE:]


def _candidate_confirmation(track: SlotTrack, candidate: SlotCandidate) -> tuple[str, int, int]:
    strong_window = track.observations[-STRONG_WINDOW_SIZE:]
    medium_window = track.observations[-MEDIUM_WINDOW_SIZE:]
    strong_hits = sum(
        item.candidate is not None
        and item.candidate.identity == candidate.identity
        and item.candidate.evidence_grade == "strong"
        for item in strong_window
    )
    medium_hits = sum(
        item.candidate is not None and item.candidate.identity == candidate.identity
        and ((candidate.rule == "ocr_exact_fallback") == (item.candidate.rule == "ocr_exact_fallback"))
        for item in medium_window
    )
    conflicting_strong_short = any(
        item.candidate is not None
        and item.candidate.evidence_grade == "strong"
        and item.candidate.identity != candidate.identity
        for item in strong_window
    )
    conflicting_strong = any(
        item.candidate is not None
        and item.candidate.evidence_grade == "strong"
        and item.candidate.identity != candidate.identity
        for item in medium_window
    )
    confirmed_as = ""
    if candidate.evidence_grade == "strong" and strong_hits >= STRONG_REQUIRED_HITS and not conflicting_strong_short:
        confirmed_as = "strong"
    elif medium_hits >= MEDIUM_REQUIRED_HITS and not conflicting_strong:
        confirmed_as = "medium"
    hits = strong_hits if candidate.evidence_grade == "strong" else medium_hits
    window_size = len(strong_window if candidate.evidence_grade == "strong" else medium_window)
    return confirmed_as, hits, window_size


def record_slot_observation(
    track: SlotTrack, raw_slot: Mapping[str, Any], *, observed_at: float,
    frame_id: int, candidate: SlotCandidate | None,
    captured_at: float = 0.0, revocation_eligible: bool = False,
    lexical_evidence: Mapping[str, Any] | None = None,
) -> bool:
    """每个独立捕获都占一票，包括稳定槽 miss；回填不增加窗口长度。"""
    if frame_id > 0 and frame_id <= track.last_frame_id:
        return False
    if frame_id > 0:
        track.last_frame_id = frame_id
    shadow = raw_slot.get("ocr_shadow") if isinstance(raw_slot.get("ocr_shadow"), Mapping) else {}
    production = raw_slot.get("ocr_production") if isinstance(raw_slot.get("ocr_production"), Mapping) else {}
    _append_observation(track, observed_at=observed_at, frame_id=frame_id,
                        fingerprint=str(raw_slot.get("evidence_fingerprint") or ""), candidate=candidate,
                        rgb_sha256=str(shadow.get("input_sha256") or production.get("rgb_sha256") or ""))
    from dataclasses import replace
    from .identity_revocation import bind_lexical_evidence
    observation = replace(track.observations[-1], captured_at=captured_at,
                          revocation_eligible=revocation_eligible)
    track.observations[-1] = bind_lexical_evidence(observation, lexical_evidence or {})
    return True


def update_slot(
    index: int,
    track: SlotTrack,
    raw_slot: Mapping[str, Any],
    *,
    observed_at: float,
    frame_id: int,
    candidate: SlotCandidate | None,
    rejection_reason: str = "",
    observation_recorded: bool = False,
    revocation_enabled: bool = False,
) -> tuple[dict[str, Any], bool]:
    """返回当前安全展示槽，以及本帧是否确认了新视觉 generation。"""

    if track.slot_generation <= 0:
        track.slot_generation = 1
    fingerprint = str(raw_slot.get("evidence_fingerprint") or "").strip()
    shadow = raw_slot.get("ocr_shadow") if isinstance(raw_slot.get("ocr_shadow"), Mapping) else {}
    production = (
        raw_slot.get("ocr_production")
        if isinstance(raw_slot.get("ocr_production"), Mapping)
        else {}
    )
    rgb_sha256 = str(shadow.get("input_sha256") or production.get("rgb_sha256") or "")

    # 相同真实帧不能重复累计证据。稳定槽保持 last-good，未稳定槽保持 detecting。
    if not observation_recorded and frame_id > 0 and track.last_frame_id >= frame_id:
        if track.stable_slot is not None:
            return dict(track.stable_slot), False
        pending = unknown_slot(index, diagnostic="duplicate_frame")
        pending.update({"slot_generation": track.slot_generation, "temporal_state": "duplicate_frame"})
        return pending, False
    if not observation_recorded:
        record_slot_observation(track, raw_slot, observed_at=observed_at, frame_id=frame_id, candidate=candidate)

    current_candidate = candidate
    if candidate is None and rejection_reason != "ocr_template_conflict":
        candidate = next((item.candidate for item in reversed(track.observations)
                          if item.candidate is not None and item.candidate.rule == "ocr_exact_fallback"
                          and _candidate_confirmation(track, item.candidate)[0]), None)

    generation_changed = False
    if track.stable_slot is not None:
        from .identity_revocation import contradiction_ready
        if contradiction_ready(track, str(track.stable_slot.get("canonical_id") or track.stable_slot.get("augment_id") or "")) and revocation_enabled:
            _begin_replacement_generation(track, [], reason="lexical_identity_revoked", observed_at=observed_at)
            track.pending_started_at = observed_at
            pending = unknown_slot(index, diagnostic="replacement_transition")
            pending.update(slot_generation=track.slot_generation, temporal_state="replacement_transition",
                           replacement_reason="lexical_identity_revoked", observed_at=observed_at)
            return pending, False
        if current_candidate is not None and current_candidate.identity == _stable_identity(track):
            latest = track.observations[-1] if track.observations else None
            stable_id = str(track.stable_slot.get("canonical_id") or track.stable_slot.get("augment_id") or "")
            lexical_conflict = bool(latest and latest.frame_id == frame_id and latest.lexical_candidate_ids
                                    and stable_id not in latest.lexical_candidate_ids)
            if lexical_conflict or (current_candidate.evidence_grade != "strong"
                                    and current_candidate.rule != "ocr_exact_fallback"):
                return dict(track.stable_slot), False
            return _update_same_identity(
                track,
                current_candidate,
                fingerprint=fingerprint,
                observed_at=observed_at,
                rgb_sha256=rgb_sha256,
                # A changed old template alone cannot bless new pixels before
                # delayed OCR reports its lexical contradiction.
                update_baseline=(current_candidate.rule == "ocr_exact_fallback"
                                 or fingerprint in track.baseline_fingerprints),
            ), False
        if candidate is not None and candidate.identity == _stable_identity(track):
            return dict(track.stable_slot), False
        if candidate is not None and candidate.rule == "ocr_exact_fallback":
            # OCR exact 本身已经绑定当前 session/epoch/slot generation 和真实帧。
            # 它必须在 last-good 背后完成 3/5 累计，第三票到达时才一次性切换；
            # 若先走两帧 visual transition，会制造 READY→detecting→READY 闪烁。
            replaced = _confirm_ocr_replacement(
                track,
                candidate,
                fingerprint=fingerprint,
                observed_at=observed_at,
                frame_id=frame_id,
                rgb_sha256=rgb_sha256,
            )
            return (replaced, True) if replaced is not None else (dict(track.stable_slot), False)
        if str(raw_slot.get("transition_observation") or "") == "content_absent":
            if _observe_content_absence(track, observed_at=observed_at, frame_id=frame_id):
                evidence = [
                    CandidateEvidence(observed_at, None, value, "")
                    for value in track.transition_absent_frames[-2:]
                ]
                _begin_replacement_generation(
                    track,
                    evidence,
                    reason="content_transition",
                    observed_at=observed_at,
                )
                pending = unknown_slot(index, diagnostic="replacement_transition")
                pending.update(
                    {
                        "slot_generation": track.slot_generation,
                        "temporal_state": "replacement_transition",
                        "replacement_reason": "content_transition",
                        "rejection_reason": "content_transition",
                        "observed_at": observed_at,
                    }
                )
                return pending, False
            return dict(track.stable_slot), False
        if fingerprint:
            if fingerprint in track.baseline_fingerprints:
                _clear_replacement_candidate(track)
                return dict(track.stable_slot), False
            # 普通 strong/双字体结果与感知指纹漂移都没有换卡授权；系统性误识别
            # 重复出现不会增加独立信息。仅点击、双帧 content-absent 或 OCR exact
            # 3/5 能替换当前 epoch 的 last-good。
            _clear_replacement_candidate(track)
            return dict(track.stable_slot), False
        elif candidate is None:
            # 历史事件没有感知指纹；单帧 miss 不得撤下同 epoch 的 last-good。
            return dict(track.stable_slot), False
    else:
        # A timed out transition remains unconfirmed. Never revive withdrawn identity.
        if (track.replacement_in_progress and track.replacement_transition_started_at > 0
            and observed_at - track.replacement_transition_started_at > TRANSITION_WINDOW_SECONDS):
            track.replacement_previous_slot = None

    if candidate is None:
        track.candidate_identity = ""
        track.candidate_frames = 0
        track.weak_miss_frames += 1
        temporal_state = pending_temporal_state(track, observed_at, window_size=MEDIUM_WINDOW_SIZE)
        pending = unknown_slot(index, diagnostic=temporal_state)
        pending.update(
            {
                "slot_generation": track.slot_generation,
                "temporal_state": temporal_state,
                "candidate_identity": "",
                "evidence_hits": 0,
                "evidence_window": len(track.observations),
                "required_hits": MEDIUM_REQUIRED_HITS,
                "rejection_reason": str(
                    rejection_reason or raw_slot.get("diagnostic") or raw_slot.get("reason") or temporal_state
                ),
                "observed_at": observed_at,
            }
        )
        return pending, generation_changed

    track.weak_miss_frames = 0
    track.candidate_identity = candidate.identity
    confirmed_as, hits, evidence_window = _candidate_confirmation(track, candidate)
    track.candidate_frames = hits

    # 没有生产指纹的 fixture/历史事件保持旧合同：stable 只允许 strong 替换。
    if track.stable_slot is not None and not fingerprint and confirmed_as != "strong":
        return dict(track.stable_slot), False

    if confirmed_as:
        replacing_stable = track.stable_slot is not None or track.replacement_in_progress
        if track.stable_slot is not None and not fingerprint:
            # 兼容历史/fixture：没有指纹时仍由既有 strong M-of-N 确认换卡。
            track.slot_generation = max(1, track.slot_generation) + 1
            generation_changed = True
        elif track.replacement_in_progress:
            generation_changed = True
        stable_slot = candidate.ready_slot()
        required_hits = STRONG_REQUIRED_HITS if confirmed_as == "strong" else MEDIUM_REQUIRED_HITS
        stable_slot.update(
            {
                "required_frames": required_hits,
                "slot_generation": track.slot_generation,
                "observed_frames": hits,
                "replacement_reason": (
                    f"{track.replacement_transition_reason}_confirmed"
                    if track.replacement_in_progress and track.replacement_transition_reason
                    else "replacement_confirmed"
                    if replacing_stable
                    else f"initial_{confirmed_as}"
                ),
                "temporal_state": "confirmed",
                "evidence_hits": hits,
                "evidence_window": evidence_window,
                "observed_at": observed_at,
            }
        )
        track.stable_slot = stable_slot
        track.pending_started_at = 0.0
        _clear_replacement_candidate(track)
        _clear_transition(track)
        # Baseline contains only the observations that actually confirm this ID.
        track.baseline_fingerprints.clear()
        track.baseline_input_hashes.clear()
        for item in track.observations:
            if item.candidate is not None and item.candidate.identity == candidate.identity:
                _remember_baseline_fingerprint(track, item.fingerprint)
                _remember_baseline_hash(track, item.rgb_sha256)

    if track.stable_slot is None:
        temporal_state = pending_temporal_state(track, observed_at, window_size=MEDIUM_WINDOW_SIZE)
        pending = unknown_slot(index, diagnostic=temporal_state)
        pending.update(
            {
                "slot_generation": track.slot_generation,
                "temporal_state": temporal_state,
                "candidate_identity": candidate.identity,
                "confidence": candidate.confidence,
                "top_candidates": [dict(item) for item in candidate.top_candidates],
                "evidence_grade": candidate.evidence_grade,
                "evidence_hits": hits,
                "evidence_window": evidence_window,
                "required_hits": STRONG_REQUIRED_HITS if candidate.evidence_grade == "strong" else MEDIUM_REQUIRED_HITS,
                "rejection_reason": str(raw_slot.get("diagnostic") or candidate.diagnostic),
                "replacement_reason": track.replacement_transition_reason,
                "observed_at": observed_at,
            }
        )
        return pending, generation_changed
    return dict(track.stable_slot), generation_changed


__all__ = ["begin_explicit_transition", "update_slot"]

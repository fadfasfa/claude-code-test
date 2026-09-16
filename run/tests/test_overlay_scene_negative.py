"""OCR 场景否定证据的严格词表、身份绑定与幂等归约。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hextech.infrastructure.vision.ocr_completed import (
    OcrEvidenceContext,
    build_production_evidence,
)
from hextech.infrastructure.vision.scene_negative import (
    BODY_SHARD_OCR_NAMES,
    SceneNegativeState,
    classify_scene_negative,
    evaluate_scene_negative,
)
from hextech.infrastructure.vision.state_models import CandidateEvidence, SlotTrack


KNOWN_BODY_SHARDS = {
    "坚不可摧碎片",
    "攻击速度碎片",
    "穿甲碎片",
    "魔法抗性碎片",
    "技能急速碎片",
    "迅捷碎片",
    "法术穿透碎片",
    "力量碎片",
}


def _context(*, slot: int, frame: int, generation: int = 1, captured_at: float = 100.0):
    return OcrEvidenceContext(
        session_id="game-r10-e8",
        selection_epoch=8,
        slot_index=slot,
        slot_generation=generation,
        input_sha256=f"{slot + frame:064x}",
        perceptual_fingerprint=f"fp-{slot}-{frame}",
        captured_frame_id=frame,
        captured_at=captured_at,
        template_candidate_id="1088" if slot == 2 else "",
        template_evidence_grade="medium" if slot == 2 else "",
    )


def _negative_evidence(
    name: str,
    *,
    slot: int,
    frame: int,
    confidence: float = 0.99,
    generation: int = 1,
    captured_at: float = 100.0,
):
    result = {
        "raw_text": name,
        "confidence": confidence,
        "match_rule": "",
        "matched_id": "",
        "matched_name": "",
    }
    return build_production_evidence(
        result,
        _context(slot=slot, frame=frame, generation=generation, captured_at=captured_at),
        minimum_confidence=0.95,
    )


def _raw_slot(evidence: dict, *, slot: int, frame: int) -> dict:
    return {
        "slot": slot,
        "evidence_fingerprint": f"fp-{slot}-{frame}",
        "ocr_shadow": {"input_sha256": evidence["rgb_sha256"]},
        "ocr_production": evidence,
        # r10 e8 中右槽曾由模板给出 1088/终极刷新；负面证据不能进入该正面池。
        "augment_id": "1088" if slot == 2 else "",
        "name": "终极刷新" if slot == 2 else "",
        "evidence_grade": "medium" if slot == 2 else "",
    }


def _tracker(*, frame: int = 5, generation: int = 1):
    slots = [SlotTrack(slot_generation=generation) for _ in range(3)]
    for index, track in enumerate(slots):
        track.observations.append(
            CandidateEvidence(
                100.0,
                None,
                frame,
                f"fp-{index}-{frame}",
                f"{index + frame:064x}",
            )
        )
    return SimpleNamespace(epoch=8, slots=slots)


def _event(frame: int, slots: list[dict], *, completed: list[dict] | None = None) -> dict:
    payload = {
        "active": True,
        "selection_type": "hextech",
        "source": {
            "session_id": "game-r10-e8",
            "scene_present": True,
            "scene_kind": "hextech",
            "frame_id": frame,
        },
        "_raw_slots": slots,
        "timing": {"captured_at": 100.0, "recognition_completed_at": 100.1},
    }
    if completed is not None:
        payload["_completed_ocr_evidence"] = completed
    return payload


def test_body_shard_vocabulary_is_full_name_exact_only() -> None:
    assert set(BODY_SHARD_OCR_NAMES) == KNOWN_BODY_SHARDS
    for name in KNOWN_BODY_SHARDS:
        matched = classify_scene_negative(name, 0.95)
        assert matched["state"] == "admitted"
        assert matched["scene_kind"] == "body_shard"
        assert matched["name"] == name
        assert matched["match_rule"] == "exact"

    assert classify_scene_negative("  魔法抗性碎片  ", 0.999)["state"] == "admitted"
    assert classify_scene_negative("魔法抗性", 0.999)["state"] == "rejected"
    assert classify_scene_negative("x魔法抗性碎片", 0.999)["state"] == "rejected"
    assert classify_scene_negative("魔法抗件碎片", 0.999)["state"] == "rejected"
    assert classify_scene_negative("魔法抗性碎片", 0.949999)["state"] == "rejected"


def test_production_evidence_keeps_negative_binding_out_of_positive_pool() -> None:
    evidence = _negative_evidence("魔法抗性碎片", slot=0, frame=5, confidence=0.999319)

    assert evidence["state"] == "rejected"
    assert evidence["canonical_id"] == ""
    assert evidence["acceptance_rule"] == ""
    assert evidence["reason"] == "ocr_exact_gate_rejected"
    assert evidence["scene_negative"] == {
        "schema_version": 1,
        "state": "admitted",
        "reason": "",
        "scene_kind": "body_shard",
        "name": "魔法抗性碎片",
        "normalized_text": "魔法抗性碎片",
        "confidence": 0.999319,
        "match_rule": "exact",
    }
    assert evidence["session_id"] == "game-r10-e8"
    assert evidence["selection_epoch"] == 8
    assert evidence["slot_generation"] == 1
    assert evidence["captured_frame_id"] == 5
    assert len(evidence["rgb_sha256"]) == 64
    assert evidence["captured_at"] == 100.0


def test_rf8_reviewed_names_are_negative_only_and_require_two_bound_slots():
    left = _negative_evidence("法术穿透碎片", slot=0, frame=5)
    right = _negative_evidence("力量碎片", slot=2, frame=5)
    for evidence in (left, right):
        assert evidence["state"] == "rejected" and evidence["canonical_id"] == ""
        assert evidence["scene_negative"]["state"] == "admitted"
    state, tracker = SceneNegativeState(), _tracker(frame=5)
    first = evaluate_scene_negative(state, _event(5, [_raw_slot(left, slot=0, frame=5)]), tracker, observed_at=100.1)
    assert first.kind == "conflict"
    both = evaluate_scene_negative(state, _event(5, [_raw_slot(left, slot=0, frame=5), {},
                                                   _raw_slot(right, slot=2, frame=5)]), tracker, observed_at=100.1)
    assert both.kind == "body_shard" and both.trusted_slots == (0, 2)
    for name in ("法术穿透碎片", "力量碎片"):
        assert classify_scene_negative(name, .949)["state"] == "rejected"
        assert classify_scene_negative("x"+name, .999)["state"] == "rejected"


def test_one_trusted_slot_conflicts_and_two_distinct_slots_latch_idempotently() -> None:
    state = SceneNegativeState()
    tracker = _tracker(frame=5)
    first = _negative_evidence("魔法抗性碎片", slot=0, frame=5, confidence=0.999319)
    second = _negative_evidence("技能急速碎片", slot=1, frame=5, confidence=0.996069)
    third = _negative_evidence("迅捷碎片", slot=2, frame=5, confidence=0.998907)

    decision = evaluate_scene_negative(
        state,
        _event(5, [_raw_slot(first, slot=0, frame=5)]),
        tracker,
        observed_at=100.1,
    )
    assert decision.kind == "conflict"
    assert decision.trusted_slots == (0,)

    # begin/finish 对同一 captured frame 重复归约不能新增一票。
    repeated = evaluate_scene_negative(
        state,
        _event(5, [_raw_slot(first, slot=0, frame=5)]),
        tracker,
        observed_at=100.1,
    )
    assert repeated.kind == "conflict"
    assert repeated.trusted_slots == (0,)
    assert "duplicate" in repeated.outcomes

    latched = evaluate_scene_negative(
        state,
        _event(
            5,
            [
                _raw_slot(first, slot=0, frame=5),
                _raw_slot(second, slot=1, frame=5),
                _raw_slot(third, slot=2, frame=5),
            ],
        ),
        tracker,
        observed_at=100.1,
    )
    assert latched.kind == "body_shard"
    assert latched.trusted_slots == (0, 1, 2)
    assert latched.matched_names == (
        "魔法抗性碎片",
        "技能急速碎片",
        "迅捷碎片",
    )


@pytest.mark.parametrize(
    ("mutation", "expected_outcome"),
    [
        (lambda evidence: evidence.update(selection_epoch=7), "rejected_context"),
        (lambda evidence: evidence.update(slot_generation=2), "rejected_context"),
        (lambda evidence: evidence.update(captured_frame_id=4), "rejected_frame_binding"),
        (lambda evidence: evidence.update(rgb_sha256="f" * 64), "rejected_frame_binding"),
        (lambda evidence: evidence.update(captured_at=99.0), "rejected_cutoff"),
        (lambda evidence: evidence.update(captured_at=100.2), "rejected_future"),
    ],
)
def test_completed_negative_rejects_old_context_frame_sha_cutoff_and_future(
    mutation, expected_outcome: str
) -> None:
    state = SceneNegativeState()
    tracker = _tracker(frame=5)
    evidence = _negative_evidence("迅捷碎片", slot=2, frame=5, captured_at=100.0)
    mutation(evidence)
    decision = evaluate_scene_negative(
        state,
        _event(6, [], completed=[{"evidence": evidence}]),
        tracker,
        observed_at=100.1,
        minimum_captured_at=99.5,
    )
    assert decision.kind == "none"
    assert expected_outcome in decision.outcomes


def test_completed_negative_accepts_only_original_recent_observation() -> None:
    state = SceneNegativeState()
    tracker = _tracker(frame=5)
    evidence = _negative_evidence("迅捷碎片", slot=2, frame=5, captured_at=100.0)

    decision = evaluate_scene_negative(
        state,
        _event(6, [], completed=[{"evidence": evidence}]),
        tracker,
        observed_at=100.1,
        minimum_captured_at=99.5,
    )

    assert decision.kind == "conflict"
    assert decision.trusted_slots == (2,)
    assert decision.matched_names == ("迅捷碎片",)

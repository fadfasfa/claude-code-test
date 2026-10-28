"""生产 OCR 兼容行为、canonical 碰撞与缓存诊断隔离。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from hextech.infrastructure.vision.ocr_completed import (
    OcrEvidenceContext,
    OcrBatch,
    build_inference_result,
    build_production_evidence,
    reliable_icon_candidate_id,
)
from hextech.infrastructure.vision.ocr_shadow import (
    OcrShadowRuntime,
    build_ocr_vocabulary,
    match_ocr_text,
    normalize_ocr_text,
)
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.infrastructure.vision.matcher import candidate_from_ocr_evidence


def _vocabulary(*names):
    return build_ocr_vocabulary([SimpleNamespace(augment_id=identity, name=name) for identity, name in names])


def _context(**changes):
    context = OcrEvidenceContext(
        session_id="collision-test", selection_epoch=1, slot_index=0, slot_generation=1,
        input_sha256="a" * 64, perceptual_fingerprint="same-name-roi", captured_frame_id=1,
        captured_at=1.0,
    )
    return replace(context, **changes)


def _evidence(text, vocabulary, *, confidence=0.99, context=None):
    result = {"raw_text": text, "confidence": confidence, **match_ocr_text(text, vocabulary)}
    return build_production_evidence(result, context or _context(), minimum_confidence=0.95)


def test_production_nfkc_spaces_and_case_keep_accepted_punctuation_compatibility():
    assert normalize_ocr_text(" 罪恶 快感！\n") == "罪恶快感"
    assert normalize_ocr_text("ＡＢＣ： １２３？") == "abc123"
    assert normalize_ocr_text("罪恶快感") == normalize_ocr_text("罪恶快感！")


def test_production_does_not_enable_unaccepted_strict_punctuation_candidate():
    vocabulary = _vocabulary(("1322", "罪恶快感"), ("3004", "罪恶快感！"))
    punctuated = _evidence("罪恶快感!", vocabulary)
    bare = _evidence("罪恶快感", vocabulary, confidence=0.994981)
    assert punctuated["state"] == "rejected"
    assert punctuated["reason"] == "ambiguous_identity"
    assert bare["state"] == "rejected"
    assert bare["reason"] == "ambiguous_identity"
    assert bare["canonical_id"] == ""
    assert bare["candidate_ids"] == ["1322", "3004"]
    assert bare["punctuation_collision_ids"] == ["1322", "3004"]
    assert _evidence("罪恶快感", tuple(reversed(vocabulary)))["reason"] == bare["reason"]


def test_nonambiguous_missing_punctuation_keeps_prior_production_compatibility():
    vocabulary = _vocabulary(("3004", "罪恶快感！"))
    assert _evidence("罪恶快感", vocabulary)["state"] == "admitted"
    assert _evidence("罪恶快感?", vocabulary)["state"] == "admitted"


def test_same_identity_aliases_merge_but_different_identity_collision_never_picks_first():
    aliases = _vocabulary(("7", "ＡＢＣ！"), ("7", "abc!"), ("7", "abc"))
    assert len(aliases) == 1
    assert _evidence("abc!", aliases)["canonical_id"] == "7"
    assert _evidence("abc", aliases)["canonical_id"] == "7"
    collision = _vocabulary(("7", "ABC！"), ("9", "abc!"))
    for vocabulary in (collision, tuple(reversed(collision)), (*collision, collision[0])):
        evidence = _evidence("abc!", vocabulary)
        assert evidence["state"] == "rejected"
        assert evidence["reason"] == "ambiguous_identity"
        assert evidence["candidate_ids"] == ["7", "9"]


def test_numeric_canonical_id_remains_vote_identity_even_if_names_normalize_equal():
    old = _evidence("罪恶快感", _vocabulary(("1322", "罪恶快感")))
    new = _evidence("罪恶快感!", _vocabulary(("3004", "罪恶快感！")))
    old_candidate = candidate_from_ocr_evidence(0, old)
    new_candidate = candidate_from_ocr_evidence(0, new)
    assert old_candidate is not None and new_candidate is not None
    assert old_candidate.identity == "1322"
    assert new_candidate.identity == "3004"
    assert old_candidate.recognition_key != new_candidate.recognition_key


def test_colliding_names_cannot_borrow_each_others_temporal_votes():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id, identity, name in ((1, "1322", "罪恶快感"), (2, "1322", "罪恶快感"), (3, "3004", "罪恶快感！")):
        evidence = _evidence(name, _vocabulary((identity, name)), context=_context(captured_frame_id=frame_id))
        event = {
            "active": True, "selection_type": "hextech",
            "source": {"session_id": "collision-test", "scene_present": True,
                "selection_window_active": True, "frame_id": frame_id},
            "_raw_slots": [{"slot": 0, "evidence_fingerprint": "same-name-roi", "ocr_production": evidence}],
            "timing": {"captured_at": float(frame_id), "recognition_completed_at": float(frame_id)},
        }
        assert tracker.update(event)["slots"][0]["state"] == "detecting"


@pytest.mark.parametrize("name", ["双刀流", "生机迸发", "有始有终", "极限超载：极速"])
def test_legitimate_short_or_punctuated_names_keep_exact_gate(name):
    evidence = _evidence(name, _vocabulary(("1401", name)))
    assert evidence["state"] == "admitted"
    assert evidence["match_rule"] == "exact"


def test_containment_and_character_substitution_never_enter_production():
    vocabulary = _vocabulary(("1401", "生机迸发"), ("1225", "双刀流"))
    assert _evidence("生机进发", vocabulary, confidence=0.999474)["reason"] == "exact_name_missing"
    containment = _evidence("廣双刀流", vocabulary)
    assert containment["match_rule"] == "unique_containment"
    assert containment["reason"] == "exact_name_missing"


def test_lexical_exact_evidence_is_separate_from_admission_and_fuzzy_diagnostics():
    vocabulary = _vocabulary(("1322", "罪恶快感"), ("3004", "罪恶快感！"), ("1401", "生机迸发"))
    collision = match_ocr_text(" 罪恶 快感！", vocabulary)
    assert collision["exact_name_key"] == "罪恶快感!"
    assert collision["exact_candidate_ids"] == ["1322", "3004"]
    assert collision["matched_id"] == ""
    assert match_ocr_text("前生机迸发", vocabulary)["exact_candidate_ids"] == []
    assert match_ocr_text("生机进发", vocabulary)["exact_candidate_ids"] == []


@pytest.mark.parametrize("confidence", [0.949999, 0.9499999, float("nan"), float("inf")])
def test_low_or_invalid_confidence_cannot_enter_exact_gate(confidence):
    assert _evidence("双刀流", _vocabulary(("1225", "双刀流")), confidence=confidence)["reason"] == "low_confidence"


def test_inference_confidence_does_not_round_up_across_point_95_floor():
    result = build_inference_result(
        "a" * 64, "双刀流", 0.9499999, match_ocr_text("双刀流", _vocabulary(("1225", "双刀流"))),
        elapsed_ms=1.0, task=OcrBatch((), ()), started_at=1.0,
    )
    assert build_production_evidence(result, _context(), minimum_confidence=0.95)["reason"] == "low_confidence"


def _icon_slot(identity="3004", confidence=0.99, margin=0.10):
    return {"channels": {"icon": {"margin": margin, "top_candidates": [
        {"augment_id": identity, "confidence": confidence},
    ]}}}


def test_icon_conflict_remains_diagnostic_and_never_disambiguates_collisions():
    vocabulary = _vocabulary(("1322", "罪恶快感"), ("3004", "罪恶快感！"))
    for slot in (_icon_slot("1322"), _icon_slot("1322", 0.617), _icon_slot("1322", margin=0.0)):
        context = _context(reliable_icon_candidate_id=reliable_icon_candidate_id(slot))
        assert _evidence("罪恶快感", vocabulary, context=context)["reason"] == "ambiguous_identity"
    unique = _vocabulary(("1225", "双刀流"))
    conflicting = _context(reliable_icon_candidate_id=reliable_icon_candidate_id(_icon_slot()))
    evidence = _evidence("双刀流", unique, context=conflicting)
    assert evidence["state"] == "admitted"
    assert evidence["diagnostic_reason"] == "icon_conflict"
    for slot in (_icon_slot(confidence=0.617), _icon_slot(margin=0.0)):
        context = _context(reliable_icon_candidate_id=reliable_icon_candidate_id(slot))
        assert _evidence("双刀流", unique, context=context)["state"] == "admitted"


def test_cache_reuses_only_lexical_result_and_rebinds_current_slot_diagnostic():
    class Recognizer:
        calls = 0

        def recognize(self, images):
            self.calls += 1
            return [("双刀流", 0.99)] * len(images)

    recognizer = Recognizer()
    runtime = OcrShadowRuntime(
        [SimpleNamespace(augment_id="1225", name="双刀流")], mode="admit",
        recognizer_factory=lambda _: recognizer,
    )
    try:
        image = Image.new("RGB", (30, 20), "white")
        runtime.set_production_context(session_id="collision-test", selection_epoch=1,
            slot_generations=[1], captured_frame_id=1, captured_at=1.0)
        runtime.observe([image], [{"evidence_fingerprint": "old-roi"}])
        assert runtime.wait_until_idle()
        original = runtime.drain_completed_evidence(session_id="collision-test", selection_epoch=1,
            slot_generations=[1], observed_at=1.1, scene_open=True)
        assert original[0]["evidence"]["state"] == "admitted"
        # Identical pixels, different slot generation and new independent icon evidence.
        runtime.set_production_context(session_id="collision-test", selection_epoch=1,
            slot_generations=[2], captured_frame_id=2, captured_at=2.0)
        slot = {"evidence_fingerprint": "new-roi", **_icon_slot()}
        runtime.observe([image], [slot])
        assert slot["ocr_production"]["state"] == "admitted"
        assert slot["ocr_production"]["diagnostic_reason"] == "icon_conflict"
        assert slot["ocr_production"]["slot_generation"] == 2
        assert slot["ocr_production"]["inference_reused"] is True
        assert recognizer.calls == 1
    finally:
        runtime.close()


def test_nonambiguous_punctuated_name_still_requires_three_independent_frames():
    vocabulary = _vocabulary(("3004", "罪恶快感！"))
    tracker = SelectionTracker(scene_enter_frames=1)
    observed = []
    for frame_id in (1, 1, 2, 3):
        evidence = _evidence("罪恶快感！", vocabulary, context=_context(captured_frame_id=frame_id))
        event = {
            "active": True, "selection_type": "hextech",
            "source": {"session_id": "collision-test", "scene_present": True,
                "selection_window_active": True, "frame_id": frame_id},
            "_raw_slots": [{"slot": 0, "evidence_fingerprint": "same-name-roi", "ocr_production": evidence}],
            "timing": {"captured_at": float(frame_id), "recognition_completed_at": float(frame_id)},
        }
        observed.append(tracker.update(event))
    assert [event["slots"][0]["state"] for event in observed] == ["detecting"] * 3 + ["ready"]
    assert observed[-1]["slots"][0]["augment_id"] == "3004"

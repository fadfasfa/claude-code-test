"""Offline contradiction replay and production withdrawal boundaries."""
from __future__ import annotations

from copy import deepcopy

import pytest

from hextech.infrastructure.vision.state import SelectionTracker
from hextech.infrastructure.vision.ocr_completed import OcrEvidenceContext, build_production_evidence
from hextech.interfaces.overlay.host_render_state import SlotRenderCache
from test_overlay_vision_state import _production_event, _ocr_slot


def event(frame, *, confidence=.94, ids=("new",), key="完整新名称", fingerprint="new-pixels",
          input_hash="b" * 64, generation=1, source_updates=None):
    raw = _ocr_slot(0, frame_id=frame, canonical_id="new", name="完整新名称",
                    fingerprint=fingerprint, slot_generation=generation)
    evidence = raw["ocr_production"]
    evidence.update(state="admitted" if confidence >= .95 else "rejected", confidence=confidence,
                    reason="" if confidence >= .95 else "low_confidence", rgb_sha256=input_hash,
                    captured_at=100 + frame / 10 - .01, exact_candidate_ids=list(ids), exact_name_key=key,
                    inference_reused=frame > 3,
                    inference_timing={"inference_started_at": 100.2, "inference_completed_at": 100.3})
    result = _production_event(frame, 100 + frame / 10, raw_slots={0: raw}, fingerprints={0: fingerprint},
                             source_updates={"game_instance_id": "production-session", **(source_updates or {})})
    result["selection_type"] = "hextech"
    return result


def tracker(*, enabled=True):
    result = SelectionTracker(scene_enter_frames=1, identity_revocation_enabled=enabled)
    for frame in (1, 2):
        raw = _production_event(frame, 100 + frame / 10,
                                source_updates={"game_instance_id": "production-session"})
        raw["_raw_slots"][0]["ocr_shadow"] = {"input_sha256": "a" * 64}
        result.update(raw)
    assert result.slots[0].baseline_fingerprints == ["stable-fp-0"]
    assert result.slots[0].baseline_input_hashes == ["a" * 64]
    return result


def test_rejected_ocr_lexical_evidence_is_bound_without_admitting_identity():
    context = OcrEvidenceContext("game", 4, 0, 2, "a" * 64, "fp", 8, 100)
    evidence = build_production_evidence({"confidence": .94, "matched_id": "new", "match_rule": "exact",
        "exact_candidate_ids": ["new"], "exact_name_key": "完整名称"}, context, minimum_confidence=.95)
    assert evidence["state"] == "rejected" and evidence["canonical_id"] == ""
    assert evidence["exact_candidate_ids"] == ["new"] and evidence["exact_name_key"] == "完整名称"


def test_low_score_withdraws_only_offline_and_requires_fresh_generation_votes():
    state = tracker()
    for frame in (3, 4):
        assert state.update(event(frame))["slots"][0]["state"] == "ready"
    withdrawn = state.update(event(5))
    assert withdrawn["slots"][0]["state"] == "detecting"
    assert withdrawn["slots"][0]["slot_generation"] == 2
    assert withdrawn["source"]["selection_revision"] == 1
    assert [s["state"] for s in withdrawn["slots"]][1:] == ["ready", "ready"]
    assert not state.slots[0].observations
    assert state.update(event(6, confidence=.99))["slots"][0]["state"] == "detecting"
    for frame in (7, 8, 9):
        ready = state.update(event(frame, confidence=.99, generation=2))
    assert ready["slots"][0]["augment_id"] == "new"
    assert ready["slots"][0]["slot_generation"] == 2
    assert ready["source"]["selection_revision"] == 2


def test_default_production_preserves_low_score_identity_and_records_denominators():
    state = tracker(enabled=False)
    for frame in (3, 4, 5):
        result = state.update(event(frame))
    assert result["slots"][0]["state"] == "ready"
    diagnostic = state.slots[0].identity_diagnostics
    assert diagnostic["ready_contradiction"] is True
    assert diagnostic["observation_count"] == 3
    assert diagnostic["total_observation_count"] == 5
    assert diagnostic["independent_capture_frames"] == 3
    assert diagnostic["actual_inference_count"] == 1


@pytest.mark.parametrize("override", [
    {"ids": ()}, {"ids": ("augment_a", "new")}, {"ids": ("augment_a",)}, {"key": ""},
    {"fingerprint": "stable-fp-0"}, {"input_hash": "a" * 64},
    {"source_updates": {"cursor_over_slots": [0]}},
    {"source_updates": {"game_instance_id": "other-game"}},
    {"generation": 0},
])
def test_noise_alias_old_candidate_frozen_and_stale_binding_do_not_withdraw(override):
    state = tracker()
    for frame in (3, 4, 5):
        result = state.update(event(frame, **override))
    assert result["slots"][0]["state"] == "ready"
    assert result["slots"][0]["slot_generation"] == 1


@pytest.mark.parametrize("variable,values", [("key", ["甲", "乙", "甲"]),
                                          ("input_hash", ["b" * 64, "c" * 64, "b" * 64])])
def test_name_and_hash_must_agree_across_consecutive_frames(variable, values):
    state = tracker()
    for frame, value in zip((3, 4, 5), values):
        result = state.update(event(frame, **{variable: value}))
    assert result["slots"][0]["state"] == "ready"


def test_repeated_and_out_of_order_frames_cannot_manufacture_three_captures():
    state = tracker()
    for frame in (3, 3, 2, 3, 4, 4):
        result = state.update(event(frame))
    assert result["slots"][0]["state"] == "ready"
    assert state.update(event(5))["slots"][0]["state"] == "detecting"


def test_unrelated_candidate_never_poison_confirmed_baseline():
    state = tracker()
    for frame in (3, 4):
        state.update(event(frame))
    assert state.slots[0].baseline_fingerprints == ["stable-fp-0"]
    assert state.slots[0].baseline_input_hashes == ["a" * 64]


def test_lexical_contradiction_does_not_let_wrong_old_template_poison_baseline():
    from support.vision_events import ready_slot
    state = tracker()
    for frame in (3, 4, 5):
        raw = event(frame)
        raw["_raw_slots"][0].update(ready_slot(0, "augment_a", "强化 A"))
        result = state.update(raw)
    assert result["slots"][0]["state"] == "detecting"


def test_old_template_drift_before_async_ocr_does_not_bless_new_pixels():
    from support.vision_events import ready_slot
    state = tracker()
    raw = event(3)
    raw["_raw_slots"][0].pop("ocr_production")
    raw["_raw_slots"][0].update(ready_slot(0, "augment_a", "强化 A"))
    raw["_raw_slots"][0]["ocr_shadow"] = {"input_sha256": "b" * 64}
    state.update(raw)
    assert state.slots[0].baseline_fingerprints == ["stable-fp-0"]
    assert state.slots[0].baseline_input_hashes == ["a" * 64]


def test_same_identity_old_votes_on_current_miss_cannot_advance_generation_or_baseline():
    state = tracker()
    for frame in (3, 4, 5):
        raw = event(frame, confidence=.99, ids=("augment_a",))
        raw["_raw_slots"][0]["ocr_production"].update(canonical_id="augment_a", name="强化 A")
        state.update(raw)
    miss = event(6, ids=(), fingerprint="unproven-pixels", input_hash="c" * 64)
    result = state.update(miss)
    assert result["slots"][0]["slot_generation"] == 1
    assert "unproven-pixels" not in state.slots[0].baseline_fingerprints
    assert "c" * 64 not in state.slots[0].baseline_input_hashes


def test_repeated_click_during_pending_transition_advances_generation_once():
    state = tracker()
    for frame in (3, 4):
        raw = event(frame, source_updates={"selection_click": True, "cursor_over_slots": [0]})
        result = state.update(raw)
    assert result["slots"][0]["state"] == "detecting"
    assert state.slots[0].slot_generation == 2


@pytest.mark.parametrize("reason", ["scoreboard_key_down", "game_not_foreground"])
def test_mouse_eligibility_change_flushes_queued_edges_with_reason(reason):
    from hextech.infrastructure.vision.mouse_transition import MouseTransitionObserver
    observer = MouseTransitionObserver(button_probe=lambda: True, cursor_probe=lambda: (100, 100), clock=lambda: 10)
    observer.update_context(window_hwnd=1, game_instance_id="g", selection_epoch=1, eligible=True)
    observer.poll_once()
    observer.update_context(window_hwnd=1, game_instance_id="g", selection_epoch=1, eligible=False, reason=reason)
    status = observer.status()
    assert status["queue_depth"] == 0
    assert status["eligibility_reason"] == reason and status["eligible"] is False
    assert status["drop_reasons"]["context_clear"] == 1


def test_paused_and_scoreboard_frames_preserve_identity_without_lexical_votes():
    state = tracker()
    state.update(event(3))
    assert state.pause("game_not_foreground")["source"]["scene_state"] == "paused"
    state.update(event(4, source_updates={"scoreboard_key_down": True}))
    result = state.update(event(5))
    assert result["slots"][0]["state"] == "ready"


def test_completed_low_score_binds_lexical_evidence_to_original_capture():
    state = tracker()
    for frame in (3, 4, 5):
        raw = event(frame)
        evidence = raw["_raw_slots"][0].pop("ocr_production")
        raw["_raw_slots"][0]["ocr_shadow"] = {"input_sha256": evidence["rgb_sha256"]}
        raw["_completed_ocr_evidence"] = [{"evidence": evidence}]
        raw["_completed_ocr_outcomes"] = []
        result = state.update(raw)
    assert result["slots"][0]["state"] == "detecting"


def test_host_generation_change_clears_stat_and_synergy_even_without_revision_change():
    cache = SlotRenderCache()
    snapshot = {"slots": [{"slot": i, "state": "ready", "augment_id": str(i), "slot_generation": 1}
                          for i in range(3)]}
    model = {"stats": [{"slot": i, "status_code": "READY", "stats_text": "55%", "synergy_status": "READY"}
                       for i in range(3)], "synergies": [{"slot": i, "text": "old"} for i in range(3)]}
    cache.merge(snapshot, model, current_selection_key=("game", 1), context_revision=1, generation_id="stats")
    changed = deepcopy(snapshot)
    changed["slots"][0] = {"slot": 0, "state": "detecting", "slot_generation": 2}
    shell = cache.build_shell(changed, current_selection_key=("game", 1))
    assert shell["stats"][0]["status_code"] == "DETECTING"
    assert shell["stats"][1]["stats_text"] == "55%"
    stale = cache.merge(changed, model, current_selection_key=("game", 1), context_revision=1, generation_id="stats")
    assert stale["stats"][0]["status_code"] == "DETECTING"
    assert [s["slot"] for s in stale["synergies"]] == [1, 2]

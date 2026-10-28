from hextech.infrastructure.vision.epoch_diagnostics import EpochRecognitionAccumulator


def observation(at, state="detecting", identity="", *, frame=1, generation=1, reason=""):
    return {"selection_type": "hextech", "selection_window_active": True, "scene_state": "active",
            "captured_at": at, "recognition_completed_at": at + .02, "captured_frame_id": frame,
            "selection_revision": 1,
            "slots": [{"state": state, "augment_id": identity, "slot_generation": generation,
                       "ocr_production": {"state": "rejected", "reason": reason}}]}


def test_reroll_before_any_ready_does_not_make_epoch_latency_a_card_latency():
    accumulator = EpochRecognitionAccumulator()
    accumulator.observe(observation(100, reason="ambiguous_identity"))
    # 未确认卡重随时，旧诊断里 slot_generation/revision 未必改变。
    accumulator.observe(observation(108, frame=40, reason="exact_name_missing"))
    accumulator.observe(observation(115.4, "ready", "1029", frame=80))
    accumulator.observe(observation(116, "ready", "2116", frame=90, generation=2))
    summary = accumulator.summary()
    assert summary["metric_scope"] == "selection_epoch"
    assert summary["card_latency_qualified"] is False
    slot = summary["slots"][0]
    assert slot["first_ready_ms"] == 15420
    assert slot["card_latency_ms"] is None
    assert slot["first_ready_identity"]["canonical_id"] == "1029"
    assert slot["first_ready_identity"]["frame_id"] == 80
    assert slot["ocr_rejection_counts"] == {"ambiguous_identity": 1, "exact_name_missing": 1}


def test_ocr_rejection_counters_are_observation_counts_and_bounded():
    accumulator = EpochRecognitionAccumulator()
    for i in range(40):
        accumulator.observe(observation(100 + i, frame=i+1, reason=f"reason-{i}"))
    counts = accumulator.summary()["slots"][0]["ocr_rejection_counts"]
    assert len(counts) == 16 and counts["other"] == 25
    assert sum(counts.values()) == 40

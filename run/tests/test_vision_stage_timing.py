from dataclasses import replace

from hextech.infrastructure.vision.ocr_completed import OcrBatch, OcrEvidenceContext, build_inference_result, build_production_evidence
from hextech.infrastructure.vision.sidecar_diagnostics import _selection_timeline_entry


def test_reused_inference_keeps_original_timing_but_rebinds_real_capture(monkeypatch):
    monkeypatch.setattr("hextech.infrastructure.vision.ocr_completed.time.time", lambda: 12.0)
    result = build_inference_result("hash", "测试", .99,
        {"match_rule": "exact", "matched_id": "123", "matched_name": "测试"}, elapsed_ms=60,
        task=OcrBatch((), (), submitted_wall_at=10.0), started_at=11.0)
    context = OcrEvidenceContext("game", 1, 0, 1, "hash", "fp", 7, 9.0)
    original = build_production_evidence(result, context, minimum_confidence=.95)
    monkeypatch.setattr("hextech.infrastructure.vision.ocr_completed.time.time", lambda: 14.0)
    reused = build_production_evidence(result, replace(context, captured_frame_id=8, captured_at=13.0),
                                       minimum_confidence=.95, cache_hit=True)
    assert original["inference_timing"] == reused["inference_timing"] == {
        "batch_first_queued_at": 10.0, "inference_started_at": 11.0, "inference_completed_at": 12.0}
    assert not original["inference_reused"] and reused["inference_reused"]
    assert reused["captured_frame_id"] == 8 and reused["captured_at"] == 13.0
    assert reused["state"] == "admitted"


def test_timeline_keeps_scene_and_reducer_clocks_without_replacing_existing_clock():
    timing = {"captured_at": 10., "recognition_completed_at": 10.2, "scene_evaluated_at": 10.01,
              "scene_admitted_at": 10.02, "identity_reduced_at": 10.21}
    entry = _selection_timeline_entry({"source": {"frame_id": 7}, "timing": timing}, 1)
    for key, value in timing.items():
        assert entry[key] == value

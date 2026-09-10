"""用实际 timeline producer 验证模板路径的帧身份，不向 fixture 注入假字段。"""
from hextech.infrastructure.vision.sidecar_diagnostics import _selection_timeline_entry
from test_overlay_truth_probe import _truth, _write_json, _write_timeline, _report


def test_template_ready_timeline_retains_runner_capture_identity(tmp_path):
    event = {
        "active": True, "selection_type": "hextech",
        "source": {
            "build_id": "build-a", "session_id": "session-a", "game_instance_id": "session-a",
            "sidecar_instance_id": "sidecar-a", "sidecar_pid": 4321, "frame_id": 1,
            "selection_epoch": 1, "selection_revision": 1,
            "scene_state": "active", "selection_window_active": True,
        },
        "timing": {"capture_started_at": 9.97, "captured_at": 10.0,
                   "recognition_completed_at": 10.05, "capture_status": "captured",
                   "observation_kind": "recognition"},
        "slots": [{"slot": i, "state": "ready", "augment_id": name, "name": name.upper(),
                   "slot_generation": 1, "acceptance_rule": "strong_dual"}
                  for i, name in enumerate(("a", "b", "c"))],
    }
    entry = _selection_timeline_entry(event, observation_seq=1)
    assert entry.get("captured_frame_id") == 1
    assert all(not slot["ocr_production"] for slot in entry["slots"])
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [entry])
    truth = _write_json(tmp_path / "truth.json", _truth(tmp_path, [("frame-1", 1, 10.0, "frame.png")]))
    report = _report(timeline, truth)
    assert report["qualified"] is True
    assert report["passed"] is True
    assert report["whole_real_game_go"] is False

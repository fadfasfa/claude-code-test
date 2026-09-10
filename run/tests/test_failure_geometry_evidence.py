"""失败ROI的坐标可追溯性，不改变公共事件或扩大图像采集。"""
from copy import deepcopy
from PIL import Image
from hextech.infrastructure.vision.failure_evidence import FailureEvidenceCollector


def test_failure_draft_keeps_exact_crop_geometry_and_capture_time(monkeypatch):
    drafts = []
    class Writer:
        def submit(self, draft):
            drafts.append(draft)
            return True
    frame = Image.new("RGB", (100, 80), "black")
    frame.info.update(hextech_capture_mode="roi_union", hextech_roi_origin=(10, 10), hextech_roi_size=(70, 60))
    event = {"source": {"session_id": "s", "game_instance_id": "g", "selection_epoch": 1,
        "frame_id": 12, "scene_present": True, "scene_state": "active", "layout_transform":
        {"dx_ratio": .01, "dy_ratio": -.02, "scale": 1.05, "unrelated_secret": "must-not-copy"}},
        "slots": [{"state": "detecting", "temporal_state": "evidence_starved"}]}
    raw = {"source": event["source"], "timing": {"captured_at": 123.5}, "_raw_slots": [{}]}
    before = deepcopy((event, raw))
    monkeypatch.setattr(FailureEvidenceCollector, "_boxes", staticmethod(
        lambda *_args: ((5, 12, 95, 35), (15, 40, 30, 60), (30, 60, 50, 75))))
    FailureEvidenceCollector(Writer(), pool_id="pool").observe(frame, raw, event, slot_generations=[1])
    assert len(drafts) == 1
    geometry = drafts[0].record["roi_geometry"]
    assert geometry["title_box"] == [5, 12, 95, 35]
    assert geometry["frame_size"] == [100, 80]
    assert geometry["capture_rect"] == [10, 10, 80, 70]
    assert geometry["title_inside_capture"] is False
    assert geometry["layout_transform"] == {"dx_ratio": .01, "dy_ratio": -.02, "scale": 1.05}
    assert drafts[0].record["captured_at"] == 123.5
    assert (event, raw) == before
    from hextech.infrastructure.vision.failure_evidence import _occurrence
    occurrence = _occurrence(drafts[0].record)
    assert occurrence["roi_geometry"] == geometry
    assert occurrence["captured_at"] == 123.5

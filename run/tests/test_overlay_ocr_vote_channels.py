"""OCR 与模板 medium 不得通过最后一帧通道变换互相冒充确认票。"""
from test_overlay_ocr_completed_evidence import _raw_slot, _review_event, _review_template_slot
from hextech.infrastructure.vision.state import SelectionTracker


def test_medium_template_cannot_confirm_using_two_prior_ocr_votes():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame in (1, 2):
        result = tracker.update(_review_event(frame, slots=[_raw_slot(0, frame, ocr=True)]))
        assert result["slots"][0]["state"] == "detecting"
    result = tracker.update(_review_event(3, slots=[_review_template_slot(3, medium=True)]))
    assert result["slots"][0]["state"] == "detecting"

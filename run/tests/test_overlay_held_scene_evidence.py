"""场景保留不能切断新证据，也不能扩大场景或身份授权。"""
from dataclasses import replace
from types import SimpleNamespace
import time

import pytest
from PIL import Image

from hextech.infrastructure.vision import sidecar_detection as detection
from hextech.infrastructure.vision.held_scene import CaptureBinding, HeldSceneEvidence
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.modules.vision.layout import LayoutTransform, SceneObservation


def _scene(present=True):
    return SceneObservation(present, "candidate" if present else "absent", .9 if present else .5,
                            "1920x1080", (870, 840, 1050, 890), .8, (.8, .8, .8),
                            LayoutTransform(), True)


def _setup(monkeypatch):
    monkeypatch.setattr(detection, "detect_selection_scene", lambda *a, **k: _scene(False))
    monkeypatch.setattr(detection, "_name_text_mask", lambda image: image.convert("L"))
    monkeypatch.setattr(detection, "_name_crop_has_residue", lambda *a, **k: True)
    monkeypatch.setattr(detection, "_body_shard_name_scores", lambda *a, **k: [0., 0., 0.])
    monkeypatch.setattr(detection, "_blocking_modal_present", lambda *a: False)
    return Image.new("RGB", (1920, 1080), "white")


def _binding():
    return CaptureBinding("game", 101, (0, 0, 1920, 1080), 1)


def _held():
    return HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))


def test_hold_reaches_detector_and_ocr_without_claiming_scene_present(monkeypatch):
    frame = _setup(monkeypatch)
    calls = []
    def detect(*args, **kwargs):
        calls.append(kwargs["eligible_slots"])
        return [{"slot": i, "diagnostic": "pending", "evidence_fingerprint": f"fp{i}"} for i in range(3)], {}
    monkeypatch.setattr(detection, "_detect_slots", detect)
    ocr = SimpleNamespace(observe=lambda images, slots, **kw: calls.append(kw["eligible_slots"]))
    event = detection.detect_overlay_choices(frame, [], held_scene=_held(),
                                            capture_binding=_binding(), ocr_shadow=ocr)
    assert calls == [(True, True, True), (True, True, True)]
    assert len(event["_raw_slots"]) == 3
    assert event["source"]["scene_present"] is False
    assert event["source"]["selection_window_active"] is False
    assert event["active"] is False
    assert event["source"]["hold_evidence_state"] == "collecting"


@pytest.mark.parametrize("change", [{"game_instance_id": "other"}, {"window_hwnd": 102},
    {"client_rect": (10, 0, 1930, 1080)}, {"selection_epoch": 2}])
def test_hold_rejects_changed_capture_binding(monkeypatch, change):
    frame = _setup(monkeypatch)
    monkeypatch.setattr(detection, "_detect_slots", lambda *a, **kw: pytest.fail("untrusted hold"))
    event = detection.detect_overlay_choices(frame, [], held_scene=_held(),
                                            capture_binding=replace(_binding(), **change))
    assert event["_raw_slots"] == []


@pytest.mark.parametrize("block", ["button", "modal", "fragment", "invalid_capture"])
def test_hold_rechecks_safety_before_new_evidence(monkeypatch, block):
    frame = _setup(monkeypatch)
    if block == "button":
        monkeypatch.setattr(detection, "detect_selection_scene", lambda *a, **k: replace(_scene(False), button_box=None))
    elif block == "modal":
        monkeypatch.setattr(detection, "_blocking_modal_present", lambda *a: True)
    elif block == "fragment":
        monkeypatch.setattr(detection, "_body_shard_name_scores", lambda *a, **k: [1., 1., 1.])
    else:
        frame.info.update(hextech_capture_mode="roi_union", hextech_roi_origin=(0, 500),
                          hextech_roi_size=(1920, 580), hextech_client_size=frame.size)
    monkeypatch.setattr(detection, "_detect_slots", lambda *a, **kw: pytest.fail("blocked evidence"))
    event = detection.detect_overlay_choices(frame, [], held_scene=_held(), capture_binding=_binding())
    assert not event.get("active")
    assert not event.get("_raw_slots")


def test_confirmed_context_requires_active_normal_scene_and_current_binding():
    tracker = SelectionTracker(scene_enter_frames=1)
    raw = {"selection_type": "hextech", "source": {"scene_present": True,
           "selection_button_present": True, "layout_transform": {"scale": 1.0}}, "_raw_slots": []}
    assert HeldSceneEvidence.from_confirmed(raw, tracker, _binding()) is None
    tracker.scene_active = True
    tracker.epoch = 1
    held = HeldSceneEvidence.from_confirmed(raw, tracker, _binding())
    assert held is not None
    tracker.slots[1].stable_slot = {"name": "stable"}
    assert held.for_frame(tracker, _binding(), (2,)).eligible_slots == (True, False, False)
    tracker.body_shard_latched = True
    assert held.for_frame(tracker, _binding()) is None


def test_hold_without_candidates_exposes_starvation_and_stays_recoverable():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame_id in range(1, 9):
        event = tracker.update({"selection_type": "hextech", "source": {
            "scene_present": frame_id == 1, "selection_button_present": True,
            "card_residue": True, "name_residue": [True]*3, "frame_id": frame_id,
        }, "_raw_slots": [], "timing": {"recognition_completed_at": 100 + frame_id*.5}})
    assert tracker.scene_active
    assert all(slot["state"] == "detecting" and slot["diagnostic"] == "evidence_starved" for slot in event["slots"])


def test_invalid_capture_pauses_without_clearing_confirmed_selection():
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=5)
    tracker.slots[0].stable_slot = {"state": "ready", "name": "双刀流", "augment_id": "1225"}
    event = tracker.update({"source": {"reason": "capture_roi_invalid"}, "_raw_slots": []})
    assert tracker.epoch == 5 and tracker.slots[0].stable_slot is not None
    assert event["source"]["scene_state"] == "paused"


def test_button_loss_grace_invalidates_lease_even_while_display_is_active():
    from hextech.infrastructure.vision.runner_helpers import next_held_scene_evidence
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=1)
    raw = {"source": {"scene_present": False, "selection_button_present": False,
                       "card_residue": True, "name_residue": [True]*3},
           "_raw_slots": [], "timing": {"recognition_completed_at": 100.}}
    event = tracker.update(raw)
    assert tracker.scene_active and event["source"]["scene_temporal_state"] == "grace_hold"
    assert next_held_scene_evidence(_held(), raw, event, tracker, _binding()) is None
    raw["source"].update(selection_button_present=True, hold_evidence_state="collecting")
    event = tracker.update(raw)
    assert next_held_scene_evidence(None, raw, event, tracker, _binding()) is None


@pytest.mark.parametrize("change", ["ok", "foreground", "rect", "game", "fullscreen"])
def test_post_capture_binding_drift_is_rejected(monkeypatch, change):
    from hextech.infrastructure.vision.runner_helpers import captured_window_still_current
    from hextech.infrastructure.vision import sidecar_capture
    from hextech.modules.vision import window
    frame = Image.new("RGB", (1920, 1080))
    frame.info["hextech_client_rect"] = (0, 0, 1920, 1080)
    monkeypatch.setattr(sidecar_capture, "_is_lol_game_foreground", lambda _: change != "foreground")
    monkeypatch.setattr(window, "_window_client_rect", lambda *_a, **_k:
                        (10, 0, 1930, 1080) if change == "rect" else (0, 0, 1920, 1080))
    monkeypatch.setattr(window, "game_window_identity", lambda _: {
        "game_instance_id": "other" if change == "game" else "game",
        "game_window_mode_status": "unsupported" if change == "fullscreen" else "supported",
        "game_window_mode": "fullscreen" if change == "fullscreen" else "windowed"})
    assert captured_window_still_current(frame, hwnd=101, client_rect=(0, 0, 1920, 1080),
                                        game_instance_id="game") == (change == "ok")


def test_detector_worker_tracker_hold_sequence_confirms_independent_frames(monkeypatch):
    """真实检测入口/worker/回流/仲裁；仅替换模型与模板计算，不直接注入身份票。"""
    from hextech.infrastructure.vision.ocr_shadow import OcrShadowRuntime
    from hextech.infrastructure.vision.runner_helpers import attach_completed_ocr_evidence
    from hextech.infrastructure.vision.slot_evidence import slot_evidence_fingerprint

    frame = _setup(monkeypatch)
    def detect(image, boxes, templates, *, name_crops=None, **kwargs):
        return [{"slot": i, "evidence_fingerprint": slot_evidence_fingerprint(name_crops[i], image.crop(box))}
                for i, box in enumerate(boxes)], {}
    monkeypatch.setattr(detection, "_detect_slots", detect)
    class Recognizer:
        model_sha256 = "test-model"
        def recognize(self, images):
            return [("双刀流", .99)] * len(images)
    runtime = OcrShadowRuntime([SimpleNamespace(augment_id="1225", name="双刀流")],
                              mode="admit", recognizer_factory=lambda _: Recognizer())
    tracker = SelectionTracker(scene_enter_frames=1)
    held = None
    start = time.time()
    try:
        for fid in range(1, 7):
            monkeypatch.setattr(detection, "detect_selection_scene", lambda *a, **k: _scene(fid == 1))
            runtime.set_production_context(session_id="game", selection_epoch=1,
                slot_generations=(1, 1, 1), captured_frame_id=fid, captured_at=start+fid*.1)
            # 只取左槽，证明不压缩槽索引、不让同一个身份在多槽伪成功。
            context = replace(held, eligible_slots=(True, False, False)) if held else None
            raw = detection.detect_overlay_choices(frame, [], held_scene=context,
                capture_binding=_binding(), ocr_shadow=runtime if fid > 1 else None)
            raw["source"].update(session_id="game", frame_id=fid, cursor_over_slots=[])
            raw["timing"] = {"captured_at": start+fid*.1, "recognition_completed_at": start+fid*.1+.01}
            assert runtime.wait_until_idle()
            outcomes = attach_completed_ocr_evidence(raw, runtime, tracker, session_id="game",
                selection_epoch=1, observed_at=start+fid*.1+.05)
            event = tracker.update(raw)
            runtime.record_completed_outcomes(outcomes)
            if fid == 1:
                held = HeldSceneEvidence.from_confirmed(raw, tracker, _binding())
                assert held is not None
            if fid < 4:
                assert tracker.slots[0].stable_slot is None
        assert tracker.slots[0].stable_slot["augment_id"] == "1225"
        assert all(track.stable_slot is None for track in tracker.slots[1:])
        assert event["source"]["selection_epoch"] == 1
        assert runtime.status()["production_admissions"] > 0
    finally:
        runtime.close()

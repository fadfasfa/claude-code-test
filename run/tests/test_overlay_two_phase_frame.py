"""先场景后身份不能让同帧推进两次或让旧结果穿过暂停。"""
from hextech.infrastructure.vision.state import SelectionTracker


def raw(fid):
    return {"selection_type": "hextech", "source": {"session_id": "g", "window_hwnd": 10,
        "frame_id": fid, "scene_present": True, "selection_button_present": True},
        "timing": {"captured_at": 100+fid, "recognition_completed_at": 100+fid+.01}, "_raw_slots": []}


def test_phase_advances_scene_once_and_slots_once():
    tracker = SelectionTracker(scene_enter_frames=2)
    ticket = tracker.begin_frame(raw(1))
    assert tracker.scene_frames == 1 and not tracker.scene_active
    assert all(s.raw_observation_count == 0 for s in tracker.slots)
    result = tracker.finish_frame(ticket, raw(1))
    assert result is not None and tracker.scene_frames == 1
    assert all(s.raw_observation_count == 1 for s in tracker.slots)
    assert tracker.finish_frame(ticket, raw(1)) is None
    assert tracker.begin_frame(raw(1)) is None
    next_ticket = tracker.begin_frame(raw(2))
    assert tracker.scene_active and next_ticket.event["source"]["selection_window_active"]
    assert tracker.scene_frames == 2
    tracker.finish_frame(next_ticket, raw(2))
    assert tracker.scene_frames == 2 and tracker.epoch == 1


def test_newer_frame_and_reset_reject_old_identity():
    tracker = SelectionTracker(scene_enter_frames=1)
    old = tracker.begin_frame(raw(1))
    new = tracker.begin_frame(raw(2))
    assert tracker.finish_frame(old, raw(1)) is None
    tracker.reset()
    assert tracker.finish_frame(new, raw(2)) is None


def test_pause_invalidates_pending_identity():
    tracker = SelectionTracker(scene_enter_frames=1)
    ticket = tracker.begin_frame(raw(1))
    tracker.pause("game_not_foreground")
    assert tracker.finish_frame(ticket, raw(1)) is None


def test_production_frame_pipeline_publishes_scene_before_identity_work():
    from types import SimpleNamespace
    from PIL import Image
    from hextech.infrastructure.vision.frame_pipeline import process_captured_frame
    from hextech.infrastructure.vision.held_scene import CaptureBinding
    order = []
    tracker = SelectionTracker(scene_enter_frames=1)
    def detect(frame, templates, *, on_scene, **kwargs):
        item = raw(1)
        on_scene(item)
        assert order == ["published-scene"]
        assert tracker.scene_frames == 1
        assert all(s.raw_observation_count == 0 for s in tracker.slots)
        order.append("identity-compute")
        return item
    sidecar = SimpleNamespace(is_left_mouse_button_down=lambda: False,
        _cursor_over_card_slots=lambda *a: [], detect_overlay_choices=detect)
    ocr = SimpleNamespace(drain_completed_evidence=lambda **k: [], record_completed_outcomes=lambda _: None)
    _, result, _ = process_captured_frame(Image.new("RGB",(1920,1080)),[],sidecar=sidecar,
        tracker=tracker,ocr=ocr,binding=CaptureBinding("g",10,(0,0,1920,1080),1),frame_id=1,
        capture_started_at=101.,captured_at=101.01,preset="auto",min_confidence=.8,held_scene=None,
        mouse_observer=None,left_mouse_was_down=False,minimum_captured_at=0.,
        publish_scene=lambda item: order.append("published-scene"))
    assert order == ["published-scene", "identity-compute"]
    assert tracker.scene_frames == 1
    assert all(s.raw_observation_count == 1 for s in tracker.slots)
    assert result["source"]["frame_phase"] == "identity"


def test_reroll_scene_feedback_removes_known_stale_identity_once():
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=1)
    for i, slot in enumerate(tracker.slots):
        slot.slot_generation = 1
        slot.stable_slot = {"slot": i, "state": "ready", "augment_id": str(i+1), "name": f"card{i}", "slot_generation": 1}
    item = raw(4)
    item["source"].update(selection_click=True, transition_kind="reroll", transition_source="async_mouse_down",
                           transition_slot=0, cursor_over_slots=[0])
    ticket = tracker.begin_frame(item)
    assert ticket.event["slots"][0]["state"] != "ready"
    assert not ticket.event["slots"][0].get("augment_id")
    assert ticket.event["slots"][0]["slot_generation"] == 2
    assert [s["state"] for s in ticket.event["slots"]][1:] == ["ready", "ready"]
    result = tracker.finish_frame(ticket, item)
    assert tracker.slots[0].slot_generation == 2
    assert result["slots"][0]["slot_generation"] == 2


def test_single_negative_does_not_swallow_empty_scene_exit():
    from test_overlay_scene_negative import _negative_evidence, _raw_slot
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=8)
    for i, slot in enumerate(tracker.slots):
        slot.slot_generation = 1
        slot.stable_slot = {"slot": i, "state": "ready", "augment_id": str(i+1), "name": f"card{i}", "slot_generation": 1}
    negative = _negative_evidence("迅捷碎片", slot=2, frame=5, captured_at=100.4)
    item = {"selection_type": "hextech", "source": {"session_id": "game-r10-e8", "frame_id": 5,
            "scene_present": True, "selection_button_present": True},
            "timing": {"captured_at": 100.4, "recognition_completed_at": 100.4},
            "_raw_slots": [{}, {}, _raw_slot(negative, slot=2, frame=5)]}
    assert tracker.update(item)["source"]["reason"] == "scene_type_conflict"
    for fid, stamp in [(6,101.),(7,102.)]:
        item["source"].update(frame_id=fid, scene_present=False, selection_button_present=False)
        item["timing"].update(captured_at=stamp, recognition_completed_at=stamp)
        item["_raw_slots"] = []
        tracker.update(item)
    assert not tracker.scene_active
    assert all(s.stable_slot is None for s in tracker.slots)


def test_real_detector_submits_ocr_and_scene_before_template(monkeypatch):
    from types import SimpleNamespace
    from PIL import Image
    from hextech.infrastructure.vision import sidecar_detection as detection
    from test_overlay_held_scene_evidence import _scene
    order = []
    monkeypatch.setattr(detection, "detect_selection_scene", lambda *a, **k: _scene(True))
    monkeypatch.setattr(detection, "_body_shard_name_scores", lambda *a, **k: [0.,0.,0.])
    monkeypatch.setattr(detection, "_blocking_modal_present", lambda *a: False)
    def detect(*a, **k):
        assert order == ["ocr", "scene"]
        order.append("templates")
        return [{"slot": i, "evidence_fingerprint": "fp"} for i in range(3)], {}
    monkeypatch.setattr(detection, "_detect_slots", detect)
    detection.detect_overlay_choices(Image.new("RGB",(1920,1080),"white"), [],
        ocr_shadow=SimpleNamespace(observe=lambda *a, **k: order.append("ocr")),
        on_scene=lambda event: order.append("scene"))
    assert order == ["ocr", "scene", "templates"]


def test_r10_e8_negative_preempts_positive_identity_in_finish():
    from test_overlay_scene_negative import _negative_evidence, _raw_slot
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=8)
    for slot in tracker.slots:
        slot.slot_generation = 1
    item = {"selection_type": "hextech", "source": {"session_id": "game-r10-e8", "window_hwnd": 10,
            "frame_id": 9, "scene_present": True, "selection_button_present": True},
            "timing": {"captured_at": 200., "recognition_completed_at": 200.01}, "_raw_slots": []}
    ticket = tracker.begin_frame(item)
    names = ["魔法抗性碎片", "技能急速碎片", "迅捷碎片"]
    item["_raw_slots"] = [_raw_slot(_negative_evidence(name, slot=i, frame=9, captured_at=200.), slot=i, frame=9)
                           for i, name in enumerate(names)]
    result = tracker.finish_frame(ticket, item)
    assert result["selection_type"] == "body_shard"
    assert not result["active"] and result["source"]["ready_slots"] == 0
    assert tracker.epoch == 8 and tracker.body_shard_latched
    assert all(s.stable_slot is None for s in tracker.slots)

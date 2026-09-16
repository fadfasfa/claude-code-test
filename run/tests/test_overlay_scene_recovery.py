"""按钮短暂漏检后的场景恢复只能重取新鲜证据，不能复用旧身份票。"""

from dataclasses import replace
from types import SimpleNamespace
import time

from PIL import Image
import pytest

from hextech.infrastructure.vision import sidecar_capture
from hextech.infrastructure.vision import sidecar_detection as detection
from hextech.infrastructure.vision.held_scene import CaptureBinding, HeldSceneEvidence
from hextech.infrastructure.vision.ocr_shadow import OcrShadowRuntime
from hextech.infrastructure.vision.runner_helpers import (
    attach_completed_ocr_evidence,
    next_held_scene_evidence,
)
from hextech.infrastructure.vision.scene_recovery import (
    RECOVERY_REFERENCE_MAX_AGE_SECONDS,
    advance_scene_recovery,
    consume_scene_recovery_capture,
    recovery_confirmation_allows_held,
)
from hextech.infrastructure.vision.slot_evidence import slot_evidence_fingerprint
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.modules.vision.layout import LayoutTransform, SceneObservation


def test_idle_diagnostic_does_not_claim_scene_reconfirmed():
    from hextech.infrastructure.vision.runner_helpers import advance_recovery_event
    tracker = SelectionTracker()
    raw = {"source": {"scene_present": False}, "selection_type": "hextech", "_raw_slots": []}
    result = tracker.update(raw)
    advance_recovery_event(None, None, raw, result, tracker, _binding(), now=10., full_capture_used=False)
    assert result["source"]["scene_recovery_state"] == "inactive"


def _binding(**changes):
    value = CaptureBinding("game", 101, (-1920, 0, 0, 1080), 4, 1.5)
    return replace(value, **changes)


def _scene(*, present: bool, button: bool) -> SceneObservation:
    return SceneObservation(
        present,
        "candidate" if present else "absent",
        .9 if present else .5,
        "1920x1080",
        (870, 840, 1050, 890) if button else None,
        .8 if button else 0.0,
        (.8, .8, .8),
        LayoutTransform(),
        True,
    )


def _raw(*, button: bool, present: bool = False) -> dict:
    return {
        "selection_type": "hextech",
        "source": {
            "scene_present": present,
            "selection_button_present": button,
            "card_residue": True,
            "name_residue": [True, True, True],
            "layout_transform": {"dx_ratio": 0.0, "dy_ratio": 0.0, "scale": 1.0},
        },
        "_raw_slots": [],
    }


def test_button_loss_then_return_requests_exactly_one_full_client_capture():
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))
    tracker.slots[0].candidate_identity = "old-ocr"
    tracker.slots[0].candidate_frames = 2
    tracker.slots[1].stable_slot = {"state": "ready", "augment_id": "keep"}
    lost = _raw(button=False)
    lost["timing"] = {"captured_at": 1000.0}
    lost_event = tracker.update(lost)
    recovery = advance_scene_recovery(
        None, held, lost, lost_event, tracker, _binding(), now=100.0
    )
    assert recovery is not None and not recovery.full_capture_pending
    assert recovery.evidence_not_before == 1000.0
    assert tracker.slots[0].candidate_identity == ""
    assert tracker.slots[1].stable_slot["augment_id"] == "keep"

    returned = _raw(button=True)
    returned_event = tracker.update(returned)
    assert returned_event["source"]["scene_temporal_state"] == "button_hold"
    recovery = advance_scene_recovery(
        recovery, None, returned, returned_event, tracker, _binding(), now=100.1
    )
    assert recovery is not None and recovery.full_capture_pending

    recovery, capture_full = consume_scene_recovery_capture(
        recovery, _binding(), now=100.11
    )
    assert capture_full is True
    recovery, capture_full = consume_scene_recovery_capture(
        recovery, _binding(), now=100.12
    )
    assert capture_full is False

    # 按钮持续存在不能每帧重新触发；只有新的消失→重现边才可再触发。
    same_event = tracker.update(returned)
    recovery = advance_scene_recovery(
        recovery, None, returned, same_event, tracker, _binding(), now=100.2
    )
    assert recovery is not None and not recovery.full_capture_pending


@pytest.mark.parametrize(
    "binding",
    [
        _binding(game_instance_id="other"),
        _binding(window_hwnd=102),
        _binding(client_rect=(-1920, 0, 0, 1200)),
        _binding(selection_epoch=5),
        _binding(dpi_scale=1.0),
    ],
)
def test_recovery_reference_rejects_identity_geometry_epoch_or_dpi_change(binding):
    tracker = SelectionTracker(scene_enter_frames=2, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))
    lost = _raw(button=False)
    recovery = advance_scene_recovery(
        None, held, lost, tracker.update(lost), tracker, _binding(), now=10.0
    )
    recovery, capture_full = consume_scene_recovery_capture(recovery, binding, now=10.1)
    assert recovery is None and capture_full is False


def test_recovery_reference_expires_and_pause_clears_it():
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))
    lost = _raw(button=False)
    recovery = advance_scene_recovery(
        None, held, lost, tracker.update(lost), tracker, _binding(), now=10.0
    )
    returned = _raw(button=True)
    recovery = advance_scene_recovery(
        recovery,
        None,
        returned,
        tracker.update(returned),
        tracker,
        _binding(),
        now=10.0 + RECOVERY_REFERENCE_MAX_AGE_SECONDS + .001,
    )
    assert recovery is not None and not recovery.full_capture_pending

    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=4)
    recovery = advance_scene_recovery(
        None, held, lost, tracker.update(lost), tracker, _binding(), now=20.0
    )
    paused = tracker.pause("scoreboard_key_down")
    recovery = advance_scene_recovery(
        recovery, None, {"source": {}}, paused, tracker, _binding(), now=20.1
    )
    assert recovery is None


def test_expired_geometry_reference_still_requires_two_normal_confirmations():
    tracker = SelectionTracker(scene_enter_frames=2, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))
    lost = _raw(button=False)
    lost["timing"] = {"captured_at": 1000.0}
    recovery = advance_scene_recovery(
        None, held, lost, tracker.update(lost), tracker, _binding(), now=10.0
    )
    recovery, capture_full = consume_scene_recovery_capture(
        recovery, _binding(), now=10.0 + RECOVERY_REFERENCE_MAX_AGE_SECONDS + .001
    )
    assert recovery is not None and capture_full is False

    normal = _raw(button=True, present=True)
    event = tracker.update(normal)
    previous = recovery
    recovery = advance_scene_recovery(
        recovery, None, normal, event, tracker, _binding(), now=10.8
    )
    assert recovery is not None
    assert not recovery_confirmation_allows_held(
        previous, recovery, normal, tracker, _binding(), now=10.8
    )

    event = tracker.update(normal)
    previous = recovery
    recovery = advance_scene_recovery(
        recovery, None, normal, event, tracker, _binding(), now=10.9
    )
    assert recovery is None
    assert recovery_confirmation_allows_held(
        previous, recovery, normal, tracker, _binding(), now=10.9
    )


def test_each_real_button_loss_advances_cutoff_without_extending_geometry_ttl():
    tracker = SelectionTracker(scene_enter_frames=2, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))
    lost = _raw(button=False)
    lost["timing"] = {"captured_at": 1000.0}
    recovery = advance_scene_recovery(
        None, held, lost, tracker.update(lost), tracker, _binding(), now=10.0
    )
    created_at = recovery.created_at

    returned = _raw(button=True)
    recovery = advance_scene_recovery(
        recovery, None, returned, tracker.update(returned), tracker, _binding(), now=10.1
    )
    normal = _raw(button=True, present=True)
    recovery = advance_scene_recovery(
        recovery, None, normal, tracker.update(normal), tracker, _binding(), now=10.2
    )
    tracker.slots[0].candidate_identity = "stale-second-edge"
    tracker.slots[0].candidate_frames = 2

    second_loss = _raw(button=False)
    second_loss["timing"] = {"captured_at": 1000.3}
    recovery = advance_scene_recovery(
        recovery, None, second_loss, tracker.update(second_loss), tracker, _binding(), now=10.3
    )
    assert recovery.created_at == created_at
    assert recovery.evidence_not_before == 1000.3
    assert tracker.slots[0].candidate_identity == ""

    returned_again = _raw(button=True)
    recovery = advance_scene_recovery(
        recovery, None, returned_again, tracker.update(returned_again), tracker, _binding(), now=10.4
    )
    recovery, capture_full = consume_scene_recovery_capture(
        recovery, _binding(), now=10.4
    )
    assert capture_full is True
    recovery, capture_full = consume_scene_recovery_capture(
        recovery, _binding(), now=10.8
    )
    assert recovery is not None and capture_full is False


@pytest.mark.parametrize("captured_at", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_loss_timestamp_never_poison_cutoff(captured_at):
    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, True, True))
    lost = _raw(button=False)
    lost["timing"] = {"captured_at": captured_at}
    recovery = advance_scene_recovery(
        None, held, lost, tracker.update(lost), tracker, _binding(), now=10.0
    )
    assert recovery is not None and recovery.evidence_not_before == 0.0


def test_pre_loss_ocr_ticket_cannot_cross_recovery_boundary():
    class Runtime:
        def drain_completed_evidence(self, **kwargs):
            return [
                {"evidence": {"captured_at": 99.9, "canonical_id": "old"}},
                {"evidence": {"captured_at": 100.1, "canonical_id": "new"}},
            ]

    tracker = SelectionTracker(scene_enter_frames=1, scene_active=True, epoch=4)
    raw = _raw(button=True)
    attach_completed_ocr_evidence(
        raw,
        Runtime(),
        tracker,
        session_id="game",
        selection_epoch=4,
        observed_at=100.2,
        minimum_captured_at=100.0,
    )
    assert [item["evidence"]["canonical_id"] for item in raw["_completed_ocr_evidence"]] == ["new"]

def test_recovery_capture_uses_mss_for_full_client(monkeypatch):
    boxes = []

    class Backend:
        def capture_rgb(self, box):
            boxes.append(box)
            return Image.new("RGB", (box[2] - box[0], box[3] - box[1]), "navy")

    frame = sidecar_capture._capture_lol_game_rect(
        (-1920, 0, 0, 1080), backend=Backend(), force_full_client=True
    )
    assert boxes == [(-1920, 0, 0, 1080)]
    assert frame is not None and frame.size == (1920, 1080)
    assert frame.info["hextech_capture_mode"] == "client_full_recovery"


def test_runner_e4_control_flow_uses_one_full_capture_then_reconfirms(monkeypatch):
    from hextech.infrastructure.vision import runner, sidecar

    class StopLoop(RuntimeError):
        pass

    class FakeSource:
        def read_hint_cache(self):
            return {"schema_version": 1, "hints": {}, "name_index": {}}

    class FakeOcr:
        def set_production_context(self, **kwargs):
            return None

        def drain_completed_evidence(self, **kwargs):
            return []

        def record_completed_outcomes(self, outcomes):
            return None

        def status(self):
            return {}

    runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
    target = (101, (-1920, 0, 0, 1080))
    events = [
        _raw(button=True, present=True),
        _raw(button=True, present=True),
        _raw(button=False),
        _raw(button=True),
        _raw(button=True, present=True),
        _raw(button=True, present=True),
    ]
    capture_modes = []
    sleeps = 0

    def capture(_rect, **kwargs):
        capture_modes.append(bool(kwargs.get("force_full_client")))
        return Image.new("RGB", (1920, 1080), "white")

    def stop_after_six(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 6:
            raise StopLoop()

    monkeypatch.setattr(runner, "CatalogVisionDataSource", lambda **kwargs: FakeSource())
    monkeypatch.setattr(runner, "load_or_build_default_template_runtime", lambda **kwargs: runtime)
    monkeypatch.setattr(runner, "_prepare_compute_runtime", lambda *_: None)
    monkeypatch.setattr(runner, "_write_sidecar_status", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_write_sidecar_ready_from_env", lambda **k: None)
    monkeypatch.setattr(runner.OcrShadowRuntime, "from_environment", lambda *_: FakeOcr())
    monkeypatch.setattr(runner, "game_window_identity", lambda *_: {
        "game_instance_id": "game", "game_window_mode_status": "supported"})
    monkeypatch.setattr(sidecar, "_set_dpi_awareness", lambda: None)
    monkeypatch.setattr(sidecar, "_find_lol_game_window", lambda: target)
    monkeypatch.setattr(sidecar, "_is_lol_game_foreground", lambda *_: True)
    monkeypatch.setattr(sidecar, "is_scoreboard_key_down", lambda: False)
    monkeypatch.setattr(sidecar, "is_left_mouse_button_down", lambda: False)
    monkeypatch.setattr(sidecar, "_capture_lol_game_rect", capture)
    monkeypatch.setattr(sidecar, "detect_overlay_choices", lambda *a, **k: events.pop(0))
    monkeypatch.setattr(sidecar, "_window_dpi_scale", lambda *_: 1.5)
    monkeypatch.setattr(sidecar, "_cursor_over_card_slots", lambda *a, **k: [])
    monkeypatch.setattr(runner.time, "sleep", stop_after_six)

    with pytest.raises(StopLoop):
        runner._run_loop_impl(required_frames=2)

    assert capture_modes == [False, False, False, False, True, False]
    assert not events


def test_detector_ocr_tracker_recovers_with_only_new_frame_evidence(monkeypatch):
    """控制流复现 e4；身份只能来自恢复后的 detector→OCR→Tracker。"""
    monkeypatch.setattr(detection, "_name_text_mask", lambda image: image.convert("L"))
    monkeypatch.setattr(detection, "_name_crop_has_residue", lambda *a, **k: True)
    monkeypatch.setattr(detection, "_body_shard_name_scores", lambda *a, **k: [0., 0., 0.])
    monkeypatch.setattr(detection, "_blocking_modal_present", lambda *a: False)

    def detect(image, boxes, templates, *, name_crops=None, **kwargs):
        return [
            {
                "slot": i,
                "diagnostic": "pending",
                "evidence_fingerprint": slot_evidence_fingerprint(name_crops[i], image.crop(box)),
            }
            for i, box in enumerate(boxes)
        ], {}

    monkeypatch.setattr(detection, "_detect_slots", detect)

    class Recognizer:
        model_sha256 = "recovery-test-model"

        def recognize(self, images):
            return [("双刀流", .99), ("无法识别乙", .99), ("无法识别丙", .99)][:len(images)]

    runtime = OcrShadowRuntime(
        [SimpleNamespace(augment_id="1225", name="双刀流")],
        mode="admit",
        recognizer_factory=lambda _: Recognizer(),
    )
    frame = Image.new("RGB", (1920, 1080), "white")
    tracker = SelectionTracker(scene_enter_frames=2, scene_active=True, epoch=4)
    held = HeldSceneEvidence(_binding(), LayoutTransform(), (True, False, False))
    recovery = None
    now = time.time()
    completed_frames = 0
    try:
        monkeypatch.setattr(
            detection, "detect_selection_scene", lambda *a, **k: _scene(present=False, button=False)
        )
        raw = detection.detect_overlay_choices(frame, [], capture_binding=_binding())
        raw["source"].update(session_id="game", frame_id=1)
        recovery = advance_scene_recovery(
            recovery, held, raw, tracker.update(raw), tracker, _binding(), now=now
        )
        assert recovery is not None

        monkeypatch.setattr(
            detection, "detect_selection_scene", lambda *a, **k: _scene(present=False, button=True)
        )
        raw = detection.detect_overlay_choices(frame, [], capture_binding=_binding())
        raw["source"].update(session_id="game", frame_id=2)
        recovery = advance_scene_recovery(
            recovery, None, raw, tracker.update(raw), tracker, _binding(), now=now + .1
        )
        recovery, force_full = consume_scene_recovery_capture(
            recovery, _binding(), now=now + .11
        )
        assert force_full is True

        # full-client 重检恢复正常 scene 后，只提交恢复后图像产生的新 OCR 工作。
        monkeypatch.setattr(
            detection, "detect_selection_scene", lambda *a, **k: _scene(present=True, button=True)
        )
        restored_held = None
        for frame_id in range(3, 7):
            frame = Image.new("RGB", (1920, 1080), (240 + frame_id, 240, 240))
            runtime.set_production_context(
                session_id="game",
                selection_epoch=4,
                slot_generations=(1, 1, 1),
                captured_frame_id=frame_id,
                captured_at=now + frame_id * .1,
            )
            raw = detection.detect_overlay_choices(frame, [], ocr_shadow=runtime)
            raw["source"].update(session_id="game", frame_id=frame_id)
            raw["timing"] = {
                "captured_at": now + frame_id * .1,
                "recognition_completed_at": now + frame_id * .1 + .01,
            }
            assert runtime.wait_until_idle()
            outcomes = attach_completed_ocr_evidence(
                raw,
                runtime,
                tracker,
                session_id="game",
                selection_epoch=4,
                observed_at=now + frame_id * .1 + .05,
            )
            completed_frames += int(bool(raw.get("_completed_ocr_evidence")))
            event = tracker.update(raw)
            runtime.record_completed_outcomes(outcomes)
            previous = recovery
            recovery = advance_scene_recovery(
                recovery, None, raw, event, tracker, _binding(), now=now + frame_id * .1
            )
            allow_held = recovery_confirmation_allows_held(
                previous, recovery, raw, tracker, _binding(), now=now + frame_id * .1
            )
            restored_held = next_held_scene_evidence(
                restored_held, raw, event, tracker, _binding(), allow_confirmed=allow_held
            )
            if frame_id == 3:
                assert recovery is not None and restored_held is None
            if frame_id == 4:
                assert recovery is None and restored_held is not None
        assert tracker.slots[0].stable_slot is not None, (event, runtime.status())
        assert tracker.slots[0].stable_slot["augment_id"] == "1225"
        assert all(slot.stable_slot is None for slot in tracker.slots[1:])
        assert completed_frames >= 3
    finally:
        runtime.close()

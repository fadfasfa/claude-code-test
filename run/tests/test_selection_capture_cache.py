"""V2 uses existing ROI frames before admission; retention never edits legacy/manual truth."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from PIL import Image

from hextech.infrastructure.vision.failure_evidence import FailureEvidenceWriter
from hextech.infrastructure.vision import selection_capture_cache as module


def event(index=1, *, epoch=0, ready=False, suspected=True):
    return {"source": {"session_id": "game", "game_instance_id": "game", "frame_id": index,
            "selection_epoch": epoch, "scene_state": "candidate" if suspected else "absent",
            "scene_present": suspected, "capture_size": [320, 200]},
            "timing": {"captured_at": index/4},
            "slots": [{"slot": i, "state": "ready" if ready else "detecting", "slot_generation": 1,
                       "augment_id": "possibly-wrong" if ready else ""} for i in range(3)]}


def buffer():
    clock = [0.]
    drafts = []
    writer = SimpleNamespace(submit=lambda draft: drafts.append(draft) or True)
    collector = module.SelectionCaptureBuffer(writer, clock=lambda: clock[0])
    return collector, clock, drafts


def build_draft():
    collector, clock, drafts = buffer()
    collector.capture(Image.new("RGB", (320, 200), "gold"), event())
    collector.observe_result(event())
    return drafts[-1]


def test_capture_ab_switch_defaults_on_without_changing_roi_debug(monkeypatch):
    from hextech.modules.vision.diagnostic_settings import selection_capture_enabled, SELECTION_CAPTURE_ENV

    monkeypatch.delenv(SELECTION_CAPTURE_ENV, raising=False)
    assert selection_capture_enabled()
    monkeypatch.setenv(SELECTION_CAPTURE_ENV, "0")
    assert not selection_capture_enabled()


def test_no_scene_no_capture_and_rejected_structure_still_captured():
    collector, clock, drafts = buffer()
    raw = event(suspected=False)
    collector.capture(Image.new("RGB", (320, 200)), raw)
    assert not collector.frames
    assert collector.status()["recent_available"]
    assert not drafts
    assert collector.save_recent() and drafts[-1].manual
    clock[0] = .3
    raw["source"]["panel_scores"] = [.42, .40, .1]
    collector.capture(Image.new("RGB", (320, 200)), raw)
    collector.observe_result(raw)
    assert drafts and drafts[-1].frames[0].metadata["selection_epoch"] == 0


def test_candidate_no_epoch_and_wrong_ready_retained_without_mutating_state():
    collector, clock, drafts = buffer()
    image = Image.new("RGB", (320, 200), "navy")
    raw = event(ready=True)
    original = json.dumps(raw)
    collector.capture(image, raw)
    collector.observe_result(raw)
    draft = drafts[-1]
    assert len(draft.diagnostic_id) == 32 and draft.terminal["selection_epoch"] == 0
    assert draft.frames[0].metadata["slots"][0]["state"] == "ready"
    assert draft.frames[0].image.size != image.size
    assert json.dumps(raw) == original


def test_ring_is_four_fps_two_seconds_with_first_clear_last():
    collector, clock, drafts = buffer()
    for i in range(101):
        clock[0] = i*.05
        image = Image.new("RGB", (320, 200), (i, 100, 20))
        collector.capture(image, event(i))
        collector.observe_result(event(i, ready=i > 20))
    assert len(collector.frames) <= 8
    assert collector.frames[-1].sampled_at-collector.frames[0].sampled_at < 2
    collector.finish({"source": {"reason": "selection_completed"}})
    draft = drafts[-1]
    assert len(draft.frames) <= 10
    assert draft.frames[draft.first_id].metadata["frame_id"] == 0
    assert draft.frames[draft.last_id].metadata["frame_id"] >= 95
    assert draft.terminal["reason"] == "selection_completed"
    assert collector.save_recent()
    assert drafts[-1].manual and drafts[-1].diagnostic_id != draft.diagnostic_id


def test_binding_change_ends_old_group_and_reroll_is_snapshotted():
    collector, clock, drafts = buffer()
    frame = Image.new("RGB", (320, 200))
    first = event()
    collector.capture(frame, first)
    collector.observe_result(first)
    old_id = collector.diagnostic_id
    clock[0] = .3
    reroll = event(2)
    reroll["slots"][0]["slot_generation"] = 2
    collector.capture(frame, reroll)
    collector.observe_result(reroll)
    assert any(d.terminal.get("reason") == "slot_generation_changed" for d in drafts)
    assert collector.diagnostic_id != old_id
    changed = event(3)
    changed["source"]["game_instance_id"] = "other-game"
    clock[0] = .6
    collector.capture(frame, changed)
    assert collector.diagnostic_id != old_id
    assert drafts[-1].terminal["reason"] == "capture_binding_changed"


def test_cache_rotates_only_owned_unlabelled_auto_groups(tmp_path):
    root = tmp_path / "selection-cache-v2"
    old = tmp_path / "failure-inbox" / "protected.json"
    old.parent.mkdir()
    old.write_text("original")
    first = build_draft()
    module.persist_selection_capture(root, first, group_limit=2)
    manual = replace(first, diagnostic_id=uuid4().hex, manual=True)
    module.persist_selection_capture(root, manual, group_limit=2)
    second = replace(first, diagnostic_id=uuid4().hex)
    status = module.persist_selection_capture(root, second, group_limit=2)
    assert not (root / first.diagnostic_id).exists()
    assert (root / manual.diagnostic_id).exists()
    assert status["automatic_groups"] == 1 and status["groups"] == 2
    assert old.read_text() == "original"


@pytest.mark.parametrize("protect", ["labels", "unknown_file", "unknown_field", "invalid_contract"])
def test_labelled_and_unknown_assets_are_not_updated_or_rotated(tmp_path, protect):
    first = build_draft()
    module.persist_selection_capture(tmp_path, first, group_limit=1)
    path = tmp_path / first.diagnostic_id
    manifest = path / "manifest.json"
    if protect == "unknown_file":
        (path / "truth.txt").write_text("author")
    else:
        data = json.loads(manifest.read_text())
        if protect == "invalid_contract":
            data["retention_class"] = "manual"
        else:
            data["labels" if protect == "labels" else "truth"] = ["human"]
        manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="protected"):
        module.persist_selection_capture(tmp_path, first)
    with pytest.raises(ValueError, match="budget_exhausted"):
        module.persist_selection_capture(tmp_path, replace(first, diagnostic_id=uuid4().hex), group_limit=1)
    assert path.exists()


def test_bytes_and_disk_failures_report_real_saved_count(tmp_path, monkeypatch):
    draft = build_draft()
    with pytest.raises(ValueError, match="byte_budget"):
        module.persist_selection_capture(tmp_path / "budget", draft, byte_limit=20)
    writer = FailureEvidenceWriter(tmp_path / "legacy", cache_root=tmp_path / "cache")
    monkeypatch.setattr("hextech.infrastructure.vision.failure_evidence.persist_selection_capture",
                        Mock(side_effect=OSError("disk full")))
    assert writer.submit(draft)
    assert writer.wait_empty()
    writer.close()
    assert writer.status()["failed"] == 1
    assert writer.status()["saved"] == 0
    assert writer.status()["completed"] == 1


def test_each_atomic_write_stays_within_transient_byte_budget(tmp_path, monkeypatch):
    from hextech.infrastructure.vision import failure_evidence

    draft = build_draft()
    probe = tmp_path / "probe"
    probe_status = module.persist_selection_capture(probe, draft)
    byte_limit = probe_status["bytes"] + 1024
    root = tmp_path / "checked"
    original = failure_evidence._atomic_write_bytes
    observed_peaks = []

    def disk_bytes():
        return sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) if root.exists() else 0

    def checked_write(path, content):
        predicted_peak = disk_bytes() + len(content)
        observed_peaks.append(predicted_peak)
        assert predicted_peak <= byte_limit
        original(path, content)
        assert disk_bytes() <= byte_limit

    monkeypatch.setattr(failure_evidence, "_atomic_write_bytes", checked_write)
    status = module.persist_selection_capture(root, draft, byte_limit=byte_limit)
    assert observed_peaks and max(observed_peaks) <= byte_limit
    assert status["bytes"] <= byte_limit


def test_tight_update_budget_rejects_before_writing_and_preserves_old_group(tmp_path, monkeypatch):
    from hextech.infrastructure.vision import failure_evidence

    base = build_draft()
    size = base.frames[0].image.size

    def noisy_frame(seed):
        image = Image.effect_noise(size, 80 + seed).convert("RGB")
        return replace(base.frames[0], image=failure_evidence.RoiImage.from_image(image), clarity=float(seed))

    old = replace(base, frames=(noisy_frame(1),), first_id=0, clear_id=0, last_id=0)
    updated = replace(base, frames=(noisy_frame(2),), first_id=0, clear_id=0, last_id=0,
                      terminal={"reason": "selection_completed"})

    probe = tmp_path / "probe"
    module.persist_selection_capture(probe, old)
    final_status = module.persist_selection_capture(probe, updated)

    root = tmp_path / "cache"
    initial_status = module.persist_selection_capture(root, old)
    group = root / old.diagnostic_id
    before = {path.name: path.read_bytes() for path in group.iterdir()}
    # Both the old and final committed groups fit, but old bytes + new PNG + temporary
    # manifest do not. The update must fail before creating any half-write.
    byte_limit = max(initial_status["bytes"], final_status["bytes"]) + 1024
    writes = Mock(wraps=failure_evidence._atomic_write_bytes)
    monkeypatch.setattr(failure_evidence, "_atomic_write_bytes", writes)
    with pytest.raises(ValueError, match="budget_exhausted"):
        module.persist_selection_capture(root, updated, byte_limit=byte_limit)
    assert writes.call_count == 0
    assert {path.name: path.read_bytes() for path in group.iterdir()} == before


def test_writer_updates_group_atomically_and_manual_save_is_separate(tmp_path):
    writer = FailureEvidenceWriter(tmp_path / "legacy", cache_root=tmp_path / "cache")
    draft = build_draft()
    assert writer.submit(draft) and writer.wait_empty()
    assert writer.submit(replace(draft, terminal={"reason": "selection_completed"})) and writer.wait_empty()
    manual = replace(draft, diagnostic_id=uuid4().hex, manual=True)
    assert writer.submit(manual) and writer.wait_empty()
    writer.close()
    cache = writer.status()["selection_cache"]
    assert cache["groups"] == 2 and cache["automatic_groups"] == 1
    assert cache["last_manual_diagnostic_id"] == manual.diagnostic_id
    manifest = json.loads((tmp_path / "cache" / draft.diagnostic_id / "manifest.json").read_text())
    assert manifest["terminal"]["reason"] == "selection_completed"
    assert not manifest["automatic_exemplar_eligible"]


def test_pre_admission_hook_runs_before_tracker(tmp_path):
    from hextech.infrastructure.vision.frame_pipeline import process_captured_frame
    calls = []
    raw = event()
    def detect(frame, templates, **kwargs):
        kwargs["on_scene"](raw)
        return raw
    tracker = SimpleNamespace(epoch=0, slots=[], begin_frame=lambda raw: calls.append("admit"),
                             pause=lambda reason: event(), update=lambda raw: raw)
    sidecar = SimpleNamespace(is_left_mouse_button_down=lambda: False, _cursor_over_card_slots=lambda *a: [],
                              detect_overlay_choices=detect)
    binding = SimpleNamespace(dpi_scale=1, client_rect=(0, 0, 320, 200), game_instance_id="g", window_hwnd=1,
                              selection_epoch=0)
    from unittest.mock import patch
    with patch("hextech.infrastructure.vision.frame_pipeline.captured_frame_source", return_value={}), \
         patch("hextech.infrastructure.vision.frame_pipeline.attach_completed_ocr_evidence", return_value=[]):
        process_captured_frame(Image.new("RGB", (320, 200)), [], sidecar=sidecar, tracker=tracker,
            ocr=SimpleNamespace(record_completed_outcomes=lambda x: None), binding=binding, frame_id=1,
            capture_started_at=0, captured_at=0, preset="auto", min_confidence=.8, held_scene=None,
            mouse_observer=None, left_mouse_was_down=False, minimum_captured_at=0, publish_scene=lambda x: None,
            capture_suspected=lambda frame, raw: calls.append("capture"))
    assert calls == ["capture", "admit"]


def test_process_frame_production_light_without_generations_still_binds_full_ready(monkeypatch):
    from hextech.infrastructure.vision.frame_pipeline import process_captured_frame
    from hextech.modules.vision.events import build_overlay_event
    collector, clock, drafts = buffer()
    seen_light = []

    def detect(frame, templates, **kwargs):
        light = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        light["source"].update(scene_state="candidate", scene_present=True)
        kwargs["on_scene"](light)
        seen_light.append(light)
        return event(1, ready=True)

    tracker = SimpleNamespace(
        epoch=0, slots=[], begin_frame=lambda light: SimpleNamespace(event=light),
        finish_frame=lambda ticket, raw: raw, pause=lambda reason: event(), update=lambda raw: raw,
    )
    sidecar = SimpleNamespace(
        is_left_mouse_button_down=lambda: False,
        _cursor_over_card_slots=lambda *args: [], detect_overlay_choices=detect,
    )
    binding = SimpleNamespace(
        dpi_scale=1, client_rect=(0, 0, 320, 200), game_instance_id="game", window_hwnd=1,
        selection_epoch=0,
    )
    monkeypatch.setattr("hextech.infrastructure.vision.frame_pipeline.captured_frame_source",
                        lambda *args: {"session_id": "game", "game_instance_id": "game", "frame_id": 1,
                                      "capture_size": [320, 200]})
    monkeypatch.setattr("hextech.infrastructure.vision.frame_pipeline.attach_completed_ocr_evidence",
                        lambda *args, **kwargs: [])
    _, result, _ = process_captured_frame(
        Image.new("RGB", (320, 200)), [], sidecar=sidecar, tracker=tracker,
        ocr=SimpleNamespace(record_completed_outcomes=lambda outcomes: None), binding=binding, frame_id=1,
        capture_started_at=.2, captured_at=.25, preset="auto", min_confidence=.8, held_scene=None,
        mouse_observer=None, left_mouse_was_down=False, minimum_captured_at=0,
        publish_scene=lambda feedback: None,
        capture_suspected=lambda frame, light: collector.capture(frame, light),
    )
    assert len(seen_light[0]["slots"]) == 3
    assert [slot["slot_generation"] for slot in seen_light[0]["slots"]] == [0, 0, 0]
    assert [slot["slot_generation"] for slot in collector.last.metadata["slots"]] == [0, 0, 0]
    collector.observe_result(result)
    final = collector.last.metadata["final_classification"]
    assert len(final["slots"]) == 3 and final["full_ready_elapsed_ms"] == 0.0
    assert final["generation_binding"] == "unavailable"
    assert drafts[-1].retention_class == "success"


def test_explicit_recent_request_saves_existing_buffer_without_full_capture(tmp_path, monkeypatch):
    from hextech.infrastructure.vision import diagnostic_capture_control as control_module
    from hextech.modules.session.selection_diagnostics import request_recent_selection
    from hextech.infrastructure.vision.sidecar_status import SIDECAR_INSTANCE_ID
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(control_module.time, "time", lambda: 1000.)
    monkeypatch.setattr(control_module.time, "monotonic", lambda: 100.)
    status = {"status": "running", "build_id": "b", "heartbeat_at": 1000,
              "sidecar_instance_id": SIDECAR_INSTANCE_ID, "selection_capture": {"recent_available": True}}
    (state / "game_overlay_sidecar_status.json").write_text(json.dumps(status))
    assert request_recent_selection(tmp_path)["ok"]
    collector = SimpleNamespace(save_recent=Mock(return_value=True), status=lambda: {"last_manual_id": "diag"})
    control = control_module.ExplicitCaptureControl(tmp_path, SimpleNamespace(), "b", selection_collector=collector)
    control.poll()
    assert collector.save_recent.call_count == 1
    assert control.session is None and not control.wants_full_client()
    assert control.status()["recent_save_request"]["diagnostic_id"] == "diag"


def test_unlabelled_partial_bytes_exhaust_quota_without_being_deleted(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    partial = root / "interrupted-write.bin"
    partial.write_bytes(b"x" * 12000)
    with pytest.raises(ValueError, match="budget_exhausted"):
        module.persist_selection_capture(root, build_draft(), byte_limit=13000)
    assert partial.read_bytes() == b"x" * 12000
    assert not list(root.glob("*/manifest.json"))


def test_missing_image_does_not_count_as_saved(tmp_path, monkeypatch):
    from hextech.infrastructure.vision import failure_evidence
    atomic = failure_evidence._atomic_write_bytes
    monkeypatch.setattr(failure_evidence, "_atomic_write_bytes",
        lambda path, content: None if path.suffix == ".png" else atomic(path, content))
    writer = FailureEvidenceWriter(tmp_path / "old", cache_root=tmp_path / "cache")
    assert writer.submit(build_draft()) and writer.wait_empty()
    writer.close()
    assert writer.status()["saved"] == 0 and writer.status()["failed"] == 1


def test_queue_coalesces_auto_group_but_bounds_distinct_capture_memory(tmp_path, monkeypatch):
    writer = FailureEvidenceWriter(tmp_path)
    monkeypatch.setattr(writer, "start", lambda: None)
    first = build_draft()
    assert writer.submit(first)
    assert writer.submit(replace(first, terminal={"reason": "final"}))
    assert writer.status()["queue_depth"] == 1
    assert writer.submit(replace(first, diagnostic_id=uuid4().hex))
    assert not writer.submit(replace(first, diagnostic_id=uuid4().hex))
    assert writer.status()["last_error"] == "selection_cache_queue_full"


def test_non_trigger_buffer_retains_two_seconds_and_manual_save_uses_them():
    collector, clock, drafts = buffer()
    for i in range(16):
        clock[0] = i*.25
        raw = event(i, suspected=False)
        collector.capture(Image.new("RGB", (320, 200)), raw)
        collector.observe_result(raw)
    assert not drafts and not collector.diagnostic_id
    assert len(collector.recent_frames) == 8
    assert collector.save_recent()
    assert [f.metadata["frame_id"] for f in drafts[-1].frames] == list(range(8, 16))


@pytest.mark.parametrize("mismatch", ["missing_frame_id", "other_game", "other_capture_time"])
def test_result_never_rebinds_an_unmatched_captured_frame(mismatch):
    collector, clock, drafts = buffer()
    raw = event()
    if mismatch == "missing_frame_id":
        raw["source"].pop("frame_id")
    collector.capture(Image.new("RGB", (320, 200)), raw)
    captured = json.loads(json.dumps(collector.last.metadata))
    outcome = event(ready=True)
    if mismatch == "missing_frame_id":
        outcome["source"].pop("frame_id")
    elif mismatch == "other_game":
        outcome["source"].update(session_id="other", game_instance_id="other")
    else:
        outcome["timing"]["captured_at"] = 999.
    collector.observe_result(outcome)
    assert collector.last.metadata == captured


def test_explicit_same_frame_generation_conflict_rejects_ready_timing():
    collector, clock, drafts = buffer()
    raw = event()
    collector.capture(Image.new("RGB", (320, 200)), raw)
    outcome = event(ready=True)
    outcome["slots"][0]["slot_generation"] = 2
    collector.observe_result(outcome)
    final = collector.last.metadata["final_classification"]
    assert final["generation_binding"] == "mismatch"
    assert "full_ready_elapsed_ms" not in final
    assert drafts[-1].retention_class == "anomaly"


def test_matching_result_enriches_classification_but_not_original_capture_geometry():
    collector, clock, drafts = buffer()
    raw = event()
    collector.capture(Image.new("RGB", (320, 200)), raw)
    result = event(ready=True)
    result["source"]["capture_size"] = [999, 999]
    result["source"]["scene_state"] = "active"
    result["slots"][0]["rejection_reason"] = "exact_ocr_required"
    collector.observe_result(result)
    assert collector.last.metadata["slots"][0]["state"] == "detecting"
    assert collector.last.metadata["raw_scene_evidence"]["scene_state"] == "candidate"
    final = collector.last.metadata["final_classification"]
    assert final["slots"][0]["state"] == "ready"
    assert final["rejection_stage"] == "slot_admission"
    assert collector.last.metadata["capture_size"] == [320, 200]
    assert collector.last.metadata["captured_at"] == .25


def test_absent_button_contradiction_retains_raw_evidence_separately_as_anomaly():
    collector, clock, drafts = buffer()
    raw = event(suspected=False)
    raw["source"].update(selection_button_present=True, button_box=[10, 20, 30, 40],
                         panel_scores=[.00409, .0001, 0.0], reason="scene_not_admitted")
    collector.capture(Image.new("RGB", (320, 200)), raw)
    outcome = event(suspected=False)
    outcome["source"].update(selection_button_present=True, reason="selection_scene_not_detected")
    collector.observe_result(outcome)
    draft = drafts[-1]
    evidence = draft.frames[0].metadata["raw_scene_evidence"]
    assert evidence["frame_id"] == 1 and evidence["captured_at"] == .25
    assert evidence["scene_state"] == "absent"
    assert evidence["panel_scores"] == [.00409, .0001, 0.0]
    assert evidence["button_evidence"] == {"present": True, "box": [10, 20, 30, 40]}
    assert evidence["trigger_reasons"] == ["selection_button"]
    assert draft.frames[0].metadata["final_classification"]["scene_state"] == "absent"
    assert draft.retention_class == "anomaly"


def test_slow_full_ready_is_retained_as_anomaly():
    collector, clock, drafts = buffer()
    raw = event()
    collector.capture(Image.new("RGB", (320, 200)), raw)
    clock[0] = 1.1
    collector.observe_result(event(ready=True))
    final = drafts[-1].frames[0].metadata["final_classification"]
    assert final["full_ready_elapsed_ms"] == 1100.0
    clock[0] = 2.2
    collector.capture(Image.new("RGB", (320, 200)), event(2, ready=True))
    collector.observe_result(event(2, ready=True))
    assert collector.last.metadata["final_classification"]["full_ready_elapsed_ms"] == 1100.0
    assert drafts[-1].retention_class == "anomaly"


def test_one_ready_slot_does_not_satisfy_full_three_slot_budget():
    collector, clock, drafts = buffer()
    frame = Image.new("RGB", (320, 200))
    collector.capture(frame, event())
    partial = event()
    partial["slots"][0]["state"] = "ready"
    collector.observe_result(partial)
    assert "full_ready_elapsed_ms" not in collector.last.metadata["final_classification"]

    clock[0] = 1.1
    collector.capture(frame, event(2, ready=True))
    collector.observe_result(event(2, ready=True))
    assert collector.last.metadata["final_classification"]["full_ready_elapsed_ms"] == 1100.0
    assert collector._draft(manual=False, terminal={}).retention_class == "anomaly"


def test_ready_classification_cannot_turn_raw_absent_scene_into_success():
    collector, clock, drafts = buffer()
    raw = event(suspected=False)
    raw["source"].update(selection_button_present=True, panel_scores=[0.0, 0.0, 0.0])
    collector.capture(Image.new("RGB", (320, 200)), raw)
    final = event(ready=True)
    final["source"]["scene_state"] = "active"
    collector.observe_result(final)
    assert drafts[-1].frames[0].metadata["raw_scene_evidence"]["scene_state"] == "absent"
    assert drafts[-1].retention_class == "anomaly"


def test_full_ready_elapsed_is_latched_and_reroll_starts_a_new_clock():
    collector, clock, drafts = buffer()
    frame = Image.new("RGB", (320, 200))
    collector.capture(frame, event(ready=True))
    collector.observe_result(event(ready=True))
    clock[0] = 2.0
    collector.capture(frame, event(2, ready=True))
    collector.observe_result(event(2, ready=True))
    assert collector.last.metadata["final_classification"]["full_ready_elapsed_ms"] == 0.0

    clock[0] = 2.3
    reroll = event(3)
    reroll["slots"][0]["slot_generation"] = 2
    collector.capture(frame, reroll)
    collector.observe_result(reroll)
    assert collector._full_ready_elapsed is None
    clock[0] = 3.5
    ready = event(4, ready=True)
    ready["slots"][0]["slot_generation"] = 2
    collector.capture(frame, ready)
    collector.observe_result(ready)
    assert collector.last.metadata["final_classification"]["full_ready_elapsed_ms"] == 1200.0
    assert collector._draft(manual=False, terminal={}).retention_class == "anomaly"


def test_unsampled_reroll_does_not_reuse_old_ready_frame_or_zero_latency():
    collector, clock, drafts = buffer()
    frame = Image.new("RGB", (320, 200))
    collector.capture(frame, event(ready=True))
    collector.observe_result(event(ready=True))
    old_frame = collector.last

    clock[0] = .1  # Below the 4fps sampling interval.
    reroll = event(2)
    reroll["slots"][0]["slot_generation"] = 2
    collector.capture(frame, reroll)
    assert collector.last is old_frame
    collector.observe_result(reroll)
    assert collector.last is None and collector.first is None
    assert collector._full_ready_elapsed is None

    clock[0] = 1.5
    ready = event(3, ready=True)
    ready["slots"][0]["slot_generation"] = 2
    collector.capture(frame, ready)
    collector.observe_result(ready)
    final = collector.last.metadata["final_classification"]
    assert final["full_ready_elapsed_ms"] == 1400.0
    assert collector.last.metadata["frame_id"] == 3
    assert drafts[-1].retention_class == "anomaly"


def test_weak_structure_flood_gets_one_snapshot_not_periodic_rewrites():
    collector, clock, drafts = buffer()
    for index in range(24):
        clock[0] = index * .25
        raw = event(index, suspected=False)
        raw["source"]["panel_scores"] = [.42, .40, .1]
        collector.capture(Image.new("RGB", (320, 200), (index, 0, 0)), raw)
        collector.observe_result(raw)
    assert len(drafts) == 1
    assert drafts[0].terminal["capture_reason"] == "initial_snapshot"
    assert drafts[0].retention_class == "weak"


def test_synchronous_cross_snapshot_persistence_retains_anomaly_png_and_classification(tmp_path):
    snapshots = []

    class SyncWriter:
        def submit(self, draft):
            module.persist_selection_capture(tmp_path, draft)
            manifest = json.loads((tmp_path / draft.diagnostic_id / "manifest.json").read_text())
            snapshots.append(manifest)
            return True

    clock = [0.0]
    collector = module.SelectionCaptureBuffer(SyncWriter(), clock=lambda: clock[0])
    for frame_id in range(1, 18):
        clock[0] = (frame_id - 1) * .25
        raw = event(frame_id)
        collector.capture(Image.new("RGB", (320, 200), (frame_id, 0, 0)), raw)
        final = event(frame_id)
        if frame_id == 2:
            final["source"].update(scene_state="blocked", reason="scene_type_conflict")
        collector.observe_result(final)

    rolling = [manifest for manifest in snapshots if manifest["terminal"].get("capture_reason") == "rolling_snapshot"]
    assert len(rolling) == 2
    anomaly_files = []
    for manifest in rolling:
        assert manifest["retention_class"] == "anomaly"
        assert len(manifest["frames"]) <= module.MAX_COALESCED_FRAMES
        anomaly = [frame for frame in manifest["frames"]
                   if (frame.get("final_classification") or {}).get("reason") == "scene_type_conflict"]
        assert [frame["frame_id"] for frame in anomaly] == [2]
        anomaly_files.append(anomaly[0]["file"])
        assert (tmp_path / manifest["diagnostic_id"] / anomaly[0]["file"]).is_file()
    assert anomaly_files[0] == anomaly_files[1]


def test_priority_queue_preempts_weak_for_anomaly_and_manual(tmp_path, monkeypatch):
    writer = FailureEvidenceWriter(tmp_path)
    monkeypatch.setattr(writer, "start", lambda: None)
    weak_one = build_draft()
    weak_two = replace(weak_one, diagnostic_id=uuid4().hex)
    anomaly = replace(weak_one, diagnostic_id=uuid4().hex,
                      terminal={"reason": "reroll_unconfirmed"})
    manual = replace(weak_one, diagnostic_id=uuid4().hex, manual=True)
    assert writer.submit(weak_one) and writer.submit(weak_two)
    assert writer.submit(anomaly) and writer.submit(manual)
    queued = list(writer._tasks)
    assert {item.diagnostic_id for item in queued} == {anomaly.diagnostic_id, manual.diagnostic_id}
    assert writer.status()["dropped"] == 2


def test_same_group_coalesce_keeps_original_first_and_latest_boundary(tmp_path, monkeypatch):
    writer = FailureEvidenceWriter(tmp_path)
    monkeypatch.setattr(writer, "start", lambda: None)
    collector, clock, drafts = buffer()
    for index in range(4):
        clock[0] = index * .25
        raw = event(index)
        collector.capture(Image.new("RGB", (320, 200), (index, 0, 0)), raw)
        collector.observe_result(raw)
    first = drafts[0]
    latest = collector._draft(manual=False, terminal={"reason": "reroll_unconfirmed"})
    assert latest is not None and writer.submit(first) and writer.submit(latest)
    queued = writer._tasks[0]
    assert queued.frames[queued.first_id].metadata["frame_id"] == 0
    assert queued.frames[queued.last_id].metadata["frame_id"] == 3
    assert queued.terminal["reason"] == "reroll_unconfirmed"


def test_coalesce_is_bounded_even_when_every_frame_repeats_boundary_reason():
    base = build_draft()
    frames = []
    for index in range(31):
        metadata = json.loads(json.dumps(base.frames[0].metadata))
        metadata["frame_id"] = index
        metadata["captured_at"] = index / 4
        metadata["final_classification"] = {
            "reason": "scene_type_conflict",
            "slots": [{"slot": slot, "slot_generation": 1} for slot in range(3)],
        }
        frames.append(module.SelectionFrame(base.frames[0].image, metadata, index / 4, float(index)))
    previous = replace(base, frames=tuple(frames[:16]), first_id=0, clear_id=15, last_id=15)
    latest = replace(base, frames=tuple(frames[15:]), first_id=0, clear_id=15, last_id=15)
    merged = module.coalesce_selection_drafts(previous, latest)
    assert len(merged.frames) == module.MAX_COALESCED_FRAMES
    assert merged.frames[merged.first_id].metadata["frame_id"] == 0
    assert merged.frames[merged.last_id].metadata["frame_id"] == 30
    assert merged.frames[merged.clear_id].metadata["frame_id"] == 30


def test_coalesce_retains_one_anomaly_representative_and_following_state_transition():
    base = build_draft()
    frames = []
    for index in range(31):
        metadata = json.loads(json.dumps(base.frames[0].metadata))
        metadata["frame_id"] = index
        metadata["captured_at"] = index / 4
        metadata["final_classification"] = {
            "reason": "scene_type_conflict" if index == 1 else "slots_detecting",
            "scene_state": "blocked" if index == 1 else "candidate",
            "slots": [{"slot": slot, "state": "detecting", "slot_generation": 1} for slot in range(3)],
        }
        frames.append(module.SelectionFrame(base.frames[0].image, metadata, index / 4, 1.0))
    previous = replace(base, frames=tuple(frames[:16]), first_id=0, clear_id=0, last_id=15)
    latest = replace(base, frames=tuple(frames[15:]), first_id=0, clear_id=0, last_id=15)
    merged = module.coalesce_selection_drafts(previous, latest)
    ids = [frame.metadata["frame_id"] for frame in merged.frames]
    assert len(ids) <= module.MAX_COALESCED_FRAMES
    assert 1 in ids and 2 in ids
    assert merged.retention_class == "anomaly"


def test_rewriting_same_group_reuses_encoded_png_and_marks_sparse(tmp_path, monkeypatch):
    from hextech.infrastructure.vision import failure_evidence
    original = failure_evidence._png_bytes
    encode = Mock(side_effect=original)
    monkeypatch.setattr(failure_evidence, "_png_bytes", encode)
    draft = build_draft()
    module.persist_selection_capture(tmp_path, draft)
    first_count = encode.call_count
    module.persist_selection_capture(tmp_path, replace(draft, terminal={"reason": "selection_completed"}))
    assert encode.call_count == first_count
    manifest = json.loads((tmp_path / draft.diagnostic_id / "manifest.json").read_text())
    assert manifest["evidence_completeness"] == "sparse"
    assert not manifest["temporal_acceptance"] and not manifest["qualified"]
    assert manifest["retention_class"] == "success"


def test_exact_legacy_manifest_shape_remains_owned_but_unknown_extension_is_protected(tmp_path):
    first = build_draft()
    module.persist_selection_capture(tmp_path, first, group_limit=1)
    manifest_path = tmp_path / first.diagnostic_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for key in ("retention_class", "evidence_completeness", "temporal_acceptance", "qualified"):
        manifest.pop(key)
    manifest_path.write_text(json.dumps(manifest))
    second = replace(first, diagnostic_id=uuid4().hex, terminal={"reason": "selection_completed"})
    module.persist_selection_capture(tmp_path, second, group_limit=1)
    assert not (tmp_path / first.diagnostic_id).exists()
    second_manifest = tmp_path / second.diagnostic_id / "manifest.json"
    extended = json.loads(second_manifest.read_text())
    extended["unrecognized_extension"] = True
    second_manifest.write_text(json.dumps(extended))
    with pytest.raises(ValueError, match="budget_exhausted"):
        module.persist_selection_capture(tmp_path, replace(first, diagnostic_id=uuid4().hex), group_limit=1)
    assert second_manifest.exists()


def test_rotation_prefers_weak_then_success_before_anomaly(tmp_path):
    base = build_draft()
    anomaly = replace(base, diagnostic_id=uuid4().hex, terminal={"reason": "evidence_starved"})
    success = replace(base, diagnostic_id=uuid4().hex, terminal={"reason": "selection_completed"})
    weak = replace(base, diagnostic_id=uuid4().hex, terminal={"capture_reason": "initial_snapshot"})
    module.persist_selection_capture(tmp_path, anomaly, group_limit=3)
    module.persist_selection_capture(tmp_path, success, group_limit=3)
    module.persist_selection_capture(tmp_path, weak, group_limit=3)
    replacement = replace(base, diagnostic_id=uuid4().hex, terminal={"reason": "selection_completed"})
    module.persist_selection_capture(tmp_path, replacement, group_limit=3)
    assert (tmp_path / anomaly.diagnostic_id).exists()
    assert (tmp_path / success.diagnostic_id).exists()
    assert not (tmp_path / weak.diagnostic_id).exists()
    assert (tmp_path / replacement.diagnostic_id).exists()


def test_lower_priority_incoming_group_cannot_evict_anomaly(tmp_path):
    base = build_draft()
    anomaly = replace(base, diagnostic_id=uuid4().hex, terminal={"reason": "evidence_starved"})
    weak = replace(base, diagnostic_id=uuid4().hex, terminal={"capture_reason": "initial_snapshot"})
    module.persist_selection_capture(tmp_path, anomaly, group_limit=1)
    with pytest.raises(ValueError, match="budget_exhausted"):
        module.persist_selection_capture(tmp_path, weak, group_limit=1)
    assert (tmp_path / anomaly.diagnostic_id / "manifest.json").exists()
    assert not (tmp_path / weak.diagnostic_id).exists()


def test_same_group_latest_terminal_is_saved_without_downgrading_persisted_anomaly(tmp_path):
    base = build_draft()
    anomaly = replace(base, terminal={"reason": "evidence_starved"})
    module.persist_selection_capture(tmp_path, anomaly)
    manifest_path = tmp_path / anomaly.diagnostic_id / "manifest.json"
    weak = replace(base, terminal={"capture_reason": "initial_snapshot"})
    module.persist_selection_capture(tmp_path, weak)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["retention_class"] == "anomaly"
    assert manifest["terminal"] == {"capture_reason": "initial_snapshot"}


def test_writer_saves_three_physical_slot_name_icon_assets_with_identity(tmp_path):
    from hextech.infrastructure.vision.failure_evidence import FailureEvidenceCollector
    writer = FailureEvidenceWriter(tmp_path / "legacy", cache_root=tmp_path / "cache")
    collector = FailureEvidenceCollector(writer, pool_id="pool-current")
    raw = event()
    raw["source"]["layout_transform"] = {"dx_ratio": .01, "dy_ratio": -.01, "scale": 1.0}
    frame = Image.new("RGB", (320, 200), "navy")
    collector.capture_suspected(frame, raw)
    collector.observe_result(raw)
    assert writer.wait_empty()
    writer.close()
    manifest_path = next((tmp_path / "cache").glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    observation = manifest["frames"][0]
    assert observation["pool_id"] == "pool-current" and observation["build_id"]
    assert observation["coordinate_space"] == "physical_client"
    assert len(observation["slot_rois"]) == 3
    from hextech.infrastructure.vision.sidecar_common import LayoutTransform, apply_transform
    from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset
    preset = resolve_roi_preset(*frame.size)
    for slot in observation["slot_rois"]:
        for kind, definitions in (("name", preset.name_slots), ("icon", preset.slots)):
            roi = slot[kind]
            assert roi["box"] == list(apply_transform(definitions[slot["slot"]], frame.size,
                                                     LayoutTransform(dx_ratio=.01, dy_ratio=-.01, scale=1)))
            assert roi["valid"]
            content = (manifest_path.parent / roi["file"]).read_bytes()
            import hashlib
            assert hashlib.sha256(content).hexdigest() == roi["sha256"]
            with Image.open(manifest_path.parent / roi["file"]) as crop:
                assert crop.size == (roi["box"][2]-roi["box"][0], roi["box"][3]-roi["box"][1])


@pytest.mark.parametrize("invalid", ["missing_transform", "outside_capture", "clipped_client"])
def test_invalid_slot_geometry_is_explicit_and_not_cropped(tmp_path, invalid):
    collector, clock, drafts = buffer()
    raw = event()
    frame = Image.new("RGB", (320, 200))
    if invalid != "missing_transform":
        raw["source"]["layout_transform"] = {"dx_ratio": 1.0 if invalid == "clipped_client" else 0.,
                                             "dy_ratio": 0., "scale": 1.}
    if invalid == "outside_capture":
        frame.info.update(hextech_roi_origin=(145, 80), hextech_roi_size=(25, 50))
    collector.capture(frame, raw)
    collector.observe_result(raw)
    assert drafts
    module.persist_selection_capture(tmp_path, drafts[-1])
    manifest = json.loads(next(tmp_path.glob("*/manifest.json")).read_text())
    rois = [slot[kind] for slot in manifest["frames"][0]["slot_rois"] for kind in ("name", "icon")]
    assert all(not roi["valid"] and roi["reason"] and "file" not in roi for roi in rois)

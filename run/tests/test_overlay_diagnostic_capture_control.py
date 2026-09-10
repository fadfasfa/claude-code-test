"""显式一次性请求接入生产循环；默认关闭、限额和过期请求不触发采集。"""
import json
from uuid import uuid4
from types import SimpleNamespace

from PIL import Image

from hextech.infrastructure.vision import diagnostic_capture_control as module


def request(tmp_path, *, age=0., build="b"):
    from hextech.infrastructure.vision.sidecar_status import SIDECAR_INSTANCE_ID
    path = tmp_path / "state" / "diagnostic_capture_request.v1.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "request_id": str(uuid4()),
                               "requested_at": 1000-age, "expected_build_id": build,
                               "expected_sidecar_instance_id": SIDECAR_INSTANCE_ID}), encoding="utf-8")


def test_default_and_stale_request_never_capture(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "time", lambda: 1000.)
    control = module.ExplicitCaptureControl(tmp_path, SimpleNamespace(), "b")
    control.poll()
    assert control.session is None and not control.wants_full_client()
    request(tmp_path, age=31.)
    clock[0] += 2
    control.poll()
    assert control.session is None
    request(tmp_path, build="other")
    clock[0] += 2
    control.poll()
    assert control.session is None


def test_request_is_once_and_waits_for_actual_selection(tmp_path, monkeypatch):
    clock = [100.]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "time", lambda: 1000.)
    drafts = []
    writer = SimpleNamespace(submit_capture=lambda session, draft: drafts.append(draft))
    control = module.ExplicitCaptureControl(tmp_path, writer, "b")
    request(tmp_path)
    control.poll()
    assert control.session is not None and not control.wants_full_client()
    frame = Image.new("RGB", (1920,1080))
    frame.info.update(hextech_capture_mode="client_full", hextech_roi_origin=(0,0),
                      hextech_roi_size=frame.size, hextech_client_size=frame.size)
    source = {"scene_present": True, "session_id": "g", "selection_epoch": 1}
    event = {"source": source}
    for i in range(40):
        control.observe(frame, event, event)
        clock[0] += .11
    assert control._full_count == 3 and control._roi_count == 12
    assert control._done and not control.wants_full_client()
    old = control.session
    clock[0] += 2
    control.poll()
    assert control.session is old
    assert sum(d.include_full_client for d in drafts) == 3
    assert sum(d.include_roi_set for d in drafts) == 12
    assert drafts[-1].terminal_reason == "budget_exhausted"
    assert not (tmp_path / "debug").exists()  # controller不负责图片写入。


def test_request_cli_has_no_default_write_and_binds_live_build(tmp_path, monkeypatch):
    from tooling.diagnostics.overlay_capture_session import request_capture
    assert request_capture(tmp_path, requested=False)["reason"] == "explicit_request_required"
    assert not list(tmp_path.iterdir())
    state = tmp_path / "state"
    state.mkdir()
    status = {"status": "running", "build_id": "b", "heartbeat_at": module.time.time(),
              "sidecar_instance_id": "test-sidecar", "explicit_capture": {"enabled": False}}
    (state / "game_overlay_sidecar_status.json").write_text(json.dumps(status))
    assert request_capture(tmp_path, requested=True)["ok"]
    payload = json.loads((state / "diagnostic_capture_request.v1.json").read_text())
    assert payload["expected_build_id"] == "b"
    assert payload["schema_version"] == 1


def test_request_cannot_replay_in_restarted_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.)
    monkeypatch.setattr(module.time, "time", lambda: 1000.)
    request(tmp_path)
    original = module.ExplicitCaptureControl(tmp_path, SimpleNamespace(), "b")
    original.poll()
    assert original.session is not None
    restarted = module.ExplicitCaptureControl(tmp_path, SimpleNamespace(), "b")
    restarted.sidecar_instance_id = "new-instance"
    restarted.poll()
    assert restarted.session is None


def test_conflict_draft_keeps_raw_ocr_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100.)
    monkeypatch.setattr(module.time, "time", lambda: 1000.)
    request(tmp_path)
    drafts = []
    control = module.ExplicitCaptureControl(tmp_path, SimpleNamespace(submit_capture=lambda _, d: drafts.append(d)), "b")
    control.poll()
    raw = {"source": {"scene_present": True, "session_id": "g", "selection_epoch": 1},
           "_raw_slots": [{"slot": 0, "ocr_shadow": {"raw_text": "迅捷碎片", "input_sha256": "a"*64}}]}
    final = {"source": {"scene_state": "blocked", "reason": "scene_type_conflict"}}
    control.observe(Image.new("RGB", (1920,1080)), raw, final)
    assert drafts[0].event["_raw_slots"][0]["ocr_shadow"]["raw_text"] == "迅捷碎片"

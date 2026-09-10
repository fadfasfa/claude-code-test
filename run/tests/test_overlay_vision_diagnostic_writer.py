"""验证 Vision 诊断写入不阻塞主识别且保持有界。"""

from __future__ import annotations

from threading import Event, current_thread

from PIL import Image


def _event(seq: int) -> dict:
    return {
        "seq": seq,
        "active": True,
        "selection_type": "hextech",
        "source": {
            "session_id": "session-a",
            "game_instance_id": "game-a",
            "selection_epoch": 1,
            "scene_state": "active",
        },
        "timing": {"observation_kind": "recognition", "capture_status": "captured"},
        "slots": [],
    }


def test_fragment_observation_and_terminal_are_retained_but_not_active(tmp_path):
    import json
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter
    from hextech.infrastructure.vision.sidecar_diagnostics import write_selection_timeline_observation

    item = _event(1)
    item.update(selection_type="body_shard", active=False)
    item["source"].update(scene_state="blocked", selection_window_active=False, reason="body_shard_only",
                          build_id="test-build", sidecar_instance_id="test-sidecar")
    writer = VisionDiagnosticWriter(trace_writer=lambda *_args: None, timeline_writer=write_selection_timeline_observation)
    assert writer.submit(item, tmp_path / "trace.json", write_trace=False)
    terminal = {**item, "source": {**item["source"], "reason": "selection_completed", "scene_state": "absent"}}
    assert writer.submit(terminal, tmp_path / "trace.json", write_trace=False)
    writer.close(timeout=2)
    files = list((tmp_path / "overlay_vision_timelines").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert all(row["selection_type"] == "body_shard" and not row["public_active"] for row in rows)
    assert rows[-1]["source_reason"] == "selection_completed"


def test_diagnostic_writer_preserves_order_off_hot_path(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    release = Event()
    started = Event()
    calls: list[tuple[str, int]] = []

    def write_trace(event, _path) -> None:
        started.set()
        release.wait(2.0)
        calls.append(("trace", int(event["seq"])))

    def write_timeline(event, _path) -> None:
        calls.append(("timeline", int(event["seq"])))

    writer = VisionDiagnosticWriter(
        trace_writer=write_trace,
        timeline_writer=write_timeline,
    )
    assert writer.submit(_event(1), tmp_path / "trace.json", write_trace=True)
    assert started.wait(1.0)
    # worker 被慢 trace 占用时，第二帧仍只入队，不等待磁盘。
    assert writer.submit(_event(2), tmp_path / "trace.json", write_trace=True)
    release.set()
    writer.close(timeout=2.0)

    assert calls == [
        ("trace", 1),
        ("timeline", 1),
        ("trace", 2),
        ("timeline", 2),
    ]
    assert writer.status()["completed_count"] == 2


def test_diagnostic_writer_drops_new_diagnostics_when_queue_is_full(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    release = Event()
    started = Event()

    def write_trace(_event, _path) -> None:
        started.set()
        release.wait(2.0)

    writer = VisionDiagnosticWriter(
        trace_writer=write_trace,
        timeline_writer=lambda _event, _path: None,
        max_queue=1,
    )
    assert writer.submit(_event(1), tmp_path / "trace.json", write_trace=True)
    assert started.wait(1.0)
    assert writer.submit(_event(2), tmp_path / "trace.json", write_trace=True)
    assert writer.submit(_event(3), tmp_path / "trace.json", write_trace=True) is False
    assert writer.status()["dropped_count"] == 1
    release.set()
    writer.close(timeout=2.0)


def test_diagnostic_writer_failure_only_updates_status(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    def fail(_event, _path) -> None:
        raise OSError("disk unavailable")

    writer = VisionDiagnosticWriter(
        trace_writer=fail,
        timeline_writer=lambda _event, _path: None,
    )
    assert writer.submit(_event(1), tmp_path / "trace.json", write_trace=True)
    writer.close(timeout=2.0)

    status = writer.status()
    assert status["failed_count"] == 1
    assert status["last_error"] == "OSError"


def test_trace_failure_does_not_block_timeline_and_reports_stage(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    calls: list[str] = []

    def fail_trace(_event, _path) -> None:
        raise FileNotFoundError("trace parent missing")

    writer = VisionDiagnosticWriter(
        trace_writer=fail_trace,
        timeline_writer=lambda _event, _path: calls.append("timeline"),
    )
    assert writer.submit(_event(1), tmp_path / "trace.json", write_trace=True)
    writer.close(timeout=2.0)

    status = writer.status()
    assert calls == ["timeline"]
    assert status["failed_count"] == 1
    assert status["trace_failed_count"] == 1
    assert status["timeline_failed_count"] == 0
    assert status["last_error"] == "FileNotFoundError"
    assert status["last_error_stage"] == "trace"


def test_timeline_status_changes_only_after_actual_append(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    def no_append(_event, _path):
        return None

    writer = VisionDiagnosticWriter(
        trace_writer=lambda _event, _path: None,
        timeline_writer=no_append,
    )
    assert writer.submit(_event(1), tmp_path / "trace.json", write_trace=False)
    writer.close(timeout=2.0)

    status = writer.status()
    assert status["current_timeline_path_hash"] == ""
    assert status["timeline_last_written_at"] == 0.0
    assert status["timeline_epoch"] == 0


def test_already_terminal_does_not_claim_a_new_timeline_write(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    target = tmp_path / "selection.jsonl"

    def skip_terminal(event, _path):
        event["_timeline_write_disposition"] = "already_terminal"
        return target

    writer = VisionDiagnosticWriter(
        trace_writer=lambda _event, _path: None,
        timeline_writer=skip_terminal,
    )
    assert writer.submit(_event(1), tmp_path / "trace.json", write_trace=False)
    writer.close(timeout=2.0)

    status = writer.status()
    assert status["timeline_skipped_count"] == 1
    assert status["current_timeline_path_hash"] == ""
    assert status["timeline_last_written_at"] == 0.0
    assert status["timeline_epoch"] == 0


def test_diagnostic_writer_ignores_ten_thousand_empty_window_probes(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    writer = VisionDiagnosticWriter(
        trace_writer=lambda _event, _path: None,
        timeline_writer=lambda _event, _path: None,
    )
    idle = {
        "selection_type": "hextech",
        "source": {"reason": "game_window_missing", "selection_epoch": 4},
        "timing": {"observation_kind": "visibility_probe"},
    }

    assert sum(writer.submit(idle, tmp_path / "trace.json", write_trace=True) for _ in range(10_000)) == 0
    assert writer.status()["ineligible_count"] == 10_000
    assert not (tmp_path / "trace.json").exists()
    writer.close()


def test_visibility_probe_writes_on_change_or_thirty_second_heartbeat(monkeypatch, tmp_path) -> None:
    from hextech.infrastructure.vision import diagnostic_writer as diagnostic

    now = [100.0]
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: now[0])
    calls: list[str] = []
    writer = diagnostic.VisionDiagnosticWriter(
        trace_writer=lambda _event, _path: None,
        timeline_writer=lambda event, _path: calls.append(str(event["source"]["reason"])),
    )
    probe = {
        "selection_type": "hextech",
        "source": {
            "session_id": "s1",
            "game_instance_id": "g1",
            "selection_epoch": 2,
            "scene_state": "paused",
            "reason": "game_not_foreground",
            "transient_pause": True,
        },
        "timing": {"observation_kind": "visibility_probe"},
        "slots": [],
    }

    assert writer.submit(probe, tmp_path / "trace.json", write_trace=False)
    now[0] += 29.0
    assert not writer.submit(probe, tmp_path / "trace.json", write_trace=False)
    now[0] += 1.0
    assert writer.submit(probe, tmp_path / "trace.json", write_trace=False)
    writer.close(timeout=2.0)
    assert calls == ["game_not_foreground", "game_not_foreground"]


def test_terminal_event_is_submitted_once(tmp_path) -> None:
    from hextech.infrastructure.vision.diagnostic_writer import VisionDiagnosticWriter

    calls: list[str] = []
    writer = VisionDiagnosticWriter(
        trace_writer=lambda _event, _path: None,
        timeline_writer=lambda event, _path: calls.append(str(event["source"]["reason"])),
    )
    ended = {
        "selection_type": "hextech",
        "source": {
            "session_id": "s1",
            "game_instance_id": "g1",
            "selection_epoch": 3,
            "scene_state": "paused",
            "reason": "gameflow_ended",
        },
        "timing": {"observation_kind": "visibility_probe"},
        "slots": [],
    }

    assert writer.submit(ended, tmp_path / "trace.json", write_trace=False)
    assert not writer.submit(ended, tmp_path / "trace.json", write_trace=False)
    writer.close(timeout=2.0)
    assert calls == ["gameflow_ended"]


def test_roi_writer_moves_png_work_off_recognition_thread(tmp_path) -> None:
    from hextech.infrastructure.vision.roi_diagnostic_writer import RoiDiagnosticWriter

    release = Event()
    started = Event()
    calls: list[tuple[str, int | None]] = []

    def dump(_root, _frame, event, *, observation_seq=None) -> None:
        calls.append((current_thread().name, observation_seq))
        started.set()
        release.wait(2.0)
        assert event["seq"] == 1

    writer = RoiDiagnosticWriter(dump_writer=dump)
    frame = Image.new("RGB", (32, 24), "black")

    assert writer.submit(tmp_path, frame, {"seq": 1}, observation_seq=7)
    assert started.wait(1.0)
    assert calls == [("overlay-roi-diagnostics", 7)]
    release.set()
    writer.close(timeout=2.0)

    assert writer.status()["completed_count"] == 1


def test_roi_writer_drops_new_task_when_bounded_queue_is_full(tmp_path) -> None:
    from hextech.infrastructure.vision.roi_diagnostic_writer import RoiDiagnosticWriter

    release = Event()
    started = Event()

    def dump(_root, _frame, _event, *, observation_seq=None) -> None:
        del observation_seq
        started.set()
        release.wait(2.0)

    writer = RoiDiagnosticWriter(dump_writer=dump, max_queue=1)
    frame = Image.new("RGB", (8, 8), "black")
    assert writer.submit(tmp_path, frame, {"seq": 1}, observation_seq=1)
    assert started.wait(1.0)
    assert writer.submit(tmp_path, frame, {"seq": 2}, observation_seq=2)
    assert writer.submit(tmp_path, frame, {"seq": 3}, observation_seq=3) is False
    assert writer.status()["dropped_count"] == 1
    release.set()
    writer.close(timeout=2.0)


def test_roi_writer_failure_only_updates_status(tmp_path) -> None:
    from hextech.infrastructure.vision.roi_diagnostic_writer import RoiDiagnosticWriter

    def fail(_root, _frame, _event, *, observation_seq=None) -> None:
        del observation_seq
        raise OSError("disk unavailable")

    writer = RoiDiagnosticWriter(dump_writer=fail)
    assert writer.submit(
        tmp_path,
        Image.new("RGB", (8, 8), "black"),
        {"seq": 1},
        observation_seq=1,
    )
    writer.close(timeout=2.0)

    status = writer.status()
    assert status["failed_count"] == 1
    assert status["last_error"] == "OSError"

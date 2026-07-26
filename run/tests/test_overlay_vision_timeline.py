"""验证 selection epoch 结构化时间线不会被空闲心跳挤掉。"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from hextech.infrastructure.vision import runner, sidecar_diagnostics


def _event(*, epoch: int, observed_at: float, active: bool = True) -> dict:
    raw_slot = {
        "slot": 0,
        "diagnostic": "text_icon_disagree",
        "channels": {
            "text": {
                "margin": 0.12,
                "top_candidates": [{"augment_id": "101", "name": "双发快射", "confidence": 0.91}],
            },
            "text_alt": {
                "margin": 0.08,
                "top_candidates": [{"augment_id": "101", "name": "双发快射", "confidence": 0.88}],
            },
            "icon": {
                "margin": 0.03,
                "top_candidates": [{"augment_id": "202", "name": "错误图标", "confidence": 0.74}],
            },
        },
    }
    return {
        "active": active,
        "selection_type": "hextech",
        "source": {
            "session_id": "session-a",
            "selection_epoch": epoch,
            "selection_revision": 1,
            "scene_state": "active" if active else "absent",
            "scene_present": active,
            "scene_score": 0.93,
            "selection_button_present": active,
            "card_residue": True,
            "name_residue": [True, True, True],
            "cursor_over_slots": [1],
            "selection_click": False,
            "scene_temporal_state": "grace_hold" if active else "ended",
        },
        "timing": {
            "capture_started_at": observed_at - 0.05,
            "captured_at": observed_at - 0.02,
            "recognition_completed_at": observed_at,
        },
        "_raw_slots": [raw_slot, {}, {}],
        "slots": [
            {
                "slot": 0,
                "state": "detecting",
                "candidate_identity": "双发快射",
                "evidence_grade": "medium",
                "evidence_hits": 1,
                "evidence_window": 1,
                "required_hits": 3,
                "temporal_state": "evidence_pending",
            },
            {"slot": 1, "state": "detecting"},
            {"slot": 2, "state": "detecting"},
        ],
    }


def test_selection_timeline_appends_real_observations_without_images(tmp_path: Path) -> None:
    trace_path = tmp_path / "state" / "overlay_vision_trace.v1.json"

    target = sidecar_diagnostics.write_selection_timeline_observation(_event(epoch=7, observed_at=10.0), trace_path)
    sidecar_diagnostics.write_selection_timeline_observation(_event(epoch=7, observed_at=10.2), trace_path)

    assert target is not None
    entries = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [entry["observation_seq"] for entry in entries] == [1, 2]
    assert entries[0]["recognition_completed_at"] == 10.0
    assert entries[0]["capture_status"] == "captured"
    assert entries[0]["source"] == {"reason": ""}
    assert entries[0]["scene_present"] is True
    assert entries[0]["cursor_over_slots"] == [1]
    assert entries[0]["latency_ms"] == {"capture": 30.0, "recognition": 20.0, "total": 50.0}
    assert entries[0]["slots"][0]["text"]["name"] == "双发快射"
    assert entries[0]["slots"][0]["icon"]["name"] == "错误图标"
    assert not any("image" in key or "frame" in key for key in entries[0])


def test_selection_timeline_ignores_idle_events_and_keeps_latest_twenty_epochs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    trace_path = tmp_path / "state" / "overlay_vision_trace.v1.json"
    monkeypatch.setattr(sidecar_diagnostics, "VISION_TIMELINE_EPOCH_LIMIT", 20)

    assert sidecar_diagnostics.write_selection_timeline_observation(
        _event(epoch=1, observed_at=1.0, active=False), trace_path
    ) is None
    for epoch in range(1, 23):
        sidecar_diagnostics.write_selection_timeline_observation(
            _event(epoch=epoch, observed_at=float(epoch)), trace_path
        )

    files = sorted((trace_path.parent / "overlay_vision_timelines").glob("*.jsonl"))
    assert len(files) == 20
    assert not any(path.name.endswith("e0001.jsonl") for path in files)
    assert not any(path.name.endswith("e0002.jsonl") for path in files)


def test_diagnostic_roi_sampler_keeps_first_five_observations_per_epoch() -> None:
    sampler = runner._DiagnosticEpochSampler()
    source = {"session_id": "session-a", "selection_epoch": 7, "scene_state": "active"}

    assert [sampler.next_observation_seq(source) for _ in range(6)] == [1, 2, 3, 4, 5, None]
    assert sampler.next_observation_seq({**source, "scene_state": "absent"}) is None
    assert sampler.next_observation_seq({**source, "selection_epoch": 8}) == 1


def test_diagnostic_dump_contains_only_bounded_rois_and_observation_metadata(tmp_path: Path) -> None:
    event = _event(epoch=9, observed_at=20.0)
    event["source"]["preset"] = "auto"

    target = sidecar_diagnostics._write_roi_diagnostic_dump(
        tmp_path,
        Image.new("RGB", (1920, 1080), "black"),
        event,
        observation_seq=3,
    )

    assert {path.name for path in target.iterdir()} == {
        "button.png",
        "icon_0.png",
        "icon_1.png",
        "icon_2.png",
        "name_0.png",
        "name_1.png",
        "name_2.png",
        "report.json",
    }
    report = json.loads((target / "report.json").read_text(encoding="utf-8"))
    assert report["observation"] == {
        "observation_id": target.name,
        "observation_seq": 3,
        "selection_epoch": 9,
        "selection_revision": 1,
        "capture_started_at": 19.95,
        "captured_at": 19.98,
        "recognition_completed_at": 20.0,
    }


def test_selection_timeline_end_event_records_epoch_latency_percentiles(tmp_path: Path) -> None:
    trace_path = tmp_path / "state" / "overlay_vision_trace.v1.json"
    sidecar_diagnostics.write_selection_timeline_observation(_event(epoch=11, observed_at=30.0), trace_path)
    sidecar_diagnostics.write_selection_timeline_observation(_event(epoch=11, observed_at=30.2), trace_path)
    ended = _event(epoch=11, observed_at=30.4, active=False)
    ended["source"]["reason"] = "scene_loss_confirmed"

    target = sidecar_diagnostics.write_selection_timeline_observation(ended, trace_path)

    assert target is not None
    final = json.loads(target.read_text(encoding="utf-8").splitlines()[-1])
    assert final["source"] == {"reason": "scene_loss_confirmed"}
    assert final["epoch_latency_ms"] == {
        "capture": {"count": 3, "p50": 30.0, "p95": 30.0},
        "recognition": {"count": 3, "p50": 20.0, "p95": 20.0},
        "total": {"count": 3, "p50": 50.0, "p95": 50.0},
    }


def test_selection_timeline_keeps_pause_and_gameflow_end_in_original_epoch(tmp_path: Path) -> None:
    trace_path = tmp_path / "state" / "overlay_vision_trace.v1.json"
    paused = _event(epoch=12, observed_at=40.0, active=False)
    paused["source"].update(
        {
            "reason": "game_not_foreground",
            "scene_state": "paused",
            "transient_pause": True,
            "game_instance_id": "game-a",
        }
    )
    ended = _event(epoch=12, observed_at=40.2, active=False)
    ended["source"].update(
        {
            "reason": "gameflow_ended",
            "game_instance_id": "game-a",
        }
    )

    target = sidecar_diagnostics.write_selection_timeline_observation(paused, trace_path)
    sidecar_diagnostics.write_selection_timeline_observation(ended, trace_path)

    assert target is not None
    entries = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [entry["source_reason"] for entry in entries] == ["game_not_foreground", "gameflow_ended"]
    assert [entry["session_id"] for entry in entries] == ["session-a", "session-a"]
    assert [entry["game_instance_id"] for entry in entries] == ["game-a", "game-a"]
    assert entries[-1]["epoch_latency_ms"]["total"]["count"] == 2


def test_pause_events_use_non_captured_monotonic_timing() -> None:
    from hextech.infrastructure.vision import sidecar

    paused = {
        "source": {"reason": "game_not_foreground", "transient_pause": True},
    }
    ended = {"source": {"reason": "gameflow_ended"}}

    sidecar.attach_visibility_probe_timing(paused)
    sidecar.attach_visibility_probe_timing(ended)

    for event in (paused, ended):
        timing = event["timing"]
        assert timing["capture_status"] == "not_captured"
        assert timing["observation_kind"] == "visibility_probe"
        assert timing["capture_started_at"] <= timing["captured_at"] <= timing["recognition_completed_at"]


def test_selection_timeline_marks_zero_duration_visibility_probe(tmp_path: Path) -> None:
    trace_path = tmp_path / "state" / "overlay_vision_trace.v1.json"
    paused = _event(epoch=13, observed_at=50.0, active=False)
    paused["source"].update(
        {
            "reason": "game_not_foreground",
            "scene_state": "paused",
            "transient_pause": True,
        }
    )
    paused["timing"] = {
        "observation_kind": "visibility_probe",
        "capture_started_at": 50.0,
        "captured_at": 50.0,
        "recognition_completed_at": 50.0,
    }

    target = sidecar_diagnostics.write_selection_timeline_observation(paused, trace_path)

    assert target is not None
    entry = json.loads(target.read_text(encoding="utf-8").strip())
    assert entry["observation_kind"] == "visibility_probe"
    assert entry["capture_started_at"] == 50.0
    assert entry["captured_at"] == 50.0
    assert entry["recognition_completed_at"] == 50.0
    assert entry["latency_ms"] == {"capture": 0.0, "recognition": 0.0, "total": 0.0}


def test_epoch_latency_excludes_visibility_probes(tmp_path: Path) -> None:
    trace_path = tmp_path / "state" / "overlay_vision_trace.v1.json"
    sidecar_diagnostics.write_selection_timeline_observation(_event(epoch=14, observed_at=60.0), trace_path)
    paused = _event(epoch=14, observed_at=60.1, active=False)
    paused["source"].update({"reason": "game_not_foreground", "scene_state": "paused", "transient_pause": True})
    paused["timing"] = {
        "observation_kind": "visibility_probe",
        "capture_status": "not_captured",
        "capture_started_at": 60.1,
        "captured_at": 60.1,
        "recognition_completed_at": 60.1,
    }
    sidecar_diagnostics.write_selection_timeline_observation(paused, trace_path)
    capture_failure = _event(epoch=14, observed_at=60.15, active=False)
    capture_failure["source"]["reason"] = "capture_unavailable"
    capture_failure["timing"] = {
        "observation_kind": "capture_failure",
        "capture_status": "unavailable",
        "capture_started_at": 60.15,
        "captured_at": 60.15,
        "recognition_completed_at": 60.15,
    }
    sidecar_diagnostics.write_selection_timeline_observation(capture_failure, trace_path)
    ended = _event(epoch=14, observed_at=60.2, active=False)
    ended["source"]["reason"] = "gameflow_ended"
    ended["timing"] = {
        "observation_kind": "visibility_probe",
        "capture_status": "not_captured",
        "capture_started_at": 60.2,
        "captured_at": 60.2,
        "recognition_completed_at": 60.2,
    }

    target = sidecar_diagnostics.write_selection_timeline_observation(ended, trace_path)

    assert target is not None
    final = json.loads(target.read_text(encoding="utf-8").splitlines()[-1])
    assert final["epoch_latency_ms"] == {
        "capture": {"count": 1, "p50": 30.0, "p95": 30.0},
        "recognition": {"count": 1, "p50": 20.0, "p95": 20.0},
        "total": {"count": 1, "p50": 50.0, "p95": 50.0},
    }

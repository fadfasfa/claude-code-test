"""真实 Overlay timeline 与 Host report 的性能门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tooling.acceptance.overlay_performance_probe import build_real_session_performance_report


def _fixture(
    root: Path,
    *,
    total_ms: float = 170.0,
    first_present_ms: float = 850.0,
    second_present_ms: float = 800.0,
    first_feedback_ms: float = 200.0,
    reroll_ready_ms: float = 800.0,
    event_to_present_ms: float = 80.0,
):
    session_id = "session-performance"
    timeline = root / "selection-e0001.jsonl"
    rows = []
    for index in range(50):
        started = 1000.0 + index * 0.25
        epoch = 1 if index < 25 else 2
        rows.append(
            {
                "session_id": session_id,
                "selection_epoch": epoch,
                "selection_type": "hextech",
                "scene_state": "active",
                "selection_window_active": True,
                "observation_kind": "recognition",
                "capture_status": "captured",
                "capture_started_at": started,
                "captured_at": started + 0.03,
                "recognition_completed_at": started + total_ms / 1000.0,
            }
        )
    generations = [1, 1, 1]
    identities = ["augment-a", "augment-b", "augment-c"]
    transition_rows = []
    transitions = []
    for index in range(15):
        slot = index % 3
        generations[slot] += 1
        observed_at = 1013.0 + index
        transition_slots = [
            {
                "slot": item,
                "state": "detecting" if item == slot else "ready",
                "augment_id": "" if item == slot else identities[item],
                "slot_generation": generations[item],
            }
            for item in range(3)
        ]
        transition_rows.append(
            {
                "session_id": session_id,
                "selection_epoch": 2,
                "selection_type": "hextech",
                "scene_state": "active",
                "selection_window_active": True,
                "observation_kind": "visibility_probe",
                "capture_status": "not_captured",
                "capture_started_at": observed_at,
                "captured_at": observed_at,
                "recognition_completed_at": observed_at,
                "event_written_at": observed_at + 0.05,
                "mouse_event_sequence": index + 1,
                "mouse_event_observed_at": observed_at,
                "transition_source": "async_mouse_down",
                "transition_slot": slot,
                "slots": transition_slots,
            }
        )
        new_identity = f"reroll-{index}"
        transitions.append(
            {
                "slot": slot,
                "observed_at": observed_at,
                "generation": generations[slot],
                "before_identities": list(identities),
                "before_generations": list(generations),
                "new_identity": new_identity,
            }
        )
        identities[slot] = new_identity
    timeline.write_text(
        "\n".join(json.dumps(row) for row in [*rows, *transition_rows]) + "\n",
        encoding="utf-8",
    )
    reports = root / "reports"
    reports.mkdir()
    report_rows = []

    def add_report(*, epoch: int, presented: float, ready: bool, slots: list[dict], revision: int) -> None:
        written = presented - event_to_present_ms / 1000.0
        payload = {
            "session_id": session_id,
            "event": {"visible": True},
            "source": {"selection_epoch": epoch, "selection_revision": revision},
            "presentation": {"event_selection_epoch": epoch, "ready_frame": ready},
            "slots": slots,
            "render": {
                "rows": [
                    {"status_code": "READY" if ready else "DETECTING"}
                    for _ in range(3)
                ]
            },
            "timing": {
                "event_written_at": written,
                "host_read_at": written + 0.02,
                "draw_started_at": written + 0.03,
                "draw_completed_at": written + 0.05,
                "presented_at": presented,
            },
        }
        report_rows.append(payload)

    initial_slots = [
        {"slot": index, "state": "ready", "augment_id": f"augment-{index}", "slot_generation": 1}
        for index in range(3)
    ]
    add_report(
        epoch=1,
        presented=1000.0 + first_feedback_ms / 1000.0,
        ready=False,
        slots=[{**item, "state": "detecting", "augment_id": ""} for item in initial_slots],
        revision=1,
    )
    add_report(
        epoch=1,
        presented=1000.0 + first_present_ms / 1000.0,
        ready=True,
        slots=initial_slots,
        revision=1,
    )
    second_started = 1000.0 + 25 * 0.25
    add_report(
        epoch=2,
        presented=second_started + first_feedback_ms / 1000.0,
        ready=False,
        slots=[{**item, "state": "detecting", "augment_id": ""} for item in initial_slots],
        revision=1,
    )
    add_report(
        epoch=2,
        presented=second_started + second_present_ms / 1000.0,
        ready=True,
        slots=initial_slots,
        revision=1,
    )
    live_identities = ["augment-a", "augment-b", "augment-c"]
    live_generations = [1, 1, 1]
    for index, transition in enumerate(transitions, start=1):
        slot = transition["slot"]
        live_generations[slot] = transition["generation"]
        detecting_slots = [
            {
                "slot": item,
                "state": "detecting" if item == slot else "ready",
                "augment_id": "" if item == slot else live_identities[item],
                "slot_generation": live_generations[item],
            }
            for item in range(3)
        ]
        add_report(
            epoch=2,
            presented=transition["observed_at"] + 0.13,
            ready=False,
            slots=detecting_slots,
            revision=index,
        )
        live_identities[slot] = transition["new_identity"]
        ready_slots = [
            {
                "slot": item,
                "state": "ready",
                "augment_id": live_identities[item],
                "slot_generation": live_generations[item],
            }
            for item in range(3)
        ]
        add_report(
            epoch=2,
            presented=transition["observed_at"] + reroll_ready_ms / 1000.0,
            ready=True,
            slots=ready_slots,
            revision=index + 1,
        )
    for index, payload in enumerate(report_rows):
        (reports / f"overlay-session-{index:03d}.json").write_text(json.dumps(payload), encoding="utf-8")
    return timeline, reports


def test_real_session_performance_passes_locked_gates(tmp_path: Path) -> None:
    timeline, reports = _fixture(tmp_path)

    result = build_real_session_performance_report(timeline, reports)

    assert result["latency"]["capture_recognition"]["p95_ms"] == 170.0
    assert result["latency"]["capture"]["p95_ms"] == 30.0
    assert result["latency"]["recognition"]["p95_ms"] == 140.0
    assert result["eligible_epoch_count"] == 2
    assert result["excluded_epochs_by_reason"] == {}
    assert result["first_full_canvas"]["samples_ms"] == [850.0, 800.0]
    assert result["first_full_canvas"]["p95_ms"] == 850.0
    assert result["first_full_canvas"]["pass_coverage"] is True
    assert result["first_feedback"]["p95_ms"] == 200.0
    assert result["reroll_refresh"]["count"] == 15
    assert result["reroll_refresh"]["mouse_down_to_new_ready_presented"]["p95_ms"] == 800.0
    assert result["reroll_refresh"]["passed"] is True
    assert result["latency"]["event_written_to_presented"]["p95_ms"] == 80.0
    assert result["passed"] is True


def test_real_session_performance_fails_each_locked_boundary(tmp_path: Path) -> None:
    timeline, reports = _fixture(
        tmp_path,
        total_ms=181.0,
        first_present_ms=901.0,
        second_present_ms=901.0,
        first_feedback_ms=301.0,
        reroll_ready_ms=901.0,
        event_to_present_ms=101.0,
    )

    result = build_real_session_performance_report(timeline, reports)

    assert result["gates"]["capture_recognition_p95"] is False
    assert result["gates"]["first_feedback"] is False
    assert result["gates"]["first_full_canvas"] is False
    assert result["gates"]["event_written_to_presented"] is False
    assert result["gates"]["reroll_refresh"] is False
    assert result["passed"] is False


def test_real_session_performance_rejects_epoch_without_full_canvas(tmp_path: Path) -> None:
    timeline, reports = _fixture(tmp_path)
    for path in list(reports.glob("overlay-session-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["source"]["selection_epoch"] == 2:
            payload["presentation"]["ready_frame"] = False
            payload["render"]["rows"][1]["status_code"] = "DETECTING"
            path.write_text(json.dumps(payload), encoding="utf-8")

    result = build_real_session_performance_report(timeline, reports)

    assert result["first_full_canvas"]["expected_epoch_count"] == 2
    assert result["first_full_canvas"]["complete_epoch_count"] == 1
    assert result["first_full_canvas"]["pass_coverage"] is False
    assert result["passed"] is False


def test_real_session_performance_excludes_probe_and_duplicate_samples(tmp_path: Path) -> None:
    timeline, reports = _fixture(tmp_path)
    rows = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
    rows[0]["observation_seq"] = 1
    duplicate = dict(rows[0])
    visibility_probe = {
        **rows[0],
        "observation_kind": "visibility_probe",
        "observation_seq": 2,
        "recognition_completed_at": rows[0]["capture_started_at"] + 30.0,
    }
    timeline.write_text(
        "\n".join(json.dumps(row) for row in [*rows, duplicate, visibility_probe]) + "\n",
        encoding="utf-8",
    )
    repeated_report = json.loads((reports / "overlay-session-000.json").read_text(encoding="utf-8"))
    (reports / "overlay-session-duplicate.json").write_text(
        json.dumps(repeated_report),
        encoding="utf-8",
    )

    result = build_real_session_performance_report(timeline, reports)

    assert result["captured_observations"]["count"] == 50
    assert result["latency"]["capture_recognition"]["p95_ms"] == 170.0
    assert result["latency"]["event_written_to_presented"]["count"] == 34


def test_real_session_performance_requires_fifteen_rerolls(tmp_path: Path) -> None:
    timeline, reports = _fixture(tmp_path)
    rows = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
    timeline.write_text(
        "\n".join(json.dumps(row) for row in rows if not row.get("mouse_event_sequence")) + "\n",
        encoding="utf-8",
    )

    result = build_real_session_performance_report(timeline, reports)

    assert result["reroll_refresh"]["count"] == 0
    assert result["gates"]["reroll_refresh"] is False
    assert result["passed"] is False


def test_real_session_performance_excludes_candidate_body_shard_and_pause_epochs(
    tmp_path: Path,
) -> None:
    timeline, reports = _fixture(tmp_path)
    rows = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
    rows.extend(
        [
            {
                **rows[0],
                "selection_epoch": 3,
                "selection_type": "hextech",
                "scene_state": "candidate",
                "selection_window_active": False,
            },
            {
                **rows[0],
                "selection_epoch": 4,
                "selection_type": "body_shard",
                "scene_state": "blocked",
                "selection_window_active": False,
            },
            {
                **rows[0],
                "selection_epoch": 5,
                "selection_type": "hextech",
                "scene_state": "paused",
                "selection_window_active": False,
                "observation_kind": "visibility_probe",
                "capture_status": "not_captured",
            },
        ]
    )
    timeline.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = build_real_session_performance_report(timeline, reports)

    assert result["eligible_epochs"] == [1, 2]
    assert result["excluded_epochs_by_reason"] == {
        "body_shard": 1,
        "candidate_only": 1,
        "probe_or_pause_only": 1,
    }
    assert result["captured_observations"]["count"] == 50


def test_real_session_performance_binds_old_composition_callback_to_draw_event(tmp_path: Path) -> None:
    from tooling.acceptance.overlay_performance_probe import _bound_presentation_records

    draw_report = {
        "session_id": "session-1",
        "event": {"visible": True},
        "source": {"selection_epoch": 4},
        "render": {"rows": [{"status_code": "READY"} for _ in range(3)]},
        "timing": {
            "event_written_at": 10.0,
            "host_read_at": 10.01,
            "draw_started_at": 10.02,
            "draw_completed_at": 10.03,
            "presented_at": 0.0,
        },
    }
    callback_report = {
        **draw_report,
        "timing": {
            **draw_report["timing"],
            # composition 回调时文件里已经是更新的事件，不能与旧 Canvas 配对。
            "event_written_at": 10.2,
            "host_read_at": 10.21,
            "draw_started_at": 10.02,
            "presented_at": 10.09,
        },
    }

    records = _bound_presentation_records(
        [draw_report, callback_report],
        session_id="session-1",
    )

    assert len(records) == 1
    assert records[0]["event_written_at"] == 10.0
    assert records[0]["presented_at"] == 10.09
    assert records[0]["selection_epoch"] == 4
    assert records[0]["ready_frame"] is True


def test_real_session_performance_prefers_explicit_presented_event_binding() -> None:
    from tooling.acceptance.overlay_performance_probe import _bound_presentation_records

    report = {
        "session_id": "session-1",
        "source": {"selection_epoch": 9},
        "presentation": {
            "event_selection_epoch": 8,
            "ready_frame": True,
        },
        "timing": {
            "event_written_at": 20.0,
            "presented_event_written_at": 19.9,
            "host_read_at": 19.92,
            "draw_started_at": 19.93,
            "draw_completed_at": 19.94,
            "presented_at": 19.98,
        },
    }

    records = _bound_presentation_records([report], session_id="session-1")

    assert records[0]["event_written_at"] == 19.9
    assert records[0]["selection_epoch"] == 8
    assert records[0]["ready_frame"] is True


def test_same_event_redraw_does_not_inflate_independent_latency_count():
    from tooling.acceptance.overlay_performance_probe import _first_event_presentations
    rows = [{"selection_epoch": 1, "event_written_at": 10., "presented_at": 11.1},
            {"selection_epoch": 1, "event_written_at": 10., "presented_at": 10.2},
            {"selection_epoch": 1, "event_written_at": 12., "presented_at": 13.1}]
    unique = _first_event_presentations(rows)
    assert len(unique) == 2
    assert unique[0]["presented_at"] == 10.2
    assert unique[1]["presented_at"] == 13.1  # 真正1.1秒慢事件仍保留。
    assert len(rows) == 3  # 不改写历史报告，也不丢弃升级为完整三槽的记录。


def test_real_session_performance_requires_one_target_build_and_sidecar(tmp_path: Path) -> None:
    timeline, reports = _fixture(tmp_path)
    rows = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["build_id"] = "build-third"
        row["sidecar_instance_id"] = "sidecar-third"
    timeline.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    for path in reports.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["build_id"] = "build-third"
        path.write_text(json.dumps(payload), encoding="utf-8")

    result = build_real_session_performance_report(
        timeline,
        reports,
        expected_build_id="build-third",
    )
    assert result["expected_build_id"] == "build-third"

    rows[0]["sidecar_instance_id"] = "foreign-sidecar"
    timeline.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Sidecar instance"):
        build_real_session_performance_report(
            timeline,
            reports,
            expected_build_id="build-third",
        )

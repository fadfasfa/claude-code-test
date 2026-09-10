"""Overlay READY 必须由独立截图人工真值验收，不能自证正确。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from tooling.acceptance.overlay_truth_probe import build_overlay_truth_report


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _timeline_row(
    seq: int,
    *,
    captured_at: float,
    ids: tuple[str, str, str] = ("a", "b", "c"),
    epoch: int = 1,
    build_id: str = "build-a",
    session_id: str = "session-a",
    sidecar_id: str = "sidecar-a",
    generations: tuple[int, int, int] = (1, 1, 1),
) -> dict:
    return {
        "schema_version": 2,
        "build_id": build_id,
        "session_id": session_id,
        "sidecar_instance_id": sidecar_id,
        "sidecar_pid": 4321,
        "selection_epoch": epoch,
        "observation_seq": seq,
        "captured_frame_id": seq,
        "selection_type": "hextech",
        "scene_state": "active",
        "selection_window_active": True,
        "observation_kind": "recognition",
        "capture_status": "captured",
        "capture_started_at": captured_at - 0.03,
        "captured_at": captured_at,
        "recognition_completed_at": captured_at + 0.05,
        "slots": [
            {
                "slot": slot,
                "state": "ready",
                "augment_id": augment_id,
                "name": augment_id.upper(),
                "slot_generation": generations[slot],
            }
            for slot, augment_id in enumerate(ids)
        ],
    }


def _write_timeline(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _truth(
    root: Path,
    frames: list[tuple[str, int, float, str]],
    *,
    build_id: str = "build-a",
) -> dict:
    truth_frames = []
    for capture_id, seq, captured_at, filename in frames:
        image = root / filename
        color = tuple(hashlib.sha256(capture_id.encode()).digest()[:3])
        Image.new("RGB", (2, 2), color).save(image)
        truth_frames.append(
            {
                "capture_id": capture_id,
                "selection_epoch": 1,
                "observation_seq": seq,
                "captured_frame_id": seq,
                "captured_at": captured_at,
                "image_kind": "full_frame",
                "image_source": filename,
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "build_id": build_id,
        "session_id": "session-a",
        "sidecar_instance_id": "sidecar-a",
        "sidecar_pid": 4321,
        "frames": truth_frames,
        "truth_spans": [
            {
                "selection_epoch": 1,
                "slot": slot,
                "slot_generation": 1,
                "start_observation_seq": min(frame[1] for frame in frames),
                "end_observation_seq": max(frame[1] for frame in frames),
                "augment_id": augment_id,
                "name": augment_id.upper(),
                "label_source": "human",
            }
            for slot, augment_id in enumerate(("a", "b", "c"))
        ],
    }


def _report(timeline: Path, truth_file: Path, **overrides: object) -> dict:
    arguments = {
        "expected_build_id": "build-a",
        "session_id": "session-a",
        "expected_sidecar_instance_id": "sidecar-a",
        "expected_sidecar_pid": 4321,
        **overrides,
    }
    return build_overlay_truth_report(timeline, truth_file, **arguments)


def test_missing_explicit_truth_is_json_unqualified(tmp_path: Path) -> None:
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [_timeline_row(1, captured_at=10.0)])
    missing = tmp_path / "missing-truth.json"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tooling.acceptance.overlay_truth_probe",
            "--timeline",
            str(timeline),
            "--truth-file",
            str(missing),
            "--expected-build-id",
            "build-a",
            "--session-id",
            "session-a",
            "--expected-sidecar-instance-id",
            "sidecar-a",
            "--expected-sidecar-pid",
            "4321",
        ],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    report = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert completed.stderr == ""
    assert report["qualified"] is False
    assert report["passed"] is False
    assert "truth_file_missing" in report["qualification_errors"]


def test_wrong_ready_then_correct_retains_false_ready_and_fails_gate(tmp_path: Path) -> None:
    timeline = _write_timeline(
        tmp_path / "timeline.jsonl",
        [
            _timeline_row(1, captured_at=10.0, ids=("wrong", "b", "c")),
            _timeline_row(2, captured_at=10.3),
        ],
    )
    truth_file = _write_json(
        tmp_path / "truth.json",
        _truth(tmp_path, [("capture-1", 1, 10.0, "one.png"), ("capture-2", 2, 10.3, "two.png")]),
    )

    report = _report(timeline, truth_file)

    assert report["qualified"] is True
    assert report["false_ready"]["count"] == 1
    assert report["false_ready"]["records"][0]["observed_augment_id"] == "wrong"
    assert report["first_correct_per_slot"][0]["observation_seq"] == 2
    assert report["all_three_correct"]["samples_ms"] == [380.0]
    assert report["gates"]["zero_false_ready"] is False
    assert report["passed"] is False


def test_same_capture_is_not_counted_twice(tmp_path: Path) -> None:
    timeline = _write_timeline(
        tmp_path / "timeline.jsonl",
        [_timeline_row(1, captured_at=10.0), _timeline_row(1, captured_at=10.0)],
    )
    payload = _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")])
    payload["frames"].append(dict(payload["frames"][0]))
    truth_file = _write_json(tmp_path / "truth.json", payload)

    report = _report(timeline, truth_file)

    assert report["qualified_frame_count"] == 1
    assert report["duplicate_capture_count"] == 1
    assert report["timeline_duplicate_count"] == 1
    assert report["all_three_correct"]["count"] == 1
    assert report["passed"] is False


def test_equal_image_bytes_can_be_independent_attested_captures(tmp_path: Path) -> None:
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    Image.new("RGB", (2, 2), "black").save(first)
    Image.new("RGB", (2, 2), "black").save(second)
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    payload = _truth(tmp_path, [("capture-1", 1, 10.0, "replace-1.png"), ("capture-2", 2, 10.3, "replace-2.png")])
    payload["frames"] = [
        {
            "capture_id": "capture-1",
            "selection_epoch": 1,
            "observation_seq": 1,
            "captured_frame_id": 1,
            "captured_at": 10.0,
            "image_kind": "full_frame",
            "image_source": "one.png",
            "image_sha256": digest,
        },
        {
            "capture_id": "capture-2",
            "selection_epoch": 1,
            "observation_seq": 2,
            "captured_frame_id": 2,
            "captured_at": 10.3,
            "image_kind": "full_frame",
            "image_source": "two.png",
            "image_sha256": digest,
        },
    ]
    timeline = _write_timeline(
        tmp_path / "timeline.jsonl",
        [_timeline_row(1, captured_at=10.0), _timeline_row(2, captured_at=10.3)],
    )

    report = _report(timeline, _write_json(tmp_path / "truth.json", payload))

    assert report["qualified_frame_count"] == 2
    assert report["duplicate_capture_count"] == 0
    assert report["passed"] is True
    assert report["whole_real_game_go"] is False


def test_stale_out_of_order_and_cross_epoch_ready_cannot_count(tmp_path: Path) -> None:
    correct = _timeline_row(1, captured_at=10.0)
    stale = _timeline_row(2, captured_at=10.3)
    stale["slots"][0]["ocr_production"] = {
        "state": "admitted",
        "session_id": "old-session",
        "selection_epoch": 9,
        "slot_index": 0,
        "slot_generation": 1,
        "captured_frame_id": 2,
        "canonical_id": "a",
    }
    out_of_order = _timeline_row(3, captured_at=9.9)
    cross_epoch = _timeline_row(4, captured_at=10.6, epoch=2)
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [correct, stale, out_of_order, cross_epoch])
    payload = _truth(
        tmp_path,
        [
            ("capture-1", 1, 10.0, "one.png"),
            ("capture-2", 2, 10.3, "two.png"),
            ("capture-3", 3, 9.9, "three.png"),
            ("capture-4", 4, 10.6, "four.png"),
        ],
    )
    payload["frames"][1]["captured_frame_id"] = 2
    truth_file = _write_json(tmp_path / "truth.json", payload)

    report = _report(timeline, truth_file)

    assert report["invalid_result_count"] >= 3
    assert report["excluded_frames_by_reason"]["stale_result_binding"] == 1
    assert report["excluded_frames_by_reason"]["out_of_order_capture"] == 1
    assert report["excluded_frames_by_reason"]["timeline_identity_mismatch"] == 1
    assert report["qualified_frame_count"] == 1
    assert report["passed"] is False


def test_unconfirmed_slot_timeout_is_retained(tmp_path: Path) -> None:
    row = _timeline_row(1, captured_at=10.0)
    row["slots"][1] = {
        "slot": 1,
        "state": "detecting",
        "augment_id": "",
        "name": "",
        "slot_generation": 1,
    }
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [row])
    truth_file = _write_json(tmp_path / "truth.json", _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")]))

    report = _report(timeline, truth_file)

    assert report["unconfirmed_timeouts"]["count"] == 1
    assert report["unconfirmed_timeouts"]["records"][0]["slot"] == 1
    assert report["gates"]["all_truth_spans_confirmed"] is False
    assert report["passed"] is False


def test_mismatched_expected_build_is_unqualified_not_filtered_to_pass(tmp_path: Path) -> None:
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [_timeline_row(1, captured_at=10.0)])
    truth_file = _write_json(
        tmp_path / "truth.json",
        _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")], build_id="build-b"),
    )

    report = _report(timeline, truth_file)

    assert report["qualified"] is False
    assert "expected_build_truth_mismatch" in report["qualification_errors"]
    assert report["passed"] is False


def test_ocr_self_label_and_missing_image_source_are_unqualified(tmp_path: Path) -> None:
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [_timeline_row(1, captured_at=10.0)])
    payload = _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")])
    payload["truth_spans"][0]["label_source"] = "ocr"
    payload["frames"][0].pop("image_source")

    report = _report(timeline, _write_json(tmp_path / "truth.json", payload))

    assert report["qualified"] is False
    assert "human_truth_span_invalid" in report["qualification_errors"]
    assert "image_source_or_hash_missing" in report["qualification_errors"]
    assert report["passed"] is False


@pytest.mark.parametrize(
    "ocr_override",
    [
        {"captured_frame_id": 0, "canonical_id": "a"},
        {"canonical_id": "a"},
        {"captured_frame_id": 1, "canonical_id": ""},
    ],
)
def test_admitted_ocr_missing_frame_or_canonical_binding_cannot_count(tmp_path: Path, ocr_override: dict) -> None:
    row = _timeline_row(1, captured_at=10.0)
    row["slots"][0]["ocr_production"] = {
        "state": "admitted",
        "session_id": "session-a",
        "selection_epoch": 1,
        "slot_index": 0,
        "slot_generation": 1,
        **ocr_override,
    }
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [row])
    payload = _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")])
    payload["frames"][0]["captured_frame_id"] = 1

    report = _report(timeline, _write_json(tmp_path / "truth.json", payload))

    assert report["qualified_frame_count"] == 0
    assert report["excluded_frames_by_reason"]["stale_result_binding"] == 1
    assert report["invalid_result_count"] == 1
    assert report["passed"] is False


def test_duplicate_or_positional_slot_rows_are_rejected(tmp_path: Path) -> None:
    row = _timeline_row(1, captured_at=10.0)
    row["slots"][2]["slot"] = 1
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [row])
    truth_file = _write_json(tmp_path / "truth.json", _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")]))

    report = _report(timeline, truth_file)

    assert report["qualified_frame_count"] == 0
    assert report["excluded_frames_by_reason"]["timeline_slots_ambiguous"] == 1
    assert report["passed"] is False


def test_omitting_early_wrong_ready_or_shifting_span_start_is_unqualified(tmp_path: Path) -> None:
    timeline = _write_timeline(
        tmp_path / "timeline.jsonl",
        [
            _timeline_row(1, captured_at=10.0, ids=("wrong", "b", "c")),
            _timeline_row(2, captured_at=10.3),
        ],
    )
    # 只提供后续正确帧，同时把人工 span 起点移到 seq=2，不能形成较小的通过子集。
    truth_file = _write_json(tmp_path / "truth.json", _truth(tmp_path, [("capture-2", 2, 10.3, "two.png")]))

    report = _report(timeline, truth_file)

    assert report["qualified"] is False
    assert "truth_frame_coverage_incomplete" in report["qualification_errors"]
    assert report["excluded_frames_by_reason"]["truth_frame_coverage_incomplete"] == 1
    assert report["qualified_frame_count"] == 1
    assert report["passed"] is False


@pytest.mark.parametrize(
    "timing_override",
    [
        {"capture_started_at": 0.0},
        {"capture_started_at": float("nan")},
        {"captured_at": float("inf")},
        {"recognition_completed_at": 9.0},
    ],
)
def test_non_finite_zero_or_backward_timing_cannot_pass(tmp_path: Path, timing_override: dict) -> None:
    row = _timeline_row(1, captured_at=10.0)
    row.update(timing_override)
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [row])
    truth_file = _write_json(tmp_path / "truth.json", _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")]))

    report = _report(timeline, truth_file)

    assert report["qualified_frame_count"] == 0
    assert report["excluded_frames_by_reason"]["timeline_timing_invalid"] == 1
    assert report["all_three_correct"]["passed"] is False
    assert report["passed"] is False
    json.dumps(report, allow_nan=False)


def test_non_finite_truth_capture_time_is_unqualified(tmp_path: Path) -> None:
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [_timeline_row(1, captured_at=10.0)])
    payload = _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")])
    payload["frames"][0]["captured_at"] = float("nan")

    report = _report(timeline, _write_json(tmp_path / "truth.json", payload))

    assert report["qualified"] is False
    assert "capture_provenance_missing" in report["qualification_errors"]
    assert report["passed"] is False
    json.dumps(report, allow_nan=False)


def test_truth_and_timeline_require_cross_bound_positive_frame_id(tmp_path: Path) -> None:
    timeline_row = _timeline_row(1, captured_at=10.0)
    timeline_row.pop("captured_frame_id")
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [timeline_row])
    payload = _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")])
    truth_file = _write_json(tmp_path / "truth.json", payload)

    report = _report(timeline, truth_file)

    assert report["qualified"] is False
    assert "timeline_capture_identity_missing_or_ambiguous" in report["qualification_errors"]
    assert report["qualified_frame_count"] == 0

    mismatch = _timeline_row(1, captured_at=10.0)
    mismatch["captured_frame_id"] = 99
    mismatch_timeline = _write_timeline(tmp_path / "mismatch.jsonl", [mismatch])
    report = _report(mismatch_timeline, truth_file)
    assert report["qualified"] is False
    assert "captured_frame_id_mismatch" in report["qualification_errors"]

    payload["frames"][0].pop("captured_frame_id")
    report = _report(timeline, _write_json(tmp_path / "truth-missing-id.json", payload))
    assert report["qualified"] is False
    assert "capture_provenance_missing" in report["qualification_errors"]


def test_duplicate_positive_captured_frame_id_is_not_independent(tmp_path: Path) -> None:
    timeline = _write_timeline(
        tmp_path / "timeline.jsonl",
        [_timeline_row(1, captured_at=10.0), _timeline_row(2, captured_at=10.3)],
    )
    payload = _truth(
        tmp_path,
        [("capture-1", 1, 10.0, "one.png"), ("capture-2", 2, 10.3, "two.png")],
    )
    payload["frames"][1]["captured_frame_id"] = 1

    report = _report(timeline, _write_json(tmp_path / "truth.json", payload))

    assert report["duplicate_capture_count"] == 1
    assert "capture_identity_duplicate" in report["qualification_errors"]
    assert report["qualified_frame_count"] == 1
    assert report["passed"] is False


def test_expected_runtime_identity_must_be_explicit_and_pid_must_match(tmp_path: Path) -> None:
    timeline = _write_timeline(tmp_path / "timeline.jsonl", [_timeline_row(1, captured_at=10.0)])
    truth_file = _write_json(tmp_path / "truth.json", _truth(tmp_path, [("capture-1", 1, 10.0, "one.png")]))

    exploratory = build_overlay_truth_report(timeline, truth_file)

    assert exploratory["qualified"] is False
    assert {
        "expected_build_id_required",
        "expected_session_id_required",
        "expected_sidecar_instance_id_required",
        "expected_sidecar_pid_required",
    }.issubset(exploratory["qualification_errors"])
    assert exploratory["passed"] is False

    wrong_pid = _report(timeline, truth_file, expected_sidecar_pid=9999)
    assert wrong_pid["qualified"] is False
    assert "expected_sidecar_pid_truth_mismatch" in wrong_pid["qualification_errors"]
    assert wrong_pid["passed"] is False

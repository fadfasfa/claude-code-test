from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from hextech.infrastructure.vision.diagnostic_capture_session import (
    DIAGNOSTIC_CAPTURE_ROOT,
    DiagnosticCaptureSession,
)
from hextech.infrastructure.vision.sidecar_diagnostics import _selection_timeline_entry


def _frame(*, mode: str = "client_full_recovery", noisy: bool = False) -> Image.Image:
    size = (1920, 1080)
    if noisy:
        sample = bytes((index * 73 + index // 11) % 256 for index in range(256 * 256 * 3))
        image = Image.frombytes("RGB", (256, 256), sample).resize(size)
    else:
        image = Image.new("RGB", size, (12, 23, 34))
    image.info.update(
        {
            "hextech_capture_mode": mode,
            "hextech_roi_origin": (0, 0),
            "hextech_roi_size": size,
            "hextech_client_size": size,
            "hextech_client_rect": (-1920, 0, 0, 1080),
            "hextech_capture_screen_rect": (-1920, 0, 0, 1080),
        }
    )
    return image


def _event(*, reason: str = "slots_detecting", state: str = "candidate") -> dict:
    return {
        "active": False,
        "selection_type": "hextech",
        "slots": [
            {"slot": 0, "state": "detecting", "rejection_reason": "margin_below_threshold"},
            {"slot": 1, "state": "detecting", "rejection_reason": "ocr_exact_gate_rejected"},
            {"slot": 2, "state": "ready", "augment_id": "1088", "name": "终极刷新"},
        ],
        "_raw_slots": [
            {"slot": 0, "diagnostic": "margin_below_threshold"},
            {
                "slot": 1,
                "diagnostic": "ocr_exact_gate_rejected",
                "ocr_shadow": {
                    "state": "ready",
                    "input_sha256": "a" * 64,
                    "normalized_text": "技能急速碎片",
                },
            },
            {"slot": 2},
        ],
        "source": {
            "build_id": "build-r11",
            "sidecar_instance_id": "sidecar-r11",
            "sidecar_pid": 321,
            "session_id": "session-r11",
            "game_instance_id": "game-r11",
            "window_hwnd": 456,
            "client_rect": [-1920, 0, 0, 1080],
            "capture_size": [1920, 1080],
            "capture_mode": "client_full_recovery",
            "capture_roi_origin": [0, 0],
            "capture_roi_size": [1920, 1080],
            "dpi_scale": 1.5,
            "monitor_id": r"\\.\DISPLAY2",
            "frame_id": 44,
            "selection_epoch": 8,
            "selection_revision": 3,
            "reason": reason,
            "scene_state": state,
            "scene_kind": "hextech",
            "scene_score": 1.0,
            "scene_recovery_state": "full_capture_pending",
            "scene_recovery_edge_id": "edge-7-8",
            "scene_recovery_full_capture": True,
            "preset": "1920x1080",
            "layout_id": "1920x1080",
            "layout_transform": {"dx_ratio": 0.001, "dy_ratio": -0.002, "scale": 1.0},
            "panel_scores": [0.91, 0.92, 0.93],
            "body_shard_scores": [0.0, 0.850847, 0.0],
        },
        "timing": {
            "capture_started_at": 100.0,
            "captured_at": 100.02,
            "recognition_completed_at": 100.13,
            "event_written_at": 100.14,
        },
    }


def _manifest(session: DiagnosticCaptureSession) -> dict:
    assert session.session_path is not None
    return json.loads((session.session_path / "manifest.json").read_text(encoding="utf-8"))


def test_disabled_session_has_zero_filesystem_effect(tmp_path: Path) -> None:
    session = DiagnosticCaptureSession(tmp_path)
    assert session.plan(_frame(), _event(), include_full_client=True) is None
    assert session.finalize() is None
    assert not (tmp_path / DIAGNOSTIC_CAPTURE_ROOT).exists()


def test_written_observation_binds_frame_geometry_identity_and_hashes(tmp_path: Path) -> None:
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    frame = _frame()
    draft = session.plan(frame, _event(), include_full_client=True)
    assert draft is not None
    target = session.write(draft)
    assert target is not None
    report = json.loads((target / "metadata.json").read_text(encoding="utf-8"))

    assert report["binding"] == {
        "build_id": "build-r11",
        "sidecar_instance_id": "sidecar-r11",
        "sidecar_pid": 321,
        "session_id": "session-r11",
        "game_instance_id": "game-r11",
        "window_hwnd": 456,
        "client_rect": [-1920, 0, 0, 1080],
        "dpi_scale": 1.5,
        "monitor": r"\\.\DISPLAY2",
        "selection_epoch": 8,
        "selection_revision": 3,
        "captured_frame_id": 44,
        "frame_rgb_sha256": report["binding"]["frame_rgb_sha256"],
    }
    assert len(report["binding"]["frame_rgb_sha256"]) == 64
    assert report["capture"]["valid_rect"] == [0, 0, 1920, 1080]
    assert report["recognition"]["scene_recovery_edge_id"] == "edge-7-8"
    assert report["recognition"]["body_shard_scores"] == [0.0, 0.850847, 0.0]
    assert report["recognition"]["slots"][1]["ocr_text"] == "技能急速碎片"
    assert len(report["assets"]) == 7
    for asset in report["assets"]:
        payload = (target / asset["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == asset["png_sha256"]
        assert len(payload) == asset["bytes"]


def test_plan_reserves_caps_before_background_writer_runs(tmp_path: Path) -> None:
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    drafts = [
        session.plan(_frame(), _event(), include_full_client=True, include_roi_set=True)
        for _ in range(14)
    ]
    accepted = [draft for draft in drafts if draft is not None]
    assert len(accepted) == 12
    assert sum(draft.include_full_client for draft in accepted) == 3
    assert sum(draft.include_roi_set for draft in accepted) == 12
    status = session.status()
    assert status["planned_full_client_frames"] == 3
    assert status["planned_roi_sets"] == 12
    assert session.session_path is None


def test_invalid_full_client_metadata_fails_closed_without_committed_observation(tmp_path: Path) -> None:
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    draft = session.plan(
        _frame(mode="roi_union"),
        _event(),
        include_full_client=True,
        include_roi_set=False,
    )
    assert draft is not None
    assert session.write(draft) is None
    manifest = _manifest(session)
    assert manifest["status"] == "incomplete"
    assert manifest["terminal_reason"] == "writer_failed"
    assert manifest["last_error"] == "ValueError"
    assert manifest["counts"]["written_observations"] == 0


def test_byte_budget_is_hard_and_reports_incomplete(tmp_path: Path) -> None:
    session = DiagnosticCaptureSession(tmp_path, enabled=True, max_bytes=4096)
    draft = session.plan(
        _frame(noisy=True),
        _event(),
        include_full_client=True,
        include_roi_set=False,
    )
    assert draft is not None
    assert session.write(draft) is None
    manifest = _manifest(session)
    assert manifest["terminal_reason"] == "budget_exhausted"
    assert manifest["last_error"] == "session_byte_budget_exhausted"
    assert manifest["bytes_committed_excluding_manifest"] == 0
    total = sum(path.stat().st_size for path in session.session_path.rglob("*") if path.is_file())
    assert total <= 4096


def test_terminal_metadata_only_observation_can_close_complete_session(tmp_path: Path) -> None:
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    first = session.plan(_frame(), _event(), include_roi_set=True)
    assert first is not None and session.write(first) is not None
    terminal = session.plan(
        None,
        _event(reason="gameflow_ended", state="absent"),
        include_roi_set=False,
        terminal_reason="gameflow_ended",
    )
    assert terminal is not None and session.write(terminal) is not None
    manifest = _manifest(session)
    assert manifest["status"] == "complete"
    assert manifest["terminal_reason"] == "gameflow_ended"
    assert manifest["counts"]["written_observations"] == 2


def test_timeline_includes_recovery_and_capture_calibration_fields() -> None:
    entry = _selection_timeline_entry(_event(), 9)
    assert entry["capture_mode"] == "client_full_recovery"
    assert entry["capture_valid_rect"] == [0, 0, 1920, 1080]
    assert entry["layout_transform"] == {"dx_ratio": 0.001, "dy_ratio": -0.002, "scale": 1.0}
    assert entry["panel_scores"] == [0.91, 0.92, 0.93]
    assert entry["body_shard_scores"] == [0.0, 0.850847, 0.0]
    assert entry["scene_recovery_state"] == "full_capture_pending"
    assert entry["scene_recovery_edge_id"] == "edge-7-8"
    assert entry["scene_recovery_full_capture"] is True


def test_dropped_queue_is_memory_only_until_worker_finalizes(tmp_path):
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    draft = session.plan(_frame(), _event())
    session.record_drop(draft)
    assert session.status()["dropped_count"] == 1
    assert session.session_path is None and not list(tmp_path.iterdir())
    session.finalize()
    assert _manifest(session)["status"] == "incomplete"


def test_request_plan_does_not_wait_for_png_writer_lock(tmp_path, monkeypatch):
    import threading
    import time
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    draft = session.plan(_frame(), _event())
    entered, release = threading.Event(), threading.Event()
    original = session._prepare_observation_locked
    def slow(item):
        entered.set()
        assert release.wait(2)
        return original(item)
    monkeypatch.setattr(session, "_prepare_observation_locked", slow)
    thread = threading.Thread(target=session.write, args=(draft,))
    thread.start()
    try:
        assert entered.wait(1)
        started = time.perf_counter()
        assert session.plan(_frame(), _event()) is not None
        session.status()
        assert time.perf_counter()-started < .2
    finally:
        release.set()
        thread.join(2)


def test_frame_without_monitor_proof_is_not_qualified(tmp_path):
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    event = _event()
    for key in ("monitor", "monitor_id", "monitor_device", "target_monitor"):
        event["source"].pop(key, None)
    assert session.write(session.plan(_frame(), event)) is None
    assert _manifest(session)["status"] == "incomplete"


def test_reparse_observations_directory_is_rejected_before_writes(tmp_path, monkeypatch):
    session = DiagnosticCaptureSession(tmp_path, enabled=True)
    assert session.write(session.plan(_frame(), _event())) is not None
    observations = session.session_path / "observations"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == observations or original(path))
    assert session.write(session.plan(_frame(), _event())) is None
    assert session.status()["failed_count"] == 1
    assert not (observations / "0002").exists()

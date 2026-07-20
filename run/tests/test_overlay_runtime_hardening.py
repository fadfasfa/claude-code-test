"""Overlay 窗口观察、三态 gameflow 与功能健康回归测试。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch


def test_window_target_poller_uses_keyword_call_and_recovers_after_error() -> None:
    from hextech.interfaces.overlay.host_common import WindowTargetPoller
    from hextech.modules.vision.window import WindowProbeResult

    target = (101, (0, 0, 1920, 1080))
    calls = 0
    error_seen = threading.Event()

    def finder(*, window_titles: list[str]):
        nonlocal calls
        assert window_titles == ["League of Legends (TM) Client"]
        calls += 1
        if calls == 1:
            return WindowProbeResult(status="found", hwnd=target[0], client_rect=target[1], observed_at=time.time())
        if calls == 2:
            error_seen.set()
            raise TypeError("probe failed")
        return WindowProbeResult(status="found", hwnd=target[0], client_rect=target[1], observed_at=time.time())

    poller = WindowTargetPoller(
        ["League of Legends (TM) Client"],
        finder=finder,
        interval_seconds=0.1,
    )
    poller.start()
    try:
        assert error_seen.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while poller.status()["probe_status"] != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert poller.current() == target
        assert poller.status()["last_error_type"] == "TypeError"
        deadline = time.monotonic() + 1.0
        while poller.status()["probe_status"] != "found" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert poller.status()["consecutive_failures"] == 0
    finally:
        poller.stop()


def test_sidecar_window_observation_wins_and_records_desync() -> None:
    from hextech.interfaces.overlay.host_common import WindowTargetPoller
    from hextech.interfaces.overlay import host_sync

    visibility = {
        "window_visible": False,
        "window_target_poller": WindowTargetPoller([], initial_target=(100, (0, 0, 1920, 1080))),
    }
    snapshot = {
        "ok": True,
        "source": {"window_hwnd": 200, "client_rect": [10, 20, 2570, 1620]},
    }
    with patch.object(host_sync, "is_window_renderable", return_value=True):
        host_sync._refresh_target_window(object(), {}, visibility, snapshot)

    assert visibility["target_hwnd"] == 200
    assert visibility["window_source"] == "sidecar"
    assert visibility["window_target_desync"] is True
    assert visibility["host_scan_hwnd"] == 100
    assert visibility["sidecar_hwnd"] == 200


def test_window_poller_identity_reaches_host_sidecar_desync_gate() -> None:
    from hextech.interfaces.overlay.host_common import WindowTargetPoller
    from hextech.interfaces.overlay import host_sync
    from hextech.modules.vision.window import WindowProbeResult

    observed = threading.Event()

    def finder(*, window_titles: list[str]):
        observed.set()
        return WindowProbeResult(
            status="found",
            hwnd=100,
            client_rect=(0, 0, 1920, 1080),
            observed_at=time.time(),
            process_id=10,
            process_started_at=100.0,
            game_instance_id="host-game",
            identity_quality="process",
        )

    poller = WindowTargetPoller([], finder=finder, interval_seconds=0.1)
    poller.start()
    try:
        assert observed.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while poller.status()["game_instance_id"] != "host-game" and time.monotonic() < deadline:
            time.sleep(0.01)
        status = poller.status()
        assert status["process_id"] == 10
        assert status["process_started_at"] == 100.0
        assert status["identity_quality"] == "process"

        visibility = {"window_visible": False, "window_target_poller": poller}
        snapshot = {
            "ok": True,
            "source": {
                "window_hwnd": 100,
                "client_rect": [0, 0, 1920, 1080],
                "game_instance_id": "sidecar-game",
            },
        }
        with patch.object(host_sync, "is_window_renderable", return_value=True):
            host_sync._refresh_target_window(object(), {}, visibility, snapshot)
        assert visibility["host_game_instance_id"] == "host-game"
        assert visibility["sidecar_game_instance_id"] == "sidecar-game"
        assert visibility["game_identity_desync"] is True
    finally:
        poller.stop()


def test_gameflow_unknown_remains_typed_and_uses_waiting_state() -> None:
    from hextech.interfaces.overlay import gameflow
    from hextech.interfaces.overlay.host_visibility import decide_visibility

    with (
        patch.object(gameflow, "probe_live_client_in_progress", return_value=None),
        patch.object(gameflow, "probe_lcu_gameflow_in_progress", return_value=None),
    ):
        assert gameflow.probe_gameflow_state() is gameflow.GameflowState.UNKNOWN

    assert decide_visibility(
        user_enabled=True,
        event_visible=True,
        game_foreground=True,
        content_ready=True,
        selection_window_active=True,
        gameflow_in_progress=gameflow.GameflowState.UNKNOWN,
        game_hwnd=101,
        game_rect=(0, 0, 1920, 1080),
        game_renderable=True,
        ready_slots=3,
    ) == (True, "waiting_gameflow")


def test_shared_overlay_data_source_delegates_to_canonical_context_reader() -> None:
    from hextech.modules.data import overlay_source

    expected = {"ok": True, "champion_id": "103", "error": ""}
    with patch.object(overlay_source, "read_overlay_context", return_value=expected) as reader:
        actual = overlay_source.SharedOverlayDataSource(snapshot_client=object()).read_context()
    assert actual == expected
    reader.assert_called_once_with()


def test_desktop_visibility_reader_accepts_v1_and_v2(tmp_path: Path) -> None:
    from hextech.interfaces.desktop import service_manager

    status_file = tmp_path / "visibility.json"
    base = {
        "updated_at": time.time(),
        "host": {},
        "scene": {},
        "context": {},
        "decision": {"window_visible": False, "reason": "game_window_missing"},
    }
    with patch.object(service_manager, "OVERLAY_HOST_VISIBILITY_FILE", status_file):
        status_file.write_text(json.dumps({**base, "schema_version": 1}), encoding="utf-8")
        old = service_manager.ServiceManager._overlay_host_visibility_status()
        status_file.write_text(
            json.dumps(
                {
                    **base,
                    "schema_version": 2,
                    "functional_status": "degraded",
                    "functional_reason": "window_probe_error",
                    "window": {"probe_status": "error"},
                    "render": {"last_tick_at": 1.0},
                }
            ),
            encoding="utf-8",
        )
        new = service_manager.ServiceManager._overlay_host_visibility_status()

    assert old["ok"] is True and old["functional_status"] == "ready"
    assert new["ok"] is True and new["functional_status"] == "degraded"
    assert new["window"]["probe_status"] == "error"

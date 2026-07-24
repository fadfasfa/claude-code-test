from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import requests

from hextech.bootstrap.data_service_runtime import (
    DataServiceApplication,
    DataServiceCore,
)
from hextech.infrastructure.persistence.file_lock import InterProcessFileLock
from hextech.modules.data.generation import DataSnapshotPublisher


def test_private_stats_handle_tracks_the_same_action_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.interfaces.desktop import runtime

    class Process:
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"accepted": True, "action_id": "policy-1"}

    handle = runtime.DataServiceHandle(Process(), 52001, "nonce", 4321)
    statuses = iter(
        [
            {"actions": {}, "last_action": None},
            {
                "actions": {
                    "policy-1": {
                        "action_id": "policy-1",
                        "status": "completed",
                        "result": {"state": "ready", "private_stats_enabled": True},
                    }
                }
            },
        ]
    )
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(handle, "get_status", lambda: next(statuses))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    result = handle.set_private_stats(True)

    assert result == {"state": "ready", "private_stats_enabled": True}


def test_refresh_action_coalesces_running_triggers_into_one_recheck(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    rechecked = threading.Event()
    calls: list[bool] = []

    def refresh_action(force: bool) -> dict[str, object]:
        calls.append(force)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=2)
        else:
            rechecked.set()
        return {"state": "ready", "generation_id": "test-generation"}

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            private_stats_enabled=False,
            refresh_action=refresh_action,
        ),
        parent_pid=1,
    )
    first = application.submit_action("refresh")
    assert entered.wait(timeout=1)
    second = application.submit_action("refresh")
    third = application.submit_action("refresh")
    release.set()
    assert rechecked.wait(timeout=1)
    application.request_shutdown()

    assert first["accepted"] is True
    assert second["status"] == "coalesced"
    assert third["status"] == "coalesced"
    assert calls == [False, False]


def test_force_refresh_upgrades_pending_recheck(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    followed_up = threading.Event()
    calls: list[bool] = []

    def refresh_action(force: bool) -> dict[str, object]:
        calls.append(force)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=2)
        else:
            followed_up.set()
        return {"state": "ready", "generation_id": "test-generation"}

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            private_stats_enabled=False,
            refresh_action=refresh_action,
        ),
        parent_pid=1,
    )
    application.submit_action("refresh")
    assert entered.wait(timeout=1)

    normal = application.submit_action("refresh")
    forced = application.submit_action("refresh", {"force": True})
    release.set()
    assert followed_up.wait(timeout=1)
    application.request_shutdown()

    assert normal["force"] is False
    assert forced["force"] is True
    assert calls == [False, True]


def test_shutdown_clears_pending_refresh_without_starting_followup(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def refresh_action(force: bool) -> dict[str, object]:
        calls.append(force)
        entered.set()
        release.wait(timeout=2)
        return {"state": "ready", "generation_id": "test-generation"}

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            private_stats_enabled=False,
            refresh_action=refresh_action,
        ),
        parent_pid=1,
    )
    application.submit_action("refresh")
    assert entered.wait(timeout=1)
    application.submit_action("refresh", {"force": True})

    application.request_shutdown()
    release.set()
    time.sleep(0.05)

    assert calls == [False]
    assert application.submit_action("refresh") == {"accepted": False, "reason_code": "shutdown_requested"}


def test_data_service_instance_lock_is_exclusive(tmp_path: Path) -> None:
    first = InterProcessFileLock(tmp_path / "data-service.lock")
    second = InterProcessFileLock(tmp_path / "data-service.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_data_service_refresh_delegates_force_to_coordinator(tmp_path: Path) -> None:
    calls: list[bool] = []
    service = DataServiceCore(
        publisher=DataSnapshotPublisher(tmp_path),
        private_stats_enabled=False,
        refresh_action=lambda force: calls.append(force) or {"state": "ready", "generation_id": "g"},
    )

    assert service.refresh(force=True)["state"] == "ready"
    assert calls == [True]

"""验证 Sidecar PID、创建时间和 heartbeat 存活判定。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager


class _Process:
    def __init__(self, pid: int = 4321, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def _manager(
    status_file: Path,
    *,
    process: _Process | None = None,
    pid_exists=lambda _pid: True,
    process_create_time=lambda _pid: 90.0,
) -> OverlayRuntimeManager:
    manager = OverlayRuntimeManager(
        start_context_poller_func=None,
        sidecar_status_file=status_file,
        now_func=lambda: 100.0,
        pid_exists=pid_exists,
        process_create_time=process_create_time,
    )
    manager.sidecar_process = process or _Process()
    manager._sidecar_started_at = 0.0
    return manager


def _write_status(path: Path, **overrides: object) -> None:
    payload = {
        "schema_version": 2,
        "pid": 4321,
        "pid_started_at": 90.0,
        "heartbeat_at": 99.0,
        "generation": "generation-a",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_sidecar_status_writer_records_liveness_contract(monkeypatch, tmp_path: Path) -> None:
    from hextech.infrastructure.vision import runner

    target = tmp_path / "sidecar-status.json"
    monkeypatch.setattr(runner, "SIDECAR_STATUS_FILE", target)
    runner._write_sidecar_status("running", generation="generation-a")
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert payload["pid_started_at"] > 0
    assert payload["heartbeat_at"] > 0
    assert payload["generation"] == "generation-a"


def test_sidecar_status_diagnostics_cannot_override_contract_fields(monkeypatch, tmp_path: Path) -> None:
    from hextech.infrastructure.vision import runner

    target = tmp_path / "sidecar-status.json"
    monkeypatch.setattr(runner, "SIDECAR_STATUS_FILE", target)
    runner._write_sidecar_status(
        "starting",
        schema_version=5,
        build_id="cache-build",
        pid=-1,
        heartbeat_at=1.0,
        phase="template_runtime_cache_ready",
    )
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert payload["build_id"] != "cache-build"
    assert payload["pid"] > 0
    assert payload["heartbeat_at"] > 1.0
    assert payload["phase"] == "template_runtime_cache_ready"


@pytest.mark.parametrize(
    ("overrides", "pid_exists", "create_time", "expected"),
    [
        ({}, lambda _pid: True, lambda _pid: 90.0, ("running", "")),
        ({"heartbeat_at": 89.0}, lambda _pid: True, lambda _pid: 90.0, ("stale", "heartbeat_stale")),
        ({"pid_started_at": 10.0}, lambda _pid: True, lambda _pid: 90.0, ("stale", "pid_reused")),
        ({}, lambda _pid: False, lambda _pid: 90.0, ("stale", "pid_missing")),
        ({"pid": 9999}, lambda _pid: True, lambda _pid: 90.0, ("stale", "pid_mismatch")),
    ],
)
def test_sidecar_liveness_rejects_missing_reused_or_stale_status(
    tmp_path: Path,
    overrides: dict[str, object],
    pid_exists,
    create_time,
    expected: tuple[str, str],
) -> None:
    status_file = tmp_path / "status.json"
    _write_status(status_file, **overrides)

    liveness = _manager(
        status_file,
        pid_exists=pid_exists,
        process_create_time=create_time,
    )._read_sidecar_liveness()

    assert (liveness["status"], liveness["reason"]) == expected


def test_exited_sidecar_marks_runtime_stale_before_restart_policy(tmp_path: Path) -> None:
    status_file = tmp_path / "status.json"
    manager = _manager(status_file, process=_Process(returncode=9))
    manager.status = "running"
    manager.host_process = _Process(pid=9001)

    snapshot = manager.snapshot()

    assert snapshot["sidecar_liveness"]["reason"] == "process_exited"
    assert snapshot["status"] == "stale"

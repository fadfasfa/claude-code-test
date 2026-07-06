"""测试 RuntimeSupervisor 编排。

调用方: pytest; 关键依赖: hextech.runtime_supervisor。
"""
from __future__ import annotations

import json
import inspect
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import psutil


def _request(base_url: str, method: str, path: str, *, nonce: str = "test-nonce", host: str = "127.0.0.1", body: dict | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "Host": host,
            "X-Hextech-Supervisor-Nonce": nonce,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8") or "{}")


class RuntimeSupervisorTests(unittest.TestCase):
    def test_control_api_requires_safe_host_and_nonce(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        supervisor = RuntimeSupervisor(parent_pid=0, session_nonce="test-nonce")
        server = supervisor.serve_in_thread(port=0)
        try:
            base = f"http://127.0.0.1:{server.port}"

            status, payload = _request(base, "GET", "/v1/status")
            self.assertEqual(status, 200)
            self.assertEqual(payload["supervisor_instance_id"], supervisor.supervisor_instance_id)

            with self.assertRaises(urllib.error.HTTPError) as bad_nonce:
                _request(base, "GET", "/v1/status", nonce="wrong")
            self.assertEqual(bad_nonce.exception.code, 401)

            with self.assertRaises(urllib.error.HTTPError) as bad_host:
                _request(base, "GET", "/v1/status", host="evil.example")
            self.assertEqual(bad_host.exception.code, 403)
        finally:
            server.shutdown()

    def test_refresh_action_returns_running_and_completes_async(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        calls: list[bool] = []
        release_refresh = threading.Event()

        def refresh_func(force: bool = False):
            calls.append(force)
            release_refresh.wait(timeout=5)
            return {"state": "ready"}

        with tempfile.TemporaryDirectory() as tmp:
            events_file = Path(tmp) / "events.jsonl"
            supervisor = RuntimeSupervisor(
                parent_pid=0,
                session_nonce="test-nonce",
                refresh_func=refresh_func,
                event_log_path=events_file,
            )
            server = supervisor.serve_in_thread(port=0)
            try:
                base = f"http://127.0.0.1:{server.port}"

                status, lease = _request(base, "POST", "/v1/lease/renew", body={"control_instance_id": "ui-1"})
                self.assertEqual(status, 200)
                self.assertEqual(lease["lease"]["control_instance_id"], "ui-1")

                started_at = time.perf_counter()
                status, action = _request(base, "POST", "/v1/actions/refresh", body={"force": True})
                elapsed = time.perf_counter() - started_at
                self.assertEqual(status, 202)
                self.assertLess(elapsed, 0.5)
                self.assertEqual(action["status"], "running")
                action_id = action["action_id"]

                status, duplicate = _request(base, "POST", "/v1/actions/refresh", body={"force": True})
                self.assertEqual(status, 202)
                self.assertEqual(duplicate["action_id"], action_id)
                self.assertEqual(duplicate["status"], "running")
                self.assertEqual(calls, [True])

                release_refresh.set()
                deadline = time.time() + 3
                completed = {}
                while time.time() < deadline:
                    _, completed = _request(base, "GET", f"/v1/actions/{action_id}")
                    if completed["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual(completed["status"], "completed")
                self.assertEqual(completed["result"]["state"], "ready")

                events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(events[-1]["event"], "refresh.completed")
            finally:
                release_refresh.set()
                server.shutdown()

    def test_supervisor_tick_delays_startup_refresh_and_handles_stale_lease(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        calls: list[bool] = []

        def refresh_func(force: bool = False):
            calls.append(force)
            return {"state": "ready"}

        supervisor = RuntimeSupervisor(
            parent_pid=os.getpid(),
            session_nonce="test-nonce",
            refresh_func=refresh_func,
            refresh_interval_seconds=60.0,
            lease_timeout_seconds=1.0,
            orphan_grace_seconds=1.0,
        )

        supervisor.tick()
        self.assertEqual(calls, [])
        self.assertEqual(supervisor.snapshot()["actions"], {})
        self.assertIsNotNone(supervisor.snapshot()["next_refresh_at"])
        self.assertGreater(supervisor.snapshot()["next_refresh_at"], time.time())

        supervisor.renew_lease({"control_instance_id": "ui-1"})
        with supervisor._lock:
            supervisor._lease["last_renewed_at"] = time.time() - 5
        supervisor.tick()
        self.assertTrue(supervisor.wait_for_shutdown(0))
        self.assertEqual(supervisor.snapshot()["shutdown_reason"], "lease_expired")

    def test_supervisor_tick_schedules_refresh_when_next_refresh_is_due(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        calls: list[bool] = []

        def refresh_func(force: bool = False):
            calls.append(force)
            return {"state": "ready"}

        supervisor = RuntimeSupervisor(
            parent_pid=os.getpid(),
            session_nonce="test-nonce",
            refresh_func=refresh_func,
            refresh_interval_seconds=60.0,
        )
        with supervisor._lock:
            supervisor._last_refresh_at = time.time() - 120.0
            supervisor._next_refresh_at = time.time() - 1.0

        supervisor.tick()

        self.assertEqual(calls, [False])
        action = next(iter(supervisor.snapshot()["actions"].values()))
        self.assertIn(action["status"], {"running", "completed"})

    def test_result_payload_supports_slots_dataclasses(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        @dataclass(slots=True)
        class SlotsResult:
            state: str
            published: bool

        supervisor = RuntimeSupervisor(parent_pid=0, session_nonce="test-nonce")

        self.assertEqual(
            supervisor._result_payload(SlotsResult(state="ready", published=True)),
            {"state": "ready", "published": True},
        )

    def test_root_launcher_exposes_runtime_supervisor_mode(self):
        import hextech_ui

        source = inspect.getsource(hextech_ui.main)

        self.assertIn("--runtime-supervisor", source)
        self.assertIn("hextech.runtime_supervisor", source)

    def test_desktop_runtime_bootstraps_supervisor_process(self):
        from hextech.display.desktop import runtime as desktop_runtime

        handle = desktop_runtime.start_runtime_supervisor_process(parent_pid=os.getpid())
        wrapper_pid = handle.process.pid
        supervisor_pid = handle.pid
        try:
            self.assertTrue(handle.port > 0)
            self.assertTrue(handle.session_nonce)
            self.assertTrue(handle.supervisor_instance_id.startswith("sup-"))
            self.assertIsNone(handle.process.poll())
            self.assertTrue(hasattr(handle, "job_object_attached"))
        finally:
            handle.stop()
        self.assertFalse(psutil.pid_exists(wrapper_pid))
        self.assertFalse(psutil.pid_exists(supervisor_pid))

    def test_desktop_runtime_bootstrap_timeout_is_not_blocked_by_readline(self):
        from hextech.display.desktop import runtime as desktop_runtime

        class SlowStdout:
            def readline(self):
                time.sleep(0.25)
                return ""

        class FakeProcess:
            pid = 123456
            stdout = SlowStdout()
            stderr = None
            returncode = None
            terminated = False
            killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.killed = True

        fake_process = FakeProcess()
        with mock.patch.object(desktop_runtime.subprocess, "Popen", return_value=fake_process):
            started_at = time.perf_counter()
            with self.assertRaises(TimeoutError):
                desktop_runtime.start_runtime_supervisor_process(parent_pid=os.getpid(), timeout=0.05)
            elapsed = time.perf_counter() - started_at

        self.assertLess(elapsed, 0.2)
        self.assertTrue(fake_process.terminated)

    def test_desktop_ui_starts_supervisor_and_lease_thread(self):
        from hextech.display.desktop.app import HextechUI

        source = inspect.getsource(HextechUI)

        self.assertIn("start_runtime_supervisor_process", source)
        self.assertIn("_start_supervisor_lease_thread", source)
        self.assertIn("stop_runtime_supervisor_process", source)


if __name__ == "__main__":
    unittest.main()

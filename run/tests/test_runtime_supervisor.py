"""测试 RuntimeSupervisor 编排。

调用方: pytest; 关键依赖: hextech.runtime_supervisor。
"""
from __future__ import annotations

import json
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

    def test_game_overlay_action_is_async_and_updates_component_status(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.calls: list[bool] = []
                self.release = threading.Event()
                self.snapshot_payload = {
                    "desired_enabled": False,
                    "status": "stopped",
                    "phase": "idle",
                    "host_pid": None,
                    "sidecar_pid": None,
                    "context_status": "stopped",
                    "cache_status": "idle",
                    "cache_hit": None,
                    "startup_seconds": 0.0,
                    "visible_reason": "",
                    "last_error": "",
                }

            def set_enabled(self, enabled: bool) -> dict:
                self.calls.append(bool(enabled))
                self.snapshot_payload = {
                    **self.snapshot_payload,
                    "desired_enabled": bool(enabled),
                    "status": "running" if enabled else "stopped",
                    "phase": "running" if enabled else "stopped",
                    "host_pid": 101 if enabled else None,
                    "sidecar_pid": 202 if enabled else None,
                }
                self.release.wait(timeout=5)
                return self.snapshot()

            def snapshot(self) -> dict:
                return dict(self.snapshot_payload)

            def shutdown(self, reason: str = "shutdown") -> None:
                self.snapshot_payload = {**self.snapshot_payload, "status": "stopped", "phase": reason}

        overlay = FakeOverlayRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = RuntimeSupervisor(
                parent_pid=0,
                session_nonce="test-nonce",
                event_log_path=Path(tmp) / "events.jsonl",
                overlay_runtime=overlay,
            )
            server = supervisor.serve_in_thread(port=0)
            try:
                base = f"http://127.0.0.1:{server.port}"
                status, action = _request(base, "POST", "/v1/actions/game-overlay", body={"enabled": True})
                self.assertEqual(status, 202)
                self.assertEqual(action["status"], "running")
                action_id = action["action_id"]

                status, duplicate = _request(base, "POST", "/v1/actions/game-overlay", body={"enabled": True})
                self.assertEqual(status, 202)
                self.assertEqual(duplicate["action_id"], action_id)
                self.assertEqual(overlay.calls, [True])

                overlay.release.set()
                deadline = time.time() + 3
                completed = {}
                while time.time() < deadline:
                    _, completed = _request(base, "GET", f"/v1/actions/{action_id}")
                    if completed["status"] == "completed":
                        break
                    time.sleep(0.05)
                self.assertEqual(completed["status"], "completed")
                _, snapshot = _request(base, "GET", "/v1/status")
                self.assertEqual(snapshot["components"]["game_overlay"]["status"], "running")
                self.assertEqual(snapshot["components"]["game_overlay"]["host_pid"], 101)
            finally:
                overlay.release.set()
                server.shutdown()

    def test_game_overlay_opposite_action_is_not_deduplicated(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.calls: list[bool] = []
                self.release_start = threading.Event()
                self.state = {"status": "stopped", "phase": "idle"}

            def set_enabled(self, enabled: bool) -> dict:
                self.calls.append(bool(enabled))
                if enabled:
                    self.state = {"status": "starting", "phase": "sidecar_start"}
                    self.release_start.wait(timeout=5)
                    self.state = {"status": "running", "phase": "running"}
                else:
                    self.state = {"status": "stopped", "phase": "stopped"}
                return self.snapshot()

            def snapshot(self) -> dict:
                return dict(self.state)

            def shutdown(self, reason: str = "shutdown") -> None:
                self.state = {"status": "stopped", "phase": reason}

        overlay = FakeOverlayRuntime()
        supervisor = RuntimeSupervisor(parent_pid=0, session_nonce="test-nonce", overlay_runtime=overlay)
        server = supervisor.serve_in_thread(port=0)
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, start_action = _request(base, "POST", "/v1/actions/game-overlay", body={"enabled": True})
            self.assertEqual(status, 202)
            self.assertEqual(start_action["status"], "running")

            status, stop_action = _request(base, "POST", "/v1/actions/game-overlay", body={"enabled": False})
            self.assertEqual(status, 202)
            self.assertNotEqual(stop_action["action_id"], start_action["action_id"])

            overlay.release_start.set()
            deadline = time.time() + 3
            while time.time() < deadline:
                _, stopped = _request(base, "GET", f"/v1/actions/{stop_action['action_id']}")
                if stopped["status"] == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(stopped["status"], "completed")
            self.assertEqual(overlay.calls, [True, False])
        finally:
            overlay.release_start.set()
            server.shutdown()

    def test_overlay_runtime_disable_cancels_inflight_generation_without_waiting_for_start_lock(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        class FakeProcess:
            def __init__(self, pid: int):
                self.pid = pid
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        prepare_entered = threading.Event()
        release_prepare = threading.Event()
        inactive_events: list[str] = []
        start_calls: list[str] = []
        host = FakeProcess(301)
        sidecar = FakeProcess(302)

        def prepare_data():
            prepare_entered.set()
            release_prepare.wait(timeout=5)
            return {}

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: (start_calls.append("host"), host)[1],
            start_sidecar_func=lambda: (start_calls.append("sidecar"), sidecar)[1],
            start_context_poller_func=lambda: object(),
            prepare_data_func=prepare_data,
            write_inactive_func=lambda: inactive_events.append("inactive"),
        )

        start_thread = threading.Thread(target=lambda: runtime.set_enabled(True))
        start_thread.start()
        self.assertTrue(prepare_entered.wait(timeout=1))
        starting_generation = runtime.snapshot()["generation"]

        started_at = time.perf_counter()
        stopped = runtime.set_enabled(False)
        elapsed = time.perf_counter() - started_at

        release_prepare.set()
        start_thread.join(timeout=3)

        self.assertLess(elapsed, 0.5)
        self.assertFalse(start_thread.is_alive())
        self.assertGreater(stopped["generation"], starting_generation)
        self.assertFalse(stopped["desired_enabled"])
        self.assertEqual(runtime.snapshot()["status"], "stopped")
        self.assertEqual(start_calls, [])
        self.assertGreaterEqual(len(inactive_events), 1)

    def test_overlay_runtime_retries_transient_sidecar_start_once_with_fixed_backoff(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        class FakeProcess:
            pid = 302

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        attempts: list[int] = []
        delays: list[float] = []

        def start_sidecar(**_kwargs):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 2:
                raise RuntimeError("Vision sidecar 在 readiness 前退出，exit_code=1")
            return FakeProcess()

        runtime = OverlayRuntimeManager(
            start_host_func=FakeProcess,
            start_sidecar_func=start_sidecar,
            start_context_poller_func=None,
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
            retry_sleep_func=delays.append,
        )

        snapshot = runtime.set_enabled(True)

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(delays, [1.0])

    def test_overlay_runtime_warm_budget_recommends_fallback_without_stopping_start(self):
        from hextech import runtime_supervisor

        class FakeProcess:
            pid = 303

            def __init__(self):
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        loader_entered = threading.Event()
        release_loader = threading.Event()

        def load_template_runtime(**kwargs):
            kwargs["status_callback"]("template_runtime_cache_lookup", {"cache_hit": True})
            loader_entered.set()
            release_loader.wait(timeout=2)
            return object()

        with mock.patch.object(runtime_supervisor, "OVERLAY_WARM_STARTUP_BUDGET_SECONDS", 0.05):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=FakeProcess,
                start_sidecar_func=FakeProcess,
                start_context_poller_func=None,
                prepare_data_func=lambda: {},
                write_inactive_func=lambda: None,
                load_template_runtime_func=load_template_runtime,
            )
            runtime.start_template_prewarm()
            self.assertTrue(loader_entered.wait(timeout=1))
            start_thread = threading.Thread(target=lambda: runtime.set_enabled(True))
            start_thread.start()
            time.sleep(0.08)

            snapshot = runtime.snapshot()

            self.assertEqual(snapshot["startup_mode"], "warm")
            self.assertEqual(snapshot["target_budget_seconds"], 0.05)
            self.assertGreaterEqual(snapshot["startup_elapsed_seconds"], 0.05)
            self.assertTrue(snapshot["fallback_recommended"])
            self.assertFalse(snapshot["hard_timeout_reached"])
            self.assertEqual(snapshot["status"], "starting")

            release_loader.set()
            start_thread.join(timeout=2)
            self.assertFalse(start_thread.is_alive())
            recovered = runtime.snapshot()
            self.assertEqual(recovered["status"], "running")
            self.assertFalse(recovered["fallback_recommended"])
            runtime.set_enabled(False)

    def test_overlay_runtime_first_enable_keeps_elapsed_prewarm_time(self):
        from hextech import runtime_supervisor

        class FakeProcess:
            pid = 304

            def __init__(self):
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        loader_entered = threading.Event()
        release_loader = threading.Event()
        sidecar_entered = threading.Event()
        release_sidecar = threading.Event()

        def load_template_runtime(**kwargs):
            kwargs["status_callback"]("template_runtime_cache_lookup", {"cache_hit": True})
            loader_entered.set()
            release_loader.wait(timeout=2)
            return object()

        def start_sidecar(**_kwargs):
            sidecar_entered.set()
            release_sidecar.wait(timeout=2)
            return FakeProcess()

        with mock.patch.object(runtime_supervisor, "OVERLAY_WARM_STARTUP_BUDGET_SECONDS", 0.05):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=FakeProcess,
                start_sidecar_func=start_sidecar,
                start_context_poller_func=None,
                prepare_data_func=lambda: {},
                write_inactive_func=lambda: None,
                load_template_runtime_func=load_template_runtime,
            )
            runtime.start_template_prewarm()
            self.assertTrue(loader_entered.wait(timeout=1))
            time.sleep(0.08)
            release_loader.set()

            start_thread = threading.Thread(target=lambda: runtime.set_enabled(True))
            start_thread.start()
            self.assertTrue(sidecar_entered.wait(timeout=1))
            snapshot = runtime.snapshot()

            self.assertGreaterEqual(snapshot["startup_elapsed_seconds"], 0.08)
            self.assertTrue(snapshot["fallback_recommended"])
            release_sidecar.set()
            start_thread.join(timeout=2)
            runtime.set_enabled(False)

    def test_overlay_runtime_unknown_and_cold_modes_use_cold_fallback_budget(self):
        from hextech import runtime_supervisor

        class FakeProcess:
            pid = 305

            def __init__(self):
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        with mock.patch.object(runtime_supervisor, "OVERLAY_COLD_STARTUP_BUDGET_SECONDS", 0.05):
            for cache_hit, expected_mode in ((None, "unknown"), (False, "cold")):
                with self.subTest(mode=expected_mode):
                    sidecar_entered = threading.Event()
                    release_sidecar = threading.Event()

                    def start_sidecar(**_kwargs):
                        sidecar_entered.set()
                        release_sidecar.wait(timeout=2)
                        return FakeProcess()

                    runtime = runtime_supervisor.OverlayRuntimeManager(
                        start_host_func=FakeProcess,
                        start_sidecar_func=start_sidecar,
                        start_context_poller_func=None,
                        prepare_data_func=lambda: {},
                        write_inactive_func=lambda: None,
                    )
                    runtime.cache_hit = cache_hit
                    start_thread = threading.Thread(target=lambda: runtime.set_enabled(True))
                    start_thread.start()
                    self.assertTrue(sidecar_entered.wait(timeout=1))
                    time.sleep(0.08)
                    snapshot = runtime.snapshot()

                    self.assertEqual(snapshot["startup_mode"], expected_mode)
                    self.assertEqual(snapshot["target_budget_seconds"], 0.05)
                    self.assertTrue(snapshot["fallback_recommended"])
                    release_sidecar.set()
                    start_thread.join(timeout=2)
                    runtime.set_enabled(False)

    def test_overlay_runtime_prewarm_hard_timeout_rolls_back_host_and_enters_error(self):
        from hextech import runtime_supervisor

        class FakeProcess:
            pid = 305

            def __init__(self):
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        host = FakeProcess()
        release_loader = threading.Event()

        def load_template_runtime(**kwargs):
            kwargs["status_callback"]("template_runtime_cache_lookup", {"cache_hit": True})
            release_loader.wait(timeout=2)
            return object()

        with (
            mock.patch.object(runtime_supervisor, "OVERLAY_WARM_STARTUP_BUDGET_SECONDS", 0.05),
            mock.patch.object(runtime_supervisor, "OVERLAY_CONTINUATION_SECONDS", 0.05),
        ):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=lambda: host,
                start_sidecar_func=FakeProcess,
                start_context_poller_func=None,
                prepare_data_func=lambda: {},
                write_inactive_func=lambda: None,
                load_template_runtime_func=load_template_runtime,
            )
            runtime.start_template_prewarm()

            with self.assertRaisesRegex(TimeoutError, "硬截止"):
                runtime.set_enabled(True)
            snapshot = runtime.snapshot()

            self.assertEqual(snapshot["status"], "error")
            self.assertEqual(snapshot["last_start_failure_kind"], "hard_timeout")
            self.assertTrue(snapshot["hard_timeout_reached"])
            self.assertTrue(snapshot["fallback_recommended"])
            self.assertTrue(host.stopped)
            release_loader.set()

    def test_overlay_runtime_passes_deadline_and_cancel_signal_to_sidecar(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        class FakeProcess:
            pid = 304

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        captured: dict[str, object] = {}

        def start_sidecar(**kwargs):
            captured.update(kwargs)
            return FakeProcess()

        runtime = OverlayRuntimeManager(
            start_host_func=FakeProcess,
            start_sidecar_func=start_sidecar,
            start_context_poller_func=None,
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
        )

        snapshot = runtime.set_enabled(True)

        self.assertEqual(snapshot["status"], "running")
        self.assertGreater(float(captured["readiness_timeout_seconds"]), 0.0)
        self.assertIsInstance(captured["cancel_event"], threading.Event)
        self.assertEqual(len(snapshot["startup_attempts"]), 1)
        self.assertEqual(snapshot["startup_attempts"][0]["status"], "ready")

    def test_overlay_runtime_repeated_enable_keeps_completed_startup_session(self):
        from hextech import runtime_supervisor

        class FakeProcess:
            pid = 306

            def __init__(self):
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        with (
            mock.patch.object(runtime_supervisor, "OVERLAY_COLD_STARTUP_BUDGET_SECONDS", 0.01),
            mock.patch.object(runtime_supervisor, "OVERLAY_CONTINUATION_SECONDS", 0.01),
        ):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=FakeProcess,
                start_sidecar_func=FakeProcess,
                start_context_poller_func=None,
                prepare_data_func=lambda: {},
                write_inactive_func=lambda: None,
            )
            first = runtime.set_enabled(True)
            time.sleep(0.03)
            second = runtime.set_enabled(True)

            self.assertEqual(second["status"], "running")
            self.assertFalse(second["hard_timeout_reached"])
            self.assertEqual(second["startup_attempts"], first["startup_attempts"])
            self.assertAlmostEqual(second["startup_elapsed_seconds"], first["startup_elapsed_seconds"], delta=0.005)
            runtime.set_enabled(False)

    def test_overlay_runtime_rejects_sidecar_ready_after_hard_deadline(self):
        from hextech import runtime_supervisor

        class FakeProcess:
            pid = 307

            def __init__(self):
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        sidecar = FakeProcess()

        def slow_sidecar(**_kwargs):
            time.sleep(0.05)
            return sidecar

        with (
            mock.patch.object(runtime_supervisor, "OVERLAY_COLD_STARTUP_BUDGET_SECONDS", 0.01),
            mock.patch.object(runtime_supervisor, "OVERLAY_CONTINUATION_SECONDS", 0.01),
        ):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=FakeProcess,
                start_sidecar_func=slow_sidecar,
                start_context_poller_func=None,
                prepare_data_func=lambda: {},
                write_inactive_func=lambda: None,
            )

            with self.assertRaisesRegex(TimeoutError, "硬截止"):
                runtime.set_enabled(True)

            snapshot = runtime.snapshot()
            self.assertEqual(snapshot["status"], "error")
            self.assertEqual(snapshot["last_start_failure_kind"], "hard_timeout")
            self.assertTrue(sidecar.stopped)

    def test_overlay_runtime_does_not_retry_deterministic_sidecar_failure(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        class FakeProcess:
            pid = 401

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        attempts: list[int] = []
        delays: list[float] = []

        def start_sidecar():
            attempts.append(1)
            raise RuntimeError("Vision sidecar 模板缺失：template_missing")

        runtime = OverlayRuntimeManager(
            start_host_func=FakeProcess,
            start_sidecar_func=start_sidecar,
            start_context_poller_func=None,
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
            retry_sleep_func=delays.append,
        )

        with self.assertRaisesRegex(RuntimeError, "模板缺失"):
            runtime.set_enabled(True)

        self.assertEqual(attempts, [1])
        self.assertEqual(delays, [])

    def test_overlay_runtime_does_not_retry_sidecar_resource_error(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        attempts: list[int] = []

        def start_sidecar(**_kwargs):
            attempts.append(1)
            raise PermissionError("template cache access denied")

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: mock.Mock(pid=402, poll=lambda: None),
            start_sidecar_func=start_sidecar,
            start_context_poller_func=None,
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
        )

        with self.assertRaises(PermissionError):
            runtime.set_enabled(True)

        self.assertEqual(attempts, [1])

    def test_overlay_runtime_waits_for_template_prewarm_before_sidecar_start(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        class FakeProcess:
            def __init__(self, pid: int):
                self.pid = pid
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        loader_entered = threading.Event()
        release_loader = threading.Event()
        sidecar_started = threading.Event()

        def load_template_runtime(**kwargs):
            status_callback = kwargs.get("status_callback")
            if callable(status_callback):
                status_callback("template_index_build", {"cache_hit": False})
            loader_entered.set()
            release_loader.wait(timeout=5)
            return object()

        def start_sidecar():
            sidecar_started.set()
            return FakeProcess(402)

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: FakeProcess(401),
            start_sidecar_func=start_sidecar,
            start_context_poller_func=lambda: object(),
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
            load_template_runtime_func=load_template_runtime,
        )

        runtime.start_template_prewarm()
        self.assertTrue(loader_entered.wait(timeout=1))

        start_thread = threading.Thread(target=lambda: runtime.set_enabled(True))
        start_thread.start()
        time.sleep(0.1)

        self.assertFalse(sidecar_started.is_set())
        self.assertEqual(runtime.snapshot()["phase"], "cache_wait")

        release_loader.set()
        start_thread.join(timeout=3)

        self.assertFalse(start_thread.is_alive())
        self.assertTrue(sidecar_started.is_set())
        self.assertEqual(runtime.snapshot()["status"], "running")

    def test_overlay_runtime_raises_when_finished_prewarm_failed(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        sidecar_started = threading.Event()

        class FakeProcess:
            pid = 381

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        def fail_loader(**_kwargs):
            raise RuntimeError("cache build failed")

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: FakeProcess(),
            start_sidecar_func=lambda: (sidecar_started.set(), FakeProcess())[1],
            start_context_poller_func=lambda: object(),
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
            load_template_runtime_func=fail_loader,
            prewarm_wait_timeout_seconds=0.2,
        )

        runtime.start_template_prewarm()
        deadline = time.time() + 2
        while time.time() < deadline and runtime.snapshot()["cache_status"] != "error":
            time.sleep(0.02)

        with self.assertRaisesRegex(RuntimeError, "cache build failed"):
            runtime.set_enabled(True)
        self.assertFalse(sidecar_started.is_set())

    def test_overlay_runtime_prewarm_wait_uses_same_session_and_releases_stop_path(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        class FakeProcess:
            def __init__(self):
                self.pid = 501
                self.stopped = False

            def poll(self):
                return 0 if self.stopped else None

            def terminate(self):
                self.stopped = True

            def wait(self, timeout=None):
                self.stopped = True

            def kill(self):
                self.stopped = True

        loader_entered = threading.Event()
        release_loader = threading.Event()
        host_started = threading.Event()
        sidecar_started = threading.Event()

        def slow_loader(**_kwargs):
            loader_entered.set()
            release_loader.wait(timeout=5)
            return object()

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: (host_started.set(), FakeProcess())[1],
            start_sidecar_func=lambda: (sidecar_started.set(), FakeProcess())[1],
            start_context_poller_func=lambda: object(),
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: None,
            load_template_runtime_func=slow_loader,
            prewarm_wait_timeout_seconds=0.2,
        )

        runtime.start_template_prewarm()
        self.assertTrue(loader_entered.wait(timeout=1))

        start_thread = threading.Thread(target=lambda: runtime.set_enabled(True))
        start_thread.start()
        self.assertTrue(host_started.wait(timeout=1))
        time.sleep(0.1)
        snapshot = runtime.snapshot()
        self.assertTrue(host_started.is_set())
        self.assertFalse(sidecar_started.is_set())
        self.assertEqual(snapshot["status"], "starting")
        self.assertIn(snapshot["phase"], {"cache_wait", "vision_prewarming"})

        started_at = time.perf_counter()
        runtime.set_enabled(False)
        self.assertLess(time.perf_counter() - started_at, 1.0)
        release_loader.set()
        start_thread.join(timeout=1)

        self.assertFalse(start_thread.is_alive())
        self.assertEqual(runtime.snapshot()["status"], "stopped")

    def test_overlay_runtime_host_readiness_timeout_sets_failure_kind_without_sidecar_retry(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        sidecar_started = threading.Event()
        inactive_events: list[str] = []

        def start_host():
            raise TimeoutError("game_overlay host 启动超时：5.0s (ready_file=missing, pid=4321)")

        runtime = OverlayRuntimeManager(
            start_host_func=start_host,
            start_sidecar_func=lambda: (sidecar_started.set(), object())[1],
            start_context_poller_func=lambda: object(),
            prepare_data_func=lambda: {},
            write_inactive_func=lambda: inactive_events.append("inactive"),
            prewarm_wait_timeout_seconds=0.2,
        )

        with self.assertRaisesRegex(TimeoutError, "host 启动超时"):
            runtime.set_enabled(True)

        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["phase"], "failed")
        self.assertEqual(snapshot["last_start_failure_kind"], "host_readiness_timeout")
        self.assertIn("ready_file=missing", snapshot["last_error"])
        self.assertFalse(sidecar_started.is_set())
        self.assertGreaterEqual(len(inactive_events), 2)

    def test_overlay_runtime_classifies_sidecar_token_mismatch_as_sidecar_failure(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        self.assertEqual(
            OverlayRuntimeManager._classify_start_failure_kind("Vision sidecar readiness token 不匹配"),
            "sidecar_failed",
        )
        self.assertEqual(
            OverlayRuntimeManager._classify_start_failure_kind("game_overlay host readiness token 不匹配"),
            "host_readiness_token_mismatch",
        )
        self.assertEqual(
            OverlayRuntimeManager._classify_start_failure_kind("game_overlay host 启动失败：sidecar cache still warming"),
            "start_failed",
        )

    def test_overlay_runtime_status_reads_host_visible_reason(self):
        from hextech.runtime_supervisor import OverlayRuntimeManager

        with tempfile.TemporaryDirectory() as tmp:
            visibility_path = Path(tmp) / "game_overlay_visibility.v1.json"
            visibility_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": time.time(),
                        "decision": {"window_visible": False, "reason": "game_not_foreground"},
                    }
                ),
                encoding="utf-8",
            )
            runtime = OverlayRuntimeManager(
                start_context_poller_func=None,
                prepare_data_func=lambda: {},
                write_inactive_func=lambda: None,
                visibility_status_file=visibility_path,
            )

            self.assertEqual(runtime.snapshot()["visible_reason"], "game_not_foreground")

    def test_supervisor_shutdown_stops_overlay_runtime(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.shutdown_reasons: list[str] = []
                self.shutdown_entered = threading.Event()
                self.release_shutdown = threading.Event()

            def snapshot(self) -> dict:
                return {"status": "running", "phase": "running"}

            def set_enabled(self, enabled: bool) -> dict:
                return self.snapshot()

            def shutdown(self, reason: str = "shutdown") -> None:
                self.shutdown_reasons.append(reason)
                self.shutdown_entered.set()
                self.release_shutdown.wait(timeout=5)

        overlay = FakeOverlayRuntime()
        supervisor = RuntimeSupervisor(parent_pid=0, session_nonce="test-nonce", overlay_runtime=overlay)

        request_thread = threading.Thread(target=lambda: supervisor.request_shutdown("requested"))
        request_thread.start()
        self.assertTrue(overlay.shutdown_entered.wait(timeout=1))

        self.assertTrue(supervisor.wait_for_shutdown(0.1))
        self.assertEqual(overlay.shutdown_reasons, ["requested"])
        request_thread.join(timeout=0.2)
        self.assertFalse(request_thread.is_alive())
        overlay.release_shutdown.set()
        self.assertTrue(supervisor.wait_for_overlay_shutdown(1.0))

    def test_supervisor_process_wait_covers_bounded_host_and_sidecar_cleanup(self):
        from hextech import runtime_supervisor
        from hextech.overlay import lifecycle

        per_process_upper_bound = (
            lifecycle.HOST_GRACEFUL_EXIT_TIMEOUT_SECONDS + 1.5 + 1.0
        )

        self.assertGreater(
            runtime_supervisor.OVERLAY_SHUTDOWN_WAIT_SECONDS,
            per_process_upper_bound * 2,
        )

    def test_supervisor_registers_overlay_cleanup_before_publishing_shutdown(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def snapshot(self) -> dict:
                return {"status": "running", "phase": "running"}

            def shutdown(self, _reason: str) -> None:
                return None

        supervisor = RuntimeSupervisor(parent_pid=0, overlay_runtime=FakeOverlayRuntime())
        observed: list[bool] = []

        class OrderingEvent(threading.Event):
            def set(self) -> None:
                observed.append(supervisor._overlay_shutdown_thread is not None)
                super().set()

        supervisor._shutdown_requested = OrderingEvent()

        supervisor.request_shutdown("test")

        self.assertEqual(observed, [True])
        self.assertTrue(supervisor.wait_for_overlay_shutdown(1.0))

    def test_supervisor_does_not_restart_overlay_action_after_template_prewarm(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.calls: list[bool] = []
                self.state = {
                    "desired_enabled": True,
                    "status": "error",
                    "phase": "failed",
                    "host_pid": None,
                    "sidecar_pid": None,
                    "context_status": "stopped",
                    "cache_status": "ready",
                    "cache_hit": False,
                    "startup_seconds": 24.0,
                    "visible_reason": "",
                    "last_error": "",
                    "last_start_failure_kind": "template_prewarm_timeout",
                }

            def set_enabled(self, enabled: bool) -> dict:
                self.calls.append(bool(enabled))
                self.state = {
                    **self.state,
                    "desired_enabled": bool(enabled),
                    "status": "running",
                    "phase": "running",
                    "host_pid": 701,
                    "sidecar_pid": 702,
                    "last_error": "",
                }
                return self.snapshot()

            def snapshot(self) -> dict:
                return dict(self.state)

            def shutdown(self, reason: str = "shutdown") -> None:
                self.state = {**self.state, "status": "stopped", "phase": reason}

        overlay = FakeOverlayRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = RuntimeSupervisor(
                parent_pid=os.getpid(),
                session_nonce="test-nonce",
                event_log_path=Path(tmp) / "events.jsonl",
                overlay_runtime=overlay,
            )

            supervisor.tick()

            self.assertEqual(overlay.calls, [])
            self.assertEqual(supervisor.snapshot()["components"]["game_overlay"]["status"], "error")
            self.assertFalse((Path(tmp) / "events.jsonl").exists())

    def test_supervisor_does_not_retry_unrelated_overlay_failure(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.calls: list[bool] = []

            def set_enabled(self, enabled: bool) -> dict:
                self.calls.append(bool(enabled))
                return self.snapshot()

            def snapshot(self) -> dict:
                return {
                    "desired_enabled": True,
                    "status": "error",
                    "phase": "failed",
                    "cache_status": "ready",
                    "last_start_failure_kind": "sidecar_failed",
                    "last_error": "game_overlay sidecar 启动后立即退出",
                }

            def shutdown(self, reason: str = "shutdown") -> None:
                pass

        overlay = FakeOverlayRuntime()
        supervisor = RuntimeSupervisor(parent_pid=os.getpid(), session_nonce="test-nonce", overlay_runtime=overlay)

        supervisor.tick()
        time.sleep(0.1)

        self.assertEqual(overlay.calls, [])

    def test_supervisor_does_not_retry_while_overlay_action_is_running(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.calls: list[bool] = []

            def set_enabled(self, enabled: bool) -> dict:
                self.calls.append(bool(enabled))
                return self.snapshot()

            def snapshot(self) -> dict:
                return {
                    "desired_enabled": True,
                    "status": "error",
                    "phase": "failed",
                    "cache_status": "ready",
                    "last_start_failure_kind": "template_prewarm_timeout",
                    "last_error": "",
                }

            def shutdown(self, reason: str = "shutdown") -> None:
                pass

        overlay = FakeOverlayRuntime()
        supervisor = RuntimeSupervisor(parent_pid=os.getpid(), session_nonce="test-nonce", overlay_runtime=overlay)
        with supervisor._lock:
            supervisor._active_overlay_action_id = "act-stop"
            supervisor._actions["act-stop"] = {
                "action_id": "act-stop",
                "type": "game_overlay",
                "status": "running",
                "enabled": False,
            }

        supervisor.tick()
        time.sleep(0.1)

        self.assertEqual(overlay.calls, [])

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

    def test_supervisor_tick_defers_due_refresh_while_overlay_is_starting(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        calls: list[bool] = []

        class FakeOverlayRuntime:
            def snapshot(self):
                return {
                    "desired_enabled": True,
                    "status": "starting",
                    "phase": "vision_prewarming",
                    "cache_status": "building",
                }

            def set_enabled(self, enabled: bool):
                raise AssertionError("tick 不应重启 Overlay")

            def shutdown(self, reason: str = "shutdown"):
                return None

        supervisor = RuntimeSupervisor(
            parent_pid=os.getpid(),
            session_nonce="test-nonce",
            refresh_func=lambda force=False: calls.append(bool(force)),
            overlay_runtime=FakeOverlayRuntime(),
        )
        with supervisor._lock:
            supervisor._next_refresh_at = time.time() - 1.0

        supervisor.tick()

        self.assertEqual(calls, [])
        self.assertGreater(supervisor.snapshot()["next_refresh_at"], time.time())

    def test_supervisor_tick_allows_due_refresh_after_overlay_error_even_if_cache_builds(self):
        from hextech.runtime_supervisor import RuntimeSupervisor

        calls: list[bool] = []

        class FakeOverlayRuntime:
            def snapshot(self):
                return {
                    "desired_enabled": True,
                    "status": "error",
                    "phase": "failed",
                    "cache_status": "building",
                }

            def shutdown(self, reason: str = "shutdown"):
                return None

        supervisor = RuntimeSupervisor(
            parent_pid=os.getpid(),
            session_nonce="test-nonce",
            refresh_func=lambda force=False: calls.append(bool(force)),
            overlay_runtime=FakeOverlayRuntime(),
        )
        with supervisor._lock:
            supervisor._next_refresh_at = time.time() - 1.0

        supervisor.tick()

        self.assertEqual(calls, [False])

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

        with (
            mock.patch.object(os.sys, "argv", ["hextech_ui.py", "--runtime-supervisor", "--parent-pid", "123"]),
            mock.patch("hextech.runtime_supervisor.main", return_value=17) as run_supervisor,
            self.assertRaises(SystemExit) as raised,
        ):
            hextech_ui.main()

        self.assertEqual(raised.exception.code, 17)
        run_supervisor.assert_called_once_with(["--parent-pid", "123"])

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
        from hextech.display.desktop import app as desktop_app

        ui = HextechUI.__new__(HextechUI)
        ui.runtime_supervisor = None
        ui._start_supervisor_lease_thread = mock.Mock()
        ui._restore_persisted_game_overlay = mock.Mock()
        handle = object()

        with mock.patch.object(desktop_app.ui_runtime, "start_runtime_supervisor_process", return_value=handle) as start:
            ui._start_runtime_supervisor()

        start.assert_called_once_with(parent_pid=os.getpid(), prewarm_templates=True)
        self.assertIs(ui.runtime_supervisor, handle)
        ui._start_supervisor_lease_thread.assert_called_once_with()
        ui._restore_persisted_game_overlay.assert_called_once_with()

    def test_desktop_ui_defers_persisted_game_overlay_until_supervisor_ready(self):
        from hextech.display.desktop.app import HextechUI

        ui = HextechUI.__new__(HextechUI)
        ui.feature_flags = {"web_frontend_enabled": True, "game_overlay_enabled": True}
        ui._game_overlay_desired_enabled = False
        ui._toggle_web_frontend = mock.Mock()
        ui._toggle_game_overlay = mock.Mock()
        ui.runtime_supervisor = None

        ui._apply_persisted_feature_flags()

        ui._toggle_web_frontend.assert_called_once_with()
        ui._toggle_game_overlay.assert_not_called()
        self.assertTrue(ui._game_overlay_desired_enabled)

        ui.runtime_supervisor = object()
        ui.game_overlay_var = mock.Mock()
        ui.game_overlay_var.get.return_value = True
        ui._feature_toggle_is_busy = mock.Mock(return_value=False)
        ui._set_overlay_status_summary = mock.Mock()

        ui._restore_persisted_game_overlay()

        ui._toggle_game_overlay.assert_called_once_with()

    def test_desktop_ui_game_overlay_toggle_uses_supervisor_action(self):
        from hextech.display.desktop.app import HextechUI

        ui = HextechUI.__new__(HextechUI)
        ui.game_overlay_var = mock.Mock()
        ui.game_overlay_var.get.return_value = True
        ui._game_overlay_desired_enabled = False
        ui._overlay_operation_lock = threading.Lock()
        ui._closing = False
        ui.runtime_supervisor = mock.Mock()
        ui.runtime_supervisor.set_game_overlay_enabled.return_value = {"status": "completed"}
        ui.service_manager = mock.Mock()
        ui.service_manager.start_game_overlay.side_effect = AssertionError("legacy controller must not start")
        ui.service_manager.stop_game_overlay.side_effect = AssertionError("legacy controller must not stop")
        ui._set_feature_toggle_busy = mock.Mock()
        ui._set_overlay_status_summary = mock.Mock()
        ui._persist_feature_flags_from_controls = mock.Mock()
        ui._restore_feature_toggle_after_failure = mock.Mock()
        ui._start_tracked_thread = lambda target, **_kwargs: target()
        ui._run_on_ui_thread = lambda callback: callback()

        ui._toggle_game_overlay()

        ui.runtime_supervisor.set_game_overlay_enabled.assert_called_once_with(True)
        ui.service_manager.start_game_overlay.assert_not_called()
        ui.service_manager.stop_game_overlay.assert_not_called()
        ui._persist_feature_flags_from_controls.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

"""测试 RuntimeSupervisor 编排。

调用方: pytest; 关键依赖: hextech.bootstrap.supervisor。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from hextech.interfaces.overlay import runtime_manager as overlay_runtime_manager
from support.process_fakes import FakeProcess


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
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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

    def test_game_overlay_action_is_async_and_updates_component_status(self):
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

        attempts: list[int] = []
        delays: list[float] = []

        def start_sidecar(**_kwargs):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 2:
                raise RuntimeError("Vision sidecar 在 readiness 前退出，exit_code=1")
            return FakeProcess(302)

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: FakeProcess(302),
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
        from hextech.bootstrap import supervisor as runtime_supervisor

        loader_entered = threading.Event()
        release_loader = threading.Event()

        def load_template_runtime(**kwargs):
            kwargs["status_callback"]("template_runtime_cache_lookup", {"cache_hit": True})
            loader_entered.set()
            release_loader.wait(timeout=2)
            return object()

        with mock.patch.object(overlay_runtime_manager, "OVERLAY_WARM_STARTUP_BUDGET_SECONDS", 0.05):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=lambda: FakeProcess(303),
                start_sidecar_func=lambda: FakeProcess(303),
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
        from hextech.bootstrap import supervisor as runtime_supervisor

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
            return FakeProcess(304)

        with mock.patch.object(overlay_runtime_manager, "OVERLAY_WARM_STARTUP_BUDGET_SECONDS", 0.05):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=lambda: FakeProcess(304),
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
        from hextech.bootstrap import supervisor as runtime_supervisor

        with mock.patch.object(overlay_runtime_manager, "OVERLAY_COLD_STARTUP_BUDGET_SECONDS", 0.05):
            for cache_hit, expected_mode in ((None, "unknown"), (False, "cold")):
                with self.subTest(mode=expected_mode):
                    sidecar_entered = threading.Event()
                    release_sidecar = threading.Event()

                    def start_sidecar(**_kwargs):
                        sidecar_entered.set()
                        release_sidecar.wait(timeout=2)
                        return FakeProcess(305)

                    runtime = runtime_supervisor.OverlayRuntimeManager(
                        start_host_func=lambda: FakeProcess(305),
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

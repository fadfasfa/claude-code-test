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



class RuntimeSupervisorPrewarmTests(unittest.TestCase):
    def test_overlay_runtime_prewarm_hard_timeout_rolls_back_host_and_enters_error(self):
        from hextech.bootstrap import supervisor as runtime_supervisor

        host = FakeProcess(305)
        release_loader = threading.Event()

        def load_template_runtime(**kwargs):
            kwargs["status_callback"]("template_runtime_cache_lookup", {"cache_hit": True})
            release_loader.wait(timeout=2)
            return object()

        with (
            mock.patch.object(overlay_runtime_manager, "OVERLAY_WARM_STARTUP_BUDGET_SECONDS", 0.05),
            mock.patch.object(overlay_runtime_manager, "OVERLAY_CONTINUATION_SECONDS", 0.05),
        ):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=lambda: host,
                start_sidecar_func=lambda: FakeProcess(305),
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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

        captured: dict[str, object] = {}

        def start_sidecar(**kwargs):
            captured.update(kwargs)
            return FakeProcess(304)

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: FakeProcess(304),
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
        from hextech.bootstrap import supervisor as runtime_supervisor

        with (
            mock.patch.object(overlay_runtime_manager, "OVERLAY_COLD_STARTUP_BUDGET_SECONDS", 0.01),
            mock.patch.object(overlay_runtime_manager, "OVERLAY_CONTINUATION_SECONDS", 0.01),
        ):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=lambda: FakeProcess(306),
                start_sidecar_func=lambda: FakeProcess(306),
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
        from hextech.bootstrap import supervisor as runtime_supervisor

        sidecar = FakeProcess(307)

        def slow_sidecar(**_kwargs):
            time.sleep(0.05)
            return sidecar

        with (
            mock.patch.object(overlay_runtime_manager, "OVERLAY_COLD_STARTUP_BUDGET_SECONDS", 0.01),
            mock.patch.object(overlay_runtime_manager, "OVERLAY_CONTINUATION_SECONDS", 0.01),
        ):
            runtime = runtime_supervisor.OverlayRuntimeManager(
                start_host_func=lambda: FakeProcess(307),
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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

        attempts: list[int] = []
        delays: list[float] = []

        def start_sidecar():
            attempts.append(1)
            raise RuntimeError("Vision sidecar 模板缺失：template_missing")

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: FakeProcess(401),
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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

        sidecar_started = threading.Event()

        def fail_loader(**_kwargs):
            raise RuntimeError("cache build failed")

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: FakeProcess(381),
            start_sidecar_func=lambda: (sidecar_started.set(), FakeProcess(381))[1],
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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

        loader_entered = threading.Event()
        release_loader = threading.Event()
        host_started = threading.Event()
        sidecar_started = threading.Event()

        def slow_loader(**_kwargs):
            loader_entered.set()
            release_loader.wait(timeout=5)
            return object()

        runtime = OverlayRuntimeManager(
            start_host_func=lambda: (host_started.set(), FakeProcess(501))[1],
            start_sidecar_func=lambda: (sidecar_started.set(), FakeProcess(501))[1],
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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

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
        from hextech.bootstrap.supervisor import OverlayRuntimeManager

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

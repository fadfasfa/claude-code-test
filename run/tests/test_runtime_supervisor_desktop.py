"""测试 RuntimeSupervisor 编排。

调用方: pytest; 关键依赖: hextech.bootstrap.supervisor。
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



class RuntimeSupervisorDesktopIntegrationTests(unittest.TestCase):
    def test_supervisor_restarts_stale_sidecar_without_publishing_failed_state(self):
        from hextech.bootstrap.supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            def __init__(self):
                self.calls: list[bool] = []
                self.release = threading.Event()
                self.state = {
                    "desired_enabled": True,
                    "status": "stale",
                    "phase": "sidecar_stale",
                }

            def snapshot(self) -> dict:
                return dict(self.state)

            def prepare_sidecar_restart(self) -> bool:
                self.state = {**self.state, "status": "starting", "phase": "sidecar_restart"}
                return True

            def set_enabled(self, enabled: bool) -> dict:
                self.calls.append(enabled)
                self.release.wait(timeout=2)
                self.state = {
                    **self.state,
                    "desired_enabled": enabled,
                    "status": "running",
                    "phase": "running",
                }
                return self.snapshot()

            def shutdown(self, reason: str = "shutdown") -> None:
                self.state = {**self.state, "status": "stopped", "phase": reason}

        overlay = FakeOverlayRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = RuntimeSupervisor(
                parent_pid=0,
                overlay_runtime=overlay,
                event_log_path=Path(tmp) / "events.jsonl",
            )
            supervisor.tick()

            restarting = supervisor.snapshot()["components"]["game_overlay"]
            self.assertEqual(restarting["status"], "starting")
            self.assertEqual(restarting["phase"], "sidecar_restart")
            self.assertEqual(overlay.calls, [True])

            overlay.release.set()
            deadline = time.time() + 1
            while time.time() < deadline and overlay.snapshot()["status"] != "running":
                time.sleep(0.01)
            self.assertEqual(overlay.snapshot()["status"], "running")

    def test_supervisor_shutdown_stops_overlay_runtime(self):
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap import supervisor as runtime_supervisor
        from hextech.interfaces.overlay import lifecycle

        per_process_upper_bound = (
            lifecycle.HOST_GRACEFUL_EXIT_TIMEOUT_SECONDS + 1.5 + 1.0
        )

        self.assertGreater(
            runtime_supervisor.OVERLAY_SHUTDOWN_WAIT_SECONDS,
            per_process_upper_bound * 2,
        )

    def test_supervisor_registers_overlay_cleanup_before_publishing_shutdown(self):
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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

    def test_supervisor_tick_handles_stale_lease(self):
        from hextech.bootstrap.supervisor import RuntimeSupervisor

        class FakeOverlayRuntime:
            shutdown_reason = ""

            @staticmethod
            def snapshot():
                return {"status": "stopped", "cache_status": "ready"}

            def shutdown(self, reason: str = "shutdown") -> None:
                self.shutdown_reason = reason

        with tempfile.TemporaryDirectory() as tmp:
            overlay = FakeOverlayRuntime()
            supervisor = RuntimeSupervisor(
                parent_pid=os.getpid(),
                session_nonce="test-nonce",
                overlay_runtime=overlay,
                event_log_path=Path(tmp) / "events.jsonl",
                lease_timeout_seconds=1.0,
                orphan_grace_seconds=1.0,
            )

            supervisor.tick()
            self.assertEqual(supervisor.snapshot()["actions"], {})

            supervisor.renew_lease({"control_instance_id": "ui-1"})
            with supervisor._lock:
                supervisor._lease["last_renewed_at"] = time.time() - 5
            supervisor.tick()
            self.assertTrue(supervisor.wait_for_shutdown(0))
            self.assertTrue(supervisor.wait_for_overlay_shutdown(1.0))
            self.assertEqual(supervisor.snapshot()["shutdown_reason"], "lease_expired")
            self.assertEqual(overlay.shutdown_reason, "lease_expired")

    def test_result_payload_supports_slots_dataclasses(self):
        from hextech.bootstrap.supervisor import RuntimeSupervisor

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
        from hextech.bootstrap import desktop as hextech_ui

        with (
            mock.patch.object(os.sys, "argv", ["hextech-desktop", "--runtime-supervisor", "--parent-pid", "123"]),
            mock.patch("hextech.bootstrap.supervisor.main", return_value=17) as run_supervisor,
            self.assertRaises(SystemExit) as raised,
        ):
            hextech_ui.main()

        self.assertEqual(raised.exception.code, 17)
        run_supervisor.assert_called_once_with(["--parent-pid", "123"])

    def test_keyboard_interrupt_requests_shutdown_before_wait_and_server_close(self):
        from hextech.bootstrap import supervisor as supervisor_module

        calls: list[str] = []

        class FakeServer:
            port = 12345

            def shutdown(self) -> None:
                calls.append("server.shutdown")

        class FakeSupervisor:
            supervisor_instance_id = "sup-test"
            session_nonce = "nonce-test"

            def serve_in_thread(self, *, port: int = 0):
                del port
                return FakeServer()

            def wait_for_shutdown(self, timeout: float) -> bool:
                del timeout
                raise KeyboardInterrupt

            def request_shutdown(self, reason: str) -> None:
                calls.append(f"request_shutdown:{reason}")

            def wait_for_overlay_shutdown(self, timeout: float) -> bool:
                del timeout
                calls.append("wait_for_overlay_shutdown")
                return True

        fake = FakeSupervisor()
        with mock.patch.object(supervisor_module, "RuntimeSupervisor", return_value=fake):
            result = supervisor_module.main(["--parent-pid", "0"])

        self.assertEqual(result, 0)
        self.assertEqual(
            calls,
            ["request_shutdown:finally", "wait_for_overlay_shutdown", "server.shutdown"],
        )

    def test_desktop_runtime_bootstraps_supervisor_process(self):
        from hextech.interfaces.desktop import runtime as desktop_runtime

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
        from hextech.interfaces.desktop import runtime as desktop_runtime

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
        from hextech.interfaces.desktop.app import HextechUI
        from hextech.interfaces.desktop import app as desktop_app

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
        from hextech.interfaces.desktop.app import HextechUI

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
        from hextech.interfaces.desktop.app import HextechUI

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

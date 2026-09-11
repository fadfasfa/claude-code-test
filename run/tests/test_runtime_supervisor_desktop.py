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

    def test_frozen_roles_are_resolved_before_desktop_seed_owner(self):
        from hextech.bootstrap.desktop import _frozen_role

        self.assertEqual(_frozen_role(["--game-overlay", "--token", "x"]), "--game-overlay")
        self.assertEqual(_frozen_role(["--data-service"]), "--data-service")
        self.assertEqual(_frozen_role([]), "desktop")
        source_path = Path(__file__).resolve().parents[1] / "src" / "hextech" / "bootstrap" / "desktop.py"
        source = source_path.read_text(encoding="utf-8")
        self.assertLess(source.index("role = _frozen_role()"), source.index("install_runtime_logging()"))
        self.assertNotIn("_install_packaged_cohort_seed()\n    install_runtime_logging()", source)

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
            self.assertTrue(handle.is_running())
            self.assertTrue(hasattr(handle, "job_object_attached"))
        finally:
            handle.stop()
        self.assertFalse(psutil.pid_exists(wrapper_pid))
        self.assertFalse(psutil.pid_exists(supervisor_pid))

    def test_bootstrap_accepts_interpreter_child_of_venv_launcher(self):
        from hextech.interfaces.desktop import runtime as desktop_runtime

        class FakeLauncher:
            pid = 101
            returncode = None

            def poll(self):
                return None

        class FakeChild:
            @staticmethod
            def parents():
                return [type("Parent", (), {"pid": 101})()]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bootstrap.json"
            target.write_text(
                json.dumps({"token": "token", "pid": 202, "port": 52001, "session_nonce": "nonce"}),
                encoding="utf-8",
            )
            with mock.patch.object(desktop_runtime.psutil, "Process", return_value=FakeChild()):
                payload = desktop_runtime._read_process_bootstrap(
                    target,
                    token="token",
                    process=FakeLauncher(),
                    deadline=time.time() + 1,
                    service_name="Runtime Supervisor",
                    stderr_tail=[],
                )

        self.assertEqual(payload["pid"], 202)

    def test_bootstrap_accepts_live_interpreter_after_venv_launcher_exits(self):
        from hextech.interfaces.desktop import runtime as desktop_runtime

        class ExitedLauncher:
            pid = 101
            returncode = 0

            @staticmethod
            def poll():
                return 0

        class ReparentedChild:
            @staticmethod
            def parents():
                return []

            @staticmethod
            def is_running():
                return True

            @staticmethod
            def status():
                return "running"

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bootstrap.json"
            target.write_text(
                json.dumps({"token": "token", "pid": 202, "port": 52001, "session_nonce": "nonce"}),
                encoding="utf-8",
            )
            with mock.patch.object(desktop_runtime.psutil, "Process", return_value=ReparentedChild()):
                payload = desktop_runtime._read_process_bootstrap(
                    target,
                    token="token",
                    process=ExitedLauncher(),
                    deadline=time.time() + 1,
                    service_name="Runtime Supervisor",
                    stderr_tail=[],
                )

        self.assertEqual(payload["pid"], 202)

    def test_invalid_bootstrap_after_launcher_exit_terminates_verified_child(self):
        from hextech.interfaces.desktop import runtime as desktop_runtime

        class ExitedLauncher:
            pid = 101
            returncode = 0
            stdout = None
            stderr = None

            @staticmethod
            def poll():
                return 0

        class Child:
            alive = True

            @staticmethod
            def parents():
                return []

            def is_running(self):
                return self.alive

            @staticmethod
            def status():
                return "running"

            def terminate(self):
                self.alive = False

            @staticmethod
            def wait(timeout=None):
                del timeout

        child = Child()

        def fake_popen(*_args, **kwargs):
            env = kwargs["env"]
            Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"]).write_text(
                json.dumps(
                    {
                        "token": env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"],
                        "pid": 202,
                        "port": 0,
                        "session_nonce": "nonce",
                    }
                ),
                encoding="utf-8",
            )
            return ExitedLauncher()

        with mock.patch.object(desktop_runtime.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
            desktop_runtime.psutil, "Process", return_value=child
        ):
            with self.assertRaisesRegex(RuntimeError, "进程或端口无效"):
                desktop_runtime.start_runtime_supervisor_process(parent_pid=os.getpid())

        self.assertFalse(child.alive)

    def test_supervisor_bootstrap_construction_failure_terminates_verified_child(self):
        from hextech.interfaces.desktop import runtime as desktop_runtime

        class ExitedLauncher:
            pid = 101
            returncode = 0
            stdout = None
            stderr = None

            @staticmethod
            def poll():
                return 0

        class Child:
            alive = True

            @staticmethod
            def parents():
                return []

            def is_running(self):
                return self.alive

            @staticmethod
            def status():
                return "running"

            def terminate(self):
                self.alive = False

            @staticmethod
            def wait(timeout=None):
                del timeout

        child = Child()

        def fake_popen(*_args, **kwargs):
            env = kwargs["env"]
            Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"]).write_text(
                json.dumps(
                    {
                        "token": env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"],
                        "pid": 202,
                        "port": 52001,
                        "session_nonce": "nonce",
                    }
                ),
                encoding="utf-8",
            )
            return ExitedLauncher()

        with mock.patch.object(desktop_runtime.subprocess, "Popen", side_effect=fake_popen), mock.patch.object(
            desktop_runtime.psutil, "Process", return_value=child
        ):
            with self.assertRaises(KeyError):
                desktop_runtime.start_runtime_supervisor_process(parent_pid=os.getpid())

        self.assertFalse(child.alive)

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

        start.assert_called_once_with(
            parent_pid=os.getpid(),
            prewarm_templates=True,
            pending_job_callback=ui._register_pending_process_job,
        )
        self.assertIs(ui.runtime_supervisor, handle)
        ui._start_supervisor_lease_thread.assert_called_once_with()
        ui._restore_persisted_game_overlay.assert_called_once_with()

    def test_pending_bootstrap_job_is_closed_during_desktop_exit(self):
        from hextech.interfaces.desktop.app import HextechUI

        ui = HextechUI.__new__(HextechUI)
        ui._pending_process_jobs = {}
        ui._pending_process_jobs_lock = threading.Lock()
        ui._closing = False
        job = mock.Mock()
        ui._register_pending_process_job(123, job)
        ui._close_pending_process_jobs()

        job.close.assert_called_once_with()
        self.assertEqual(ui._pending_process_jobs, {})

    def test_foreign_role_preflight_blocks_live_orphan_but_ignores_pid_reuse(self):
        from hextech.interfaces.desktop.app import HextechUI

        ui = HextechUI.__new__(HextechUI)
        process = psutil.Process(os.getpid())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir(parents=True)
            visibility = state / "game_overlay_visibility.v1.json"
            payload = {
                "schema_version": 2,
                "pid": os.getpid(),
                "pid_started_at": process.create_time(),
                "executable": os.path.normcase(process.exe()),
                "build_id": "old-build",
            }
            visibility.write_text(json.dumps(payload), encoding="utf-8")
            with mock.patch("hextech.modules.data.ports.paths.get_var_dir", return_value=root):
                with self.assertRaisesRegex(RuntimeError, "host:pid"):
                    ui._assert_no_foreign_runtime_roles()
                payload["pid_started_at"] = process.create_time() - 100.0
                visibility.write_text(json.dumps(payload), encoding="utf-8")
                ui._assert_no_foreign_runtime_roles()
    def test_desktop_ui_defers_persisted_game_overlay_until_supervisor_ready(self):
        from hextech.interfaces.desktop.app import HextechUI

        ui = HextechUI.__new__(HextechUI)
        ui.feature_flags = {"web_frontend_enabled": True, "game_overlay_enabled": True}
        ui._game_overlay_desired_enabled = False
        ui._toggle_game_overlay = mock.Mock()
        ui.runtime_supervisor = None

        ui._apply_persisted_feature_flags()

        ui._toggle_game_overlay.assert_not_called()
        self.assertTrue(ui._game_overlay_desired_enabled)

        ui.runtime_supervisor = object()
        ui.game_overlay_var = mock.Mock()
        ui.game_overlay_var.get.return_value = True
        ui._feature_toggle_is_busy = mock.Mock(return_value=False)
        ui._set_overlay_status_summary = mock.Mock()

        ui._restore_persisted_game_overlay()

        ui._toggle_game_overlay.assert_called_once_with()

    def test_desktop_ui_restores_persisted_web_in_bootstrap_thread(self):
        from hextech.interfaces.desktop import app_bootstrap
        from hextech.interfaces.desktop.app import HextechUI

        process = object()
        manager = mock.Mock()
        manager.web.process = process
        ui = HextechUI.__new__(HextechUI)
        ui.feature_flags = {"web_frontend_enabled": True, "auto_open_browser": False}
        ui.service_manager = manager
        ui.web_process = None

        with mock.patch.object(app_bootstrap.ui_runtime, "open_companion_browser") as open_browser:
            self.assertTrue(ui._restore_persisted_web_frontend())
        manager.start_web.assert_called_once_with()
        self.assertIs(ui.web_process, process)
        open_browser.assert_not_called()

    def test_desktop_ui_web_restore_failure_keeps_preference(self):
        from hextech.interfaces.desktop.app import HextechUI

        manager = mock.Mock()
        manager.start_web.side_effect = RuntimeError("port busy")
        ui = HextechUI.__new__(HextechUI)
        ui.feature_flags = {"web_frontend_enabled": True, "auto_open_browser": False}
        ui.service_manager = manager
        ui.web_process = object()

        self.assertFalse(ui._restore_persisted_web_frontend())

        self.assertTrue(ui.feature_flags["web_frontend_enabled"])
        self.assertIsNone(ui.web_process)
        manager.stop_web.assert_called_once_with()

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

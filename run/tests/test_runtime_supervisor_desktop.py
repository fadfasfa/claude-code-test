"""测试 RuntimeSupervisor 编排。

调用方: pytest; 关键依赖: hextech.bootstrap.runtime_owner。
"""
from __future__ import annotations

import pytest

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import psutil


def _request(owner, method, path, *, body=None, **kwargs):
    if method == "POST":
        return 202, owner.run_game_overlay_action(body)
    if path == "/v1/status":
        return 200, owner.snapshot()
    return 200, owner.get_action(path.rsplit("/",1)[-1])



class RuntimeSupervisorDesktopIntegrationTests(unittest.TestCase):
    def test_supervisor_restarts_stale_sidecar_without_publishing_failed_state(self):
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

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
                monitor=False,
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
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

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
        supervisor = RuntimeSupervisor(monitor=False,  overlay_runtime=overlay)

        request_thread = threading.Thread(target=lambda: supervisor.request_shutdown("requested"))
        request_thread.start()
        self.assertTrue(overlay.shutdown_entered.wait(timeout=1))

        self.assertTrue(supervisor.wait_for_shutdown(0.1))
        self.assertEqual(overlay.shutdown_reasons, ["requested"])
        request_thread.join(timeout=0.2)
        self.assertFalse(request_thread.is_alive())
        overlay.release_shutdown.set()
        self.assertTrue(supervisor.wait_for_overlay_shutdown(1.0))


    def test_supervisor_registers_overlay_cleanup_before_publishing_shutdown(self):
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

        class FakeOverlayRuntime:
            def snapshot(self) -> dict:
                return {"status": "running", "phase": "running"}

            def shutdown(self, _reason: str) -> None:
                return None

        supervisor = RuntimeSupervisor(monitor=False, overlay_runtime=FakeOverlayRuntime())
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
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

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
                monitor=False,

                event_log_path=Path(tmp) / "events.jsonl",
                overlay_runtime=overlay,
            )

            supervisor.tick()

            self.assertEqual(overlay.calls, [])
            self.assertEqual(supervisor.snapshot()["components"]["game_overlay"]["status"], "error")
            self.assertFalse((Path(tmp) / "events.jsonl").exists())

    def test_supervisor_does_not_retry_unrelated_overlay_failure(self):
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

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
        supervisor = RuntimeSupervisor(monitor=False,  overlay_runtime=overlay)

        supervisor.tick()
        time.sleep(0.1)

        self.assertEqual(overlay.calls, [])

    def test_supervisor_does_not_retry_while_overlay_action_is_running(self):
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

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
        supervisor = RuntimeSupervisor(monitor=False,  overlay_runtime=overlay)
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


    def test_result_payload_supports_slots_dataclasses(self):
        from hextech.bootstrap.runtime_owner import AppRuntimeOwner as RuntimeSupervisor

        @dataclass(slots=True)
        class SlotsResult:
            state: str
            published: bool

        supervisor = RuntimeSupervisor(monitor=False)

        self.assertEqual(
            supervisor._result_payload(SlotsResult(state="ready", published=True)),
            {"state": "ready", "published": True},
        )











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
        ui.runtime_owner = None

        ui._apply_persisted_feature_flags()

        ui._toggle_game_overlay.assert_not_called()
        self.assertTrue(ui._game_overlay_desired_enabled)

        ui.runtime_owner = object()
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
        ui.runtime_owner = mock.Mock()
        ui.runtime_owner.set_game_overlay_enabled.return_value = {"status": "completed"}
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

        ui.runtime_owner.set_game_overlay_enabled.assert_called_once_with(True)
        ui.service_manager.start_game_overlay.assert_not_called()
        ui.service_manager.stop_game_overlay.assert_not_called()
        ui._persist_feature_flags_from_controls.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()


@pytest.fixture(autouse=True)
def _close_app_owner_after_test():
    yield
    from hextech.bootstrap import runtime_owner as app_runtime
    owner = app_runtime.active_owner()
    if owner is not None:
        owner.stop(timeout=2)

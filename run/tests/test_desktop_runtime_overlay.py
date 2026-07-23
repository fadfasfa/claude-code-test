"""测试 桌面运行态 overlay。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.runtime。
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class DesktopRuntimeOverlayTests(unittest.TestCase):
    def test_initialize_core_threads_resolves_split_runtime_owners(self):
        from hextech.interfaces.desktop import runtime_services

        targets = []

        class FakeThread:
            def __init__(self, *, target, args, daemon):
                targets.append((target.__module__, target.__name__, args, daemon))

            def start(self):
                return None

        ui = SimpleNamespace(threads=[])
        with patch.object(runtime_services.threading, "Thread", FakeThread):
            runtime_services.initialize_core_threads(ui)

        assert [(module, name) for module, name, _args, _daemon in targets] == [
            ("hextech.interfaces.desktop.runtime_window", "lcu_polling_loop"),
            ("hextech.interfaces.desktop.runtime_window", "window_sync_loop"),
            ("hextech.interfaces.desktop.runtime_interaction", "run_terminal_loop"),
        ]
        assert all(args == (ui,) and daemon for _module, _name, args, daemon in targets)

    def _new_ui_for_fallback_tests(self, *, web_enabled: bool = False):
        from hextech.interfaces.desktop import app

        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class FakeServiceManager:
            def __init__(self):
                self.calls: list[str] = []
                self.web = SimpleNamespace(process=object(), status="stopped", last_error="")

            def start_web(self):
                self.calls.append("start")
                self.web.status = "running"

            def stop_web(self):
                self.calls.append("stop")
                self.web.status = "stopped"

            def is_web_running(self):
                return self.web.status == "running"

        ui = object.__new__(app.HextechUI)
        ui._closing = False
        ui.game_overlay_var = Var(True)
        ui.web_frontend_var = Var(web_enabled)
        ui.feature_flags = {"web_frontend_enabled": web_enabled, "auto_open_browser": True}
        ui.service_manager = FakeServiceManager()
        ui.web_port_file = Path("unused")
        ui.stop_event = threading.Event()
        ui.status_messages = []
        ui._set_overlay_status_summary = lambda text, color: ui.status_messages.append((text, color))
        ui._run_on_ui_thread = lambda func: func()
        ui._start_tracked_thread = lambda func, *, name: (func(), None)[1]
        return ui

    def _new_ui_for_service_manager_tests(self):
        from hextech.interfaces.desktop import app

        ui = object.__new__(app.HextechUI)
        ui._closing = False
        ui.service_manager = None
        ui._runtime_services_ready = False
        ui._service_manager_lock = threading.Lock()
        ui._service_manager_shutdown_in_progress = None
        ui._service_manager_shutdown_completed = None
        return ui

    def test_service_manager_publish_rejects_when_closing(self):
        class FakeServiceManager:
            def __init__(self):
                self.shutdown_count = 0

            def shutdown(self):
                self.shutdown_count += 1

        ui = self._new_ui_for_service_manager_tests()
        ui._closing = True
        manager = FakeServiceManager()

        self.assertFalse(ui._publish_service_manager(manager))
        self.assertIsNone(ui.service_manager)
        self.assertFalse(ui._runtime_services_ready)
        self.assertEqual(manager.shutdown_count, 1)

    def test_on_close_ignores_duplicate_exit_request(self):
        from hextech.interfaces.desktop import app

        ui = object.__new__(app.HextechUI)
        ui._closing = True
        ui.root = SimpleNamespace(destroy=lambda: self.fail("重复退出不得再次销毁窗口"))

        ui.on_close()

    def test_service_manager_failed_bootstrap_cleans_published_manager_once(self):
        class FakeServiceManager:
            def __init__(self):
                self.shutdown_count = 0

            def shutdown(self):
                self.shutdown_count += 1

        ui = self._new_ui_for_service_manager_tests()
        manager = FakeServiceManager()

        self.assertTrue(ui._publish_service_manager(manager))
        ui._shutdown_failed_bootstrap_service_manager(manager)

        self.assertIsNone(ui.service_manager)
        self.assertFalse(ui._runtime_services_ready)
        self.assertEqual(manager.shutdown_count, 1)

    def test_service_manager_failed_bootstrap_does_not_double_shutdown_close_owner(self):
        class FakeServiceManager:
            def __init__(self):
                self.shutdown_count = 0

            def shutdown(self):
                self.shutdown_count += 1

        ui = self._new_ui_for_service_manager_tests()
        manager = FakeServiceManager()

        self.assertTrue(ui._publish_service_manager(manager))
        taken = ui._take_service_manager_for_shutdown()
        ui._shutdown_failed_bootstrap_service_manager(manager)

        self.assertIs(taken, manager)
        self.assertEqual(manager.shutdown_count, 0)

    def test_service_manager_failed_bootstrap_does_not_double_shutdown_after_close_done(self):
        class FakeServiceManager:
            def __init__(self):
                self.shutdown_count = 0

            def shutdown(self):
                self.shutdown_count += 1

        ui = self._new_ui_for_service_manager_tests()
        manager = FakeServiceManager()

        self.assertTrue(ui._publish_service_manager(manager))
        taken = ui._take_service_manager_for_shutdown()
        self.assertIs(taken, manager)
        taken.shutdown()
        with ui._service_manager_lock:
            ui._service_manager_shutdown_in_progress = None
            ui._service_manager_shutdown_completed = manager

        ui._shutdown_failed_bootstrap_service_manager(manager)

        self.assertEqual(manager.shutdown_count, 1)

    def test_web_start_failure_does_not_persist_runtime_false_as_user_intent(self):
        from hextech.interfaces.desktop import app

        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class FailingServiceManager:
            web = SimpleNamespace(process=None, status="stopped", last_error="")

            def start_web(self):
                raise RuntimeError("port busy")

            def stop_web(self):
                return None

            def is_web_running(self):
                return False

        ui = object.__new__(app.HextechUI)
        ui.web_frontend_var = Var(True)
        ui.game_overlay_var = Var(True)
        ui.private_stats_var = Var(True)
        ui.low_frequency_listener_var = Var(True)
        ui.feature_flags = {
            "web_frontend_enabled": False,
            "game_overlay_enabled": True,
            "auto_open_browser": True,
            "private_policy_stats_enabled": True,
            "low_frequency_listener_enabled": True,
        }
        ui.service_manager = FailingServiceManager()
        ui.web_port_file = Path("unused")
        ui._set_feature_toggle_busy = lambda *_args, **_kwargs: None
        ui._set_status = lambda *_args, **_kwargs: None
        ui._run_on_ui_thread = lambda func: func()
        ui._start_tracked_thread = lambda func, *, name: (func(), None)[1]

        with (
            patch.object(app.ui_runtime, "open_companion_browser", return_value=False),
            patch.object(app.ui_runtime, "close_companion_browser"),
            patch.object(app, "save_ui_feature_flags", side_effect=AssertionError("failure must not persist flags")),
        ):
            ui._toggle_web_frontend()

        self.assertFalse(ui.web_frontend_var.get())
        self.assertFalse(ui.feature_flags["web_frontend_enabled"])

    def test_overlay_fallback_starts_web_without_changing_user_switch(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests()
        overlay = {"status": "starting", "fallback_recommended": True}
        with (
            patch.object(app.ui_runtime, "open_companion_browser", return_value=True) as opened,
            patch.object(app, "save_ui_feature_flags", side_effect=AssertionError("fallback 不得持久化用户开关")),
        ):
            ui._coordinate_overlay_web_fallback(overlay)

        self.assertEqual(ui.service_manager.calls, ["start"])
        opened.assert_called_once_with(ui.web_port_file)
        self.assertTrue(ui._fallback_web_owned)
        self.assertFalse(ui.web_frontend_var.get())

    def test_overlay_recovery_closes_only_fallback_owned_web(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests()
        ui._ensure_overlay_fallback_state()
        ui._fallback_web_owned = True
        ui.service_manager.web.status = "running"
        with patch.object(app.ui_runtime, "close_companion_browser", return_value=True) as closed:
            ui._coordinate_overlay_web_fallback({"status": "running", "fallback_recommended": False})

        self.assertEqual(ui.service_manager.calls, ["stop"])
        closed.assert_called_once_with()
        self.assertFalse(ui._fallback_web_owned)
        self.assertTrue(any("已恢复" in text for text, _color in ui.status_messages))

    def test_user_web_switch_adopts_running_fallback(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests(web_enabled=True)
        ui._ensure_overlay_fallback_state()
        ui._fallback_web_owned = True
        ui.service_manager.web.status = "running"
        with patch.object(app.ui_runtime, "close_companion_browser") as closed:
            ui._coordinate_overlay_web_fallback({"status": "running", "fallback_recommended": False})

        self.assertEqual(ui.service_manager.calls, [])
        closed.assert_not_called()
        self.assertFalse(ui._fallback_web_owned)

    def test_overlay_final_failure_keeps_fallback_web_running(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests()
        ui._ensure_overlay_fallback_state()
        ui._fallback_web_owned = True
        ui.service_manager.web.status = "running"
        with patch.object(app.ui_runtime, "close_companion_browser") as closed:
            ui._coordinate_overlay_web_fallback(
                {"status": "error", "fallback_recommended": True, "hard_timeout_reached": True}
            )

        self.assertEqual(ui.service_manager.calls, [])
        closed.assert_not_called()
        self.assertTrue(ui._fallback_web_owned)
        self.assertTrue(any("最终失败" in text for text, _color in ui.status_messages))

    def test_overlay_fallback_web_failure_does_not_cancel_overlay(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests()
        ui.service_manager.start_web = lambda: (_ for _ in ()).throw(RuntimeError("port busy"))
        ui.runtime_supervisor = SimpleNamespace(
            set_game_overlay_enabled=lambda _enabled: (_ for _ in ()).throw(AssertionError("不得停止 Overlay"))
        )
        with patch.object(app.ui_runtime, "open_companion_browser") as opened:
            ui._coordinate_overlay_web_fallback({"status": "starting", "fallback_recommended": True})

        opened.assert_not_called()
        self.assertFalse(ui._fallback_web_owned)
        self.assertIn("port busy", ui._fallback_web_error)
        self.assertTrue(any("Web 备份启动失败" in text for text, _color in ui.status_messages))

    def test_disabling_overlay_cleans_fallback_owned_web(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests()
        ui._ensure_overlay_fallback_state()
        ui._fallback_web_owned = True
        ui.service_manager.web.status = "running"
        ui.game_overlay_var.set(False)
        with patch.object(app.ui_runtime, "close_companion_browser", return_value=True) as closed:
            ui._coordinate_overlay_web_fallback({"status": "stopping", "fallback_recommended": False})

        self.assertEqual(ui.service_manager.calls, ["stop"])
        closed.assert_called_once_with()
        self.assertFalse(ui._fallback_web_owned)

    def test_user_adoption_cancels_queued_fallback_cleanup(self):
        ui = self._new_ui_for_fallback_tests(web_enabled=True)
        ui._ensure_overlay_fallback_state()
        ui._fallback_web_owned = True
        ui.service_manager.web.status = "running"
        threads: list[threading.Thread] = []
        ui._start_tracked_thread = lambda func, *, name: (
            threads.append(threading.Thread(target=func, name=name)),
            threads[-1].start(),
            threads[-1],
        )[-1]

        ui._web_operation_lock.acquire()
        ui._request_overlay_web_fallback_cleanup()
        ui._adopt_fallback_web_for_user()
        ui._web_operation_lock.release()
        threads[0].join(timeout=1)
        self.assertFalse(threads[0].is_alive())
        self.assertNotIn("stop", ui.service_manager.calls)
        self.assertTrue(ui.service_manager.is_web_running())

    def test_closing_during_fallback_start_stops_captured_manager(self):
        ui = self._new_ui_for_fallback_tests()
        start_entered = threading.Event()
        release_start = threading.Event()
        original_start = ui.service_manager.start_web

        def slow_start():
            start_entered.set()
            release_start.wait(timeout=2)
            original_start()

        ui.service_manager.start_web = slow_start
        manager = ui.service_manager
        threads: list[threading.Thread] = []
        ui._start_tracked_thread = lambda func, *, name: (
            threads.append(threading.Thread(target=func, name=name)),
            threads[-1].start(),
            threads[-1],
        )[-1]

        ui._start_overlay_web_fallback("generation-1")
        self.assertTrue(start_entered.wait(timeout=1))
        ui._closing = True
        ui.service_manager = None
        release_start.set()
        threads[0].join(timeout=1)
        self.assertFalse(threads[0].is_alive())
        self.assertEqual(manager.calls, ["start", "stop"])
        self.assertFalse(manager.is_web_running())

    def test_fallback_failure_is_latched_per_overlay_generation(self):
        from hextech.interfaces.desktop import app

        ui = self._new_ui_for_fallback_tests()
        attempts: list[int] = []

        def fail_start():
            attempts.append(1)
            raise RuntimeError("port busy")

        ui.service_manager.start_web = fail_start
        overlay = {"status": "starting", "fallback_recommended": True, "generation": 7}
        with patch.object(app.ui_runtime, "open_companion_browser"):
            ui._coordinate_overlay_web_fallback(overlay)
            ui._coordinate_overlay_web_fallback(overlay)

        self.assertEqual(attempts, [1])
        self.assertIn("Web 备份启动失败", ui.status_messages[-1][0])

    def test_fallback_reports_starting_until_web_is_ready(self):
        ui = self._new_ui_for_fallback_tests()
        threads: list[threading.Thread] = []
        ui._start_tracked_thread = lambda func, *, name: (
            threads.append(threading.Thread(target=func, name=name)),
            threads[-1].start(),
            threads[-1],
        )[-1]

        ui._web_operation_lock = threading.Lock()
        ui._web_operation_lock.acquire()
        ui._coordinate_overlay_web_fallback({"status": "starting", "fallback_recommended": True, "generation": 8})

        self.assertIn("Web 备份启动中", ui.status_messages[-1][0])
        ui._web_operation_lock.release()
        threads[0].join(timeout=1)

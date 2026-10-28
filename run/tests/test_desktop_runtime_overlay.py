"""测试 桌面运行态 overlay。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.runtime。
"""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class DesktopRuntimeOverlayTests(unittest.TestCase):
    def test_sidecar_restart_phase_uses_recovery_copy(self):
        from hextech.interfaces.desktop.app_shared import _format_runtime_game_overlay_status

        text, _color = _format_runtime_game_overlay_status(
            {"status": "starting", "phase": "sidecar_restart"}
        )

        self.assertEqual(text, "识别重启中")
        self.assertNotIn("失效", text)

    def test_initialize_core_threads_resolves_split_runtime_owners(self):
        from hextech.interfaces.desktop import runtime_services

        targets = []

        class FakeThread:
            def __init__(self, *, target, args, daemon, name=None):
                targets.append((target.__module__, target.__name__, args, daemon))

            def start(self):
                return None

        ui = SimpleNamespace(threads=[])
        with patch.object(runtime_services.threading, "Thread", FakeThread):
            runtime_services.initialize_core_threads(ui)
            runtime_services.initialize_core_threads(ui)

        assert [(module, name) for module, name, _args, _daemon in targets] == [
            ("hextech.interfaces.desktop.runtime_window", "lcu_polling_loop"),
            ("hextech.interfaces.desktop.runtime_window", "window_sync_loop"),
            ("hextech.interfaces.desktop.runtime_window", "candidate_update_loop"),
        ]
        assert all(args == (ui,) and daemon for _module, _name, args, daemon in targets)


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

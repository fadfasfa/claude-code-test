"""测试 overlay watchdog 契约。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.service_manager。
"""
from __future__ import annotations

import unittest


class OverlayWatchdogContractTests(unittest.TestCase):
    def test_service_manager_defaults_to_supervisor_owned_overlay(self):
        from hextech.interfaces.desktop.service_manager import ServiceManager

        manager = ServiceManager(start_web_func=lambda: object())

        watchdog = manager.ensure_game_overlay_healthy(enabled=True)
        snapshot = manager.get_status_snapshot()

        self.assertIsNone(manager._overlay_controller)
        self.assertEqual(watchdog["last_action"], "supervisor_owned")
        self.assertEqual(snapshot["game_overlay"]["status"], "supervisor_owned")
        self.assertEqual(snapshot["game_overlay"]["host_status"], "unknown")
        self.assertEqual(snapshot["game_overlay"]["sidecar_status"], "unknown")

    def test_controller_snapshot_does_not_restore_watchdog_restarts(self):
        from hextech.interfaces.desktop.service_manager import ServiceManager

        class DegradedOverlayController:
            host_process = object()
            sidecar_process = object()
            start_called = False

            def snapshot(self):
                return {
                    "status": "running",
                    "host_status": "running",
                    "sidecar_status": "running",
                    "context_poller_status": "degraded",
                    "context_poller_error": "LCU offline",
                    "last_error": "",
                }

            def is_running(self):
                return True

            def context_poller_running(self):
                return False

            def start(self):
                self.start_called = True
                raise AssertionError("context degraded must not restart overlay")

            def stop(self):
                return None

        controller = DegradedOverlayController()
        manager = ServiceManager(start_web_func=lambda: object(), overlay_controller=controller)

        watchdog = manager.ensure_game_overlay_healthy(enabled=True)

        self.assertFalse(controller.start_called)
        self.assertEqual(watchdog["last_action"], "supervisor_owned")


if __name__ == "__main__":
    unittest.main()

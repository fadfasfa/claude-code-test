from __future__ import annotations

import inspect
import unittest


class OverlayWatchdogContractTests(unittest.TestCase):
    def test_service_manager_watchdog_no_longer_restarts_from_trace_stale(self):
        from hextech.display.desktop.service_manager import ServiceManager

        source = inspect.getsource(ServiceManager.ensure_game_overlay_healthy)

        self.assertNotIn("restart_stale_state", source)
        self.assertNotIn("_overlay_state_age_seconds", source)
        self.assertIn("start_missing_process", source)

    def test_context_degraded_does_not_trigger_overlay_restart(self):
        from hextech.display.desktop.service_manager import ServiceManager

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
        self.assertEqual(watchdog["last_action"], "healthy_degraded")


if __name__ == "__main__":
    unittest.main()

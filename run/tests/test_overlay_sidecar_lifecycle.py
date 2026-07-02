from __future__ import annotations

import inspect
import unittest


class OverlaySidecarLifecycleTests(unittest.TestCase):
    def test_lifecycle_waits_for_sidecar_ready_and_sets_exit_signal(self):
        from hextech.overlay import lifecycle

        source = inspect.getsource(lifecycle.start_sidecar_process)

        self.assertEqual(lifecycle.OVERLAY_SIDECAR_READY_FILE_ENV, "HEXTECH_OVERLAY_SIDECAR_READY_FILE")
        self.assertIn("OVERLAY_SIDECAR_READY_FILE_ENV", source)
        self.assertIn("_wait_for_sidecar_ready", source)
        self.assertIn("_hextech_overlay_exit_file", source)

    def test_sidecar_writes_ready_and_checks_exit_signal(self):
        from hextech.overlay.vision import sidecar

        run_loop_source = inspect.getsource(sidecar.run_loop)

        self.assertIn("_write_sidecar_ready_from_env", run_loop_source)
        self.assertIn("_sidecar_exit_requested", run_loop_source)

    def test_context_poller_failure_degrades_without_blocking_overlay(self):
        from hextech.overlay.lifecycle import GameOverlayController

        class FakeProcess:
            pid = 123

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        controller = GameOverlayController(
            start_host_func=lambda: FakeProcess(),
            start_sidecar_func=lambda: FakeProcess(),
            start_context_poller_func=lambda: (_ for _ in ()).throw(RuntimeError("LCU offline")),
            prepare_data_func=lambda: None,
            write_inactive_func=lambda: None,
        )

        controller.start()
        snapshot = controller.snapshot()

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["context_poller_status"], "degraded")
        self.assertIn("LCU offline", snapshot["context_poller_error"])


if __name__ == "__main__":
    unittest.main()

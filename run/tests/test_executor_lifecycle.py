from __future__ import annotations

import unittest


class ExecutorLifecycleTests(unittest.TestCase):
    def test_web_executor_health_and_shutdown_contract(self):
        from hextech.display.web import runtime

        runtime.shutdown_web_executors(wait=False)
        initial = runtime.get_web_executor_health()
        self.assertTrue(initial["augment_cache"]["shutdown"])
        self.assertTrue(initial["hextech_preload"]["shutdown"])

        runtime.ensure_web_executors_started()
        started = runtime.get_web_executor_health()
        self.assertFalse(started["augment_cache"]["shutdown"])
        self.assertFalse(started["hextech_preload"]["shutdown"])

        runtime.shutdown_web_executors(wait=False)
        stopped = runtime.get_web_executor_health()
        self.assertTrue(stopped["augment_cache"]["shutdown"])
        self.assertTrue(stopped["hextech_preload"]["shutdown"])

    def test_desktop_preload_status_executor_shutdown_contract(self):
        from hextech.display.desktop import runtime

        runtime.shutdown_desktop_executors(wait=False)
        initial = runtime.get_desktop_executor_health()
        self.assertTrue(initial["preload_status"]["shutdown"])

        runtime.ensure_desktop_executors_started()
        started = runtime.get_desktop_executor_health()
        self.assertFalse(started["preload_status"]["shutdown"])

        runtime.shutdown_desktop_executors(wait=False)
        stopped = runtime.get_desktop_executor_health()
        self.assertTrue(stopped["preload_status"]["shutdown"])


if __name__ == "__main__":
    unittest.main()

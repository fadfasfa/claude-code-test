"""测试 overlay gameflow 状态。

调用方: pytest; 关键依赖: hextech.overlay.gameflow。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


class OverlayGameflowTests(unittest.TestCase):
    def test_gameflow_falls_back_to_lcu_in_progress_when_live_client_unknown(self):
        from hextech.overlay import gameflow

        with (
            patch.object(gameflow, "probe_live_client_in_progress", return_value=None),
            patch.object(gameflow, "probe_lcu_gameflow_in_progress", return_value=True),
        ):
            self.assertTrue(gameflow.probe_gameflow_in_progress())

    def test_lcu_gameflow_probe_imports_scanner_in_function_scope(self):
        from hextech.overlay import gameflow

        class FakeResponse:
            status_code = 200

            def json(self):
                return "InProgress"

        with (
            patch("hextech.overlay.providers.official.scan_lcu_process", return_value=(57265, "test-token")),
            patch.object(gameflow, "_http_get", return_value=FakeResponse()) as get,
        ):
            self.assertTrue(gameflow.probe_lcu_gameflow_in_progress())

        self.assertEqual(get.call_count, 1)
        self.assertIn("https://127.0.0.1:57265/lol-gameflow/v1/gameflow-phase", get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

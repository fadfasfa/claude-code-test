"""测试 overlay gameflow 状态。

调用方: pytest; 关键依赖: hextech.interfaces.overlay.gameflow。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


class OverlayGameflowTests(unittest.TestCase):
    def test_gameflow_falls_back_to_lcu_in_progress_when_live_client_unknown(self):
        from hextech.interfaces.overlay import gameflow

        with (
            patch.object(gameflow, "probe_live_client_in_progress", return_value=None),
            patch.object(gameflow, "probe_lcu_gameflow_in_progress", return_value=True),
        ):
            self.assertTrue(gameflow.probe_gameflow_in_progress())

    def test_lcu_gameflow_probe_uses_composition_root_scanner(self):
        from hextech.interfaces.overlay import gameflow

        class FakeResponse:
            status_code = 200

            def json(self):
                return "InProgress"

        with (
            patch.object(gameflow, "_lcu_scanner", return_value=(57265, "test-token")),
            patch.object(gameflow, "_http_get", return_value=FakeResponse()) as get,
        ):
            self.assertTrue(gameflow.probe_lcu_gameflow_in_progress())

        self.assertEqual(get.call_count, 1)
        self.assertIn("https://127.0.0.1:57265/lol-gameflow/v1/gameflow-phase", get.call_args.args[0])

    def test_live_client_non_200_falls_back_to_lcu_gameflow(self):
        from hextech.interfaces.overlay import gameflow

        class MissingActivePlayerResponse:
            status_code = 404

        with (
            patch.object(gameflow, "_http_get", return_value=MissingActivePlayerResponse()),
            patch.object(gameflow, "probe_lcu_gameflow_in_progress", return_value=True) as lcu_probe,
        ):
            self.assertIs(gameflow.probe_gameflow_state(), gameflow.GameflowState.IN_PROGRESS)

        lcu_probe.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

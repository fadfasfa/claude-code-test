from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class DesktopRuntimeOverlayTests(unittest.TestCase):
    def test_game_overlay_host_reason_labels_scene_blockers(self):
        from hextech.display.desktop.app import _format_game_overlay_host_reason

        self.assertEqual(_format_game_overlay_host_reason("event_stale_after_tab"), "等待最新选择画面")
        self.assertEqual(_format_game_overlay_host_reason("event_expired"), "选择数据已过期")
        self.assertEqual(_format_game_overlay_host_reason("blocking_modal_present"), "等待弹窗关闭")
        self.assertEqual(_format_game_overlay_host_reason("scoreboard_key_down"), "记分板显示中")
        self.assertEqual(_format_game_overlay_host_reason("unknown_reason"), "暂不显示")

    def test_empty_web_live_state_falls_back_to_lcu(self):
        from hextech.display.desktop import runtime

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"selected_champion_ids": [], "bench_champion_ids": [], "local_champion_id": 0}

        ui = SimpleNamespace(session=SimpleNamespace(get=lambda *_args, **_kwargs: FakeResponse()))

        with (
            patch.object(runtime, "_web_frontend_available", return_value=True),
            patch.object(runtime, "_resolve_redirect_base", return_value="http://127.0.0.1:8000"),
        ):
            candidate_groups, payload = runtime._fetch_web_live_state(ui)

        self.assertIsNone(candidate_groups)
        self.assertIsNone(payload)

    def test_client_overlay_hides_when_gameflow_in_progress_without_game_hwnd(self):
        from hextech.display.desktop import runtime

        should_show, keep_topmost = runtime.resolve_client_overlay_policy(
            client_visible=True,
            game_hwnd_renderable=False,
            gameflow_in_progress=True,
            live_client_in_progress=False,
            client_active=True,
            overlay_active=False,
            recent_client_context=False,
        )

        self.assertFalse(should_show)
        self.assertFalse(keep_topmost)

    def test_client_overlay_hides_when_live_client_is_available_without_game_hwnd(self):
        from hextech.display.desktop import runtime

        should_show, keep_topmost = runtime.resolve_client_overlay_policy(
            client_visible=True,
            game_hwnd_renderable=False,
            gameflow_in_progress=False,
            live_client_in_progress=True,
            client_active=True,
            overlay_active=False,
            recent_client_context=False,
        )

        self.assertFalse(should_show)
        self.assertFalse(keep_topmost)

    def test_lcu_champ_select_poll_uses_no_retry_local_request(self):
        from hextech.display.desktop import runtime

        class RetrySession:
            def get(self, *_args, **_kwargs):
                raise AssertionError("LCU polling must not use the global retry session")

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "localPlayerCellId": 7,
                    "myTeam": [
                        {"cellId": 7, "championId": 21},
                        {"cellId": 8, "championId": 222},
                    ],
                    "benchChampions": [{"championId": 22}],
                }

        calls: list[dict] = []

        def fake_get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return FakeResponse()

        ui = SimpleNamespace(
            _lcu_port=12345,
            _lcu_token="test-token",
            session=RetrySession(),
        )

        with (
            patch.object(runtime.requests, "get", side_effect=fake_get),
            patch.object(runtime, "_write_overlay_context_from_live_state", return_value=True),
        ):
            candidate_groups = runtime.poll_lcu_live_ids(ui)

        self.assertEqual(candidate_groups["selected_champion_ids"], ["21", "222"])
        self.assertEqual(candidate_groups["bench_champion_ids"], ["22"])
        self.assertEqual(len(calls), 1)
        self.assertIn("https://127.0.0.1:12345/lol-champ-select/v1/session", calls[0]["url"])
        self.assertFalse(calls[0]["verify"])
        self.assertLessEqual(calls[0]["timeout"], 1.0)

    def test_service_manager_reads_host_visibility_status(self):
        from hextech.display.desktop import service_manager

        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "game_overlay_visibility.v1.json"
            status_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": time.time(),
                        "decision": {"window_visible": False, "reason": "gameflow_not_in_progress"},
                        "host": {"gameflow": False},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(service_manager, "OVERLAY_HOST_VISIBILITY_FILE", status_path):
                status = service_manager.ServiceManager._overlay_host_visibility_status()

        self.assertTrue(status["ok"])
        self.assertFalse(status["visible"])
        self.assertEqual(status["reason"], "gameflow_not_in_progress")
        self.assertEqual(status["host"]["gameflow"], False)

    def test_service_manager_rejects_stale_or_unknown_host_visibility_status(self):
        from hextech.display.desktop import service_manager

        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "game_overlay_visibility.v1.json"

            status_path.write_text(
                json.dumps(
                    {
                        "schema_version": 99,
                        "updated_at": time.time(),
                        "decision": {"window_visible": True, "reason": "visible_ready"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(service_manager, "OVERLAY_HOST_VISIBILITY_FILE", status_path):
                unknown_schema = service_manager.ServiceManager._overlay_host_visibility_status()

            status_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": time.time() - service_manager.OVERLAY_HOST_VISIBILITY_STALE_SECONDS - 1.0,
                        "decision": {"window_visible": True, "reason": "visible_ready"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(service_manager, "OVERLAY_HOST_VISIBILITY_FILE", status_path):
                stale = service_manager.ServiceManager._overlay_host_visibility_status()

        self.assertFalse(unknown_schema["ok"])
        self.assertEqual(unknown_schema["error"], "visibility_status_unknown_schema")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["error"], "visibility_status_stale")


if __name__ == "__main__":
    unittest.main()

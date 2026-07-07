"""测试 桌面运行态 overlay。

调用方: pytest; 关键依赖: hextech.display.desktop.runtime。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class DesktopRuntimeOverlayTests(unittest.TestCase):
    def _new_ui_for_service_manager_tests(self):
        from hextech.display.desktop import app

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
        from hextech.display.desktop import app

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

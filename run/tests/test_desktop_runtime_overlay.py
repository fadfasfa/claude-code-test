"""测试 桌面运行态 overlay。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.runtime。
"""
from __future__ import annotations

import json
import inspect
import os
import tempfile
import threading
import time
import unittest
from io import BytesIO
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
        ui._coordinate_overlay_web_fallback(
            {"status": "starting", "fallback_recommended": True, "generation": 8}
        )

        self.assertIn("Web 备份启动中", ui.status_messages[-1][0])
        ui._web_operation_lock.release()
        threads[0].join(timeout=1)

    def test_overlay_control_plane_activation_precedes_local_data_load(self):
        from hextech.interfaces.desktop import app

        source = inspect.getsource(app.HextechUI._post_visible_bootstrap)

        self.assertLess(source.index("_activate_overlay_control_plane"), source.index("self.load_data()"))

    def test_game_overlay_host_reason_labels_scene_blockers(self):
        from hextech.interfaces.desktop.app import _format_game_overlay_host_reason

        self.assertEqual(_format_game_overlay_host_reason("event_stale_after_tab"), "等待最新选择画面")
        self.assertEqual(_format_game_overlay_host_reason("event_expired"), "选择数据已过期")
        self.assertEqual(_format_game_overlay_host_reason("blocking_modal_present"), "等待弹窗关闭")
        self.assertEqual(_format_game_overlay_host_reason("scoreboard_key_down"), "记分板显示中")
        self.assertEqual(_format_game_overlay_host_reason("unknown_reason"), "暂不显示")

    def test_overlay_status_polling_uses_secondary_label_without_overriding_primary_ready(self):
        from hextech.interfaces.desktop import app

        class Var:
            def get(self):
                return True

        class Label:
            def __init__(self, text=""):
                self.text = text
                self.fg = ""

            def winfo_exists(self):
                return True

            def config(self, **kwargs):
                self.text = kwargs.get("text", self.text)
                self.fg = kwargs.get("fg", self.fg)

        overlay = {
            "status": "starting",
            "phase": "vision_prewarming",
            "cache_status": "building",
            "context_status": "running",
            "last_error": "",
        }
        ui = object.__new__(app.HextechUI)
        ui.game_overlay_var = Var()
        ui.runtime_supervisor = SimpleNamespace(get_status=lambda: {"components": {"game_overlay": overlay}})
        ui.status_label = Label("后台服务已就绪")
        ui.overlay_status_label = Label("")
        ui.stop_event = threading.Event()
        ui.stop_event.set()

        ui._refresh_overlay_status_summary()

        self.assertEqual(ui.status_label.text, "后台服务已就绪")
        self.assertIn("海克斯卡识别模板预热中", ui.overlay_status_label.text)
        self.assertNotIn("英雄", ui.overlay_status_label.text)

    def test_legacy_overlay_status_polling_uses_secondary_label_without_overriding_primary_ready(self):
        from hextech.interfaces.desktop import app

        class Var:
            def get(self):
                return True

        class Label:
            def __init__(self, text=""):
                self.text = text
                self.fg = ""

            def winfo_exists(self):
                return True

            def config(self, **kwargs):
                self.text = kwargs.get("text", self.text)
                self.fg = kwargs.get("fg", self.fg)

        snapshot = {
            "vision_sidecar": {"status": "running"},
            "overlay_event": {"active": True},
            "overlay_visibility": {"ok": True, "visible": False, "reason": "selection_window_inactive"},
            "overlay_watchdog": {"last_action": ""},
        }
        ui = object.__new__(app.HextechUI)
        ui.game_overlay_var = Var()
        ui.runtime_supervisor = None
        ui.service_manager = SimpleNamespace(get_status_snapshot=lambda: snapshot)
        ui.status_label = Label("实时数据已挂载")
        ui.overlay_status_label = Label("")
        ui.stop_event = threading.Event()
        ui.stop_event.set()
        ui._kick_game_overlay_watchdog = lambda: None

        ui._refresh_overlay_status_summary()

        self.assertEqual(ui.status_label.text, "实时数据已挂载")
        self.assertIn("游戏内显示: 等待海克斯选择 / 识别运行", ui.overlay_status_label.text)

    def test_game_overlay_toggle_uses_secondary_label_without_overriding_primary_ready(self):
        from hextech.interfaces.desktop import app

        class Var:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class Label:
            def __init__(self, text=""):
                self.text = text
                self.fg = ""

            def winfo_exists(self):
                return True

            def config(self, **kwargs):
                self.text = kwargs.get("text", self.text)
                self.fg = kwargs.get("fg", self.fg)

        calls: list[bool] = []
        ui = object.__new__(app.HextechUI)
        ui.game_overlay_var = Var(True)
        ui.feature_flags = {"game_overlay_enabled": True}
        ui.runtime_supervisor = SimpleNamespace(
            set_game_overlay_enabled=lambda enabled: (calls.append(bool(enabled)), {"status": "running"})[1]
        )
        ui.status_label = Label("后台服务已就绪")
        ui.overlay_status_label = Label("")
        ui._overlay_status_text = ""
        ui._overlay_status_color = app.UI_COLORS["muted"]
        ui._overlay_operation_lock = threading.Lock()
        ui._closing = False
        ui._set_feature_toggle_busy = lambda *_args, **_kwargs: None
        ui._restore_feature_toggle_after_failure = lambda *_args, **_kwargs: None
        ui._persist_feature_flags_from_controls = lambda: None
        ui._run_on_ui_thread = lambda func: func()
        ui._start_tracked_thread = lambda func, *, name: (func(), None)[1]

        ui._toggle_game_overlay()

        self.assertEqual(calls, [True])
        self.assertEqual(ui.status_label.text, "后台服务已就绪")
        self.assertIn("游戏内显示启动请求已提交(running)", ui.overlay_status_label.text)

    def test_game_overlay_busy_toggle_uses_secondary_status_path(self):
        from hextech.interfaces.desktop import app

        class FakeWidget:
            def __init__(self, *args, **kwargs):
                self.bindings = {}
                self.text = kwargs.get("text", "")

            def pack(self, **_kwargs):
                return None

            def bind(self, event, callback):
                self.bindings[event] = callback

            def cget(self, key):
                return self.text if key == "text" else ""

        class FakeVar:
            def get(self):
                return False

            def set(self, _value):
                raise AssertionError("busy toggle must not mutate variable")

        calls: list[tuple[str, str]] = []
        ui = object.__new__(app.HextechUI)
        ui.feature_frame = None
        ui._feature_toggle_widgets = []
        ui._runtime_services_ready = True
        ui._feature_toggle_is_busy = lambda _text: True
        ui._set_overlay_status_summary = lambda text, color: calls.append((text, color))
        ui._set_status = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must use overlay status"))
        ui._refresh_feature_toggle_styles = lambda: None

        with patch.object(app.tk, "Frame", FakeWidget), patch.object(app.tk, "Canvas", FakeWidget), patch.object(app.tk, "Label", FakeWidget):
            frame = app.HextechUI._build_feature_toggle(
                ui,
                "游戏内显示",
                FakeVar(),
                lambda: None,
                accent=app.UI_COLORS["cyan"],
            )

        self.assertEqual(frame.bindings["<Button-1>"](), "break")
        self.assertEqual(calls, [("游戏内显示: 正在切换中", app.UI_COLORS["warn"])])

    def test_expand_restores_status_labels_in_initial_pack_order(self):
        from hextech.interfaces.desktop import app

        pack_calls: list[str] = []

        class FakeRoot:
            def geometry(self, _value):
                return None

            def after(self, _delay, _func):
                return "after-id"

            def after_cancel(self, _after_id):
                return None

        class FakeLabel:
            def __init__(self, name: str):
                self.name = name

            def winfo_exists(self):
                return True

            def pack_forget(self):
                return None

            def pack(self, **_kwargs):
                pack_calls.append(self.name)

        ui = object.__new__(app.HextechUI)
        ui.root = FakeRoot()
        ui._collapsed = True
        ui._collapse_render_after_id = None
        ui.status_label = FakeLabel("status")
        ui.overlay_status_label = FakeLabel("overlay")
        ui._refresh_current_hero_ids = lambda: None

        ui._toggle_collapse()

        self.assertEqual(pack_calls, ["status", "overlay"])

    def test_empty_web_live_state_falls_back_to_lcu(self):
        from hextech.interfaces.desktop import runtime

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

    def test_web_live_state_request_includes_local_auth_headers(self):
        from hextech.interfaces.desktop import runtime
        from hextech.interfaces.desktop import runtime_interaction
        from hextech.interfaces.desktop import runtime_services

        captured = {}

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"selected_champion_ids": [], "bench_champion_ids": [], "local_champion_id": 0}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return FakeResponse()

        ui = SimpleNamespace(session=SimpleNamespace(get=fake_get), web_port_file="unused")

        with (
            patch.object(runtime_interaction, "_web_frontend_available", return_value=True),
            patch.object(runtime_interaction, "_resolve_redirect_base", return_value="http://127.0.0.1:8000"),
            patch.object(runtime_services, "resolve_web_auth_token", return_value="local-secret"),
        ):
            runtime._fetch_web_live_state(ui)

        self.assertEqual(captured["url"], "http://127.0.0.1:8000/api/live_state")
        self.assertEqual(
            captured["headers"],
            {"Origin": "http://127.0.0.1:8000", "X-Hextech-Token": "local-secret"},
        )

    def test_client_overlay_hides_when_gameflow_in_progress_without_game_hwnd(self):
        from hextech.interfaces.desktop import runtime

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
        from hextech.interfaces.desktop import runtime

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
        from hextech.interfaces.desktop import runtime
        from hextech.interfaces.desktop import runtime_services

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
            patch.object(runtime_services, "_write_overlay_context_from_live_state", return_value=True),
        ):
            candidate_groups = runtime.poll_lcu_live_ids(ui)

        self.assertEqual(candidate_groups["selected_champion_ids"], ["21", "222"])
        self.assertEqual(candidate_groups["bench_champion_ids"], ["22"])
        self.assertEqual(len(calls), 1)
        self.assertIn("https://127.0.0.1:12345/lol-champ-select/v1/session", calls[0]["url"])
        self.assertFalse(calls[0]["verify"])
        self.assertLessEqual(calls[0]["timeout"], 1.0)

    def test_service_manager_reads_host_visibility_status(self):
        from hextech.interfaces.desktop import service_manager

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
        from hextech.interfaces.desktop import service_manager

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

    def test_load_and_set_img_does_not_cache_invalid_png_response(self):
        from hextech.interfaces.desktop import runtime

        class FakeResponse:
            status_code = 200
            content = b"<html>error</html>"

        class FakeLabel:
            def winfo_exists(self):
                return True

            def config(self, **_kwargs):
                raise AssertionError("invalid image must not be published")

        ui = SimpleNamespace(
            image_cache={},
            downloading_imgs=set(),
            img_write_lock=threading.Lock(),
            session=SimpleNamespace(get=lambda *_args, **_kwargs: FakeResponse()),
            _run_on_ui_thread=lambda func: func(),
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(runtime, "ASSET_DIR", temp_dir):
            runtime.load_and_set_img(ui, "266", FakeLabel())

            self.assertFalse(os.path.exists(os.path.join(temp_dir, "266.png")))
            self.assertNotIn("266", ui.image_cache)
            self.assertNotIn("266", ui.downloading_imgs)

    def test_write_champion_icon_cache_accepts_valid_png_response(self):
        from hextech.interfaces.desktop import runtime
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(buffer, format="PNG")
        png_bytes = buffer.getvalue()

        with tempfile.TemporaryDirectory() as temp_dir:
            target = os.path.join(temp_dir, "266.png")

            runtime._write_champion_icon_cache(target, png_bytes)

            self.assertEqual(Path(target).read_bytes(), png_bytes)

    def test_window_sync_ignores_stale_client_hwnd_1400(self):
        from hextech.interfaces.desktop import runtime

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, "GetWindowRect", "无效的窗口句柄。")

        class FakeUI:
            def __init__(self):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self._window_visible = False
                self._last_client_rect = (1, 2, 3, 4)
                self._overlay_position_initialized = True
                self._auto_follow_enabled = True
                self.hide_count = 0

            def _manual_follow_cooldown_elapsed(self, _seconds):
                return True

            def _resume_auto_follow(self):
                self._auto_follow_enabled = True

            def _run_on_ui_thread(self, callback):
                callback()

            def _move_overlay_to(self, _x, _y):
                raise AssertionError("stale hwnd must not move overlay")

            def _show_overlay(self, *, topmost=False):
                self._window_visible = True

            def _set_window_topmost(self, _enabled):
                return None

            def _hide_overlay(self):
                self.hide_count += 1
                self._window_visible = False

        ui = FakeUI()

        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        with (
            patch.object(runtime.win32gui, "FindWindow", return_value=100),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "GetWindowText", return_value="League of Legends"),
            patch.object(runtime.win32gui, "IsWindowVisible", return_value=True),
            patch.object(runtime.win32gui, "IsIconic", return_value=False),
            patch.object(runtime.win32gui, "GetWindowRect", side_effect=FakeWinError()),
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(runtime.time, "sleep", side_effect=stop_after_iteration),
        ):
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertIsNone(ui._last_client_rect)
        self.assertGreaterEqual(ui.hide_count, 1)

    def test_window_sync_treats_visibility_1400_as_stale_client_hwnd(self):
        from hextech.interfaces.desktop import runtime

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, "IsWindowVisible", "无效的窗口句柄。")

        class FakeUI:
            def __init__(self):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self._window_visible = True
                self._last_client_rect = (1, 2, 3, 4)
                self._overlay_position_initialized = True
                self._auto_follow_enabled = False
                self.hide_count = 0
                self.resume_count = 0

            def _manual_follow_cooldown_elapsed(self, _seconds):
                return True

            def _resume_auto_follow(self):
                self.resume_count += 1
                self._auto_follow_enabled = True

            def _run_on_ui_thread(self, _callback):
                raise AssertionError("stale visibility hwnd must not move overlay")

            def _show_overlay(self, *, topmost=False):
                raise AssertionError("stale visibility hwnd must not show overlay")

            def _set_window_topmost(self, _enabled):
                return None

            def _hide_overlay(self):
                self.hide_count += 1
                self._window_visible = False

        ui = FakeUI()

        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        with (
            patch.object(runtime.win32gui, "FindWindow", return_value=100),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "GetWindowText", return_value="League of Legends"),
            patch.object(runtime.win32gui, "IsWindowVisible", side_effect=FakeWinError()),
            patch.object(runtime.win32gui, "IsIconic", side_effect=AssertionError("IsIconic must not run after stale visible check")),
            patch.object(runtime.win32gui, "GetWindowRect", side_effect=AssertionError("stale visibility hwnd must not read rect")),
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(runtime.time, "sleep", side_effect=stop_after_iteration),
        ):
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertIsNone(ui._last_client_rect)
        self.assertFalse(ui._overlay_position_initialized)
        self.assertGreaterEqual(ui.hide_count, 1)
        self.assertGreaterEqual(ui.resume_count, 1)

    def test_window_sync_treats_iconic_1400_as_stale_client_hwnd(self):
        from hextech.interfaces.desktop import runtime

        class FakeWinError(Exception):
            winerror = 1400

            def __init__(self):
                super().__init__(1400, "IsIconic", "无效的窗口句柄。")

        class FakeUI:
            def __init__(self):
                self.stop_event = threading.Event()
                self.pause_event = threading.Event()
                self._window_visible = True
                self._last_client_rect = (1, 2, 3, 4)
                self._overlay_position_initialized = True
                self._auto_follow_enabled = False
                self.hide_count = 0
                self.resume_count = 0

            def _manual_follow_cooldown_elapsed(self, _seconds):
                return True

            def _resume_auto_follow(self):
                self.resume_count += 1
                self._auto_follow_enabled = True

            def _run_on_ui_thread(self, _callback):
                raise AssertionError("stale iconic hwnd must not move overlay")

            def _show_overlay(self, *, topmost=False):
                raise AssertionError("stale iconic hwnd must not show overlay")

            def _set_window_topmost(self, _enabled):
                return None

            def _hide_overlay(self):
                self.hide_count += 1
                self._window_visible = False

        ui = FakeUI()

        def stop_after_iteration(_seconds):
            ui.stop_event.set()

        with (
            patch.object(runtime.win32gui, "FindWindow", return_value=100),
            patch.object(runtime, "find_lol_game_window", return_value=None),
            patch.object(runtime.win32gui, "GetForegroundWindow", return_value=100),
            patch.object(runtime.win32gui, "GetWindowText", return_value="League of Legends"),
            patch.object(runtime.win32gui, "IsWindowVisible", return_value=True),
            patch.object(runtime.win32gui, "IsIconic", side_effect=FakeWinError()),
            patch.object(runtime.win32gui, "GetWindowRect", side_effect=AssertionError("stale iconic hwnd must not read rect")),
            patch.object(runtime, "probe_live_client_in_progress", return_value=False),
            patch.object(runtime, "probe_lcu_gameflow_in_progress", return_value=False),
            patch.object(runtime.logger, "exception") as log_exception,
            patch.object(runtime.time, "sleep", side_effect=stop_after_iteration),
        ):
            runtime.window_sync_loop(ui)

        self.assertFalse(log_exception.called)
        self.assertIsNone(ui._last_client_rect)
        self.assertFalse(ui._overlay_position_initialized)
        self.assertGreaterEqual(ui.hide_count, 1)
        self.assertGreaterEqual(ui.resume_count, 1)


if __name__ == "__main__":
    unittest.main()

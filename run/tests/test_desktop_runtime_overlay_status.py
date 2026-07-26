"""测试 桌面运行态 overlay。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.runtime。
"""
from __future__ import annotations

import json
import inspect
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch



class DesktopRuntimeOverlayStatusTests(unittest.TestCase):
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
        # 单行收敛层：service 通道文本已过新鲜窗口（at=0），overlay 摘要接管显示。
        ui._status_channels = {
            "service": {"text": "后台服务已就绪", "color": app.UI_COLORS["green"], "at": 0.0},
            "overlay": {"text": "", "color": app.UI_COLORS["muted"], "at": 0.0},
        }
        ui._data_created_ts = 0.0
        ui.status_line_label = Label("")
        ui.stop_event = threading.Event()
        ui.stop_event.set()

        ui._refresh_overlay_status_summary()

        # service 通道不被 overlay 轮询覆盖，overlay 通道拿到预热短语并上屏。
        self.assertEqual(ui._status_channels["service"]["text"], "后台服务已就绪")
        self.assertIn("模板预热中", ui._overlay_status_text)
        self.assertEqual(ui.status_line_label.text, "窗口就绪 · 模板预热中")
        self.assertNotIn("英雄", ui.status_line_label.text)

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
        ui._status_channels = {
            "service": {"text": "实时数据已挂载", "color": app.UI_COLORS["green"], "at": 0.0},
            "overlay": {"text": "", "color": app.UI_COLORS["muted"], "at": 0.0},
        }
        ui._data_created_ts = 0.0
        ui.status_line_label = Label("")
        ui.stop_event = threading.Event()
        ui.stop_event.set()
        ui._kick_game_overlay_watchdog = lambda: None

        ui._refresh_overlay_status_summary()

        # legacy 路径同样不带构建号后缀，短语落 overlay 通道。
        self.assertEqual(ui._status_channels["service"]["text"], "实时数据已挂载")
        self.assertEqual(ui._overlay_status_text, "等待海克斯选择 · 识别运行")
        self.assertEqual(ui.status_line_label.text, "等待海克斯选择 · 识别运行")

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
        ui._status_channels = {
            "service": {"text": "后台服务已就绪", "color": app.UI_COLORS["green"], "at": 0.0},
            "overlay": {"text": "", "color": app.UI_COLORS["muted"], "at": 0.0},
        }
        ui._data_created_ts = 0.0
        ui.status_line_label = Label("")
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
        # toggle 瞬态文案原文保留在 overlay 通道；service 通道不受影响。
        self.assertEqual(ui._status_channels["service"]["text"], "后台服务已就绪")
        self.assertIn("游戏内显示启动请求已提交(running)", ui._overlay_status_text)

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

    def test_expand_restores_status_bar(self):
        from hextech.interfaces.desktop import app

        pack_calls: list[tuple[str, dict]] = []

        class FakeRoot:
            def geometry(self, _value):
                return None

            def after(self, _delay, _func):
                return "after-id"

            def after_cancel(self, _after_id):
                return None

        class FakeWidget:
            def __init__(self, name: str):
                self.name = name

            def winfo_exists(self):
                return True

            def pack_forget(self):
                return None

            def pack(self, **kwargs):
                pack_calls.append((self.name, kwargs))

        ui = object.__new__(app.HextechUI)
        ui.root = FakeRoot()
        ui._collapsed = True
        ui._collapse_render_after_id = None
        ui.status_bar = FakeWidget("status_bar")
        ui._refresh_current_hero_ids = lambda: None

        ui._toggle_collapse()

        # 展开时单一 status_bar 恢复到底部；旧的双标签结构已收敛。
        self.assertEqual([name for name, _kwargs in pack_calls], ["status_bar"])
        self.assertEqual(pack_calls[0][1].get("side"), app.tk.BOTTOM)

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

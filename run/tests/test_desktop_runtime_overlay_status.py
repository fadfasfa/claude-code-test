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
        self.assertEqual(_format_game_overlay_host_reason("game_window_mode_unknown"), "")
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
        ui.runtime_owner = SimpleNamespace(get_status=lambda: {"components": {"game_overlay": overlay}})
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

    def test_pending_app_owner_status_keeps_primary_channel(self):
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
        ui.runtime_owner = None
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
        self.assertEqual(ui._overlay_status_text, "识别控制面准备中")
        self.assertEqual(ui.status_line_label.text, "识别控制面准备中")

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
        ui.runtime_owner = SimpleNamespace(
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
        ):
            candidate_groups = runtime.poll_lcu_live_ids(ui)

        self.assertEqual(candidate_groups["selected_champion_ids"], ["21", "222"])
        self.assertEqual(candidate_groups["bench_champion_ids"], ["22"])
        self.assertEqual(len(calls), 1)
        self.assertIn("https://127.0.0.1:12345/lol-champ-select/v1/session", calls[0]["url"])
        self.assertFalse(calls[0]["verify"])
        self.assertLessEqual(calls[0]["timeout"], 1.0)

    def test_service_manager_reads_host_visibility_status(self):
        from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager

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

            runtime = OverlayRuntimeManager(visibility_status_file=status_path)
            if True:
                status = runtime._read_visibility_health()

        self.assertEqual(status["visible_reason"], "gameflow_not_in_progress")

    def test_service_manager_rejects_stale_or_unknown_host_visibility_status(self):
        from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager

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
            runtime = OverlayRuntimeManager(visibility_status_file=status_path)
            if True:
                unknown_schema = runtime._read_visibility_health()

            status_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": time.time() - 6.0 - 1.0,
                        "decision": {"window_visible": True, "reason": "visible_ready"},
                    }
                ),
                encoding="utf-8",
            )
            runtime = OverlayRuntimeManager(visibility_status_file=status_path)
            if True:
                stale = runtime._read_visibility_health()

        self.assertEqual(unknown_schema["functional_reason"], "unknown_schema")
        self.assertEqual(stale["functional_reason"], "host_heartbeat_stale")

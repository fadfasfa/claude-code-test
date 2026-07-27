"""测试 overlay host 可见性。

调用方: pytest; 关键依赖: hextech.interfaces.overlay.host。
"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import patch



class OverlayHostVisibilityRuntimeTests(unittest.TestCase):
    def test_render_tick_confirms_context_before_opening_cold_snapshot(self):
        from types import SimpleNamespace
        from hextech.interfaces.overlay import host_runner

        calls = []

        class FakeCanvas:
            def after(self, _delay_ms, _callback):
                return "after-1"

            def after_cancel(self, _after_id):
                return None

            def delete(self, *_args):
                calls.append("clear")

            def update_idletasks(self):
                calls.append("flush")

            def winfo_width(self):
                return 1920

        class FakeSource:
            def read_event(self):
                return {
                    "active": True,
                    "visible": True,
                    "slots": [{"state": "ready", "augment_id": str(index)} for index in range(3)],
                    "source": {
                        "session_id": "session-1",
                        "selection_epoch": 1,
                        "selection_window_active": True,
                        "game_instance_id": "game-1",
                        "window_hwnd": 100,
                    },
                }

            def read_context(self):
                calls.append("context")
                return {"ok": True, "champion_id": "4"}

            def open_view(self):
                calls.append("open_view")
                return None

            def read_hint_cache(self):
                return {}

        class FakeGate:
            def evaluate(self, *_args, **_kwargs):
                calls.append("gate")
                return SimpleNamespace(
                    state="confirmed",
                    reason="context_confirmed",
                    context_revision=1,
                    held=False,
                    payload={"ok": True, "champion_id": "4"},
                )

        visibility = {"user_enabled": True, "target_hwnd": 100, "display_mode": "compact"}

        def sync_visibility(*_args, resolved_should_show=None, **_kwargs):
            visibility["render_full_overlay"] = True
            return True if resolved_should_show is None else resolved_should_show

        with (
            patch.object(host_runner, "ContextRenderGate", FakeGate),
            patch.object(host_runner, "_refresh_target_window"),
            patch.object(host_runner, "is_scoreboard_key_down", return_value=False),
            patch.object(host_runner, "_sync_event_visibility", side_effect=sync_visibility),
            patch.object(host_runner, "build_runtime_session", return_value=object()),
            patch.object(host_runner, "build_render_model_from_session", return_value={"stats": []}),
            patch.object(host_runner, "source_has_private_stats", return_value=False),
            patch.object(host_runner, "draw_overlay_frame"),
            patch.object(host_runner, "_log_waiting_context_diagnostic"),
            patch.object(host_runner, "_write_overlay_session_report"),
            patch.object(host_runner, "_write_real_session_evidence"),
        ):
            host_runner._schedule_event_render(
                object(),
                FakeCanvas(),
                {"diagnostic_mode": False, "event_poll_ms": 120},
                visibility,
                __import__("queue").Queue(),
                data_source=FakeSource(),
            )

        self.assertLess(calls.index("context"), calls.index("open_view"))
        self.assertLess(calls.index("gate"), calls.index("open_view"))
        self.assertLess(calls.index("clear"), calls.index("open_view"))
        self.assertEqual(visibility["rendered_selection_key"], ("session-1", 1))

    def test_render_tick_reads_cached_window_without_scanning_processes(self):
        from hextech.interfaces.overlay import host

        class FakeCanvas:
            def __init__(self):
                self.after_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))
                return f"after-{len(self.after_calls)}"

            def after_cancel(self, _after_id):
                return None

            def delete(self, *_args, **_kwargs):
                return None

        class FakeSource:
            def read_event(self):
                return {"visible": False, "source": {"selection_window_active": False}, "slots": []}

            def read_hint_cache(self):
                raise AssertionError("hidden overlay should not read hint cache")

            def read_context(self):
                raise AssertionError("hidden overlay should not read context")

        visibility = {
            "user_enabled": True,
            "target_hwnd": None,
            "target_rect": None,
            "window_visible": False,
            "scoreboard_key_down": False,
            "gameflow_in_progress": True,
        }

        with (
            patch.object(host, "_find_target_game_window", side_effect=AssertionError("render tick scanned windows")),
            patch.object(host, "is_scoreboard_key_down", return_value=False),
        ):
            host._schedule_event_render(
                object(),
                FakeCanvas(),
                {"diagnostic_mode": False, "no_activate": False, "event_poll_ms": 120},
                visibility,
                __import__("queue").Queue(),
                data_source=FakeSource(),
            )

        self.assertEqual(visibility["visibility_reason"], "game_window_missing")

    def test_sync_event_visibility_uses_gameflow_gate(self):
        from hextech.interfaces.overlay import host
        from hextech.interfaces.overlay import host_sync

        visibility = {
            "user_enabled": True,
            "target_hwnd": 100,
            "target_rect": (0, 0, 1920, 1080),
            "window_visible": False,
            "scoreboard_key_down": False,
        }
        snapshot = {"visible": True, "source": {"selection_window_active": True}, "slots": []}

        with (
            patch.object(host_sync, "_is_game_window_foreground", return_value=True),
            patch.object(host_sync, "_refresh_gameflow_in_progress", return_value=False),
        ):
            should_show = host._sync_event_visibility(
                object(),
                {"diagnostic_mode": False, "no_activate": False},
                visibility,
                snapshot,
                apply_window=False,
            )

        self.assertFalse(should_show)
        self.assertEqual(visibility["visibility_reason"], "gameflow_not_in_progress")

    def test_sync_event_visibility_reports_missing_game_window_before_vision_state(self):
        from hextech.interfaces.overlay import host

        visibility = {
            "user_enabled": True,
            "target_hwnd": None,
            "target_rect": None,
            "window_visible": False,
            "scoreboard_key_down": False,
        }
        snapshot = {"visible": False, "source": {"selection_window_active": False}, "slots": []}

        with patch.object(host, "_refresh_gameflow_in_progress", return_value=True):
            should_show = host._sync_event_visibility(
                object(),
                {"diagnostic_mode": False, "no_activate": False},
                visibility,
                snapshot,
                apply_window=False,
            )

        self.assertFalse(should_show)
        self.assertEqual(visibility["visibility_reason"], "game_window_missing")

    def test_foreground_event_hook_only_sets_signal_until_tk_drain(self):
        from hextech.interfaces.overlay import host

        calls = []

        class FakeUser32:
            def SetWinEventHook(self, event_min, event_max, module, callback, process_id, thread_id, flags):
                calls.append((event_min, event_max, module, callback, process_id, thread_id, flags))
                return 77

        event = threading.Event()
        hook = host._register_foreground_event_hook(event, user32=FakeUser32())

        self.assertIsNotNone(hook)
        calls[0][3](77, host.EVENT_SYSTEM_FOREGROUND, 100, 0, 0, 0, 0)
        self.assertTrue(event.is_set())

    def test_foreground_event_drain_coalesces_multiple_events(self):
        from hextech.interfaces.overlay import host

        class FakeRoot:
            def __init__(self):
                self.after_calls = []

            def after(self, delay_ms, callback):
                self.after_calls.append((delay_ms, callback))
                return f"after-{len(self.after_calls)}"

        event = threading.Event()
        event.set()
        render_calls = []
        root = FakeRoot()

        host._schedule_foreground_event_drain(
            root,
            event,
            lambda: render_calls.append("render"),
            poll_ms=50,
        )

        self.assertEqual(render_calls, ["render"])
        self.assertFalse(event.is_set())
        self.assertEqual(root.after_calls[0][0], 50)

    def test_visibility_diagnostic_log_is_structured_and_rate_limited(self):
        from hextech.interfaces.overlay import host

        visibility = {
            "gameflow_in_progress": True,
            "target_hwnd": 100,
            "game_renderable": True,
            "game_foreground": True,
            "ready_slots": 0,
            "context_ok": False,
            "last_visibility_diagnostic_logged_at": 100.0,
        }
        snapshot = {"source": {"selection_window_active": True}, "slots": []}

        with patch.object(host.logger, "info") as info:
            host._log_visibility_diagnostic(
                visibility,
                snapshot,
                now=101.5,
                should_show=True,
                reason="visible_detecting",
            )
            host._log_visibility_diagnostic(
                visibility,
                snapshot,
                now=101.8,
                should_show=True,
                reason="visible_detecting",
            )

        self.assertEqual(info.call_count, 1)
        message, payload = info.call_args.args
        self.assertEqual(message, "game_overlay visibility=%s")
        self.assertEqual(payload["host"]["gameflow"], True)
        self.assertEqual(payload["scene"]["selection_window_active"], True)
        self.assertEqual(payload["context"]["context_ok"], False)
        self.assertEqual(payload["decision"]["reason"], "visible_detecting")

    def test_visibility_diagnostic_logs_when_context_or_scene_gate_changes(self):
        from hextech.interfaces.overlay import host

        visibility = {
            "gameflow_in_progress": True,
            "target_hwnd": 100,
            "game_renderable": True,
            "game_foreground": True,
            "ready_slots": 0,
            "context_ok": False,
            "context_champion_id": "",
            "context_source": "",
            "context_error": "context_missing",
        }
        snapshot = {"source": {"selection_window_active": True}, "slots": []}

        with patch.object(host.logger, "info") as info:
            host._log_visibility_diagnostic(
                visibility,
                snapshot,
                now=200.0,
                should_show=True,
                reason="visible_detecting",
            )
            visibility["context_ok"] = True
            visibility["context_champion_id"] = "103"
            visibility["context_source"] = "lcu"
            visibility["context_error"] = ""
            host._log_visibility_diagnostic(
                visibility,
                snapshot,
                now=200.1,
                should_show=True,
                reason="visible_detecting",
            )
            visibility["blocking_modal"] = True
            host._log_visibility_diagnostic(
                visibility,
                snapshot,
                now=200.2,
                should_show=True,
                reason="visible_detecting",
            )

        self.assertEqual(info.call_count, 3)

    def test_sync_event_visibility_writes_host_visibility_state(self):
        from hextech.interfaces.overlay import host
        from hextech.interfaces.overlay import host_sync
        from hextech.interfaces.overlay import host_visibility

        visibility = {
            "user_enabled": True,
            "target_hwnd": 100,
            "target_rect": (0, 0, 1920, 1080),
            "window_visible": False,
            "scoreboard_key_down": False,
            "context_ok": False,
            "context_error": "context_missing",
        }
        snapshot = {
            "visible": True,
            "source": {"selection_window_active": True},
            "slots": [],
        }
        writes = []

        with (
            patch.object(host_sync, "_is_game_window_foreground", return_value=True),
            patch.object(host_sync, "is_window_renderable", return_value=True),
            patch.object(host_sync, "_refresh_gameflow_in_progress", return_value=True),
            patch.object(host_visibility, "atomic_write_json", side_effect=lambda path, payload: writes.append((Path(path).name, payload))),
        ):
            should_show = host._sync_event_visibility(
                object(),
                {"diagnostic_mode": False, "no_activate": False},
                visibility,
                snapshot,
                apply_window=False,
            )

        self.assertTrue(should_show)
        self.assertEqual(writes[0][0], "game_overlay_visibility.v1.json")
        payload = writes[0][1]
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["functional_status"], "degraded")
        self.assertEqual(payload["functional_reason"], "context_unavailable")
        self.assertIn("window", payload)
        self.assertIn("render", payload)
        self.assertEqual(payload["host"]["gameflow"], True)
        self.assertEqual(payload["scene"]["selection_window_active"], True)
        self.assertEqual(payload["context"]["error"], "context_missing")
        self.assertEqual(payload["decision"]["window_visible"], True)
        self.assertEqual(payload["decision"]["reason"], "visible_detecting")

    def test_host_visibility_state_write_is_change_based(self):
        from hextech.interfaces.overlay import host
        from hextech.interfaces.overlay import host_visibility

        visibility = {
            "user_enabled": True,
            "gameflow_in_progress": False,
            "target_hwnd": 0,
            "game_renderable": False,
            "game_foreground": False,
        }
        snapshot = {"source": {"selection_window_active": False}, "slots": []}

        with patch.object(host_visibility, "atomic_write_json") as write_json:
            host._write_host_visibility_status(
                visibility,
                snapshot,
                now=100.0,
                should_show=False,
                reason="gameflow_not_in_progress",
            )
            host._write_host_visibility_status(
                visibility,
                snapshot,
                now=100.5,
                should_show=False,
                reason="gameflow_not_in_progress",
            )
            host._write_host_visibility_status(
                visibility,
                snapshot,
                now=100.6,
                should_show=False,
                reason="game_window_missing",
            )

        self.assertEqual(write_json.call_count, 2)

if __name__ == "__main__":
    unittest.main()

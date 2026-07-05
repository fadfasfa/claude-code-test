from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class OverlayHostVisibilityTests(unittest.TestCase):
    def test_visibility_snapshot_requires_in_progress_foreground_renderable_game(self):
        from hextech.overlay.host import resolve_overlay_visibility

        snapshot = resolve_overlay_visibility(
            user_enabled=True,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(10, 20, 1930, 1100),
            game_renderable=True,
            game_foreground=True,
            content_ready=True,
            now=1234.5,
        )

        self.assertTrue(snapshot.visible)
        self.assertEqual(snapshot.reason, "visible_ready")
        self.assertEqual(snapshot.game_hwnd, 100)
        self.assertEqual(snapshot.game_rect, (10, 20, 1930, 1100))
        self.assertEqual(snapshot.updated_at, 1234.5)

    def test_visibility_snapshot_hides_when_game_not_foreground(self):
        from hextech.overlay.host import resolve_overlay_visibility

        snapshot = resolve_overlay_visibility(
            user_enabled=True,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            game_foreground=False,
            content_ready=True,
            now=1234.5,
        )

        self.assertFalse(snapshot.visible)
        self.assertEqual(snapshot.reason, "game_not_foreground")

    def test_visibility_snapshot_hides_when_game_not_renderable(self):
        from hextech.overlay.host import resolve_overlay_visibility

        snapshot = resolve_overlay_visibility(
            user_enabled=True,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=False,
            game_foreground=True,
            content_ready=True,
            now=1234.5,
        )

        self.assertFalse(snapshot.visible)
        self.assertEqual(snapshot.reason, "game_window_not_renderable")

    def test_visibility_snapshot_hides_when_gameflow_not_in_progress(self):
        from hextech.overlay.host import resolve_overlay_visibility

        snapshot = resolve_overlay_visibility(
            user_enabled=True,
            gameflow_in_progress=False,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            game_foreground=True,
            content_ready=True,
            now=1234.5,
        )

        self.assertFalse(snapshot.visible)
        self.assertEqual(snapshot.reason, "gameflow_not_in_progress")

    def test_visibility_snapshot_shows_detecting_before_content_ready(self):
        from hextech.overlay.host import resolve_overlay_visibility

        snapshot = resolve_overlay_visibility(
            user_enabled=True,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            game_foreground=True,
            content_ready=False,
            now=1234.5,
        )

        self.assertTrue(snapshot.visible)
        self.assertEqual(snapshot.reason, "visible_detecting")

    def test_selection_window_active_shows_detecting_before_content_ready(self):
        from hextech.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=True,
            ready_slots=0,
            source_reason="slots_detecting",
        )

        self.assertTrue(should_show)
        self.assertEqual(reason, "visible_detecting")

    def test_selection_window_active_shows_partial_ready_slots(self):
        from hextech.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=True,
            ready_slots=2,
            source_reason="slots_detecting",
        )

        self.assertTrue(should_show)
        self.assertEqual(reason, "visible_partial")

    def test_scene_blockers_hide_overlay_before_content_rendering(self):
        from hextech.overlay.host import decide_visibility

        base_args = {
            "user_enabled": True,
            "event_visible": True,
            "game_foreground": True,
            "content_ready": True,
            "selection_window_active": True,
            "gameflow_in_progress": True,
            "game_renderable": True,
        }

        for overrides, expected_reason in (
            ({"event_error": "event_expired", "event_visible": False}, "event_expired"),
            ({"blocking_modal": True}, "blocking_modal_present"),
            ({"scoreboard_key_down": True}, "scoreboard_key_down"),
            ({"event_fresh_after_tab": False}, "event_stale_after_tab"),
        ):
            with self.subTest(expected_reason=expected_reason):
                args = dict(base_args)
                args.update(overrides)
                should_show, reason = decide_visibility(**args)
                self.assertFalse(should_show)
                self.assertEqual(reason, expected_reason)

    def test_inactive_vision_event_hides_overlay_when_game_gate_is_visible(self):
        from hextech.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=False,
            ready_slots=0,
            gameflow_in_progress=True,
            game_renderable=True,
            source_reason="selection_window_inactive",
        )

        self.assertFalse(should_show)
        self.assertEqual(reason, "selection_window_inactive")

    def test_sync_event_visibility_reads_cached_gameflow_without_querying_http(self):
        from hextech.overlay import host

        visibility = {
            "user_enabled": True,
            "target_hwnd": 100,
            "target_rect": (0, 0, 1920, 1080),
            "window_visible": False,
            "scoreboard_key_down": False,
            "gameflow_in_progress": True,
        }
        snapshot = {"visible": True, "source": {"selection_window_active": True}, "slots": []}

        with (
            patch.object(host, "_is_game_window_foreground", return_value=True),
            patch.object(host, "is_window_renderable", return_value=True),
            patch.object(host, "_query_gameflow_in_progress", side_effect=AssertionError("render tick queried gameflow")),
        ):
            should_show = host._sync_event_visibility(
                object(),
                {"diagnostic_mode": False, "no_activate": False},
                visibility,
                snapshot,
                apply_window=False,
            )

        self.assertTrue(should_show)
        self.assertEqual(visibility["visibility_reason"], "visible_detecting")

    def test_render_tick_reads_cached_window_without_scanning_processes(self):
        from hextech.overlay import host

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
        from hextech.overlay import host

        visibility = {
            "user_enabled": True,
            "target_hwnd": 100,
            "target_rect": (0, 0, 1920, 1080),
            "window_visible": False,
            "scoreboard_key_down": False,
        }
        snapshot = {"visible": True, "source": {"selection_window_active": True}, "slots": []}

        with (
            patch.object(host, "_is_game_window_foreground", return_value=True),
            patch.object(host, "_refresh_gameflow_in_progress", return_value=False),
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
        from hextech.overlay import host

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
        from hextech.overlay import host

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
        from hextech.overlay import host

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
        from hextech.overlay import host

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
        from hextech.overlay import host

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
        from hextech.overlay import host

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
            patch.object(host, "_is_game_window_foreground", return_value=True),
            patch.object(host, "is_window_renderable", return_value=True),
            patch.object(host, "_refresh_gameflow_in_progress", return_value=True),
            patch.object(host, "atomic_write_json", side_effect=lambda path, payload: writes.append((Path(path).name, payload))),
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
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["host"]["gameflow"], True)
        self.assertEqual(payload["scene"]["selection_window_active"], True)
        self.assertEqual(payload["context"]["error"], "context_missing")
        self.assertEqual(payload["decision"]["window_visible"], True)
        self.assertEqual(payload["decision"]["reason"], "visible_detecting")

    def test_host_visibility_state_write_is_change_based(self):
        from hextech.overlay import host

        visibility = {
            "user_enabled": True,
            "gameflow_in_progress": False,
            "target_hwnd": 0,
            "game_renderable": False,
            "game_foreground": False,
        }
        snapshot = {"source": {"selection_window_active": False}, "slots": []}

        with patch.object(host, "atomic_write_json") as write_json:
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

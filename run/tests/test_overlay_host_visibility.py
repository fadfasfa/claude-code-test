"""测试 overlay host 可见性。

调用方: pytest; 关键依赖: hextech.interfaces.overlay.host。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


class OverlayHostVisibilityTests(unittest.TestCase):
    def test_render_failure_retry_uses_base_poll_before_backoff(self):
        from hextech.interfaces.overlay.host import RENDER_ERROR_BACKOFF_AFTER, resolve_event_render_retry_delay_ms

        config = {"event_poll_ms": 250, "fast_event_poll_ms": 60}

        self.assertEqual(resolve_event_render_retry_delay_ms(config, 1), 250)
        self.assertEqual(resolve_event_render_retry_delay_ms(config, RENDER_ERROR_BACKOFF_AFTER), 250)
        self.assertEqual(resolve_event_render_retry_delay_ms(config, RENDER_ERROR_BACKOFF_AFTER + 1), 500)

    def test_visibility_snapshot_requires_in_progress_foreground_renderable_game(self):
        from hextech.interfaces.overlay.host import resolve_overlay_visibility

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
        from hextech.interfaces.overlay.host import resolve_overlay_visibility

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
        from hextech.interfaces.overlay.host import resolve_overlay_visibility

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
        from hextech.interfaces.overlay.host import resolve_overlay_visibility

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
        from hextech.interfaces.overlay.host import resolve_overlay_visibility

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
        from hextech.interfaces.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            ready_slots=0,
        )

        self.assertTrue(should_show)
        self.assertEqual(reason, "visible_detecting")

    def test_selection_window_active_shows_partial_ready_slots(self):
        from hextech.interfaces.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            ready_slots=2,
        )

        self.assertTrue(should_show)
        self.assertEqual(reason, "visible_partial")

    def test_scene_blockers_hide_overlay_before_content_rendering(self):
        from hextech.interfaces.overlay.host import decide_visibility

        base_args = {
            "user_enabled": True,
            "event_visible": True,
            "game_foreground": True,
            "content_ready": True,
            "selection_window_active": True,
            "gameflow_in_progress": True,
            "game_hwnd": 100,
            "game_rect": (0, 0, 1920, 1080),
            "game_renderable": True,
        }

        for overrides, expected_reason in (
            ({"blocking_modal": True}, "blocking_modal_present"),
            ({"scoreboard_key_down": True}, "scoreboard_key_down"),
            ({"transient_pause": True}, "transient_pause"),
            ({"event_fresh_after_tab": False}, "event_stale_after_tab"),
        ):
            with self.subTest(expected_reason=expected_reason):
                args = dict(base_args)
                args.update(overrides)
                should_show, reason = decide_visibility(**args)
                self.assertFalse(should_show)
                self.assertEqual(reason, expected_reason)

    def test_legacy_toggle_request_cannot_change_host_enablement(self):
        import queue

        from hextech.interfaces.overlay.host_sync import _drain_hotkey_requests

        requests: "queue.Queue[str]" = queue.Queue()
        requests.put("toggle")
        requests.put("toggle_mode")
        visibility = {"user_enabled": True, "display_mode": "compact"}

        _drain_hotkey_requests(requests, visibility)

        self.assertTrue(visibility["user_enabled"])
        self.assertEqual(visibility["display_mode"], "expanded")

    def test_inactive_vision_event_hides_overlay_when_game_gate_is_visible(self):
        from hextech.interfaces.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=False,
            ready_slots=0,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
        )

        self.assertFalse(should_show)
        self.assertEqual(reason, "selection_inactive")

    def test_expired_event_without_active_hold_hides_overlay(self):
        from hextech.interfaces.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=None,
            ready_slots=0,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            event_error="event_expired",
        )

        self.assertFalse(should_show)
        self.assertEqual(reason, "event_expired")

    def test_diagnostic_mode_does_not_override_inactive_selection(self):
        from hextech.interfaces.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=False,
            gameflow_in_progress=True,
            game_hwnd=100,
            game_rect=(0, 0, 1920, 1080),
            game_renderable=True,
            diagnostic_mode=True,
        )

        self.assertFalse(should_show)
        self.assertEqual(reason, "selection_inactive")

    def test_inactive_overlay_event_carries_scene_inactive_source(self):
        from hextech.modules.vision.events import build_inactive_overlay_event

        event = build_inactive_overlay_event(source_tag="manual-hide")

        self.assertFalse(event["active"])
        self.assertEqual(event["source"]["selection_window_active"], False)
        self.assertEqual(event["source"]["ready_slots"], 0)
        self.assertEqual(event["source"]["content_ready"], False)

    def test_decide_visibility_defaults_to_missing_game_window_without_hwnd(self):
        from hextech.interfaces.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=True,
            ready_slots=0,
            gameflow_in_progress=True,
            game_renderable=True,
        )

        self.assertFalse(should_show)
        self.assertEqual(reason, "game_window_missing")

    def test_sync_event_visibility_reads_cached_gameflow_without_querying_http(self):
        from hextech.interfaces.overlay import host
        from hextech.interfaces.overlay import host_sync

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
            patch.object(host_sync, "_is_game_window_foreground", return_value=True),
            patch.object(host_sync, "is_window_renderable", return_value=True),
            patch.object(host_sync, "_query_gameflow_in_progress", side_effect=AssertionError("render tick queried gameflow")),
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

    def test_sync_event_visibility_holds_last_active_event_for_one_second(self):
        from hextech.interfaces.overlay import host, host_sync

        visibility = {
            "user_enabled": True,
            "target_hwnd": 100,
            "target_rect": (0, 0, 1920, 1080),
            "window_visible": False,
            "scoreboard_key_down": False,
        }
        active = {
            "ok": True,
            "visible": True,
            "active": True,
            "error": "",
            "generated_at": 100.0,
            "source": {"selection_window_active": True, "ready_slots": 3},
            "slots": [{"slot": index, "state": "ready", "name": f"强化 {index}"} for index in range(3)],
        }

        common_patches = (
            patch.object(host_sync, "_is_game_window_foreground", return_value=True),
            patch.object(host_sync, "is_window_renderable", return_value=True),
            patch.object(host_sync, "_refresh_gameflow_in_progress", return_value=True),
            patch.object(host_sync, "_write_host_visibility_status"),
            patch.object(host_sync, "_log_visibility_diagnostic"),
        )
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4]:
            with patch.object(host_sync.time, "time", return_value=100.0):
                self.assertTrue(
                    host._sync_event_visibility(
                        object(),
                        {"diagnostic_mode": False, "no_activate": False},
                        visibility,
                        active,
                        apply_window=False,
                    )
                )

            expired = {
                **active,
                "ok": False,
                "visible": False,
                "error": "event_expired",
                "generated_at": 97.0,
            }
            with patch.object(host_sync.time, "time", return_value=100.2):
                self.assertTrue(
                    host._sync_event_visibility(
                        object(),
                        {"diagnostic_mode": False, "no_activate": False},
                        visibility,
                        expired,
                        apply_window=False,
                    )
                )
            self.assertEqual(visibility["visibility_reason"], "visible_stale_hold")
            self.assertTrue(expired["source"]["stale_event_hold_active"])
            self.assertEqual([slot["name"] for slot in expired["slots"]], ["强化 0", "强化 1", "强化 2"])

            expired_after_hold = {
                **active,
                "ok": False,
                "visible": False,
                "error": "event_expired",
                "generated_at": 97.0,
            }
            with patch.object(host_sync.time, "time", return_value=101.3):
                self.assertFalse(
                    host._sync_event_visibility(
                        object(),
                        {"diagnostic_mode": False, "no_activate": False},
                        visibility,
                        expired_after_hold,
                        apply_window=False,
                    )
                )
            self.assertEqual(visibility["visibility_reason"], "event_expired")

    def test_explicit_inactive_event_clears_stale_hold_immediately(self):
        from hextech.interfaces.overlay import host, host_sync

        visibility = {
            "user_enabled": True,
            "target_hwnd": 100,
            "target_rect": (0, 0, 1920, 1080),
            "window_visible": False,
            "scoreboard_key_down": False,
            "last_active_event": {"visible": True},
            "event_stale_hold_until": 999.0,
        }
        inactive = {
            "ok": True,
            "visible": False,
            "active": False,
            "error": "",
            "generated_at": 100.0,
            "source": {"selection_window_active": False, "reason": "selection_completed"},
            "slots": [],
        }

        with (
            patch.object(host_sync, "_is_game_window_foreground", return_value=True),
            patch.object(host_sync, "is_window_renderable", return_value=True),
            patch.object(host_sync, "_refresh_gameflow_in_progress", return_value=True),
            patch.object(host_sync, "_write_host_visibility_status"),
            patch.object(host_sync, "_log_visibility_diagnostic"),
        ):
            should_show = host._sync_event_visibility(
                object(),
                {"diagnostic_mode": False, "no_activate": False},
                visibility,
                inactive,
                apply_window=False,
            )

        self.assertFalse(should_show)
        self.assertEqual(visibility["visibility_reason"], "selection_inactive")
        self.assertNotIn("last_active_event", visibility)
        self.assertNotIn("event_stale_hold_until", visibility)

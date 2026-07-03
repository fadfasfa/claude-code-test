from __future__ import annotations

import unittest


class OverlayHostVisibilityTests(unittest.TestCase):
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
        self.assertEqual(reason, "detecting")

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

    def test_inactive_event_still_hides_overlay(self):
        from hextech.overlay.host import decide_visibility

        should_show, reason = decide_visibility(
            user_enabled=True,
            event_visible=False,
            game_foreground=True,
            content_ready=False,
            selection_window_active=False,
            ready_slots=0,
            source_reason="selection_window_inactive",
        )

        self.assertFalse(should_show)
        self.assertEqual(reason, "selection_window_inactive")


if __name__ == "__main__":
    unittest.main()

"""测试 overlay vision 状态。

调用方: pytest; 关键依赖: hextech.infrastructure.vision.state。
"""
from __future__ import annotations

import unittest
from unittest import mock

from support.vision_events import selection_event as _selection_event
from support.vision_events import ready_slot as _ready_slot

class OverlayVisionInteractionTests(unittest.TestCase):
    def test_cursor_over_stable_slot_freezes_only_that_slot(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        tracker.update(_selection_event())
        stable = tracker.update(_selection_event())
        changed = _selection_event()
        changed["source"]["cursor_over_slots"] = [0]
        changed["source"]["cursor_over_cards"] = True
        changed["_raw_slots"][0] = _ready_slot(0, "wrong", "瞬时错误")
        changed["_raw_slots"][1] = _ready_slot(1, "new_b", "新候选 B")

        first = tracker.update(changed)
        second = tracker.update(changed)
        third = tracker.update(changed)

        self.assertEqual(first["slots"][0]["augment_id"], stable["slots"][0]["augment_id"])
        self.assertEqual(second["slots"][0]["augment_id"], stable["slots"][0]["augment_id"])
        self.assertEqual(second["slots"][1]["augment_id"], stable["slots"][1]["augment_id"])
        self.assertEqual(third["slots"][1]["augment_id"], "new_b")
        self.assertEqual(third["source"]["cursor_over_slots"], [0])
        self.assertTrue(third["source"]["hover_occluded"])

    def test_cursor_slot_probe_returns_actual_slot_indexes(self):
        from hextech.infrastructure.vision import sidecar_event_loop

        source = {"layout_transform": {}}
        with mock.patch.object(sidecar_event_loop, "pick_card_panels", return_value=[
            (0, 0, 100, 100),
            (100, 0, 200, 100),
            (200, 0, 300, 100),
        ]), mock.patch.object(sidecar_event_loop, "apply_transform", side_effect=lambda box, *_args: box), \
             mock.patch.object(sidecar_event_loop, "get_cursor_screen_position", return_value=(150, 50)):
            slots = sidecar_event_loop._cursor_over_card_slots((0, 0, 300, 100), (300, 100), source)

        self.assertEqual(slots, [1])

    def test_hover_occlusion_keeps_stable_slots_until_cursor_leaves(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker()
        tracker.update(_selection_event())
        stable = tracker.update(_selection_event())
        self.assertTrue(stable["active"])
        stable_ids = [slot["augment_id"] for slot in stable["slots"]]

        hover_event = _selection_event()
        hover_event["source"].update(
            {
                "scene_present": False,
                "selection_button_present": False,
                "cursor_over_cards": True,
                "card_residue": False,
            }
        )
        hover_event["_raw_slots"] = []

        for _ in range(6):
            result = tracker.update(hover_event)
            self.assertTrue(result["active"])
            self.assertTrue(result["source"]["hover_occluded"])
            self.assertEqual([slot["augment_id"] for slot in result["slots"]], stable_ids)

        clear_event = dict(hover_event)
        clear_event["source"] = dict(
            hover_event["source"], cursor_over_cards=False, card_residue=False, name_residue=[False, False, False]
        )
        tracker.update(clear_event)
        exited = tracker.update(clear_event)

        self.assertFalse(exited["active"])
        self.assertEqual(exited["source"]["scene_state"], "absent")

    def test_hover_occlusion_has_finite_hold_when_cursor_never_leaves(self):
        from hextech.infrastructure.vision.state import HOVER_HOLD_FRAMES, SelectionTracker

        tracker = SelectionTracker()
        tracker.update(_selection_event())
        tracker.update(_selection_event())

        hover_event = _selection_event()
        hover_event["source"].update(
            {
                "scene_present": False,
                "selection_button_present": True,
                "cursor_over_cards": True,
                "card_residue": True,
            }
        )
        hover_event["_raw_slots"] = []

        for _ in range(HOVER_HOLD_FRAMES):
            held = tracker.update(hover_event)
            self.assertTrue(held["active"])
            self.assertEqual(held["source"]["reason"], "hover_occluded")

        expired = tracker.update(hover_event)

        self.assertFalse(expired["active"])
        self.assertEqual(expired["source"]["scene_state"], "absent")
        self.assertEqual(expired["source"]["reason"], "selection_completed")
        self.assertEqual(expired["source"]["slot_states"], [])

    def test_post_selection_cursor_residue_exits_without_long_hover_hold(self):
        from hextech.infrastructure.vision.state import RESIDUE_HOLD_FRAMES, SelectionTracker

        tracker = SelectionTracker()
        tracker.update(_selection_event())
        tracker.update(_selection_event())

        clicked_event = _selection_event()
        clicked_event["source"].update(
            {
                "scene_present": False,
                "selection_button_present": False,
                "cursor_over_cards": True,
                "card_residue": True,
                "selection_confirmed": True,
                "name_residue": [True, True, False],
            }
        )
        clicked_event["_raw_slots"] = []

        results = [tracker.update(clicked_event) for _ in range(RESIDUE_HOLD_FRAMES + 1)]

        self.assertTrue(all(result["source"]["reason"] != "hover_occluded" for result in results))
        self.assertFalse(results[0]["active"])
        self.assertEqual(results[0]["source"]["reason"], "selection_completed")

    def test_click_arms_selection_completion_and_next_epoch_recovers(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker()
        tracker.update(_selection_event())
        clicked = _selection_event()
        clicked["source"].update({"selection_click": True, "cursor_over_cards": True})
        tracker.update(clicked)

        disappeared = _selection_event()
        disappeared["source"].update({"scene_present": False, "selection_window_active": False, "cursor_over_cards": True})
        disappeared["_raw_slots"] = []
        completed = tracker.update(disappeared)
        self.assertEqual(completed["source"]["reason"], "selection_completed")
        self.assertNotIn("detecting", completed["source"]["slot_states"])

        tracker.update(_selection_event())
        recovered = tracker.update(_selection_event())
        self.assertEqual(recovered["source"]["ready_slots"], 3)

    def test_non_hover_residue_still_expires(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker()
        tracker.update(_selection_event())
        tracker.update(_selection_event())

        residue_event = _selection_event()
        residue_event["source"].update(
            {
                "scene_present": False,
                "selection_button_present": False,
                "cursor_over_cards": False,
                "card_residue": True,
                "name_residue": [True, True, False],
            }
        )
        residue_event["_raw_slots"] = []

        first = tracker.update(residue_event)
        second = tracker.update(residue_event)
        expired = tracker.update(residue_event)

        self.assertTrue(first["active"])
        self.assertTrue(second["active"])
        self.assertFalse(expired["active"])
        self.assertEqual(expired["source"]["reason"], "scene_residue_expired")

    def test_selection_active_partial_progress_writes_on_slot_changes(self):
        from hextech.infrastructure.vision import sidecar

        first_partial = {
            "active": False,
            "source": {
                "selection_window_active": True,
                "gate_state": "visible_partial",
                "ready_slots": 1,
                "reason": "",
            },
            "slots": [
                {"slot": 0, "state": "ready", "augment_id": "augment_a"},
                {"slot": 1, "state": "detecting", "augment_id": ""},
                {"slot": 2, "state": "detecting", "augment_id": ""},
            ],
        }
        second_partial = {
            "active": False,
            "source": {
                "selection_window_active": True,
                "gate_state": "visible_partial",
                "ready_slots": 2,
                "reason": "",
            },
            "slots": [
                {"slot": 0, "state": "ready", "augment_id": "augment_a"},
                {"slot": 1, "state": "ready", "augment_id": "augment_b"},
                {"slot": 2, "state": "detecting", "augment_id": ""},
            ],
        }

        first_signature = sidecar._loop_event_signature(first_partial)

        self.assertNotEqual(first_signature, sidecar._loop_event_signature(second_partial))
        self.assertTrue(
            sidecar.should_write_loop_event(
                second_partial,
                last_signature=first_signature,
                last_write_at=1000.0,
                now=1000.16,
                heartbeat_seconds=60.0,
            )
        )


if __name__ == "__main__":
    unittest.main()

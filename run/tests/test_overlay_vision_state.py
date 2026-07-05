from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _ready_slot(slot: int, augment_id: str, name: str) -> dict:
    candidate = {
        "augment_id": augment_id,
        "name": name,
        "tier": "Gold",
        "confidence": 0.91,
    }
    return {
        "slot": slot,
        "channels": {
            "text": {
                "margin": 0.05,
                "top_candidates": [candidate],
            },
            "text_alt": {
                "margin": 0.05,
                "top_candidates": [candidate],
            },
        },
    }


def _selection_event() -> dict:
    return {
        "source": {
            "scene_present": True,
            "scene_kind": "hextech",
            "selection_button_present": True,
            "cursor_over_cards": False,
            "card_residue": False,
            "name_residue": [False, False, False],
        },
        "_raw_slots": [
            _ready_slot(0, "augment_a", "强化 A"),
            _ready_slot(1, "augment_b", "强化 B"),
            _ready_slot(2, "augment_c", "强化 C"),
        ],
    }


class OverlayVisionStateTests(unittest.TestCase):
    def test_template_runtime_cache_hits_and_invalidates_by_signature(self):
        from hextech.overlay.vision import sidecar

        entry = sidecar.TemplateEntry(
            augment_id="a0",
            name="强化 1",
            tier="Gold",
            summary="test",
            fingerprint=(0.0, 1.0),
            icon_fingerprints=((0.0, 1.0),),
            name_fingerprint=(0.0, 1.0),
            name_fingerprint_alt=(1.0, 0.0),
        )

        def fake_rank(template_index):
            return sidecar._RankMatrices(
                template_index,
                (entry,),
                sidecar.np.asarray([[0.0, 1.0]], dtype=sidecar.np.float32),
                (entry,),
                sidecar.np.asarray([[0.0, 1.0]], dtype=sidecar.np.float32),
                (entry,),
                sidecar.np.asarray([[1.0, 0.0]], dtype=sidecar.np.float32),
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "overlay_vision" / "template_runtime_cache.v1.pkl"
            signature = {"schema_version": 1, "asset_digest": "a", "version_digest": "v"}
            updated_signature = {"schema_version": 1, "asset_digest": "b", "version_digest": "v"}

            with (
                mock.patch.object(sidecar, "load_default_template_index", return_value=[entry]) as load_index,
                mock.patch.object(sidecar, "_rank_matrices", side_effect=fake_rank) as rank,
            ):
                first = sidecar.load_or_build_default_template_runtime(
                    hint_cache={},
                    cache_file=cache_file,
                    resource_signature=signature,
                )
            self.assertFalse(first.stats["cache_hit"])
            self.assertEqual(load_index.call_count, 1)
            self.assertEqual(rank.call_count, 1)

            with (
                mock.patch.object(sidecar, "load_default_template_index", side_effect=AssertionError("cache hit should not rebuild")),
                mock.patch.object(sidecar, "_rank_matrices", side_effect=AssertionError("cache hit should not rebuild matrices")),
            ):
                second = sidecar.load_or_build_default_template_runtime(
                    hint_cache={},
                    cache_file=cache_file,
                    resource_signature=signature,
                )
            self.assertTrue(second.stats["cache_hit"])
            self.assertEqual(second.template_index[0].augment_id, "a0")

            with (
                mock.patch.object(sidecar, "load_default_template_index", return_value=[entry]) as load_after_change,
                mock.patch.object(sidecar, "_rank_matrices", side_effect=fake_rank),
            ):
                invalidated = sidecar.load_or_build_default_template_runtime(
                    hint_cache={},
                    cache_file=cache_file,
                    resource_signature=updated_signature,
                )
            self.assertFalse(invalidated.stats["cache_hit"])
            self.assertEqual(load_after_change.call_count, 1)

    def test_hover_occlusion_keeps_stable_slots_until_card_residue_clears(self):
        from hextech.overlay.vision.state import SelectionTracker

        tracker = SelectionTracker()
        tracker.update(_selection_event())
        stable = tracker.update(_selection_event())
        self.assertTrue(stable["active"])
        stable_ids = [slot["augment_id"] for slot in stable["slots"]]

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

        for _ in range(6):
            result = tracker.update(hover_event)
            self.assertTrue(result["active"])
            self.assertTrue(result["source"]["hover_occluded"])
            self.assertEqual([slot["augment_id"] for slot in result["slots"]], stable_ids)

        clear_event = dict(hover_event)
        clear_event["source"] = dict(hover_event["source"], card_residue=False, name_residue=[False, False, False])
        tracker.update(clear_event)
        exited = tracker.update(clear_event)

        self.assertFalse(exited["active"])
        self.assertEqual(exited["source"]["scene_state"], "absent")

    def test_hover_occlusion_expires_after_max_hold_frames(self):
        from hextech.overlay.vision.state import HOVER_OCCLUDED_MAX_HOLD_FRAMES, SelectionTracker

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

        for _ in range(HOVER_OCCLUDED_MAX_HOLD_FRAMES):
            held = tracker.update(hover_event)
            self.assertTrue(held["active"])
            self.assertEqual(held["source"]["reason"], "hover_occluded")

        expired = tracker.update(hover_event)

        self.assertFalse(expired["active"])
        self.assertEqual(expired["source"]["reason"], "hover_occluded_expired")

    def test_post_selection_cursor_residue_exits_without_long_hover_hold(self):
        from hextech.overlay.vision.state import RESIDUE_HOLD_FRAMES, SelectionTracker

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
                "name_residue": [True, True, False],
            }
        )
        clicked_event["_raw_slots"] = []

        results = [tracker.update(clicked_event) for _ in range(RESIDUE_HOLD_FRAMES + 1)]

        self.assertTrue(all(result["source"]["reason"] != "hover_occluded" for result in results))
        self.assertFalse(results[-1]["active"])
        self.assertEqual(results[-1]["source"]["reason"], "scene_residue_expired")

    def test_non_hover_residue_still_expires(self):
        from hextech.overlay.vision.state import SelectionTracker

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
        from hextech.overlay.vision import sidecar

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

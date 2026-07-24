"""测试 overlay vision 状态。

调用方: pytest; 关键依赖: hextech.infrastructure.vision.state。
"""
from __future__ import annotations

import unittest
from unittest import mock

from support.vision_events import ready_slot as _ready_slot
from support.vision_events import selection_event as _selection_event
from support.vision_events import weak_slot as _weak_slot


class OverlayVisionStateTests(unittest.TestCase):
    def test_single_slot_fails_after_three_seconds_and_can_recover(self):
        from hextech.infrastructure.vision import state

        tracker = state.SelectionTracker(scene_enter_frames=1)
        partial = _selection_event()
        partial["_raw_slots"][2] = {
            "slot": 2,
            "diagnostic": "confidence_below_threshold",
            "top_candidates": [{"augment_id": "maybe", "name": "候选", "confidence": 0.62}],
        }
        timeline = [10.0] * 3 + [13.1] * 3 + [13.2] * 3 + [13.3] * 3
        with mock.patch.object(state.time, "monotonic", side_effect=timeline):
            detecting = tracker.update(partial)
            failed = tracker.update(partial)
            tracker.update(_selection_event())
            recovered = tracker.update(_selection_event())

        self.assertEqual(detecting["slots"][2]["state"], "detecting")
        self.assertEqual(failed["slots"][2]["state"], "failed")
        self.assertEqual(failed["slots"][2]["rejection_reason"], "confidence_below_threshold")
        self.assertGreaterEqual(failed["slots"][2]["elapsed_seconds"], 3.0)
        self.assertEqual(recovered["slots"][2]["state"], "ready")

    def test_wobbling_candidates_also_fail_after_three_seconds(self):
        from hextech.infrastructure.vision import state

        tracker = state.SelectionTracker(scene_enter_frames=1)
        event_a = _selection_event()
        event_b = _selection_event()
        event_a["_raw_slots"][2] = _ready_slot(2, "augment_x", "候选 X")
        event_b["_raw_slots"][2] = _ready_slot(2, "augment_y", "候选 Y")
        timeline = [10.0] * 3 + [11.0] * 3 + [13.1] * 3

        with mock.patch.object(state.time, "monotonic", side_effect=timeline):
            tracker.update(event_a)
            tracker.update(event_b)
            failed = tracker.update(event_a)

        self.assertEqual(failed["slots"][2]["state"], "failed")
        self.assertEqual(failed["slots"][2]["candidate_identity"], "候选_x")
        self.assertGreaterEqual(failed["slots"][2]["elapsed_seconds"], 3.0)

    def test_strong_reroll_withdraws_old_slot_until_replacement_is_stable(self):
        from hextech.infrastructure.vision import state

        tracker = state.SelectionTracker(scene_enter_frames=1)
        initial = _selection_event()
        weak_x = _selection_event()
        weak_y = _selection_event()
        recovered_event = _selection_event()
        weak_x["_raw_slots"][2] = _weak_slot(2, "augment_x", "候选 X")
        weak_y["_raw_slots"][2] = _weak_slot(2, "augment_y", "候选 Y")
        recovered_event["_raw_slots"][2] = _ready_slot(2, "augment_z", "候选 Z")
        timeline = (
            [10.0] * 6
            + [11.0] * 3
            + [12.0] * 3
            + [14.1] * 3
            + [14.2] * 3
            + [14.3] * 3
        )

        with mock.patch.object(state.time, "monotonic", side_effect=timeline):
            tracker.update(initial)
            stable = tracker.update(initial)
            weak_started = tracker.update(weak_x)
            wobbling = tracker.update(weak_y)
            failed = tracker.update(weak_x)
            recovering = tracker.update(recovered_event)
            recovered = tracker.update(recovered_event)

        self.assertEqual(stable["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(weak_started["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(wobbling["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(failed["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(recovering["slots"][2]["state"], "detecting")
        self.assertEqual(recovering["slots"][2]["augment_id"], "")
        self.assertEqual(recovering["source"]["selection_revision"], 2)
        self.assertEqual(recovered["slots"][2]["state"], "ready")
        self.assertEqual(recovered["slots"][2]["augment_id"], "augment_z")
        self.assertEqual(recovered["source"]["selection_revision"], 2)
        self.assertEqual([slot["state"] for slot in recovered["slots"][:2]], ["ready", "ready"])

    def test_multi_slot_reroll_increments_revision_once_and_keeps_unchanged_slot(self):
        from hextech.infrastructure.vision import state

        tracker = state.SelectionTracker(scene_enter_frames=1)
        tracker.update(_selection_event())
        stable = tracker.update(_selection_event())
        reroll = _selection_event()
        reroll["_raw_slots"][0] = _ready_slot(0, "augment_x", "候选 X")
        reroll["_raw_slots"][2] = _ready_slot(2, "augment_z", "候选 Z")

        changing = tracker.update(reroll)
        ready = tracker.update(reroll)

        self.assertEqual(stable["source"]["selection_revision"], 1)
        self.assertEqual(changing["source"]["selection_revision"], 2)
        self.assertEqual([changing["slots"][index]["state"] for index in (0, 2)], ["detecting", "detecting"])
        self.assertEqual(changing["slots"][1]["augment_id"], "augment_b")
        self.assertEqual(ready["source"]["selection_revision"], 2)
        self.assertEqual(
            [slot["augment_id"] for slot in ready["slots"]],
            ["augment_x", "augment_b", "augment_z"],
        )

    def test_weak_temporal_evidence_never_becomes_ready(self):
        from hextech.infrastructure.vision import state

        tracker = state.SelectionTracker(scene_enter_frames=1)
        event = _selection_event()
        event["_raw_slots"][0] = _weak_slot(0, "weak", "弱候选")
        observed = [tracker.update(event) for _ in range(5)]

        self.assertTrue(all(item["slots"][0]["state"] != "ready" for item in observed))

    def test_same_name_variants_share_text_identity_and_require_icon_for_visual_id(self):
        from copy import deepcopy
        from PIL import Image, ImageDraw

        from hextech.infrastructure.vision import sidecar
        from hextech.infrastructure.vision.matcher import candidate_from_slot
        from hextech.infrastructure.vision.state import SelectionTracker

        def icon(shape: str) -> Image.Image:
            image = Image.new("RGB", (72, 72), "black")
            draw = ImageDraw.Draw(image)
            if shape == "gold":
                draw.rectangle((12, 12, 60, 60), fill="white")
            else:
                draw.ellipse((12, 12, 60, 60), fill="white")
            return image

        index = sidecar.build_template_index(
            {
                "special_glasscannon": {"name": "玻璃大炮", "tier": "黄金", "image": icon("gold")},
                "glasscannon": {"name": "玻璃大炮", "tier": "棱彩", "image": icon("prismatic")},
                "other": {"name": "其他强化", "tier": "黄金", "image": icon("other")},
            }
        )
        fingerprint = sidecar._name_fingerprint("玻璃大炮")
        self.assertIsNotNone(fingerprint)
        ranked = sidecar._rank_name_fingerprint(fingerprint, index, family="primary")
        self.assertEqual([entry.name for entry, _score in ranked].count("玻璃大炮"), 1)
        self.assertEqual(ranked[0][0].name, "玻璃大炮")

        text_candidate = {
            "augment_id": "special_glasscannon",
            "visual_variant_id": "special_glasscannon",
            "recognition_key": "玻璃大炮",
            "name_variant_count": 2,
            "name": "玻璃大炮",
            "tier": "黄金",
            "confidence": 0.91,
        }
        prismatic_icon = {
            **text_candidate,
            "augment_id": "glasscannon",
            "visual_variant_id": "glasscannon",
            "tier": "棱彩",
            "confidence": 0.90,
        }
        slot = {
            "slot": 0,
            "channels": {
                "text": {"margin": 0.05, "top_candidates": [text_candidate]},
                "text_alt": {"margin": 0.05, "top_candidates": [text_candidate]},
                "icon": {"margin": 0.03, "top_candidates": [prismatic_icon]},
            },
        }
        resolved = candidate_from_slot(slot)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.visual_variant_id, "glasscannon")
        self.assertEqual(resolved.tier, "棱彩")

        resolved_slot = deepcopy(slot)
        unresolved_slot = deepcopy(slot)
        unresolved_slot["channels"]["icon"]["margin"] = 0.001
        unresolved = candidate_from_slot(unresolved_slot)
        self.assertIsNotNone(unresolved)
        self.assertEqual(unresolved.name, "玻璃大炮")
        self.assertEqual(unresolved.visual_variant_id, "")
        self.assertEqual(unresolved.tier, "")

        base_event = _selection_event()
        base_event["_raw_slots"][0] = unresolved_slot
        tracker = SelectionTracker(scene_enter_frames=1)
        tracker.update(base_event)
        name_ready = tracker.update(base_event)
        self.assertEqual(name_ready["slots"][0]["name"], "玻璃大炮")
        self.assertEqual(name_ready["slots"][0]["visual_variant_id"], "")

        enriched_event = _selection_event()
        enriched_event["_raw_slots"][0] = resolved_slot
        enriched = tracker.update(enriched_event)
        self.assertEqual(enriched["slots"][0]["visual_variant_id"], "glasscannon")
        self.assertEqual(enriched["slots"][0]["tier"], "棱彩")
        self.assertEqual(enriched["source"]["selection_revision"], 1)

        unresolved_slot["channels"]["text"]["top_candidates"][0]["confidence"] = 0.85
        unresolved_slot["channels"]["text_alt"]["top_candidates"] = [
            {"name": "其他强化", "recognition_key": "其他强化", "confidence": 0.60}
        ]
        unresolved_slot["channels"]["text"]["margin"] = 0.013
        unresolved_slot["channels"]["text_alt"]["margin"] = 0.013
        self.assertIsNone(candidate_from_slot(unresolved_slot))

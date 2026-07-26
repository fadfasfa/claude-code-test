"""测试 overlay vision 状态。

调用方: pytest; 关键依赖: hextech.infrastructure.vision.state。
"""
from __future__ import annotations

from copy import deepcopy
import unittest
from unittest import mock

from support.vision_events import ready_slot as _ready_slot
from support.vision_events import medium_slot as _medium_slot
from support.vision_events import selection_event as _selection_event
from support.vision_events import weak_slot as _weak_slot


class OverlayVisionStateTests(unittest.TestCase):
    @staticmethod
    def _at(event: dict, timestamp: float) -> dict:
        timed = deepcopy(event)
        timed["timing"] = {
            "captured_at": timestamp - 0.1,
            "recognition_completed_at": timestamp,
        }
        return timed

    def test_single_slot_stays_detecting_after_three_seconds_and_can_recover(self):
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
            still_detecting = tracker.update(partial)
            tracker.update(_selection_event())
            recovered = tracker.update(_selection_event())

        self.assertEqual(detecting["slots"][2]["state"], "detecting")
        self.assertEqual(still_detecting["slots"][2]["state"], "detecting")
        self.assertEqual(still_detecting["slots"][2]["temporal_state"], "evidence_pending")
        self.assertEqual(still_detecting["slots"][2]["rejection_reason"], "confidence_below_threshold")
        self.assertEqual(recovered["slots"][2]["state"], "ready")

    def test_competing_strong_candidates_do_not_publish_a_false_ready(self):
        from hextech.infrastructure.vision import state

        tracker = state.SelectionTracker(scene_enter_frames=1)
        event_a = _selection_event()
        event_b = _selection_event()
        event_a["_raw_slots"][2] = _ready_slot(2, "augment_x", "候选 X")
        event_b["_raw_slots"][2] = _ready_slot(2, "augment_y", "候选 Y")
        timeline = [10.0, 11.0, 13.1]

        with mock.patch.object(state.time, "monotonic", side_effect=timeline):
            tracker.update(event_a)
            tracker.update(event_b)
            competing = tracker.update(event_a)

        self.assertEqual(competing["slots"][2]["state"], "detecting")
        self.assertEqual(competing["slots"][2]["candidate_identity"], "候选_x")
        self.assertEqual(competing["slots"][2]["evidence_hits"], 2)

    def test_strong_reroll_keeps_old_slot_until_replacement_is_stable(self):
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
            + [14.4] * 3
        )

        with mock.patch.object(state.time, "monotonic", side_effect=timeline):
            tracker.update(initial)
            stable = tracker.update(initial)
            weak_started = tracker.update(weak_x)
            wobbling = tracker.update(weak_y)
            failed = tracker.update(weak_x)
            recovering = tracker.update(recovered_event)
            tracker.update(recovered_event)
            recovered = tracker.update(recovered_event)

        self.assertEqual(stable["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(weak_started["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(wobbling["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(failed["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(recovering["slots"][2]["state"], "ready")
        self.assertEqual(recovering["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(recovering["source"]["selection_revision"], 1)
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
        still_changing = tracker.update(reroll)
        ready = tracker.update(reroll)

        self.assertEqual(stable["source"]["selection_revision"], 1)
        self.assertEqual(changing["source"]["selection_revision"], 1)
        self.assertEqual(
            [changing["slots"][index]["augment_id"] for index in (0, 2)],
            ["augment_a", "augment_c"],
        )
        self.assertEqual(changing["slots"][1]["augment_id"], "augment_b")
        self.assertEqual(still_changing["source"]["selection_revision"], 1)
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

    def test_strong_evidence_confirms_two_of_three_with_a_miss(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        strong = _selection_event()
        miss = _selection_event()
        miss["_raw_slots"][0] = _weak_slot(0, "weak", "弱候选")

        first = tracker.update(self._at(strong, 10.0))
        interrupted = tracker.update(self._at(miss, 10.5))
        ready = tracker.update(self._at(strong, 11.0))

        self.assertEqual(first["slots"][0]["state"], "detecting")
        self.assertEqual(interrupted["slots"][0]["state"], "detecting")
        self.assertEqual(ready["slots"][0]["state"], "ready")
        self.assertEqual(ready["slots"][0]["evidence_hits"], 2)
        self.assertEqual(ready["slots"][0]["replacement_reason"], "initial_strong")

    def test_medium_evidence_confirms_three_of_five_with_a_miss(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        medium = _selection_event()
        medium["_raw_slots"][0] = _medium_slot(0, "medium", "普通候选")
        miss = _selection_event()
        miss["_raw_slots"][0] = _weak_slot(0, "weak", "弱候选")

        tracker.update(self._at(medium, 20.0))
        tracker.update(self._at(miss, 20.4))
        pending = tracker.update(self._at(medium, 20.8))
        ready = tracker.update(self._at(medium, 21.2))

        self.assertEqual(pending["slots"][0]["state"], "detecting")
        self.assertEqual(pending["slots"][0]["evidence_hits"], 2)
        self.assertEqual(ready["slots"][0]["state"], "ready")
        self.assertEqual(ready["slots"][0]["evidence_hits"], 3)
        self.assertEqual(ready["slots"][0]["replacement_reason"], "initial_medium")

    def test_medium_evidence_cannot_skip_consecutive_misses_inside_raw_window(self):
        """miss 必须占据 5 帧窗口，不能让离散 medium 跨空帧直接 ready。"""

        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        medium = _selection_event()
        medium["_raw_slots"][0] = _medium_slot(0, "medium", "普通候选")
        miss = _selection_event()
        miss["_raw_slots"][0] = _weak_slot(0, "weak", "弱候选")

        tracker.update(self._at(medium, 25.0))
        tracker.update(self._at(miss, 25.2))
        tracker.update(self._at(miss, 25.4))
        tracker.update(self._at(miss, 25.6))
        tracker.update(self._at(medium, 25.8))
        pending = tracker.update(self._at(medium, 26.0))

        self.assertEqual(pending["slots"][0]["state"], "detecting")
        self.assertEqual(pending["slots"][0]["evidence_hits"], 2)
        self.assertEqual(pending["slots"][0]["evidence_window"], 5)

    def test_evidence_older_than_time_window_does_not_confirm(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        strong = _selection_event()
        miss = _selection_event()
        miss["_raw_slots"][0] = _weak_slot(0, "weak", "弱候选")

        tracker.update(self._at(strong, 30.0))
        tracker.update(self._at(miss, 37.0))
        expired = tracker.update(self._at(strong, 37.1))

        self.assertEqual(expired["slots"][0]["state"], "detecting")
        self.assertEqual(expired["slots"][0]["evidence_hits"], 1)
        self.assertEqual(expired["slots"][0]["evidence_window"], 2)

    def test_identity_churn_becomes_evidence_starved_without_clearing_ready_slots(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        observed = None
        for index, timestamp in enumerate((40.0, 40.5, 41.0, 41.5, 42.1), start=1):
            event = _selection_event()
            event["_raw_slots"][1] = _medium_slot(1, f"churn-{index}", f"跳变候选 {index}")
            observed = tracker.update(self._at(event, timestamp))

        self.assertIsNotNone(observed)
        self.assertEqual(observed["slots"][1]["state"], "detecting")
        self.assertEqual(observed["slots"][1]["temporal_state"], "evidence_starved")
        self.assertEqual(observed["slots"][1]["diagnostic"], "evidence_starved")
        self.assertEqual([observed["slots"][index]["state"] for index in (0, 2)], ["ready", "ready"])

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

    def test_medium_wrong_name_never_flashes_before_strong_correct_candidate(self):
        """固化真机“不动如山 → 吞噬灵魂”序列，弱相关重复不能直接上屏。"""

        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        wrong = _selection_event()
        wrong["_raw_slots"][1] = _medium_slot(1, "immovable", "不动如山")
        first = tracker.update(wrong)
        second = tracker.update(wrong)

        correct = _selection_event()
        correct["_raw_slots"][1] = _ready_slot(1, "consume_soul", "吞噬灵魂")
        confirming = tracker.update(correct)
        ready = tracker.update(correct)

        self.assertNotEqual(first["slots"][1].get("name"), "不动如山")
        self.assertNotEqual(second["slots"][1].get("name"), "不动如山")
        self.assertNotEqual(confirming["slots"][1].get("name"), "吞噬灵魂")
        self.assertEqual(ready["slots"][1]["name"], "吞噬灵魂")
        self.assertEqual(ready["slots"][1]["evidence_grade"], "strong")
        self.assertEqual(ready["source"]["selection_revision"], 1)

    def test_repeated_single_font_observations_never_become_ready(self):
        from hextech.infrastructure.vision.matcher import candidate_from_slot
        from hextech.infrastructure.vision.state import SelectionTracker

        single_font = {
            "slot": 0,
            "channels": {
                "text": {
                    "margin": 0.40,
                    "top_candidates": [{"augment_id": "wrong", "name": "单字体误匹配", "confidence": 0.99}],
                }
            },
        }
        self.assertIsNone(candidate_from_slot(single_font))

        tracker = SelectionTracker(scene_enter_frames=1)
        event = _selection_event()
        event["_raw_slots"][0] = single_font
        observed = [tracker.update(self._at(event, 60.0 + index * 0.6)) for index in range(5)]

        self.assertTrue(all(item["slots"][0]["state"] == "detecting" for item in observed))
        self.assertEqual(observed[-1]["slots"][0]["temporal_state"], "evidence_starved")

    def test_medium_observation_cannot_replace_a_stable_slot(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        original = _selection_event()
        tracker.update(self._at(original, 70.0))
        stable = tracker.update(self._at(original, 70.4))
        replacement = _selection_event()
        replacement["_raw_slots"][0] = _medium_slot(0, "wrong", "重复双字体误匹配")

        observed = [tracker.update(self._at(replacement, 71.0 + index * 0.4)) for index in range(5)]

        self.assertEqual(stable["slots"][0]["augment_id"], "augment_a")
        self.assertTrue(all(item["slots"][0]["augment_id"] == "augment_a" for item in observed))
        self.assertTrue(all(item["source"]["selection_revision"] == 1 for item in observed))

    def test_transient_pause_preserves_epoch_revision_and_stable_slots(self):
        from hextech.infrastructure.vision.state import SelectionTracker

        tracker = SelectionTracker(scene_enter_frames=1)
        tracker.update(self._at(_selection_event(), 80.0))
        stable = tracker.update(self._at(_selection_event(), 80.4))
        paused = tracker.pause("game_not_foreground")
        recovered = tracker.update(self._at(_selection_event(), 81.0))

        self.assertFalse(paused["active"])
        self.assertTrue(paused["source"]["transient_pause"])
        self.assertEqual(paused["source"]["selection_epoch"], stable["source"]["selection_epoch"])
        self.assertEqual(paused["source"]["selection_revision"], stable["source"]["selection_revision"])
        self.assertEqual([slot["augment_id"] for slot in paused["slots"]], ["augment_a", "augment_b", "augment_c"])
        self.assertEqual(recovered["source"]["selection_epoch"], stable["source"]["selection_epoch"])
        self.assertEqual(recovered["source"]["selection_revision"], stable["source"]["selection_revision"])

    def test_gameflow_end_keeps_epoch_revision_and_identity_for_report_linkage(self):
        from hextech.infrastructure.vision.gameflow_pause import PausedGameflowProbe, resolve_game_visibility_pause
        from hextech.infrastructure.vision.state import SelectionTracker
        from hextech.modules.vision.gameflow import GameflowState

        tracker = SelectionTracker(scene_enter_frames=1)
        tracker.update(self._at(_selection_event(), 90.0))
        stable = tracker.update(self._at(_selection_event(), 90.4))
        ended, gameflow_ended = resolve_game_visibility_pause(
            tracker,
            reason="game_not_foreground",
            should_probe=True,
            now=91.0,
            gameflow_probe=PausedGameflowProbe(
                probe=lambda: GameflowState.NOT_IN_PROGRESS,
                spawn=lambda target: target(),
            ),
            identity_source={"session_id": "game-session-a", "game_instance_id": "game-instance-a"},
        )

        self.assertTrue(gameflow_ended)
        self.assertEqual(ended["source"]["reason"], "gameflow_ended")
        self.assertEqual(ended["source"]["selection_epoch"], stable["source"]["selection_epoch"])
        self.assertEqual(ended["source"]["selection_revision"], stable["source"]["selection_revision"])
        self.assertEqual(ended["source"]["session_id"], "game-session-a")
        self.assertEqual(ended["source"]["game_instance_id"], "game-instance-a")

    def test_paused_gameflow_probe_never_blocks_the_recognition_loop(self):
        from hextech.infrastructure.vision.gameflow_pause import PausedGameflowProbe
        from hextech.modules.vision.gameflow import GameflowState

        pending: list = []
        probe = PausedGameflowProbe(
            probe=lambda: GameflowState.NOT_IN_PROGRESS,
            spawn=pending.append,  # 捕获而不执行，模拟慢速 HTTP 仍在途
        )

        # 首次调用只触发探测并立即返回缓存的 unknown，不等待结果。
        self.assertIs(probe.observe(should_probe=True, now=10.0), GameflowState.UNKNOWN)
        self.assertEqual(len(pending), 1)
        # 在途期间的后续调用不重复起探测。
        self.assertIs(probe.observe(should_probe=True, now=12.0), GameflowState.UNKNOWN)
        self.assertEqual(len(pending), 1)

        pending[0]()  # 慢探测完成
        self.assertIs(probe.observe(should_probe=True, now=12.1), GameflowState.NOT_IN_PROGRESS)

    def test_paused_gameflow_probe_reset_discards_inflight_result(self):
        from hextech.infrastructure.vision.gameflow_pause import PausedGameflowProbe
        from hextech.modules.vision.gameflow import GameflowState

        pending: list = []
        probe = PausedGameflowProbe(
            probe=lambda: GameflowState.NOT_IN_PROGRESS,
            spawn=pending.append,
        )
        probe.observe(should_probe=True, now=10.0)
        probe.reset()  # 返回前台/换局：上一局的在途结论必须作废
        pending[0]()

        self.assertIs(probe.observe(should_probe=False, now=10.5), GameflowState.UNKNOWN)
        self.assertIs(probe.state, GameflowState.UNKNOWN)

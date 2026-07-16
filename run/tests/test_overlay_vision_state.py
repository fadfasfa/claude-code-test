"""测试 overlay vision 状态。

调用方: pytest; 关键依赖: hextech.overlay.vision.state。
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
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


def _weak_slot(slot: int, augment_id: str, name: str) -> dict:
    candidate = {
        "augment_id": augment_id,
        "name": name,
        "tier": "Gold",
        "confidence": 0.69,
    }
    return {
        "slot": slot,
        "channels": {
            "text": {
                "margin": 0.015,
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
    def test_single_slot_fails_after_three_seconds_and_can_recover(self):
        from hextech.overlay.vision import state

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
        from hextech.overlay.vision import state

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
        self.assertEqual(failed["slots"][2]["candidate_identity"], "augment_x")
        self.assertGreaterEqual(failed["slots"][2]["elapsed_seconds"], 3.0)

    def test_weak_reroll_keeps_old_slot_until_replacement_is_stable(self):
        from hextech.overlay.vision import state

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
        self.assertEqual(recovering["slots"][2]["augment_id"], "augment_c")
        self.assertEqual(recovered["slots"][2]["state"], "ready")
        self.assertEqual(recovered["slots"][2]["augment_id"], "augment_z")
        self.assertEqual([slot["state"] for slot in recovered["slots"][:2]], ["ready", "ready"])

    def test_sidecar_template_runtime_entrypoint_delegates_to_cache_module(self):
        from hextech.overlay.vision import sidecar, template_runtime

        expected = object()
        with mock.patch.object(template_runtime, "load_or_build_default_template_runtime", return_value=expected) as delegated:
            result = sidecar.load_or_build_default_template_runtime(
                hint_cache={"schema_version": 1},
                cache_file="cache.npz",
                resource_signature={"schema_version": 2},
            )

        self.assertIs(result, expected)
        delegated.assert_called_once_with(
            base_dir=None,
            hint_cache={"schema_version": 1},
            cache_file="cache.npz",
            resource_signature={"schema_version": 2},
            status_callback=None,
        )

    def test_template_resource_digest_normalizes_windows_path_case_and_separators(self):
        from hextech.overlay.vision import template_runtime

        stat = SimpleNamespace(st_size=42, st_mtime_ns=123456)

        class FakePath:
            def __init__(self, value: str):
                self.value = value

            def __str__(self):
                return self.value

            def stat(self):
                return stat

        lower_slash = template_runtime._hash_runtime_resource_stats([FakePath(r"c:\hextech\data\icon.png")])
        upper_forward = template_runtime._hash_runtime_resource_stats([FakePath("C:/HEXTECH/DATA/ICON.PNG")])

        self.assertEqual(lower_slash, upper_forward)

    def test_template_runtime_cache_hits_and_invalidates_by_signature(self):
        from hextech.overlay.vision import sidecar, template_runtime

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
            return template_runtime._RankMatrices(
                template_index,
                (entry,),
                template_runtime.np.asarray([[0.0, 1.0]], dtype=template_runtime.np.float32),
                (entry,),
                template_runtime.np.asarray([[0.0, 1.0]], dtype=template_runtime.np.float32),
                (entry,),
                template_runtime.np.asarray([[1.0, 0.0]], dtype=template_runtime.np.float32),
            )

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "overlay_vision" / "template_runtime_cache.v1.npz"
            signature = {"schema_version": 1, "asset_digest": "a", "version_digest": "v"}
            updated_signature = {"schema_version": 1, "asset_digest": "b", "version_digest": "v"}

            with (
                mock.patch.object(template_runtime, "load_default_template_index", return_value=[entry]) as load_index,
                mock.patch.object(template_runtime, "_rank_matrices", side_effect=fake_rank) as rank,
            ):
                first = template_runtime.load_or_build_default_template_runtime(
                    hint_cache={},
                    cache_file=cache_file,
                    resource_signature=signature,
                )
            self.assertFalse(first.stats["cache_hit"])
            self.assertEqual(load_index.call_count, 1)
            self.assertEqual(rank.call_count, 1)

            with (
                mock.patch.object(template_runtime, "load_default_template_index", side_effect=AssertionError("cache hit should not rebuild")),
                mock.patch.object(template_runtime, "_rank_matrices", side_effect=AssertionError("cache hit should not rebuild matrices")),
            ):
                second = template_runtime.load_or_build_default_template_runtime(
                    hint_cache={},
                    cache_file=cache_file,
                    resource_signature=signature,
                )
            self.assertTrue(second.stats["cache_hit"])
            self.assertEqual(second.template_index[0].augment_id, "a0")

            with (
                mock.patch.object(template_runtime, "load_default_template_index", return_value=[entry]) as load_after_change,
                mock.patch.object(template_runtime, "_rank_matrices", side_effect=fake_rank),
            ):
                invalidated = template_runtime.load_or_build_default_template_runtime(
                    hint_cache={},
                    cache_file=cache_file,
                    resource_signature=updated_signature,
                )
            self.assertFalse(invalidated.stats["cache_hit"])
            self.assertEqual(load_after_change.call_count, 1)

    def test_template_hint_signature_ignores_stats_generation_fields(self):
        from hextech.overlay.vision import template_runtime

        first = {
            "schema_version": 1,
            "generated_at": 1,
            "snapshot": {"generation_id": "g1"},
            "hints": {"1027": {"augment_id": "1027", "name": "大地苏醒", "winrate": 0.51}},
            "name_index": {"aram_earthwake": "1027"},
        }
        second = {
            "schema_version": 1,
            "generated_at": 2,
            "snapshot": {"generation_id": "g2"},
            "hints": {"1027": {"augment_id": "1027", "name": "大地苏醒", "winrate": 0.62}},
            "name_index": {"aram_earthwake": "1027"},
        }

        self.assertEqual(
            template_runtime.template_runtime_hint_signature(first),
            template_runtime.template_runtime_hint_signature(second),
        )

    def test_concurrent_template_runtime_miss_builds_once(self):
        from hextech.overlay.vision import sidecar, template_runtime

        entry = sidecar.TemplateEntry(
            augment_id="a0",
            name="强化 1",
            tier="Gold",
            summary="test",
            fingerprint=(0.0, 1.0),
            icon_fingerprints=((0.0, 1.0),),
        )
        build_count = 0
        build_guard = threading.Lock()

        def slow_load(*_args, **_kwargs):
            nonlocal build_count
            with build_guard:
                build_count += 1
            time.sleep(0.1)
            return [entry]

        def fake_rank(template_index):
            empty = template_runtime.np.empty((0, 0), dtype=template_runtime.np.float32)
            return template_runtime._RankMatrices(template_index, (), empty, (), empty, (), empty)

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "overlay_vision" / "template_runtime_cache.v2.npz"
            signature = {"schema_version": 2, "asset_digest": "a", "version_digest": "v"}
            with (
                mock.patch.object(template_runtime, "load_default_template_index", side_effect=slow_load),
                mock.patch.object(template_runtime, "_rank_matrices", side_effect=fake_rank),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [
                    executor.submit(
                        template_runtime.load_or_build_default_template_runtime,
                        hint_cache={},
                        cache_file=cache_file,
                        resource_signature=signature,
                    )
                    for _ in range(2)
                ]
                runtimes = [future.result(timeout=5) for future in futures]

        self.assertEqual(build_count, 1)
        self.assertEqual(sum(not runtime.stats["cache_hit"] for runtime in runtimes), 1)

    def test_runner_uses_lcu_session_id_for_vision_event(self):
        import json

        from PIL import Image

        from hextech.overlay.vision import runner, sidecar

        class StopLoop(RuntimeError):
            pass

        class FakeSource:
            def read_hint_cache(self):
                return {"schema_version": 1, "hints": {}, "name_index": {}}

            def read_context(self):
                return {"session_id": "lcu-session-1"}

        runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
        frame = Image.new("RGB", (1920, 1080), "black")
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            with (
                mock.patch.object(runner, "SharedOverlayDataSource", return_value=FakeSource()),
                mock.patch.object(runner, "load_or_build_default_template_runtime", return_value=runtime),
                mock.patch.object(sidecar, "_set_dpi_awareness"),
                mock.patch.object(sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 1920, 1080))),
                mock.patch.object(sidecar, "_is_lol_game_foreground", return_value=True),
                mock.patch.object(sidecar, "is_scoreboard_key_down", return_value=False),
                mock.patch.object(sidecar, "_capture_lol_game_rect", return_value=frame),
                mock.patch.object(sidecar, "detect_overlay_choices", return_value=_selection_event()),
                mock.patch.object(sidecar, "_window_dpi_scale", return_value=1.25),
                mock.patch.object(sidecar, "_cursor_over_card_panels", return_value=False),
                mock.patch.object(runner.time, "sleep", side_effect=StopLoop),
            ):
                with self.assertRaises(StopLoop):
                    runner.run_loop(write_event=True, event_path=event_path, required_frames=1)

            payload = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"]["session_id"], "lcu-session-1")
            self.assertEqual(payload["source"]["window_hwnd"], 123)
            self.assertEqual(payload["source"]["capture_size"], [1920, 1080])
            self.assertEqual(payload["source"]["dpi_scale"], 1.25)

    def test_hover_occlusion_keeps_stable_slots_until_cursor_leaves(self):
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
        from hextech.overlay.vision.state import HOVER_HOLD_FRAMES, SelectionTracker

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
        from hextech.overlay.vision.state import SelectionTracker

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

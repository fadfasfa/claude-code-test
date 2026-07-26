"""测试 overlay vision 状态。

调用方: pytest; 关键依赖: hextech.infrastructure.vision.state。
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest import mock

from support.vision_events import selection_event as _selection_event

class OverlayVisionTemplateRuntimeTests(unittest.TestCase):
    def test_visibility_pauses_write_timeline_timing_without_faking_recognition(self):
        from hextech.infrastructure.vision import runner, sidecar

        class StopLoop(RuntimeError):
            pass

        class FakeClock:
            def __init__(self) -> None:
                self.wall = 100.0
                self.sleeps = 0

            def time(self) -> float:
                self.wall += 0.1
                return self.wall

            def perf_counter(self) -> float:
                return self.wall

            def monotonic(self) -> float:
                return self.wall

            def sleep(self, _seconds: float) -> None:
                self.sleeps += 1
                if self.sleeps >= 3:
                    raise StopLoop()

        class FakeSource:
            def read_hint_cache(self):
                return {"schema_version": 1, "hints": {}, "name_index": {}}

        clock = FakeClock()
        runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
        target = (123, (0, 0, 1920, 1080))
        recorded_events: list[dict[str, Any]] = []

        def record_timeline(event: Mapping[str, Any], _trace_path: Path) -> None:
            recorded_events.append(dict(event))

        with (
            mock.patch.object(runner, "SharedOverlayDataSource", return_value=FakeSource()),
            mock.patch.object(runner, "load_or_build_default_template_runtime", return_value=runtime),
            mock.patch.object(runner, "_prepare_compute_runtime"),
            mock.patch.object(runner, "_write_sidecar_status"),
            mock.patch.object(runner, "_write_sidecar_ready_from_env"),
            mock.patch.object(runner, "time", clock),
            mock.patch.object(sidecar, "write_selection_timeline_observation", side_effect=record_timeline),
            mock.patch.object(sidecar, "_set_dpi_awareness"),
            mock.patch.object(sidecar, "_find_lol_game_window", side_effect=[None, target, target]),
            mock.patch.object(sidecar, "_is_lol_game_foreground", side_effect=[False, True]),
            mock.patch.object(sidecar, "is_scoreboard_key_down", return_value=True),
            mock.patch.object(sidecar, "is_left_mouse_button_down", return_value=False),
            mock.patch.object(runner, "game_window_identity", return_value={"game_instance_id": "game-1"}),
        ):
            with self.assertRaises(StopLoop):
                runner.run_loop(required_frames=1)

        self.assertEqual(
            [str(event["source"]["reason"]) for event in recorded_events],
            ["game_window_missing", "game_not_foreground", "scoreboard_key_down"],
        )
        for event in recorded_events:
            timing = event["timing"]
            self.assertEqual(timing["observation_kind"], "visibility_probe")
            self.assertEqual(timing["capture_status"], "not_captured")
            self.assertGreater(float(timing["capture_started_at"]), 0.0)
            self.assertEqual(timing["capture_started_at"], timing["captured_at"])
            self.assertEqual(timing["captured_at"], timing["recognition_completed_at"])

    def test_sidecar_template_runtime_entrypoint_delegates_to_cache_module(self):
        from hextech.infrastructure.vision import sidecar, template_runtime

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

    def test_source_sidecar_entry_configures_lcu_scanner_for_pause_probe(self):
        from hextech.infrastructure.vision import sidecar

        scanner = object()
        with (
            mock.patch("hextech.infrastructure.lcu.official_overlay.scan_lcu_process", scanner),
            mock.patch("hextech.modules.vision.gameflow.configure_lcu_scanner") as configure_scanner,
        ):
            sidecar._configure_sidecar_gameflow_scanner()

        configure_scanner.assert_called_once_with(scanner)

    def test_template_resource_digest_normalizes_windows_path_case_and_separators(self):
        from hextech.infrastructure.vision import template_runtime

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
        from hextech.infrastructure.vision import sidecar, template_runtime

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
                (entry,),
                template_runtime.np.asarray([[0.0, 1.0]], dtype=template_runtime.np.float32),
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
        from hextech.infrastructure.vision import template_runtime

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
        from hextech.infrastructure.vision import sidecar, template_runtime

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
            return template_runtime._RankMatrices(
                template_index,
                (),
                empty,
                (),
                empty,
                (),
                empty,
                (),
                empty,
            )

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

    def test_runner_uses_window_identity_instead_of_lcu_session_id(self):
        import json

        from PIL import Image

        from hextech.infrastructure.vision import runner, sidecar

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
                mock.patch.object(runner, "_prepare_compute_runtime"),
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
            self.assertNotEqual(payload["source"]["session_id"], "lcu-session-1")
            self.assertEqual(payload["source"]["session_id"], payload["source"]["game_instance_id"])
            self.assertEqual(payload["source"]["window_hwnd"], 123)
            self.assertEqual(payload["source"]["window_hwnd"], 123)
            self.assertEqual(payload["source"]["capture_size"], [1920, 1080])
            self.assertEqual(payload["source"]["dpi_scale"], 1.25)

    def test_runner_alt_tab_pause_preserves_the_same_selection_epoch(self):
        import json

        from PIL import Image

        from hextech.infrastructure.vision import runner, sidecar
        from hextech.modules.vision.gameflow import GameflowState

        class StopLoop(RuntimeError):
            pass

        class FakeSource:
            def read_hint_cache(self):
                return {"schema_version": 1, "hints": {}, "name_index": {}}

        sleep_calls = 0

        def stop_after_three_sleeps(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:
                raise StopLoop()

        runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
        frame = Image.new("RGB", (1920, 1080), "black")
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            with (
                mock.patch.object(runner, "SharedOverlayDataSource", return_value=FakeSource()),
                mock.patch.object(runner, "load_or_build_default_template_runtime", return_value=runtime),
                mock.patch.object(runner, "_prepare_compute_runtime"),
                mock.patch.object(runner, "game_window_identity", return_value={"game_instance_id": "game-1"}),
                mock.patch.object(runner, "probe_gameflow_state", return_value=GameflowState.UNKNOWN),
                mock.patch.object(sidecar, "_set_dpi_awareness"),
                mock.patch.object(sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 1920, 1080))),
                mock.patch.object(sidecar, "_is_lol_game_foreground", side_effect=[True, False, True]),
                mock.patch.object(sidecar, "is_scoreboard_key_down", return_value=False),
                mock.patch.object(sidecar, "_capture_lol_game_rect", return_value=frame),
                mock.patch.object(sidecar, "detect_overlay_choices", side_effect=[_selection_event(), _selection_event()]),
                mock.patch.object(sidecar, "_window_dpi_scale", return_value=1.0),
                mock.patch.object(sidecar, "_cursor_over_card_slots", return_value=[]),
                mock.patch.object(sidecar, "is_left_mouse_button_down", return_value=False),
                mock.patch.object(runner.time, "sleep", side_effect=stop_after_three_sleeps),
            ):
                with self.assertRaises(StopLoop):
                    runner.run_loop(write_event=True, event_path=event_path, required_frames=1)

            payload = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["source"]["selection_epoch"], 1)
        self.assertEqual(payload["source"]["selection_revision"], 1)
        self.assertTrue(payload["active"])
        self.assertEqual([slot["state"] for slot in payload["slots"]], ["ready", "ready", "ready"])

    def test_confirmed_gameflow_end_resets_same_instance_before_next_selection(self):
        """同一游戏进程结束一局后，下一局不得复用上一局的稳定海克斯。"""

        from PIL import Image

        from hextech.infrastructure.vision import runner, sidecar
        from hextech.modules.vision.gameflow import GameflowState

        class StopLoop(RuntimeError):
            pass

        class FakeSource:
            def read_hint_cache(self):
                return {"schema_version": 1, "hints": {}, "name_index": {}}

        sleep_calls = 0

        def stop_after_four_sleeps(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 4:
                raise StopLoop()

        tracker = runner.SelectionTracker(scene_enter_frames=1)
        completed_events: list[dict[str, Any]] = []
        original_complete = tracker.complete

        def record_complete(reason: str, *, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
            event = original_complete(reason, source=source)
            completed_events.append(event)
            return event

        tracker.complete = record_complete  # type: ignore[method-assign]
        runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
        frame = Image.new("RGB", (1920, 1080), "black")
        target = (123, (0, 0, 1920, 1080))
        with (
            mock.patch.object(runner, "SharedOverlayDataSource", return_value=FakeSource()),
            mock.patch.object(runner, "load_or_build_default_template_runtime", return_value=runtime),
            mock.patch.object(runner, "_prepare_compute_runtime"),
            mock.patch.object(runner, "SelectionTracker", return_value=tracker),
            mock.patch.object(runner, "game_window_identity", return_value={"game_instance_id": "game-1"}),
            mock.patch.object(runner, "probe_gameflow_state", return_value=GameflowState.NOT_IN_PROGRESS) as gameflow_probe,
            mock.patch.object(runner, "_write_sidecar_status"),
            mock.patch.object(runner, "_write_sidecar_ready_from_env"),
            mock.patch.object(sidecar, "_set_dpi_awareness"),
            mock.patch.object(sidecar, "_find_lol_game_window", side_effect=[target, target, None, target]),
            mock.patch.object(sidecar, "_is_lol_game_foreground", return_value=True),
            mock.patch.object(sidecar, "is_scoreboard_key_down", return_value=False),
            mock.patch.object(sidecar, "_capture_lol_game_rect", return_value=frame),
            mock.patch.object(sidecar, "detect_overlay_choices", return_value=_selection_event()),
            mock.patch.object(sidecar, "_window_dpi_scale", return_value=1.0),
            mock.patch.object(sidecar, "_cursor_over_card_slots", return_value=[]),
            mock.patch.object(sidecar, "is_left_mouse_button_down", return_value=False),
            mock.patch.object(runner.time, "sleep", side_effect=stop_after_four_sleeps),
        ):
            with self.assertRaises(StopLoop):
                runner.run_loop(required_frames=1)

        gameflow_probe.assert_called_once_with()
        assert len(completed_events) == 1
        assert completed_events[0]["source"]["reason"] == "gameflow_ended"
        assert completed_events[0]["source"]["selection_epoch"] == 1
        assert completed_events[0]["source"]["selection_revision"] == 1
        self.assertEqual(tracker.epoch, 2)
        self.assertEqual(tracker.selection_revision, 1)
        self.assertTrue(tracker.scene_active)
        self.assertTrue(all(slot.stable_slot is None for slot in tracker.slots))

    def test_alt_tab_resets_capture_failure_streak_before_returning_to_game(self):
        import json

        from hextech.infrastructure.vision import runner, sidecar

        class StopLoop(RuntimeError):
            pass

        class FakeClock:
            def __init__(self) -> None:
                self.wall = 0.0
                self.sleeps = 0

            def time(self) -> float:
                self.wall += 1.0
                return self.wall

            def perf_counter(self) -> float:
                return self.wall

            def monotonic(self) -> float:
                return self.wall

            def sleep(self, _seconds: float) -> None:
                self.sleeps += 1
                if self.sleeps >= 3:
                    raise StopLoop()

        class FakeSource:
            def read_hint_cache(self):
                return {"schema_version": 1, "hints": {}, "name_index": {}}

        runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            with (
                mock.patch.object(runner, "SharedOverlayDataSource", return_value=FakeSource()),
                mock.patch.object(runner, "load_or_build_default_template_runtime", return_value=runtime),
                mock.patch.object(runner, "_prepare_compute_runtime"),
                mock.patch.object(runner, "_write_sidecar_status"),
                mock.patch.object(runner, "_write_sidecar_ready_from_env"),
                mock.patch.object(runner, "game_window_identity", return_value={"game_instance_id": "game-1"}),
                mock.patch.object(runner, "time", clock),
                mock.patch.object(sidecar, "_set_dpi_awareness"),
                mock.patch.object(sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 1920, 1080))),
                mock.patch.object(sidecar, "_is_lol_game_foreground", side_effect=[True, False, True]),
                mock.patch.object(sidecar, "is_scoreboard_key_down", return_value=False),
                mock.patch.object(sidecar, "_capture_lol_game_rect", return_value=None),
                mock.patch.object(sidecar, "is_left_mouse_button_down", return_value=False),
            ):
                with self.assertRaises(StopLoop):
                    runner.run_loop(write_event=True, event_path=event_path, required_frames=1)

            payload = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["source"]["reason"], "capture_unavailable")
        self.assertTrue(payload["source"]["transient_pause"])
        self.assertNotIn("error", payload)
        self.assertEqual(payload["timing"]["observation_kind"], "capture_failure")
        self.assertEqual(payload["timing"]["capture_status"], "unavailable")

    def test_persistent_client_size_mismatch_becomes_capture_failure(self):
        import json

        from PIL import Image

        from hextech.infrastructure.vision import runner, sidecar

        class StopLoop(RuntimeError):
            pass

        class FakeClock:
            def __init__(self) -> None:
                self.wall = 0.0
                self.sleeps = 0

            def time(self) -> float:
                self.wall += 2.0
                return self.wall

            def perf_counter(self) -> float:
                return self.wall

            def monotonic(self) -> float:
                return self.wall

            def sleep(self, _seconds: float) -> None:
                self.sleeps += 1
                if self.sleeps >= 2:
                    raise StopLoop()

        class FakeSource:
            def read_hint_cache(self):
                return {"schema_version": 1, "hints": {}, "name_index": {}}

        runtime = SimpleNamespace(template_index=[object()], stats={"cache_hit": True})
        clock = FakeClock()
        mismatched_frame = Image.new("RGB", (1280, 720), "black")
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            with (
                mock.patch.object(runner, "SharedOverlayDataSource", return_value=FakeSource()),
                mock.patch.object(runner, "load_or_build_default_template_runtime", return_value=runtime),
                mock.patch.object(runner, "_prepare_compute_runtime"),
                mock.patch.object(runner, "_write_sidecar_status"),
                mock.patch.object(runner, "_write_sidecar_ready_from_env"),
                mock.patch.object(runner, "game_window_identity", return_value={"game_instance_id": "game-1"}),
                mock.patch.object(runner, "time", clock),
                mock.patch.object(sidecar, "_set_dpi_awareness"),
                mock.patch.object(sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 1920, 1080))),
                mock.patch.object(sidecar, "_is_lol_game_foreground", return_value=True),
                mock.patch.object(sidecar, "is_scoreboard_key_down", return_value=False),
                mock.patch.object(sidecar, "_capture_lol_game_rect", return_value=mismatched_frame),
                mock.patch.object(sidecar, "_window_dpi_scale", return_value=1.0),
            ):
                with self.assertRaises(StopLoop):
                    runner.run_loop(write_event=True, event_path=event_path, required_frames=1)

            payload = json.loads(event_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["error"], "capture_client_size_mismatch")
        self.assertEqual(payload["source"]["failure_kind"], "capture_client_size_mismatch_persistent")
        self.assertFalse(payload["active"])
        self.assertEqual(payload["timing"]["observation_kind"], "capture_failure")
        self.assertEqual(payload["timing"]["capture_status"], "invalid_size")

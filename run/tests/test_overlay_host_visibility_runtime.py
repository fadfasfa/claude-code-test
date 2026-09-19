"""测试 overlay host 可见性。

调用方: pytest; 关键依赖: hextech.interfaces.overlay.host。
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch
import pytest
from support.host_input import PreloadedInputObserver


@pytest.fixture(autouse=True)
def preloaded_input(monkeypatch):
    from hextech.interfaces.overlay import host_runner
    monkeypatch.setattr(host_runner, "HostInputObserver", PreloadedInputObserver)



class OverlayHostVisibilityRuntimeTests(unittest.TestCase):
    def test_context_rejection_withdraws_cached_ready_stats_immediately(self):
        from types import SimpleNamespace
        from hextech.interfaces.overlay import host_runner

        callbacks = []
        rendered_statuses = []
        reported_statuses = []
        event = {
            "selection_type": "hextech",
            "active": True,
            "visible": True,
            "revision": 1,
            "source": {
                "session_id": "session-1",
                "selection_epoch": 1,
                "selection_revision": 1,
                "selection_window_active": True,
                "scene_state": "active",
                "game_instance_id": "game-1",
                "window_hwnd": 100,
            },
            "slots": [
                {
                    "slot": index,
                    "slot_generation": 1,
                    "state": "ready",
                    "augment_id": str(index),
                    "name": f"海克斯 {index}",
                    "tier": "gold",
                }
                for index in range(3)
            ],
        }
        ready_model = {
            "stats": [
                {
                    "slot": index,
                    "state": "matched",
                    "name": f"海克斯 {index}",
                    "stats_text": f"统计 {index}",
                    "status_code": "READY",
                    "synergy_status": "SOURCE_UNAVAILABLE",
                }
                for index in range(3)
            ],
            "synergies": [],
        }
        prepared = SimpleNamespace(
            generation={
                "generation_id": "generation-1",
                "stats_generation_id": "generation-1",
                "game_session_id": "session-1",
            },
            scope={},
            scope_key=("stage", 1),
            content_key="ready-content",
            state=object(),
            model=ready_model,
            event=event,
            host_read_at=1.0,
            phase="ready",
        )

        class Canvas:
            def after(self, delay_ms, callback):
                callbacks.append((delay_ms, callback))
                return f"after-{len(callbacks)}"

            def winfo_width(self):
                return 1920

            def winfo_height(self):
                return 1080

        class Source:
            def read_event(self):
                return event

            def read_context(self):
                return {"ok": True, "champion_id": "4"}

        class Gate:
            def __init__(self):
                self.calls = 0

            def evaluate(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls in {1, 2, 5}:
                    return SimpleNamespace(
                        state="holding" if self.calls == 2 else "confirmed",
                        reason="live-client-unavailable" if self.calls == 2 else "context_confirmed",
                        context_revision=1,
                        held=self.calls == 2,
                        payload={"ok": True, "champion_id": "4"},
                    )
                return SimpleNamespace(
                    state="pending",
                    reason="context_untrusted_publisher",
                    context_revision=0,
                    held=False,
                    payload={"ok": False, "champion_id": ""},
                )

        class Preparation:
            def __init__(self):
                self.invalidations = 0

            def request(self, _event, context, **_kwargs):
                return "ready" if context.get("ok") else "rejected"

            def poll(self, key):
                return prepared if key == "ready" else None

            def status(self):
                return {"generation": {}}

            def invalidate(self):
                self.invalidations += 1

        preparation = Preparation()
        visibility = {
            "user_enabled": True,
            "display_mode": "compact",
            "render_full_overlay": True,
            "prepared_shell_key": (("session-1", 1), (1, 1, 1)),
        }

        with (
            patch.object(host_runner, "ContextRenderGate", Gate),
            patch.object(host_runner, "_refresh_target_window"),
            patch.object(host_runner, "is_scoreboard_key_down", return_value=False),
            patch.object(host_runner, "_sync_event_visibility", return_value=True),
            patch.object(host_runner, "window_display_context", return_value={}),
            patch.object(host_runner, "refresh_display_geometry"),
            patch.object(
                host_runner,
                "present_overlay_model",
                side_effect=lambda _canvas, _config, _visibility, _snapshot, model, **_kwargs:
                    rendered_statuses.append([row.get("status_code") for row in model.get("stats", [])]),
            ),
            patch.object(host_runner, "_log_waiting_context_diagnostic"),
            patch.object(
                host_runner,
                "_write_overlay_session_report",
                side_effect=lambda _snapshot, model, *_args, **_kwargs: reported_statuses.append(
                    None if model is None else [row.get("status_code") for row in model.get("stats", [])]
                ),
            ),
            patch.object(host_runner, "_write_real_session_evidence"),
        ):
            host_runner._schedule_event_render(
                object(),
                Canvas(),
                {"diagnostic_mode": False, "event_poll_ms": 120},
                visibility,
                __import__("queue").Queue(),
                data_source=Source(),
                data_preparation=preparation,
            )
            self.assertEqual(rendered_statuses[-1], ["READY", "READY", "READY"])
            self.assertEqual(visibility["context_revision"], 1)
            # 同身份、可信 broker 的短暂 hold 继续保留 last-good。
            callbacks.pop(0)[1]()
            self.assertEqual(rendered_statuses, [["READY", "READY", "READY"]])
            self.assertEqual(preparation.invalidations, 0)
            self.assertIn("last_render_model", visibility)

            # 硬拒绝必须在同一 tick 用 detecting shell 覆盖旧 READY。
            callbacks.pop(0)[1]()
            self.assertEqual(rendered_statuses[-1], ["DETECTING", "DETECTING", "DETECTING"])
            self.assertNotIn("last_render_model", visibility)
            self.assertNotIn("session_state", visibility)
            self.assertEqual(preparation.invalidations, 1)

            # 拒绝持续时不重复失效；poll(None) 的报告也不能携带旧 READY。
            callbacks.pop(0)[1]()
            self.assertEqual(preparation.invalidations, 1)
            self.assertIsNone(reported_statuses[-1])

            # 同 revision 的可信 Context 恢复后仍会重新准备并绘制 READY。
            callbacks.pop(0)[1]()

        self.assertEqual(visibility["context_revision"], 1)
        self.assertEqual(preparation.invalidations, 1)
        self.assertEqual(visibility["consecutive_render_failures"], 0)
        self.assertEqual(rendered_statuses[-1], ["READY", "READY", "READY"])
        self.assertIn("last_render_model", visibility)
        self.assertIn("session_state", visibility)

    def test_render_tick_confirms_context_before_opening_cold_snapshot(self):
        from types import SimpleNamespace
        from hextech.interfaces.overlay import host_render_state, host_runner

        calls = []
        opened = threading.Event()
        release = threading.Event()
        data_threads = []

        class FakeCanvas:
            def __init__(self):
                self.after_calls = []

            def after(self, _delay_ms, _callback):
                self.after_calls.append((_delay_ms, _callback))
                return f"after-{len(self.after_calls)}"

            def after_cancel(self, _after_id):
                return None

            def delete(self, *_args):
                calls.append("clear")

            def update_idletasks(self):
                calls.append("flush")

            def winfo_width(self):
                return 1920

            def winfo_height(self):
                return 1080

        class FakeSource:
            def read_event(self):
                return {
                    "selection_type": "hextech",
                    "active": True,
                    "visible": True,
                    "slots": [{"state": "ready", "augment_id": str(index)} for index in range(3)],
                    "source": {
                        "session_id": "session-1",
                        "selection_epoch": 1,
                        "scene_state": "active",
                        "selection_window_active": True,
                        "vision_pool_generation_id": "vision-pool-a",
                        "game_instance_id": "game-1",
                        "window_hwnd": 100,
                    },
                }

            def read_context(self):
                calls.append("context")
                return {"ok": True, "champion_id": "4"}

            def open_view(self):
                calls.append("open_view")
                data_threads.append(threading.get_ident())
                opened.set()
                release.wait(2.0)
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
        canvas = FakeCanvas()

        def sync_visibility(*_args, resolved_should_show=None, **_kwargs):
            visibility["render_full_overlay"] = True
            return True if resolved_should_show is None else resolved_should_show

        with (
            patch.object(host_runner, "ContextRenderGate", FakeGate),
            patch.object(host_runner, "_refresh_target_window"),
            patch.object(host_runner, "is_scoreboard_key_down", return_value=False),
            patch.object(host_runner, "_sync_event_visibility", side_effect=sync_visibility),
            patch.object(
                host_render_state,
                "draw_overlay_frame",
                side_effect=lambda _canvas, model, **_kwargs: calls.append(
                    ("draw", [row.get("state") for row in model.get("stats", [])])
                ),
            ),
            patch.object(host_runner, "_log_waiting_context_diagnostic"),
            patch.object(host_runner, "_write_overlay_session_report"),
            patch.object(host_runner, "_write_real_session_evidence"),
        ):
            host_runner._schedule_event_render(
                object(),
                canvas,
                {"diagnostic_mode": False, "event_poll_ms": 120},
                visibility,
                __import__("queue").Queue(),
                data_source=FakeSource(),
            )

            self.assertEqual(visibility["vision_pool_generation_id"], "vision-pool-a")

            self.assertIn(("draw", ["detecting", "detecting", "detecting"]), calls)
            self.assertTrue(opened.wait(1.0))  # shell先排入呈现，同tick已提交非阻塞准备。
            self.assertEqual(canvas.after_calls[0][0], 16)
            canvas.after_calls[0][1]()
            self.assertTrue(opened.wait(1.0))
            self.assertNotIn(threading.get_ident(), data_threads)

            canvas.after_calls[1][1]()
            projected_draws = [call for call in calls if isinstance(call, tuple) and call[0] == "draw"]
            self.assertEqual(len(projected_draws), 1)
            canvas.after_calls[2][1]()
            unchanged_draws = [call for call in calls if isinstance(call, tuple) and call[0] == "draw"]
            self.assertEqual(len(unchanged_draws), 1)
            release.set()
            visibility["data_preparation"].close()

        self.assertLess(calls.index("context"), calls.index("open_view"))
        self.assertLess(calls.index("gate"), calls.index("open_view"))
        first_draw = next(index for index, item in enumerate(calls) if isinstance(item, tuple) and item[0] == "draw")
        self.assertLess(calls.index("context"), first_draw)
        self.assertNotIn("clear", calls)
        self.assertNotIn("rendered_selection_key", visibility)

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
        self.assertIn("stage_context", visibility["pinned_stats_scope"])
        self.assertIn("scoped_view", visibility["pinned_stats_scope"])

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
        from types import SimpleNamespace
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
            "data_generation_id": "generation-test",
            "stats_generation_id": "generation-test",
            "vision_pool_generation_id": "vision-generation-test",
            "capture_exclusion": {
                "status": "applied",
                "requested_affinity": 17,
                "applied_affinity": 17,
                "query_ok": True,
            },
        }
        snapshot = {
            "visible": True,
            "source": {"selection_window_active": True, "ready_slots": 1},
            "slots": [{"slot": 0, "state": "ready", "name": "强化 0"}],
        }
        writes = []
        visibility["report_writer"] = SimpleNamespace(
            submit_visibility=lambda payload: writes.append(("game_overlay_visibility.v1.json", payload)) or True,
        )

        with (
            patch.object(host_sync, "_is_game_window_foreground", return_value=True),
            patch.object(host_sync, "is_window_renderable", return_value=True),
            patch.object(host_sync, "_refresh_gameflow_in_progress", return_value=True),
            patch.object(host_visibility, "atomic_write_json", side_effect=AssertionError("Tk visibility write")),
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
        self.assertGreater(payload["pid"], 0)
        self.assertEqual(payload["data_generation_id"], "generation-test")
        self.assertEqual(payload["stats_generation_id"], "generation-test")
        self.assertEqual(payload["vision_pool_generation_id"], "vision-generation-test")
        self.assertEqual(
            payload["generation_roles"]["stats_generation_id"],
            "host_game_session",
        )
        self.assertEqual(payload["functional_status"], "degraded")
        self.assertEqual(payload["functional_reason"], "context_unavailable")
        self.assertIn("window", payload)
        self.assertIn("render", payload)
        self.assertEqual(payload["host"]["gameflow"], True)
        self.assertEqual(payload["scene"]["selection_window_active"], True)
        self.assertEqual(payload["context"]["error"], "context_missing")
        self.assertEqual(payload["decision"]["should_show"], True)
        self.assertEqual(payload["decision"]["window_visible"], True)
        self.assertEqual(payload["decision"]["reason"], "visible_partial")
        self.assertEqual(payload["presentation"]["state"], "hidden")
        self.assertEqual(payload["presentation"]["capture_exclusion"]["status"], "applied")

    def test_capture_exclusion_failure_is_explicit_functional_failure(self):
        from hextech.interfaces.overlay.host_visibility import _build_visibility_status_payload

        payload = _build_visibility_status_payload(
            {
                "capture_exclusion": {
                    "status": "failed",
                    "reason": "display_affinity_readback_mismatch",
                }
            },
            {"source": {}, "slots": []},
            now=124.0,
            should_show=False,
            reason="capture_exclusion_unavailable",
        )

        self.assertEqual(payload["functional_status"], "failed")
        self.assertEqual(payload["functional_reason"], "capture_exclusion_unavailable")
        self.assertEqual(payload["presentation"]["capture_exclusion"]["status"], "failed")

    def test_fullscreen_visibility_payload_is_degraded_and_explicit(self):
        from hextech.interfaces.overlay.host_visibility import _build_visibility_status_payload

        payload = _build_visibility_status_payload(
            {
                "user_enabled": True,
                "gameflow_in_progress": True,
                "target_hwnd": 100,
                "game_renderable": True,
                "game_foreground": True,
                "game_window_mode_status": "unsupported",
                "game_window_mode": "fullscreen",
                "game_window_mode_reason": "window_mode_fullscreen",
                "game_window_mode_source": "game_cfg",
                "game_window_mode_observed_at": 123.0,
            },
            {"source": {"selection_window_active": True}, "slots": []},
            now=124.0,
            should_show=False,
            reason="unsupported_fullscreen_mode",
        )

        self.assertEqual(payload["functional_status"], "degraded")
        self.assertEqual(payload["functional_reason"], "unsupported_fullscreen_mode")
        self.assertEqual(
            payload["game_window_mode"],
            {
                "status": "unsupported",
                "mode": "fullscreen",
                "reason": "window_mode_fullscreen",
                "source": "game_cfg",
                "observed_at": 123.0,
            },
        )

    def test_host_visibility_state_write_is_change_based(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
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
        submit = Mock(return_value=True)
        visibility["report_writer"] = SimpleNamespace(submit_visibility=submit)

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

        self.assertEqual(write_json.call_count, 0)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(visibility["last_visibility_status_enqueued_at"], 100.6)

if __name__ == "__main__":
    unittest.main()

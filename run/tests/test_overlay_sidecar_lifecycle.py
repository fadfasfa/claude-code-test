"""测试 overlay sidecar 生命周期。

调用方: pytest; 关键依赖: hextech.interfaces.overlay.lifecycle。
"""
from __future__ import annotations

from contextlib import nullcontext
import json
import runpy
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import numpy as np

from support.process_fakes import FakeProcess


class OverlaySidecarLifecycleTests(unittest.TestCase):
    def test_roi_dump_setting_precedence_and_invalid_file_fallback(self):
        from hextech.modules.vision import diagnostic_settings

        with tempfile.TemporaryDirectory() as temp_dir:
            settings = Path(temp_dir) / "overlay_diagnostic_settings.v1.json"
            settings.write_text(
                json.dumps({"schema_version": 1, "roi_dump_enabled": True}),
                encoding="utf-8",
            )

            self.assertTrue(
                diagnostic_settings.resolve_roi_dump_enabled(env={}, settings_path=settings)
            )
            self.assertFalse(
                diagnostic_settings.resolve_roi_dump_enabled(
                    env={diagnostic_settings.ROI_DUMP_ENV: "0"},
                    settings_path=settings,
                )
            )
            self.assertTrue(
                diagnostic_settings.resolve_roi_dump_enabled(
                    True,
                    env={diagnostic_settings.ROI_DUMP_ENV: "0"},
                    settings_path=settings,
                )
            )
            settings.write_text("{broken-json", encoding="utf-8")
            self.assertFalse(
                diagnostic_settings.resolve_roi_dump_enabled(env={}, settings_path=settings)
            )
            settings.write_text(
                json.dumps({"schema_version": 2, "roi_dump_enabled": True}),
                encoding="utf-8",
            )
            self.assertFalse(
                diagnostic_settings.resolve_roi_dump_enabled(env={}, settings_path=settings)
            )
            settings.write_text(
                json.dumps({"schema_version": "invalid", "roi_dump_enabled": True}),
                encoding="utf-8",
            )
            self.assertFalse(
                diagnostic_settings.resolve_roi_dump_enabled(env={}, settings_path=settings)
            )

    def test_persistent_roi_setting_adds_debug_dump_to_sidecar_command(self):
        from hextech.interfaces.overlay import lifecycle

        captured_command: list[str] = []

        def fake_popen(command, **_kwargs):
            captured_command.extend(command)
            return FakeProcess(1000)

        with (
            patch.object(lifecycle, "resolve_roi_dump_enabled", return_value=True),
            patch.object(lifecycle.subprocess, "Popen", side_effect=fake_popen),
            patch.object(lifecycle, "_wait_for_sidecar_ready", return_value=None),
        ):
            lifecycle.start_sidecar_process()

        self.assertIn("--debug-dump", captured_command)

    def test_sidecar_status_includes_debug_dump_enabled(self):
        from hextech.infrastructure.vision import runner

        payloads: list[dict] = []
        with (
            patch.object(runner, "_SIDECAR_DEBUG_DUMP_ENABLED", True),
            patch.object(
                runner._status,
                "atomic_write_json",
                side_effect=lambda _path, payload, **_kwargs: payloads.append(payload),
            ),
        ):
            runner._write_sidecar_status("running")

        self.assertTrue(payloads[-1]["debug_dump_enabled"])

    def test_sidecar_status_includes_generation_pool_and_matrix_fields(self):
        from hextech.infrastructure.vision import runner

        payloads: list[dict] = []
        runtime_stats = {
            "vision_pool_generation_id": "snapshot-a",
            "stats_generation_id": "",
            "data_generation_id": "snapshot-a",
            "catalog_generation_id": "catalog-a",
            "production_pool_id": "pool-a",
            "production_pool_state": "ready",
            "production_pool_count": 236,
            "full_catalog_count": 655,
            "rank_identity_count": 236,
            "matrix_rows": {"icon": 236, "name": 472, "alt_name": 472, "observed_name": 0},
            "excluded_reason_counts": {"disabled": 7, "unresolved": 0},
        }
        with (
            patch.object(runner._status, "runtime_fields", {}),
            patch.object(
                runner._status,
                "atomic_write_json",
                side_effect=lambda _path, payload, **_kwargs: payloads.append(payload),
            ),
        ):
            runner._publish_runtime_fields(runtime_stats)
            runner._write_sidecar_status("running", phase="loop")

        payload = payloads[-1]
        for key, value in runtime_stats.items():
            self.assertEqual(payload[key], value)
        self.assertEqual(
            payload["generation_roles"]["vision_pool_generation_id"],
            "sidecar_template_runtime",
        )

    def test_second_host_exits_before_creating_tk_window(self):
        from hextech.interfaces.overlay import host_runner

        with (
            patch.object(host_runner, "overlay_instance_lock", return_value=nullcontext(False)),
            patch.object(host_runner, "OverlayDataPreparation") as prepare_cache,
            patch.object(host_runner.tk, "Tk") as create_tk,
        ):
            host_runner.run_overlay_host()

        prepare_cache.assert_not_called()
        create_tk.assert_not_called()

    def test_second_sidecar_exits_before_writing_shared_runtime_state(self):
        from hextech.infrastructure.vision import runner, sidecar

        with (
            patch.object(runner, "overlay_instance_lock", return_value=nullcontext(False)),
            patch.object(runner, "_write_sidecar_bootstrap_from_env") as write_bootstrap,
            patch.object(runner, "_write_sidecar_status") as write_status,
            patch.object(runner, "run_loop") as run_loop,
        ):
            result = sidecar.main([])

        self.assertEqual(result, 0)
        write_bootstrap.assert_not_called()
        write_status.assert_not_called()
        run_loop.assert_not_called()

    def test_host_and_sidecar_instance_locks_share_runtime_lock_directory(self):
        from hextech.infrastructure.vision import runner
        from hextech.interfaces.overlay import host_runner

        self.assertEqual(host_runner.HOST_INSTANCE_LOCK_FILE.parent, runner.SIDECAR_INSTANCE_LOCK_FILE.parent)
        self.assertEqual(host_runner.HOST_INSTANCE_LOCK_FILE.parent.name, "locks")
        self.assertNotEqual(host_runner.HOST_INSTANCE_LOCK_FILE.name, runner.SIDECAR_INSTANCE_LOCK_FILE.name)

    def test_overlay_instance_lock_is_exclusive(self):
        from hextech.modules.vision.instance_lock import overlay_instance_lock

        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "overlay.lock"
            with overlay_instance_lock(lock_path) as first:
                with overlay_instance_lock(lock_path) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)
            with overlay_instance_lock(lock_path) as reacquired:
                self.assertTrue(reacquired)

    def test_sidecar_run_loop_delegates_to_runner_module(self):
        from hextech.infrastructure.vision import runner, sidecar

        expected = {"active": False, "source": {"reason": "test"}}
        with patch.object(runner, "run_loop", return_value=expected) as delegated:
            result = sidecar.run_loop(write_event=True, event_path="test-event.json")

        self.assertEqual(result, expected)
        delegated.assert_called_once_with(write_event=True, event_path="test-event.json")

    def test_host_ready_timeout_reports_missing_ready_file_without_token(self):
        from hextech.interfaces.overlay import lifecycle

        with tempfile.TemporaryDirectory() as temp_dir:
            ready_path = Path(temp_dir) / "missing.ready.json"
            with self.assertRaisesRegex(
                TimeoutError,
                r"ready_file=missing.*pid=9876",
            ) as raised:
                lifecycle._wait_for_host_ready(
                    FakeProcess(9876),
                    ready_path,
                    expected_token="secret-token",
                    timeout_seconds=0.01,
                )

        self.assertNotIn("secret-token", str(raised.exception))

    def test_host_ready_timeout_reports_process_exit_and_token_mismatch(self):
        from hextech.interfaces.overlay import lifecycle

        with tempfile.TemporaryDirectory() as temp_dir:
            ready_path = Path(temp_dir) / "host.ready.json"

            with self.assertRaisesRegex(RuntimeError, r"readiness 前退出.*exit_code=9"):
                lifecycle._wait_for_host_ready(FakeProcess(1201, exit_code=9), ready_path, timeout_seconds=0.01)

            ready_path.write_text(
                json.dumps({"token": "actual-token", "pid": 1202, "updated_at": time.time()}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, r"token 不匹配.*pid=1202"):
                lifecycle._wait_for_host_ready(
                    FakeProcess(1202),
                    ready_path,
                    expected_token="expected-token",
                    timeout_seconds=0.1,
                )

            ready_path.write_text(
                json.dumps({"pid": "abc", "updated_at": time.time()}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, r"token 不匹配.*pid=abc"):
                lifecycle._wait_for_host_ready(FakeProcess(1202), ready_path, timeout_seconds=0.1)

    def test_source_host_process_executes_overlay_composition_root(self):
        from hextech.interfaces.overlay import lifecycle

        captured_command: list[str] = []

        def fake_popen(command, **_kwargs):
            captured_command.extend(command)
            return FakeProcess(1203)

        with (
            patch.object(lifecycle.sys, "frozen", False, create=True),
            patch.object(lifecycle.subprocess, "Popen", side_effect=fake_popen),
            patch.object(lifecycle, "_wait_for_host_ready", return_value=None) as wait_ready,
        ):
            process = lifecycle.start_host_process()

        self.assertEqual(process.pid, 1203)
        self.assertEqual(
            captured_command,
            [sys.executable, "-m", "hextech.bootstrap.overlay"],
        )
        self.assertEqual(wait_ready.call_args.kwargs["timeout_seconds"], 5.0)
        self.assertEqual(getattr(process, "_hextech_overlay_host_startup_observation")["ready_state"], "ready")

    def test_overlay_composition_root_executes_when_run_as_module(self):
        with (
            patch("hextech.modules.data.ports.paths.ensure_var_layout") as ensure_layout,
            patch("hextech.infrastructure.observability.logging.install_runtime_logging") as install_logging,
            patch("hextech.interfaces.overlay.gameflow.configure_lcu_scanner") as configure_scanner,
            patch("hextech.interfaces.overlay.host.main", return_value=0) as run_overlay,
        ):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module("hextech.bootstrap.overlay", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        ensure_layout.assert_called_once_with()
        install_logging.assert_called_once_with()
        configure_scanner.assert_called_once()
        run_overlay.assert_called_once_with(None)

    def test_frozen_host_process_keeps_game_overlay_switch(self):
        from hextech.interfaces.overlay import lifecycle

        captured_command: list[str] = []

        def fake_popen(command, **_kwargs):
            captured_command.extend(command)
            return FakeProcess(1204)

        with (
            patch.object(lifecycle.sys, "frozen", True, create=True),
            patch.object(lifecycle.subprocess, "Popen", side_effect=fake_popen),
            patch.object(lifecycle, "_wait_for_host_ready", return_value=None) as wait_ready,
        ):
            process = lifecycle.start_host_process()

        self.assertEqual(process.pid, 1204)
        self.assertEqual(captured_command, [sys.executable, "--game-overlay"])
        self.assertEqual(wait_ready.call_args.kwargs["timeout_seconds"], 20.0)

    def test_frozen_host_ready_after_source_budget_uses_twenty_second_contract(self):
        from hextech.interfaces.overlay import lifecycle

        observed: dict[str, float] = {}

        def ready_after_eight_seconds(_process, _ready_path, **kwargs):
            observed["simulated_ready_at"] = 8.0
            observed["timeout_seconds"] = float(kwargs["timeout_seconds"])
            if observed["simulated_ready_at"] >= observed["timeout_seconds"]:
                raise TimeoutError("simulated frozen host timeout")

        with (
            patch.object(lifecycle.sys, "frozen", True, create=True),
            patch.object(lifecycle.subprocess, "Popen", return_value=FakeProcess(1205)),
            patch.object(lifecycle, "_wait_for_host_ready", side_effect=ready_after_eight_seconds),
        ):
            process = lifecycle.start_host_process()

        self.assertEqual(process.pid, 1205)
        self.assertEqual(observed, {"simulated_ready_at": 8.0, "timeout_seconds": 20.0})

    def test_host_start_surfaces_cleanup_failure_and_records_attempt(self):
        from hextech.interfaces.overlay import lifecycle

        with (
            patch.object(lifecycle.subprocess, "Popen", return_value=FakeProcess(1206)),
            patch.object(lifecycle, "_wait_for_host_ready", side_effect=TimeoutError("slow host")),
            patch.object(lifecycle, "stop_process", return_value=False),
        ):
            with self.assertRaises(lifecycle.HostCleanupError) as raised:
                lifecycle.start_host_process()

        self.assertFalse(raised.exception.retryable)
        observation = getattr(raised.exception, "host_startup_observation")
        self.assertEqual(observation["ready_state"], "timeout")
        self.assertFalse(observation["cleanup_confirmed"])

    def test_lifecycle_waits_for_sidecar_ready_and_sets_exit_signal(self):
        from hextech.interfaces.overlay import lifecycle

        captured_env: dict[str, str] = {}

        def fake_popen(_command, **kwargs):
            captured_env.update(kwargs["env"])
            return FakeProcess(1001)

        with (
            patch.object(lifecycle.subprocess, "Popen", side_effect=fake_popen),
            patch.object(lifecycle, "_wait_for_sidecar_ready", return_value=None),
        ):
            process = lifecycle.start_sidecar_process()

        self.assertEqual(process.pid, 1001)
        self.assertEqual(lifecycle.OVERLAY_SIDECAR_BOOTSTRAP_FILE_ENV, "HEXTECH_OVERLAY_SIDECAR_BOOTSTRAP_FILE")
        self.assertTrue(captured_env[lifecycle.OVERLAY_SIDECAR_BOOTSTRAP_FILE_ENV].endswith(".bootstrap.json"))
        self.assertTrue(captured_env["HEXTECH_OVERLAY_GENERATION"])
        self.assertTrue(getattr(process, "_hextech_overlay_exit_file", ""))

    def test_sidecar_readiness_wait_responds_to_cancel_signal(self):
        from hextech.interfaces.overlay import lifecycle

        cancel_event = threading.Event()
        cancel_event.set()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(lifecycle.SidecarStartCancelled):
                lifecycle._wait_for_sidecar_ready(
                    FakeProcess(1002),
                    Path(temp_dir) / "missing.ready.json",
                    expected_token="expected-token",
                    timeout_seconds=5.0,
                    cancel_event=cancel_event,
                )

    def test_sidecar_start_surfaces_cleanup_failure_as_non_retryable(self):
        from hextech.interfaces.overlay import lifecycle

        with (
            patch.object(lifecycle.subprocess, "Popen", return_value=FakeProcess(1003)),
            patch.object(lifecycle, "_wait_for_sidecar_ready", side_effect=TimeoutError("slow bootstrap")),
            patch.object(lifecycle, "stop_process", return_value=False),
        ):
            with self.assertRaises(lifecycle.SidecarCleanupError) as raised:
                lifecycle.start_sidecar_process(readiness_timeout_seconds=1.0)

        self.assertFalse(raised.exception.retryable)

    def test_sidecar_writes_ready_and_checks_exit_signal(self):
        from hextech.infrastructure.vision import runner, sidecar

        statuses: list[tuple[str, dict]] = []
        bootstrap_states: list[tuple[str, dict]] = []
        with (
            patch.object(runner, "_write_sidecar_status", side_effect=lambda status, **fields: statuses.append((status, fields))),
            patch.object(runner, "_write_sidecar_bootstrap_from_env", side_effect=lambda state, **fields: bootstrap_states.append((state, fields))),
        ):
            sidecar._write_sidecar_ready_from_env(template_count=3, started_at=time.perf_counter())

        self.assertEqual(statuses[-1][0], "running")
        self.assertEqual(bootstrap_states[-1][0], "ready")
        self.assertEqual(bootstrap_states[-1][1]["phase"], "ready")

    def test_wait_for_sidecar_ready_surfaces_failed_bootstrap_without_timeout(self):
        from hextech.interfaces.overlay import lifecycle

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ready_path = root / "sidecar.ready.json"
            bootstrap_path = root / "sidecar.bootstrap.json"
            bootstrap_path.write_text(
                json.dumps(
                    {
                        "state": "failed",
                        "token": "expected-token",
                        "error_type": "FileNotFoundError",
                        "error_message_sanitized": "模板缺失",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(lifecycle.SidecarBootstrapError, "模板缺失") as raised:
                lifecycle._wait_for_sidecar_ready(
                    FakeProcess(1002),
                    ready_path,
                    bootstrap_path=bootstrap_path,
                    expected_token="expected-token",
                    timeout_seconds=0.1,
                )

        self.assertFalse(raised.exception.retryable)

    def test_wait_for_sidecar_ready_marks_permission_error_non_retryable(self):
        from hextech.interfaces.overlay import lifecycle

        process = mock.Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ready_path = root / "sidecar.ready.json"
            bootstrap_path = root / "sidecar.bootstrap.json"
            bootstrap_path.write_text(
                json.dumps(
                    {
                        "token": "expected",
                        "state": "failed",
                        "error_type": "PermissionError",
                        "error_message_sanitized": "cache access denied",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(lifecycle.SidecarBootstrapError) as raised:
                lifecycle._wait_for_sidecar_ready(
                    process,
                    ready_path,
                    bootstrap_path=bootstrap_path,
                    expected_token="expected",
                    timeout_seconds=0.1,
                )

        self.assertFalse(raised.exception.retryable)

    def test_sidecar_main_records_starting_and_failed_bootstrap_state(self):
        from hextech.infrastructure.vision import runner, sidecar

        bootstrap_states: list[tuple[str, dict]] = []
        with (
            patch.object(runner, "_write_sidecar_bootstrap_from_env", side_effect=lambda state, **fields: bootstrap_states.append((state, fields))),
            patch.object(runner, "run_loop", side_effect=RuntimeError(r"C:\Users\alice\private\boom")),
        ):
            try:
                result = sidecar.main([])
            except RuntimeError:
                result = 1

        self.assertEqual(result, 1)
        self.assertEqual([state for state, _fields in bootstrap_states], ["starting", "failed"])
        self.assertEqual(bootstrap_states[-1][1]["error_type"], "RuntimeError")
        self.assertNotIn(r"C:\Users\alice", bootstrap_states[-1][1]["error_message_sanitized"])

    def test_sidecar_once_template_missing_records_failed_bootstrap_state(self):
        from hextech.infrastructure.vision import runner, sidecar

        bootstrap_states: list[tuple[str, dict]] = []
        event = {"source": {"reason": "template_missing"}}
        with (
            patch.object(runner, "_write_sidecar_bootstrap_from_env", side_effect=lambda state, **fields: bootstrap_states.append((state, fields))),
            patch.object(runner, "run_once", return_value=event),
            patch("hextech.infrastructure.vision.sidecar_diagnostics.sys.stdout", None),
        ):
            result = sidecar.main(["--once"])

        self.assertEqual(result, 1)
        self.assertEqual([state for state, _fields in bootstrap_states], ["starting", "failed"])
        self.assertEqual(bootstrap_states[-1][1]["phase"], "template_load")
        self.assertEqual(bootstrap_states[-1][1]["error_type"], "FileNotFoundError")

    def test_sidecar_template_runtime_constants_are_reexports(self):
        from hextech.infrastructure.vision import sidecar, template_runtime

        self.assertIs(
            sidecar.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
            template_runtime.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
        )
        self.assertIs(sidecar.TEMPLATE_RUNTIME_CACHE_FILE, template_runtime.TEMPLATE_RUNTIME_CACHE_FILE)
        self.assertIs(
            sidecar.TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE,
            template_runtime.TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE,
        )

    def test_context_poller_failure_degrades_without_blocking_overlay(self):
        from hextech.interfaces.overlay.lifecycle import GameOverlayController

        controller = GameOverlayController(
            start_host_func=lambda: FakeProcess(123),
            start_sidecar_func=lambda: FakeProcess(123),
            start_context_poller_func=lambda: (_ for _ in ()).throw(RuntimeError("LCU offline")),
            prepare_data_func=lambda: None,
            write_inactive_func=lambda: None,
        )

        self.assertIn("_runtime", controller.__dict__)
        controller.start()
        snapshot = controller.snapshot()

        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["context_poller_status"], "degraded")
        self.assertIn("LCU offline", snapshot["context_poller_error"])

    def test_controller_stop_without_owned_runtime_does_not_write_inactive(self):
        from hextech.interfaces.overlay.lifecycle import GameOverlayController

        inactive_calls: list[str] = []
        controller = GameOverlayController(
            start_context_poller_func=None,
            prepare_data_func=lambda: None,
            write_inactive_func=lambda: inactive_calls.append("inactive"),
        )

        controller.stop()

        self.assertEqual(inactive_calls, [])
        self.assertEqual(controller.snapshot()["status"], "stopped")

    def test_context_poller_starts_before_sidecar_cold_start(self):
        from hextech.interfaces.overlay.lifecycle import GameOverlayController

        calls: list[str] = []

        controller = GameOverlayController(
            start_host_func=lambda: (calls.append("host"), FakeProcess(123))[1],
            start_sidecar_func=lambda: (calls.append("sidecar"), FakeProcess(123))[1],
            start_context_poller_func=lambda: (calls.append("context"), object())[1],
            prepare_data_func=lambda: calls.append("prepare"),
            write_inactive_func=lambda: calls.append("inactive"),
        )

        controller.start()

        self.assertEqual(calls[:3], ["prepare", "inactive", "context"])
        self.assertLess(calls.index("context"), calls.index("sidecar"))

    def test_template_runtime_signature_ignores_hint_cache_runtime_metadata(self):
        from hextech.infrastructure.vision import template_runtime

        base_cache = {
            "schema_version": 1,
            "generated_at": 100.0,
            "source": {"tag": "desktop-game-overlay", "private_policy_stats_enabled": True},
            "hints": {
                "augment-a": {
                    "augment_id": "augment-a",
                    "name": "测试海克斯",
                    "icon": "icons/a.png",
                    "summary": "说明",
                }
            },
            "name_index": {"测试海克斯": "augment-a"},
        }
        changed_metadata = {
            **base_cache,
            "generated_at": 200.0,
            "source": {"tag": "mayhem-refresh", "private_policy_stats_enabled": True},
        }
        changed_identity = {
            **base_cache,
            "hints": {
                "augment-a": {
                    "augment_id": "augment-a",
                    "name": "另一个海克斯",
                    "icon": "icons/a.png",
                    "summary": "说明",
                }
            },
        }
        changed_name_index = {
            **base_cache,
            "name_index": {"测试海克斯": "augment-b"},
        }

        self.assertEqual(template_runtime.template_runtime_hint_signature(base_cache), template_runtime.template_runtime_hint_signature(changed_metadata))
        self.assertNotEqual(template_runtime.template_runtime_hint_signature(base_cache), template_runtime.template_runtime_hint_signature(changed_identity))
        self.assertNotEqual(template_runtime.template_runtime_hint_signature(base_cache), template_runtime.template_runtime_hint_signature(changed_name_index))

    def test_template_runtime_cache_round_trip_preserves_float16_matrices(self):
        from hextech.infrastructure.vision import template_runtime
        from hextech.infrastructure.vision import sidecar
        from hextech.infrastructure.vision import sidecar_matching

        template_index = [
            sidecar.TemplateEntry(
                augment_id="augment-a",
                name="测试海克斯 A",
                tier="silver",
                summary="说明 A",
                fingerprint=(0.5, -0.5, 0.25, -0.25),
                icon_fingerprints=((0.5, -0.5, 0.25, -0.25),),
                icon_digest="digest-a",
                priority=1,
                name_fingerprint=(0.1, 0.2, -0.1, -0.2),
                name_fingerprint_alt=(0.2, 0.1, -0.2, -0.1),
                observed_name_fingerprints=((0.3, -0.3, 0.4, -0.4),),
                source_icon_filenames=("a.png",),
            ),
            sidecar.TemplateEntry(
                augment_id="augment-b",
                name="测试海克斯 B",
                tier="gold",
                summary="说明 B",
                fingerprint=(-0.5, 0.5, -0.25, 0.25),
                icon_fingerprints=((-0.5, 0.5, -0.25, 0.25),),
                icon_digest="digest-b",
                priority=2,
                name_fingerprint=(-0.1, -0.2, 0.1, 0.2),
                name_fingerprint_alt=(-0.2, -0.1, 0.2, 0.1),
                observed_name_fingerprints=((-0.3, 0.3, -0.4, 0.4),),
                source_icon_filenames=("b.png",),
            ),
        ]
        with patch.object(sidecar_matching, "_cleaned_name_fingerprint", return_value=None):
            matrices = sidecar.rank_template_matrices(template_index)
        signature = {"schema_version": template_runtime.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION, "resource": "fixture"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "template_runtime_cache.v2.npz"
            write_stats = template_runtime._write_template_runtime_cache(
                cache_file,
                resource_signature=signature,
                hint_signature="hint-fixture",
                template_index=template_index,
                matrices=matrices,
            )

            self.assertEqual(write_stats["schema_version"], template_runtime.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION)
            self.assertEqual(write_stats["matrix_dtype"], "float16")
            with np.load(cache_file, allow_pickle=False) as payload:
                manifest = json.loads(np.asarray(payload["manifest_json"], dtype=np.uint8).tobytes().decode("utf-8"))
                manifest_text = json.dumps(manifest, ensure_ascii=False)
                self.assertEqual(manifest["schema_version"], template_runtime.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION)
                self.assertEqual(payload["icon_matrix"].dtype, np.float16)
                self.assertEqual(payload["observed_name_matrix"].dtype, np.float16)
                self.assertNotIn("fingerprint", manifest_text)
                self.assertNotIn("name_fingerprint", manifest_text)
                self.assertNotIn("observed_name_fingerprints", manifest_text)

            runtime = template_runtime._read_template_runtime_cache(
                cache_file,
                resource_signature=signature,
                hint_signature="hint-fixture",
            )

        self.assertIsNotNone(runtime)
        assert runtime is not None
        self.assertTrue(runtime.stats["cache_hit"])
        self.assertEqual(runtime.stats["schema_version"], template_runtime.TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION)
        self.assertEqual(runtime.stats["matrix_dtype"], "float16")
        self.assertEqual(runtime.matrices.icon_matrix.dtype, np.float16)
        self.assertIsNone(runtime.template_index[0].fingerprint)
        np.testing.assert_allclose(runtime.matrices.icon_matrix, matrices.icon_matrix, atol=1e-3)
        sidecar._RANK_MATRIX_CACHE.pop(id(runtime.template_index), None)
        restored_matrices = sidecar.rank_template_matrices(runtime.template_index)
        self.assertIs(restored_matrices, runtime.matrices)
        np.testing.assert_allclose(restored_matrices.name_matrix, matrices.name_matrix, atol=1e-3)
        np.testing.assert_allclose(
            restored_matrices.observed_name_matrix,
            matrices.observed_name_matrix,
            atol=1e-3,
        )

    def test_template_runtime_cache_v2_ready_cleans_default_v1_cache(self):
        from hextech.infrastructure.vision import template_runtime

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            v1_cache = root / "template_runtime_cache.v1.npz"
            v2_cache = root / "template_runtime_cache.v2.npz"
            v1_cache.write_bytes(b"legacy-cache")
            v2_cache.write_bytes(b"v2-cache")
            with (
                patch.object(template_runtime, "TEMPLATE_RUNTIME_CACHE_FILE", v2_cache),
                patch.object(template_runtime, "TEMPLATE_RUNTIME_CACHE_V1_FILE", v1_cache),
            ):
                self.assertTrue(template_runtime._cleanup_legacy_template_runtime_cache(v2_cache))
                self.assertFalse(v1_cache.exists())

            custom_cache = root / "custom.v2.npz"
            v1_cache.write_bytes(b"legacy-cache")
            custom_cache.write_bytes(b"custom-cache")
            with (
                patch.object(template_runtime, "TEMPLATE_RUNTIME_CACHE_FILE", v2_cache),
                patch.object(template_runtime, "TEMPLATE_RUNTIME_CACHE_V1_FILE", v1_cache),
            ):
                self.assertFalse(template_runtime._cleanup_legacy_template_runtime_cache(custom_cache))
                self.assertTrue(v1_cache.exists())


if __name__ == "__main__":
    unittest.main()

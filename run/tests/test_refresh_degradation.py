"""测试 refresh 降级路径。

调用方: pytest; 关键依赖: hextech.bootstrap.data_refresh。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class RefreshDegradationTests(unittest.TestCase):
    def test_startup_status_uses_valid_csv_for_ready(self):
        from hextech.infrastructure.sources import heal_worker

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "startup_status.json"
            invalid_csv = tmp_path / "Hextech_Data_2026-07-01.csv"
            invalid_csv.write_text("bad\n", encoding="utf-8")

            with (
                mock.patch.object(heal_worker, "build_runtime_state_path", lambda name: str(status_file)),
                mock.patch.object(heal_worker, "load_scraper_status", lambda: {"last_result": "fallback", "reason": "network_error"}),
                mock.patch.object(heal_worker, "get_latest_csv", lambda: str(invalid_csv)),
                mock.patch.object(heal_worker, "get_latest_valid_csv", lambda: None),
                mock.patch.object(heal_worker, "CORE_DATA_FILE", str(tmp_path / "missing_core.json")),
                mock.patch.object(heal_worker, "build_synergy_data_path", lambda: str(tmp_path / "missing_synergy.json")),
                mock.patch.object(heal_worker, "is_augment_icon_prefetch_ready", lambda: False),
            ):
                heal_worker._write_startup_status()

            payload = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["active_hextech_csv"], str(invalid_csv))
            self.assertIs(payload["hextech_ready"], False)
            self.assertIs(payload["hextech_degraded"], False)
            self.assertEqual(payload["hextech_warning"], "")

    def test_startup_status_includes_hextech_refresh_summary(self):
        from hextech.infrastructure.sources import heal_worker

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "startup_status.json"
            active_csv = tmp_path / "Hextech_Data_2026-07-02.csv"
            active_csv.write_text("valid\n", encoding="utf-8")

            scraper_status = {
                "last_result": "fallback",
                "reason": "thread_pool_timeout",
                "last_attempt_id": "attempt-1",
                "failure_stage": "detail_initial",
                "cdn_hit_count": 42,
                "slow_path_count": 9,
                "success_rows": 1200,
                "failure_samples": [{"champion_id": "266", "reason": "timeout"}],
                "fallback_used": True,
            }

            with (
                mock.patch.object(heal_worker, "build_runtime_state_path", lambda name: str(status_file)),
                mock.patch.object(heal_worker, "load_scraper_status", lambda: scraper_status),
                mock.patch.object(heal_worker, "get_latest_csv", lambda: str(active_csv)),
                mock.patch.object(heal_worker, "get_latest_valid_csv", lambda: str(active_csv)),
                mock.patch.object(heal_worker, "CORE_DATA_FILE", str(active_csv)),
                mock.patch.object(heal_worker, "build_synergy_data_path", lambda: str(active_csv)),
                mock.patch.object(heal_worker, "is_augment_icon_prefetch_ready", lambda: True),
            ):
                heal_worker._write_startup_status()

            payload = json.loads(status_file.read_text(encoding="utf-8"))
            refresh = payload["hextech_refresh"]
            self.assertEqual(refresh["attempt_id"], "attempt-1")
            self.assertEqual(refresh["failure_stage"], "detail_initial")
            self.assertEqual(refresh["cdn_hit_count"], 42)
            self.assertTrue(refresh["fallback_used"])

    def test_heal_worker_exception_clears_in_progress_tasks(self):
        from hextech.infrastructure.sources import heal_worker

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "startup_status.json"
            active_csv = tmp_path / "Hextech_Data_2026-07-02.csv"
            active_csv.write_text("valid\n", encoding="utf-8")

            with (
                mock.patch.object(heal_worker, "build_runtime_state_path", lambda name: str(status_file)),
                mock.patch.object(heal_worker, "build_runtime_lock_path", lambda name: str(tmp_path / name)),
                mock.patch.object(heal_worker, "LOCK_FILE", tmp_path / "heal_worker.lock"),
                mock.patch.object(heal_worker, "detect_missing_artifacts", side_effect=RuntimeError("boom")),
                mock.patch.object(heal_worker, "load_scraper_status", lambda: {"last_result": "success"}),
                mock.patch.object(heal_worker, "get_latest_csv", lambda: str(active_csv)),
                mock.patch.object(heal_worker, "get_latest_valid_csv", lambda: str(active_csv)),
                mock.patch.object(heal_worker, "CORE_DATA_FILE", str(active_csv)),
                mock.patch.object(heal_worker, "build_synergy_data_path", lambda: str(active_csv)),
                mock.patch.object(heal_worker, "is_augment_icon_prefetch_ready", lambda: True),
            ):
                report = heal_worker.heal_missing_artifacts()

            payload = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertIn("heal_worker", report["failed"])
            self.assertEqual(payload["in_progress_tasks"], [])
            self.assertIn("heal_worker failed", payload["last_error"])

    def test_refresh_result_records_fallback_activation_and_recovery(self):
        from hextech.bootstrap import data_refresh as refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events_file = tmp_path / "runtime_events.jsonl"
            degradation_file = tmp_path / "refresh_degradation.v1.json"
            fallback_csv = tmp_path / "Hextech_Data_2026-06-30.csv"
            fallback_csv.write_text("valid-local\n", encoding="utf-8")

            def state_path(name: str) -> str:
                return str(events_file if name == "runtime_events.v1.jsonl" else tmp_path / name)

            fallback_report = {
                "requested": ["hextech_rankings"],
                "repaired": [],
                "fallback": ["hextech_rankings"],
                "failed": [],
            }
            recovered_report = {
                "requested": ["hextech_rankings"],
                "repaired": ["hextech_rankings"],
                "fallback": [],
                "failed": [],
            }

            with (
                mock.patch.object(refresh, "build_runtime_state_path", state_path),
                mock.patch.object(refresh, "get_latest_valid_csv", lambda: str(fallback_csv)),
                mock.patch.object(refresh, "get_latest_csv", lambda: str(fallback_csv)),
                mock.patch.object(refresh, "rebuild_api_cache_if_needed", lambda force=False: True),
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None, force=False: None),
            ):
                refresh._ACTIVE_DEGRADATION.clear()
                with mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: fallback_report):
                    first = refresh.refresh_backend_data(force=False)
                    second = refresh.refresh_backend_data(force=False)
                self.assertTrue(degradation_file.exists())
                refresh._ACTIVE_DEGRADATION.clear()
                with mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: recovered_report):
                    recovered = refresh.refresh_backend_data(force=False)
                refresh._ACTIVE_DEGRADATION.clear()

            self.assertEqual(first.state, "degraded")
            self.assertIs(first.fallback_used, True)
            self.assertIs(first.fallback_valid, True)
            self.assertTrue(first.degradation_id)
            self.assertEqual(second.degradation_id, first.degradation_id)
            self.assertEqual(recovered.state, "ready")
            self.assertEqual(recovered.degradation_id, first.degradation_id)
            self.assertFalse(degradation_file.exists())

            events = _read_jsonl(events_file)
            self.assertEqual(
                [event["event"] for event in events],
                ["fallback.activated", "fallback.reused", "fallback.recovered"],
            )
            self.assertEqual(events[0]["schema_version"], 1)
            self.assertTrue(events[0]["publisher_instance_id"])
            self.assertIs(events[0]["ready_assertion_consistent"], True)
            self.assertGreaterEqual(events[2]["degraded_duration_seconds"], 0)
            self.assertIn("recovered_hash", events[2])

    def test_failed_refresh_recovery_uses_refresh_recovered_event(self):
        from hextech.bootstrap import data_refresh as refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events_file = tmp_path / "runtime_events.jsonl"
            recovered_csv = tmp_path / "Hextech_Data_2026-07-03.csv"
            recovered_csv.write_text("valid-remote\n", encoding="utf-8")
            latest_valid = {"path": ""}

            def state_path(name: str) -> str:
                return str(events_file if name == "runtime_events.v1.jsonl" else tmp_path / name)

            failed_report = {
                "requested": ["hextech_rankings"],
                "repaired": [],
                "fallback": [],
                "failed": ["hextech_rankings"],
            }
            recovered_report = {
                "requested": ["hextech_rankings"],
                "repaired": ["hextech_rankings"],
                "fallback": [],
                "failed": [],
            }

            with (
                mock.patch.object(refresh, "build_runtime_state_path", state_path),
                mock.patch.object(refresh, "get_latest_valid_csv", lambda: latest_valid["path"]),
                mock.patch.object(refresh, "get_latest_csv", lambda: latest_valid["path"]),
                mock.patch.object(refresh, "rebuild_api_cache_if_needed", lambda force=False: True),
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None, force=False: None),
            ):
                refresh._ACTIVE_DEGRADATION.clear()
                with mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: failed_report):
                    failed = refresh.refresh_backend_data(force=False)
                latest_valid["path"] = str(recovered_csv)
                with mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: recovered_report):
                    recovered = refresh.refresh_backend_data(force=False)
                refresh._ACTIVE_DEGRADATION.clear()

            self.assertEqual(failed.state, "failed")
            self.assertEqual(recovered.state, "ready")
            events = _read_jsonl(events_file)
            self.assertEqual([event["event"] for event in events], ["refresh.failed", "refresh.recovered"])
            self.assertEqual(events[1]["previous_state"], "failed")
            self.assertEqual(events[1]["new_state"], "ready")

    def test_heal_worker_busy_report_is_explicit(self):
        from filelock import Timeout
        from hextech.infrastructure.sources import heal_worker

        class BusyLock:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                raise Timeout("busy")

            def __exit__(self, *_args):
                return False

        with mock.patch.object(heal_worker, "FileLock", BusyLock), mock.patch.object(heal_worker, "_write_startup_status"):
            report = heal_worker.heal_missing_artifacts()

        self.assertIs(report["busy"], True)
        self.assertEqual(report["reason"], "another_repair_running")
        self.assertIn("heal_worker", report["skipped"])

    def test_refresh_busy_report_does_not_become_ready(self):
        from hextech.bootstrap import data_refresh as refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events_file = tmp_path / "runtime_events.jsonl"
            fallback_csv = tmp_path / "Hextech_Data_2026-07-03.csv"
            fallback_csv.write_text("valid-fallback\n", encoding="utf-8")

            def state_path(name: str) -> str:
                return str(events_file if name == "runtime_events.v1.jsonl" else tmp_path / name)

            busy_report = {
                "requested": [],
                "repaired": [],
                "fallback": [],
                "failed": [],
                "skipped": ["heal_worker"],
                "busy": True,
                "reason": "another_repair_running",
            }

            with (
                mock.patch.object(refresh, "build_runtime_state_path", state_path),
                mock.patch.object(refresh, "get_latest_valid_csv", lambda: str(fallback_csv)),
                mock.patch.object(refresh, "get_latest_csv", lambda: str(fallback_csv)),
                mock.patch.object(refresh, "rebuild_api_cache_if_needed", lambda force=False: True),
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None, force=False: None),
                mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: busy_report),
            ):
                refresh._ACTIVE_DEGRADATION.clear()
                result = refresh.refresh_backend_data(force=False)
                refresh._ACTIVE_DEGRADATION.clear()

            self.assertEqual(result.state, "degraded")
            self.assertEqual(result.reason_code, "heal_busy_local_fallback")
            self.assertIs(result.fallback_used, True)

    def test_refresh_cache_rebuild_failure_returns_structured_failed_result(self):
        from hextech.bootstrap import data_refresh as refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events_file = tmp_path / "runtime_events.jsonl"

            def state_path(name: str) -> str:
                return str(events_file if name == "runtime_events.v1.jsonl" else tmp_path / name)

            report = {
                "requested": ["hextech_rankings"],
                "repaired": ["hextech_rankings"],
                "fallback": [],
                "failed": [],
            }
            with (
                mock.patch.object(refresh, "build_runtime_state_path", state_path),
                mock.patch.object(refresh, "get_latest_valid_csv", lambda: ""),
                mock.patch.object(refresh, "get_latest_csv", lambda: ""),
                mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: dict(report)),
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None, force=False: None),
                mock.patch.object(
                    refresh,
                    "rebuild_api_cache_if_needed",
                    side_effect=RuntimeError("cache token=secret"),
                ),
            ):
                refresh._ACTIVE_DEGRADATION.clear()
                result = refresh.refresh_backend_data(force=True)
                refresh._ACTIVE_DEGRADATION.clear()

            self.assertEqual(result.state, "failed")
            self.assertEqual(result.reason_code, "api_cache_rebuild_failed")
            self.assertIn("api_cache", result.report["failed"])
            self.assertEqual(result.report["stage_errors"][0]["stage"], "api_cache")
            self.assertNotIn("secret", result.report["stage_errors"][0]["error_message"])
            events = _read_jsonl(events_file)
            self.assertEqual(events[-1]["event"], "refresh.failed")

    def test_force_refresh_does_not_rebuild_matching_api_cache_twice(self):
        from hextech.bootstrap import data_refresh as refresh

        report = {
            "requested": ["hextech_rankings"],
            "repaired": ["hextech_rankings"],
            "fallback": [],
            "failed": [],
        }
        cache_checks: list[bool] = []
        with (
            mock.patch.object(refresh, "heal_runtime_artifacts", return_value=report),
            mock.patch.object(refresh, "_run_mayhem_refresh_safely"),
            mock.patch.object(refresh, "get_latest_valid_csv", return_value=__file__),
            mock.patch.object(refresh, "get_latest_csv", return_value=__file__),
            mock.patch.object(
                refresh,
                "rebuild_api_cache_if_needed",
                side_effect=lambda force=False: cache_checks.append(force) or True,
            ),
            mock.patch.object(refresh, "_write_refresh_state_event"),
        ):
            result = refresh.refresh_backend_data(force=True)

        self.assertEqual(result.state, "ready")
        self.assertEqual(cache_checks, [False])

    def test_sanitize_event_message_removes_sensitive_material(self):
        from hextech.bootstrap.data_refresh import sanitize_event_message

        raw = (
            "GET http://127.0.0.1:1234/path?token=secret Authorization: Bearer abc "
            "Set-Cookie: sid=123 cookie=value nonce=xyz"
        )

        sanitized = sanitize_event_message(raw)

        self.assertNotIn("secret", sanitized)
        self.assertNotIn("Bearer abc", sanitized)
        self.assertNotIn("sid=123", sanitized)
        self.assertNotIn("nonce=xyz", sanitized)
        self.assertIn("http://127.0.0.1:1234/path", sanitized)

    def test_api_cache_failure_remains_primary_when_other_stages_also_fail(self):
        from hextech.bootstrap import data_refresh as refresh

        report = {
            "requested": ["api_cache", "champion_core"],
            "repaired": [],
            "fallback": [],
            "failed": ["api_cache", "champion_core"],
        }
        with (
            mock.patch.object(refresh, "get_latest_valid_csv", lambda: ""),
            mock.patch.object(refresh, "get_latest_csv", lambda: ""),
        ):
            result = refresh._result_from_report(report, force=True, correlation_id="combined-failure")

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.reason_code, "api_cache_rebuild_failed")
        self.assertEqual(set(result.report["failed"]), {"api_cache", "champion_core"})


if __name__ == "__main__":
    unittest.main()

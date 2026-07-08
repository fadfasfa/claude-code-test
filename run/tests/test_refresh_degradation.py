"""测试 refresh 降级路径。

调用方: pytest; 关键依赖: hextech.core.refresh。
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
        from hextech.scraping import heal_worker

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
        from hextech.scraping import heal_worker

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

    def test_scraper_failure_records_attempt_diagnostics(self):
        from hextech.scraping.hextech import scraper

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "scraper_status.json"
            active_csv = tmp_path / "Hextech_Data_2026-07-02.csv"
            active_csv.write_text("valid\n", encoding="utf-8")
            attempt = scraper._new_attempt_context()
            attempt.update(
                {
                    "total_heroes": 173,
                    "completed_heroes": 120,
                    "cdn_hit_count": 80,
                    "slow_path_count": 40,
                    "success_rows": 2400,
                    "failure_samples": [{"champion_id": "266", "reason": "thread_pool_timeout"}],
                }
            )

            with (
                mock.patch.object(scraper, "build_runtime_state_path", lambda name: str(status_file)),
                mock.patch.object(scraper, "load_scraper_status", lambda: {}),
                mock.patch.object(scraper, "get_latest_valid_csv", lambda: str(active_csv)),
            ):
                result = scraper._finish_refresh_failure(
                    "thread_pool_timeout",
                    started_at=0.0,
                    attempt=attempt,
                    failure_stage="detail_initial",
                )

            payload = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertTrue(result)
            self.assertEqual(payload["last_result"], "fallback")
            self.assertEqual(payload["failure_stage"], "detail_initial")
            self.assertEqual(payload["last_attempt"]["result"], "fallback")
            self.assertEqual(payload["last_attempt"]["completed_heroes"], 120)
            self.assertTrue(payload["fallback_used"])

    def test_main_scraper_timeout_records_fallback_without_blocking_shutdown(self):
        from hextech.scraping.hextech import scraper

        class Response:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        class PendingFuture:
            cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True
                return True

        shutdown_calls = []
        submitted = []

        class FakeExecutor:
            def __init__(self, max_workers):
                self.max_workers = max_workers

            def submit(self, *_args, **_kwargs):
                future = PendingFuture()
                submitted.append(future)
                return future

            def shutdown(self, *, wait=True, cancel_futures=False):
                shutdown_calls.append({"wait": wait, "cancel_futures": cancel_futures})

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "scraper_status.json"
            active_csv = tmp_path / "Hextech_Data_2026-07-05.csv"
            active_csv.write_text("valid\n", encoding="utf-8")

            def state_path(name: str) -> str:
                return str(status_file if name == "scraper_status.json" else tmp_path / name)

            with (
                mock.patch.object(scraper, "build_runtime_state_path", state_path),
                mock.patch.object(scraper, "build_daily_csv_path", lambda _date: str(tmp_path / "out.csv")),
                mock.patch.object(scraper, "check_execution_permission", lambda force=False: (True, "test")),
                mock.patch.object(scraper, "load_augment_map", lambda: {"炼狱导管": "棱彩"}),
                mock.patch.object(scraper, "load_champion_core_data", lambda: {"63": {"name": "复仇焰魂"}}),
                mock.patch.object(
                    scraper,
                    "fetch_with_retry",
                    side_effect=[
                        Response({"1045": {"displayName": "炼狱导管", "rarity": 3}}),
                        Response([{"championId": 63}, {"championId": 64}]),
                    ],
                ),
                mock.patch.object(
                    scraper,
                    "fetch_champion_detail_stats_fast",
                    lambda *_args, **_kwargs: {
                        "champ": {"championId": 63},
                        "name": "复仇焰魂",
                        "rows": [{"英雄名称": "复仇焰魂", "海克斯名称": "炼狱导管"}],
                        "reason": "",
                        "status_code": 200,
                        "url": "fixture",
                        "error": "",
                    },
                ),
                mock.patch.object(scraper, "ThreadPoolExecutor", FakeExecutor),
                mock.patch.object(scraper, "as_completed", side_effect=scraper.TimeoutError()),
                mock.patch.object(scraper, "get_latest_valid_csv", lambda: str(active_csv)),
                mock.patch.object(scraper, "load_scraper_status", lambda: {}),
            ):
                result = scraper.main_scraper(force=True)

            payload = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertTrue(result)
            self.assertEqual(payload["last_result"], "fallback")
            self.assertEqual(payload["reason"], "thread_pool_timeout")
            self.assertEqual(payload["failure_stage"], "detail_initial")
            self.assertEqual(payload["active_csv"], str(active_csv))
            self.assertEqual(payload["last_attempt"]["completed_heroes"], 2)
            self.assertEqual(payload["last_attempt"]["failure_samples"][0]["reason"], "thread_pool_timeout")
            self.assertEqual(shutdown_calls, [{"wait": False, "cancel_futures": True}])
            self.assertTrue(all(future.cancelled for future in submitted))

    def test_heal_worker_exception_clears_in_progress_tasks(self):
        from hextech.scraping import heal_worker

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
        from hextech.core import refresh

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
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None: None),
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
        from hextech.core import refresh

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
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None: None),
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
        from hextech.scraping import heal_worker

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
        from hextech.core import refresh

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
                mock.patch.object(refresh, "_run_mayhem_refresh_safely", lambda stop_event=None: None),
                mock.patch.object(refresh, "heal_runtime_artifacts", lambda force=False, stop_event=None: busy_report),
            ):
                refresh._ACTIVE_DEGRADATION.clear()
                result = refresh.refresh_backend_data(force=False)
                refresh._ACTIVE_DEGRADATION.clear()

            self.assertEqual(result.state, "degraded")
            self.assertEqual(result.reason_code, "heal_busy_local_fallback")
            self.assertIs(result.fallback_used, True)

    def test_sanitize_event_message_removes_sensitive_material(self):
        from hextech.core.refresh import sanitize_event_message

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


if __name__ == "__main__":
    unittest.main()

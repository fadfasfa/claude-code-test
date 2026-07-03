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

    def test_refresh_result_records_fallback_activation_and_recovery(self):
        from hextech.core import refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            events_file = tmp_path / "runtime_events.jsonl"
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

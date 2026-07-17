"""测试 运行态诊断收集器。

调用方: pytest; 关键依赖: tooling.diagnostics.runtime。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class RuntimeDiagnosticsCollectorTests(unittest.TestCase):
    def test_collects_runtime_logs_and_skips_sensitive_files(self):
        from tooling.diagnostics.runtime import collect_runtime_diagnostics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime"
            state = root / "state"
            logs = root / "logs"
            debug = root / "debug" / "official_overlay_provider"
            output = Path(tmp) / "bundle"
            state.mkdir(parents=True)
            logs.mkdir(parents=True)
            debug.mkdir(parents=True)

            (state / "startup_status.json").write_text(
                json.dumps(
                    {
                        "hextech_ready": True,
                        "hextech_degraded": True,
                        "in_progress_tasks": [],
                        "hextech_refresh": {
                            "attempt_id": "attempt-1",
                            "failure_stage": "detail_initial",
                            "fallback_used": True,
                            "active_csv": "Hextech_Data_2026-07-02.csv",
                        },
                        "hextech_warning": "check auth_token.txt local.yaml proxies.json accounts.json auth.json .env",
                        "nested": {"auth.json": "present", "message": "uses .env"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "scraper_status.json").write_text(
                json.dumps(
                    {
                        "last_result": "fallback",
                        "reason": "thread_pool_timeout",
                        "last_attempt_id": "attempt-1",
                        "active_csv": "Hextech_Data_2026-07-02.csv",
                        "last_attempt": {
                            "attempt_id": "attempt-1",
                            "result": "fallback",
                            "reason": "thread_pool_timeout",
                            "failure_stage": "detail_initial",
                            "total_heroes": 173,
                            "completed_heroes": 120,
                            "cdn_hit_count": 80,
                            "slow_path_count": 40,
                            "success_rows": 2400,
                            "failure_count": 53,
                            "failure_samples": [{"champion_id": "266", "reason": "thread_pool_timeout"}],
                            "fallback_used": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "runtime_events.v1.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"event": "refresh.started", "level": "INFO"}),
                        json.dumps(
                            {
                                "event": "fallback.activated",
                                "level": "WARNING",
                                "reason_code": "remote_http_error",
                                "published_data_path": str(root / "raw" / "hextech" / "Hextech_Data_2026-07-05.csv"),
                                "fallback_path": "C:/Users/apple/claudecode/run/var/sources/hextech/runs/example/stats.csv",
                                "url": "https://example.test/path?access_token=secret-token",
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (state / "supervisor_events.v1.jsonl").write_text(
                json.dumps({"event": "refresh.completed", "level": "INFO", "correlation_id": "act-1"}) + "\n",
                encoding="utf-8",
            )
            (state / "overlay_vision_trace_history.v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "entries": [
                            {
                                "generated_at": 1000.0,
                                "active": False,
                                "visible": False,
                                "selection_type": "hextech",
                                "reason": "partial_ready",
                                "gate_state": "partial_ready",
                                "scene_state": "candidate",
                                "scene_kind": "hextech",
                                "selection_epoch": 1,
                                "ready_slots": 1,
                                "selection_button_present": True,
                                "selection_window_active": True,
                                "slot_signature": ["ready:a", "empty:", "empty:"],
                            },
                            {
                                "generated_at": 1004.0,
                                "active": True,
                                "visible": True,
                                "selection_type": "hextech",
                                "reason": "",
                                "gate_state": "visible_ready",
                                "scene_state": "active",
                                "scene_kind": "hextech",
                                "selection_epoch": 1,
                                "ready_slots": 3,
                                "selection_button_present": True,
                                "selection_window_active": True,
                                "slot_signature": ["ready:a", "ready:b", "ready:c"],
                            },
                            {
                                "generated_at": 1010.0,
                                "active": False,
                                "visible": False,
                                "selection_type": "body_shard",
                                "reason": "body_shard_only",
                                "gate_state": "blocked",
                                "scene_kind": "body_shard",
                                "selection_epoch": 2,
                                "body_shard_latched": True,
                                "ready_slots": 0,
                            },
                            {
                                "generated_at": 1020.0,
                                "active": False,
                                "visible": False,
                                "selection_type": "hextech",
                                "reason": "game_not_foreground",
                                "gate_state": "blocked",
                                "scene_state": "blocked",
                                "selection_epoch": 3,
                                "ready_slots": 0,
                            },
                            {
                                "generated_at": 1022.0,
                                "active": True,
                                "visible": True,
                                "selection_type": "hextech",
                                "reason": "",
                                "gate_state": "visible_ready",
                                "scene_state": "active",
                                "scene_kind": "hextech",
                                "selection_epoch": 3,
                                "ready_slots": 3,
                            },
                            {
                                "generated_at": 1024.0,
                                "active": True,
                                "visible": True,
                                "selection_type": "hextech",
                                "reason": "hover_occluded",
                                "gate_state": "visible_ready",
                                "scene_state": "active",
                                "scene_kind": "hextech",
                                "selection_epoch": 3,
                                "ready_slots": 3,
                                "cursor_over_cards": True,
                                "card_residue": True,
                                "hover_occluded": True,
                                "slot_signature": ["ready:aram_getexcited", "ready:b", "ready:c"],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "overlay_vision_trace.v1.json").write_text(
                json.dumps(
                    {
                        "generated_at": 1022.0,
                        "active": True,
                        "visible": True,
                        "selection_type": "hextech",
                        "gate_state": "visible_ready",
                        "selection_epoch": 3,
                        "ready_slots": 3,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "game_overlay_sidecar_status.json").write_text(
                json.dumps({"status": "running", "template_count": 209}, ensure_ascii=False),
                encoding="utf-8",
            )
            (state / "game_overlay_context.v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ok": True,
                        "champion_id": "266",
                        "champion_name": "暗裔剑魔",
                        "source": "test",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "game_overlay_slots.v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "generated_at": 1024.0,
                        "active": True,
                        "selection_type": "hextech",
                        "source": {"reason": "hover_occluded", "ready_slots": 3, "selection_window_active": True},
                        "slots": [
                            {"slot": 0, "state": "ready", "augment_id": "aram_getexcited", "name": ""},
                            {"slot": 1, "state": "detecting", "augment_id": "", "name": ""},
                            {"slot": 2, "state": "detecting", "augment_id": "", "name": ""},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "ui_feature_flags.json").write_text(
                json.dumps({"game_overlay_enabled": True, "low_frequency_listener_enabled": True}, ensure_ascii=False),
                encoding="utf-8",
            )
            cache = root / "cache"
            cache.mkdir()
            (cache / "overlay_hint_cache.v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": {
                            "private_policy_stats_enabled": True,
                            "data_source": "runtime-csv",
                            "runtime_csv": "Hextech_Data_2026-06-24.csv",
                        },
                        "hints": {
                            "1322": {
                                "augment_id": "1322",
                                "name": "罪恶快感",
                                "tier": "Gold",
                                "stats_by_champion_id": {"266": {"winrate": 0.51, "pickrate": 0.07}},
                            },
                            "9999": {
                                "augment_id": "9999",
                                "name": "稀疏样本",
                                "tier": "Gold",
                                "stats_by_champion_id": {},
                            },
                        },
                        "name_index": {"aram_getexcited": "1322", "罪恶快感": "1322"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "auth_token.txt").write_text("secret-token", encoding="utf-8")
            (state / "lcu_session.json").write_text('{"session":"secret"}', encoding="utf-8")
            (state / "riot_client_state.json").write_text('{"token":"secret"}', encoding="utf-8")
            (logs / "hextech_runtime_summary.log").write_text(
                "ok\nERROR overlay failed local.yaml proxies.json accounts.json\n",
                encoding="utf-8",
            )
            (debug / "official-overlay.json").write_text(
                json.dumps(
                    {
                        "status": "candidates_ready",
                        "generated_at": 1022.0,
                        "choices": [
                            {"slot": 0, "state": "ready"},
                            {"slot": 1, "state": "ready"},
                            {"slot": 2, "state": "ready"},
                        ],
                        "diagnostics": {"reason": "", "live_client": {"reason": "ok"}, "lcu": {"reason": "ok"}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (debug / "sensitive-debug.json").write_text(
                json.dumps(
                    {
                        "auth_token.txt": "file-name-key",
                        "local.yaml": "config-name-key",
                        "path": str(root / "raw" / "hextech" / "Hextech_Data_2026-07-05.csv"),
                        "url": "https://example.test/debug?access_token=secret-token",
                        "note": "auth_token.txt local.yaml proxies.json accounts.json should not leak",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (debug / "sensitive-debug.log").write_text(
                f"debug path={root}\\raw\\hextech\\Hextech_Data_2026-07-05.csv token=secret-token local.yaml proxies.json accounts.json\n",
                encoding="utf-8",
            )

            summary = collect_runtime_diagnostics(
                output_dir=output,
                runtime_root=root,
                recent_minutes=60,
                tail_lines=20,
            )

            self.assertEqual(summary["state"]["startup_status"]["hextech_ready"], True)
            self.assertIn("fallback.activated", summary["events"]["events"])
            validation = summary["game_validation"]
            trace = validation["selection_trace"]
            self.assertGreaterEqual(trace["selection_epoch_count"], 2)
            self.assertGreaterEqual(trace["active_hextech_samples"], 2)
            self.assertEqual(trace["body_shard_samples"], 1)
            self.assertEqual(trace["not_foreground_samples"], 1)
            self.assertEqual(trace["hover_occluded_samples"], 1)
            self.assertEqual(trace["hover_expired_samples"], 0)
            self.assertTrue(trace["acceptance_observations"]["saw_hover_occlusion"])
            self.assertEqual(trace["selection_button_present_samples"], 2)
            self.assertGreaterEqual(len(trace["selection_epoch_timeline"]), 3)
            self.assertTrue(any(item["waiting_resolved_to_visible"] for item in trace["selection_epoch_timeline"]))
            self.assertIn("fallback.activated", validation["refresh"]["event_counts"])
            self.assertEqual(validation["refresh"]["scraper_attempt"]["attempt_id"], "attempt-1")
            self.assertEqual(validation["refresh"]["scraper_attempt"]["failure_stage"], "detail_initial")
            self.assertTrue(validation["refresh"]["scraper_attempt"]["fallback_used"])
            self.assertEqual(validation["refresh"]["scraper_attempt"]["cdn_hit_count"], 80)
            self.assertGreaterEqual(validation["refresh"]["actions"]["action_count"], 1)
            self.assertEqual(validation["official_provider"]["ready_sample_count"], 1)
            self.assertEqual(validation["host_self_check"]["skipped"], "custom_runtime_root")
            self.assertEqual(validation["hint_cache"]["hint_count"], 2)
            self.assertEqual(validation["hint_cache"]["zero_stats_hint_count"], 1)
            self.assertEqual(validation["render_status"]["status_counts"]["READY"], 1)
            self.assertEqual(validation["render_status"]["context"]["champion_id"], "266")
            self.assertEqual(summary["state"]["web_frontend"]["status"], "web_disabled_until_user_action")
            self.assertTrue(
                any("web_disabled_until_user_action" in item for item in validation["attention_items"])
            )
            self.assertIn("quick_customization_scope", validation)
            self.assertTrue(trace["acceptance_observations"]["saw_multiple_hextech_selections"])
            self.assertTrue(trace["acceptance_observations"]["saw_body_shard"])
            self.assertTrue(trace["acceptance_observations"]["saw_window_switch_or_background"])
            self.assertEqual(len(summary["logs"]["recent_problem_lines"]), 1)
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "state" / "startup_status.json").is_file())
            self.assertTrue((output / "state_tail" / "runtime_events.v1.jsonl.tail").is_file())
            self.assertTrue((output / "state_tail" / "overlay_vision_trace_history.v1.json.tail").is_file())
            self.assertTrue((output / "logs_tail" / "hextech_runtime_summary.log.tail").is_file())
            self.assertFalse((output / "state" / "auth_token.txt").exists())
            self.assertFalse((output / "state" / "lcu_session.json").exists())
            self.assertFalse((output / "state" / "riot_client_state.json").exists())
            self.assertGreaterEqual(len(summary["skipped_sensitive"]), 3)
            skipped_sensitive_blob = json.dumps(summary["skipped_sensitive"], ensure_ascii=False)
            self.assertIn("<sensitive-file>", skipped_sensitive_blob)
            self.assertNotIn("auth_token.txt", skipped_sensitive_blob)
            self.assertNotIn("lcu_session.json", skipped_sensitive_blob)
            self.assertNotIn("riot_client_state.json", skipped_sensitive_blob)
            exported_blob = (
                (output / "summary.json").read_text(encoding="utf-8")
                + (output / "state_tail" / "runtime_events.v1.jsonl.tail").read_text(encoding="utf-8")
                + (output / "state" / "startup_status.json").read_text(encoding="utf-8")
                + (output / "logs_tail" / "hextech_runtime_summary.log.tail").read_text(encoding="utf-8")
                + (output / "debug_recent" / "official_overlay_provider" / "sensitive-debug.json").read_text(encoding="utf-8")
                + (output / "debug_recent" / "official_overlay_provider" / "sensitive-debug.log").read_text(encoding="utf-8")
            )
            self.assertNotIn(str(root), exported_blob)
            self.assertNotIn("C:/Users/apple", exported_blob)
            self.assertNotIn("C:\\Users\\apple", exported_blob)
            self.assertNotIn("Hextech_Data_2026-07-05.csv", exported_blob)
            self.assertNotIn("secret-token", exported_blob)
            self.assertNotIn("auth_token.txt", exported_blob)
            self.assertNotIn("local.yaml", exported_blob)
            self.assertNotIn("proxies.json", exported_blob)
            self.assertNotIn("accounts.json", exported_blob)
            self.assertNotIn("auth.json", exported_blob)
            self.assertNotIn(".env", exported_blob)
            self.assertIn("<sensitive-file>", exported_blob)
            self.assertIn("<local-path>", exported_blob)
            self.assertIn("https://example.test/path?<redacted>", exported_blob)

    def test_watch_writes_periodic_snapshots(self):
        from tooling.diagnostics.runtime import watch_runtime_diagnostics

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime"
            state = root / "state"
            output = Path(tmp) / "watch"
            state.mkdir(parents=True)
            (state / "overlay_vision_trace_history.v1.json").write_text(
                json.dumps({"schema_version": 2, "entries": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            manifest = watch_runtime_diagnostics(
                output_dir=output,
                runtime_root=root,
                recent_minutes=5,
                tail_lines=5,
                interval_seconds=1,
                max_snapshots=2,
                sleep_func=lambda _seconds: None,
            )

            self.assertEqual(manifest["snapshot_count"], 2)
            self.assertTrue((output / "watch_summary.json").is_file())
            self.assertTrue((output / "latest_summary.json").is_file())
            watch_summary_text = (output / "watch_summary.json").read_text(encoding="utf-8")
            manifest_blob = json.dumps(manifest, ensure_ascii=False) + watch_summary_text
            self.assertNotIn(str(root), manifest_blob)
            self.assertNotIn(str(output), manifest_blob)
            self.assertNotIn("C:\\Users\\apple", manifest_blob)
            self.assertIn("<local-path>", manifest_blob)
            event_lines = (output / "watch_events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(event_lines), 2)
            first_event = json.loads(event_lines[0])
            self.assertEqual(first_event["snapshot_index"], 1)
            self.assertIn("attention_items", first_event)
            self.assertEqual(len(list((output / "snapshots").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()

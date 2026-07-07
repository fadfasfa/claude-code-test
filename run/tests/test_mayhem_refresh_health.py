"""Mayhem refresh and scrape-health regression tests.

调用方: pytest; 关键依赖: hextech.scraping.synergy.mayhem_refresh、tools.dev_checks。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class MayhemRefreshHealthTests(unittest.TestCase):
    def _build_health_summary_fixture(
        self,
        *,
        cleaned_payload: dict,
        overlay_payload: dict,
        runtime_raw_items: list | None = None,
    ) -> dict:
        import tools.dev_checks as dev_checks

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        csv_path = tmp_path / "Hextech_Data_2026-07-05.csv"
        rows = [
            "英雄 ID,英雄名称,英雄评级,英雄胜率,英雄出场率,海克斯ID,源站排名,源站层级,海克斯阶级,海克斯名称,海克斯胜率,海克斯出场率,胜率差,综合得分",
        ]
        for index in range(300):
            rows.append(f"{index},英雄{index},1,0.5,0.1,aug{index},1,T1,棱彩,海克斯{index},0.55,0.02,0.05,1.0")
        csv_path.write_text("\n".join(rows), encoding="utf-8-sig")
        state_dir = tmp_path / "state"
        cache_dir = tmp_path / "cache"
        evidence_dir = tmp_path / "evidence"
        static_dir = tmp_path / "static"
        logs_dir = tmp_path / "logs"
        state_dir.mkdir()
        cache_dir.mkdir()
        evidence_dir.mkdir()
        static_dir.mkdir()
        logs_dir.mkdir()
        (state_dir / "scraper_status.json").write_text(
            json.dumps({"last_result": "fallback", "reason": "thread_pool_timeout"}, ensure_ascii=False),
            encoding="utf-8",
        )
        (state_dir / "mayhem_refresh_status.json").write_text(
            json.dumps({"last_result": "skipped", "reason": "not_stale", "last_success_at": ""}, ensure_ascii=False),
            encoding="utf-8",
        )
        (logs_dir / "hextech_error.log").write_text(
            "Hextech detail pass timed out: label=initial reason=thread_pool_timeout\n"
            "[scrapling] curl: (28) timeout\n",
            encoding="utf-8",
        )
        (evidence_dir / "mayhem_combos.raw.json").write_text(
            json.dumps({"items": [{"champion": "Brand"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        if runtime_raw_items is not None:
            (cache_dir / "mayhem_combos.raw.json").write_text(
                json.dumps({"items": runtime_raw_items}, ensure_ascii=False),
                encoding="utf-8",
            )
        (static_dir / "Champion_Synergy_Cleaned.json").write_text(
            json.dumps(cleaned_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        (cache_dir / "overlay_hint_cache.v1.json").write_text(
            json.dumps(overlay_payload, ensure_ascii=False),
            encoding="utf-8",
        )

        with (
            mock.patch.object(dev_checks.runtime_store, "get_latest_valid_csv", lambda: str(csv_path)),
            mock.patch.object(dev_checks.runtime_store, "get_latest_csv", lambda: str(csv_path)),
            mock.patch.object(dev_checks.runtime_store, "get_runtime_root_dir", lambda: tmp_path),
            mock.patch.object(dev_checks.runtime_store, "build_runtime_state_path", lambda name: str(state_dir / name)),
            mock.patch.object(dev_checks.runtime_store, "build_runtime_cache_path", lambda name: str(cache_dir / name)),
            mock.patch.object(dev_checks.runtime_store, "build_synergy_refresh_status_path", lambda: str(state_dir / "synergy_refresh_status.json")),
            mock.patch.object(dev_checks.runtime_store, "build_synergy_cleaned_data_path", lambda: str(static_dir / "Champion_Synergy_Cleaned.json")),
            mock.patch.object(dev_checks, "DATA_EVIDENCE_DIR", evidence_dir),
            mock.patch("hextech.overlay.hints.OVERLAY_HINT_CACHE_FILE", cache_dir / "overlay_hint_cache.v1.json"),
            mock.patch("hextech.scraping.synergy.mayhem_refresh.build_runtime_state_path", lambda name: str(state_dir / name)),
            mock.patch("hextech.scraping.synergy.mayhem_refresh.build_runtime_cache_path", lambda name: str(cache_dir / name)),
        ):
            return dev_checks.build_hextech_scrape_health_summary()

    def test_mayhem_due_ignores_last_attempt_without_success(self):
        from hextech.scraping.synergy import mayhem_refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            status_file = tmp_path / "mayhem_refresh_status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "last_attempt_at": "2026-07-05T13:50:43+00:00",
                        "last_success_at": "",
                        "last_result": "skipped",
                        "reason": "not_stale",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with mock.patch.object(mayhem_refresh, "build_runtime_state_path", lambda name: str(status_file)):
                self.assertTrue(mayhem_refresh.mayhem_refresh_due(now=1783260000.0))

    def test_mayhem_success_writes_runtime_raw_status_and_rebuilds_hints(self):
        from hextech.scraping.synergy import mayhem_refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            cache_dir = tmp_path / "cache"
            state_dir.mkdir()
            cache_dir.mkdir()
            rebuilt = []

            def state_path(name: str) -> str:
                return str(state_dir / name)

            def cache_path(name: str) -> str:
                return str(cache_dir / name)

            def merge(**kwargs):
                raw_path = Path(kwargs["mayhem_raw_path"])
                self.assertEqual(raw_path, cache_dir / "mayhem_combos.raw.json")
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
                self.assertEqual(len(payload["items"]), 1)
                return {"written": True, "added_items": 1, "output_path": str(tmp_path / "cleaned.json")}

            with (
                mock.patch.object(mayhem_refresh, "build_runtime_state_path", state_path),
                mock.patch.object(mayhem_refresh, "build_runtime_cache_path", cache_path),
            ):
                result = mayhem_refresh.run_mayhem_refresh(
                    now=1783260000.0,
                    scraper=lambda: {"items": [{"champion": "Brand"}], "rejects": []},
                    merge=merge,
                    rebuild_hint_cache=lambda: rebuilt.append("yes"),
                )

            self.assertEqual(result["last_result"], "success")
            self.assertEqual(result["raw_items"], 1)
            self.assertEqual(result["added_items"], 1)
            self.assertTrue(result["last_success_at"])
            self.assertEqual(rebuilt, ["yes"])
            self.assertTrue((cache_dir / "mayhem_combos.raw.json").exists())

    def test_mayhem_failure_records_merge_exception(self):
        from hextech.scraping.synergy import mayhem_refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            cache_dir = tmp_path / "cache"
            state_dir.mkdir()
            cache_dir.mkdir()

            with (
                mock.patch.object(mayhem_refresh, "build_runtime_state_path", lambda name: str(state_dir / name)),
                mock.patch.object(mayhem_refresh, "build_runtime_cache_path", lambda name: str(cache_dir / name)),
            ):
                result = mayhem_refresh.run_mayhem_refresh(
                    now=1783260000.0,
                    scraper=lambda: {"items": [{"champion": "Brand"}], "rejects": []},
                    merge=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("merge boom")),
                    rebuild_hint_cache=lambda: None,
                )

            self.assertEqual(result["last_result"], "failed")
            self.assertIn("RuntimeError: merge boom", result["reason"])
            self.assertEqual(result["raw_items"], 1)

    def test_mayhem_cleaned_not_written_records_status_summary(self):
        from hextech.scraping.synergy import mayhem_refresh

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            cache_dir = tmp_path / "cache"
            state_dir.mkdir()
            cache_dir.mkdir()

            summary = {"written": False, "added_items": 0, "mayhem_raw_items": 1}
            with (
                mock.patch.object(mayhem_refresh, "build_runtime_state_path", lambda name: str(state_dir / name)),
                mock.patch.object(mayhem_refresh, "build_runtime_cache_path", lambda name: str(cache_dir / name)),
            ):
                result = mayhem_refresh.run_mayhem_refresh(
                    now=1783260000.0,
                    scraper=lambda: {"items": [{"champion": "Brand"}], "rejects": []},
                    merge=lambda **kwargs: summary,
                    rebuild_hint_cache=lambda: self.fail("cleaned_not_written must not rebuild overlay hints"),
                )

            self.assertEqual(result["last_result"], "failed")
            self.assertEqual(result["reason"], "cleaned_not_written")
            self.assertEqual(result["raw_items"], 1)
            self.assertEqual(result["added_items"], 0)
            self.assertEqual(result["summary"], summary)

    def test_overlay_hint_cache_preserves_arammayhem_source(self):
        from hextech.overlay import hints

        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "Champion_Synergy_Cleaned.json"
            snapshot.write_text(
                json.dumps(
                    {
                        "63": {
                            "id": "63",
                            "name": "复仇焰魂",
                            "synergy_items": [
                                {
                                    "augment_names": ["炼狱导管"],
                                    "tier": "棱彩",
                                    "rating": "S",
                                    "tag": "强力联动",
                                    "content": "技能灼烧不断缩减冷却。",
                                    "source": "arammayhem",
                                    "source_url": "https://arammayhem.com/zh-cn/combo/brand-infernal-conduit/",
                                    "source_rating": "S+",
                                    "source_tier": "Curated",
                                }
                            ],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cache = hints.build_overlay_hint_cache(
                {"复仇焰魂": {"comprehensive": [{"海克斯ID": "1045", "海克斯名称": "炼狱导管", "海克斯阶级": "棱彩"}]}},
                synergy_snapshot_path=snapshot,
            )

            synergies = cache["hints"]["1045"]["synergies"]
            self.assertEqual(synergies[0]["source"], "arammayhem")
            self.assertIn("arammayhem.com", synergies[0]["source_url"])

    def test_health_summary_distinguishes_raw_captured_not_published(self):
        summary = self._build_health_summary_fixture(
            cleaned_payload={"63": {"synergy_items": [{"augment_names": ["炼狱导管"], "content": "old"}]}},
            overlay_payload={"hints": {"1045": {"synergies": [{"content": "old"}]}}},
        )

        self.assertEqual(summary["hextech"]["issue"], "data_available_refresh_failed")
        self.assertGreater(summary["hextech"]["error_log"]["thread_pool_timeout_mentions"], 0)
        self.assertGreater(summary["hextech"]["error_log"]["scrapling_timeout_mentions"], 0)
        self.assertEqual(summary["mayhem"]["issue"], "raw_captured_not_published")
        self.assertEqual(summary["synergy"]["issue"], "old_synergy_visible_without_mayhem_source")

    def test_health_summary_distinguishes_mayhem_not_in_overlay(self):
        summary = self._build_health_summary_fixture(
            cleaned_payload={
                "63": {
                    "synergy_items": [
                        {"augment_names": ["炼狱导管"], "content": "new", "source": "arammayhem"}
                    ]
                }
            },
            overlay_payload={"hints": {"1045": {"synergies": [{"content": "old"}]}}},
            runtime_raw_items=[{"champion": "Brand"}],
        )

        self.assertEqual(summary["synergy"]["issue"], "old_synergy_visible_new_mayhem_not_in_overlay")

    def test_health_summary_distinguishes_missing_overlay_synergy_hints(self):
        summary = self._build_health_summary_fixture(
            cleaned_payload={
                "63": {
                    "synergy_items": [
                        {"augment_names": ["炼狱导管"], "content": "new", "source": "arammayhem"}
                    ]
                }
            },
            overlay_payload={"hints": {"1045": {"name": "炼狱导管"}}},
            runtime_raw_items=[{"champion": "Brand"}],
        )

        self.assertEqual(summary["synergy"]["issue"], "no_overlay_synergy_hints")


if __name__ == "__main__":
    unittest.main()

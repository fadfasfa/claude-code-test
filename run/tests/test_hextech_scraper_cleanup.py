"""测试 hextech scraper 清理。

调用方: pytest; 关键依赖: hextech.scraping.hextech.scraper。
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


def _date_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


class HextechScraperCleanupTests(unittest.TestCase):
    def test_cleanup_old_csvs_uses_separate_retention_windows(self):
        from hextech.scraping.hextech import scraper

        with tempfile.TemporaryDirectory() as tmp:
            csv_dir = Path(tmp)
            backup_dir = csv_dir / "backups"
            backup_dir.mkdir()

            root_old = csv_dir / f"Hextech_Data_{_date_days_ago(20)}.csv"
            root_recent = csv_dir / f"Hextech_Data_{_date_days_ago(5)}.csv"
            tmp_old = csv_dir / f".Hextech_Data_{_date_days_ago(3)}.csv.tmp"
            tmp_recent = csv_dir / f".Hextech_Data_{_date_days_ago(0)}.csv.tmp"
            backup_old = backup_dir / f"Hextech_Data_{_date_days_ago(10)}.backup-20260703-120000.csv"
            backup_recent = backup_dir / f"Hextech_Data_{_date_days_ago(3)}.backup-20260703-120000.csv"

            for path in (root_old, root_recent, tmp_old, tmp_recent, backup_old, backup_recent):
                path.write_text("x\n", encoding="utf-8")

            with mock.patch.object(scraper, "get_runtime_hextech_data_dir", lambda: csv_dir):
                scraper.cleanup_old_csvs()

            self.assertFalse(root_old.exists())
            self.assertTrue(root_recent.exists())
            self.assertFalse(tmp_old.exists())
            self.assertTrue(tmp_recent.exists())
            self.assertFalse(backup_old.exists())
            self.assertTrue(backup_recent.exists())

    def test_detail_cdn_base_url_can_be_overridden_by_environment(self):
        from hextech.scraping.hextech import scraper

        original_env = os.environ.get("HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL")
        try:
            with mock.patch.dict(
                os.environ,
                {"HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL": "https://example.test/champion-details"},
            ):
                reloaded = importlib.reload(scraper)
                self.assertEqual(
                    reloaded.build_hextech_champion_detail_json_url("266"),
                    "https://example.test/champion-details/266.json",
                )
        finally:
            if original_env is None:
                os.environ.pop("HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL", None)
            else:
                os.environ["HEXTECH_CHAMPION_DETAIL_CDN_BASE_URL"] = original_env
            importlib.reload(scraper)

    def test_detail_pool_timeouts_are_bounded_for_fast_failure(self):
        from hextech.scraping.hextech import scraper

        self.assertEqual(scraper.HEXTECH_DETAIL_POOL_TIMEOUT_SECONDS, 180)
        self.assertEqual(scraper.HEXTECH_DETAIL_RETRY_POOL_TIMEOUT_SECONDS, 120)


if __name__ == "__main__":
    unittest.main()

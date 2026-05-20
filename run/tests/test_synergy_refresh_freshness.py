import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import processing.orchestrator as orchestrator
import processing.runtime_store as runtime_store
import scraping.heal_worker as heal_worker
from scraping.full_synergy_scraper import (
    SYNERGY_REFRESH_META_VERSION,
    write_synergy_refresh_meta,
)


class SynergyRefreshFreshnessTests(unittest.TestCase):
    def _snapshot(self, temp_dir: str, name: str = "Champion_Synergy_20260519_223505.json") -> Path:
        path = Path(temp_dir) / name
        path.write_text(json.dumps({"804": {"synergy_items": [{"content": "ok"}]}}), encoding="utf-8")
        now = time.time()
        os.utime(path, (now, now))
        return path

    def test_missing_latest_pointer_makes_synergy_stale_even_when_snapshot_mtime_is_fresh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._snapshot(temp_dir)

            with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
                self.assertFalse(heal_worker._synergy_data_fresh())
                self.assertTrue(orchestrator.should_refresh_synergy(False, 4 * 60 * 60))

    def test_valid_latest_pointer_keeps_synergy_fresh_within_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            synergy_path = self._snapshot(temp_dir)

            with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
                write_synergy_refresh_meta(
                    target_path=synergy_path,
                    base_url="https://apexlol.info/zh",
                    resources=3,
                    mapped=1,
                    stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
                )

                self.assertTrue(heal_worker._synergy_data_fresh())
                self.assertFalse(orchestrator.should_refresh_synergy(False, 4 * 60 * 60))

                meta_path = Path(temp_dir) / "Champion_Synergy_latest.v1.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(meta["version"], SYNERGY_REFRESH_META_VERSION)
                self.assertEqual(meta["filename"], synergy_path.name)
                self.assertEqual(meta["non_empty_heroes"], 1)


if __name__ == "__main__":
    unittest.main()

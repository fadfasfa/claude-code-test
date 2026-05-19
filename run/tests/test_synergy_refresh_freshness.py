import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import processing.orchestrator as orchestrator
import scraping.heal_worker as heal_worker
from scraping.full_synergy_scraper import (
    SYNERGY_REFRESH_META_FILE,
    SYNERGY_REFRESH_META_VERSION,
    write_synergy_refresh_meta,
)


class SynergyRefreshFreshnessTests(unittest.TestCase):
    def test_missing_refresh_meta_makes_synergy_stale_even_when_json_mtime_is_fresh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            synergy_path = Path(temp_dir) / "Champion_Synergy.json"
            synergy_path.write_text("{}", encoding="utf-8")
            now = time.time()
            os.utime(synergy_path, (now, now))

            with (
                patch.object(heal_worker, "build_synergy_data_path", return_value=str(synergy_path)),
                patch.object(heal_worker, "build_runtime_state_path", return_value=str(Path(temp_dir) / SYNERGY_REFRESH_META_FILE)),
                patch.object(orchestrator, "SYNERGY_FILE", str(synergy_path)),
                patch.object(orchestrator, "build_runtime_state_path", return_value=str(Path(temp_dir) / SYNERGY_REFRESH_META_FILE)),
            ):
                self.assertFalse(heal_worker._synergy_data_fresh())
                self.assertTrue(orchestrator.should_refresh_synergy(False, 4 * 60 * 60))

    def test_valid_refresh_meta_keeps_synergy_fresh_within_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            synergy_path = Path(temp_dir) / "Champion_Synergy.json"
            synergy_path.write_text(json.dumps({"804": {"synergy_items": [{"content": "ok"}]}}), encoding="utf-8")

            with (
                patch("scraping.full_synergy_scraper.build_runtime_state_path", return_value=str(Path(temp_dir) / SYNERGY_REFRESH_META_FILE)),
                patch.object(heal_worker, "build_synergy_data_path", return_value=str(synergy_path)),
                patch.object(heal_worker, "build_runtime_state_path", return_value=str(Path(temp_dir) / SYNERGY_REFRESH_META_FILE)),
                patch.object(orchestrator, "SYNERGY_FILE", str(synergy_path)),
                patch.object(orchestrator, "build_runtime_state_path", return_value=str(Path(temp_dir) / SYNERGY_REFRESH_META_FILE)),
            ):
                write_synergy_refresh_meta(target_path=synergy_path, base_url="https://apexlol.info/zh", resources=3, mapped=1)

                self.assertTrue(heal_worker._synergy_data_fresh())
                self.assertFalse(orchestrator.should_refresh_synergy(False, 4 * 60 * 60))

                meta_path = Path(temp_dir) / SYNERGY_REFRESH_META_FILE
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(meta["version"], SYNERGY_REFRESH_META_VERSION)


if __name__ == "__main__":
    unittest.main()

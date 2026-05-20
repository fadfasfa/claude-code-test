import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import processing.precomputed_cache as precomputed_cache


class PrecomputedCacheFreshnessTests(unittest.TestCase):
    def test_cache_meta_must_match_latest_csv_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
            latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
            os.utime(latest_csv, (1000, 1000))

            cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
            cache_file.write_text(
                json.dumps(
                    {
                        "meta": {
                            "source": latest_csv.name,
                            "source_mtime": 999,
                        },
                        "data": {"酒桶": {"comprehensive": [{"海克斯名称": "旧数据"}]}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)):
                self.assertFalse(precomputed_cache._cache_matches_latest_csv(str(cache_file)))

    def test_stale_hextech_cache_is_rejected_before_api_use(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
            latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
            os.utime(latest_csv, (2000, 2000))

            cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
            cache_file.write_text(
                json.dumps(
                    {
                        "meta": {
                            "source": latest_csv.name,
                            "source_mtime": 1000,
                        },
                        "data": {"酒桶": {"comprehensive": [{"海克斯名称": "旧数据"}]}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            precomputed_cache._hextech_cache_state.update({"path": "", "mtime": 0.0, "data": {}})
            with (
                patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)),
                patch.object(precomputed_cache, "_resolve_cache_file", return_value=str(cache_file)),
            ):
                self.assertIsNone(precomputed_cache.load_precomputed_hextech_for_hero("酒桶"))


if __name__ == "__main__":
    unittest.main()

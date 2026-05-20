"""协同数据快照定位与发布熔断测试。

这些测试保护 `Champion_Synergy_latest.v1.json` 指针、时间快照扫描、
旧固定文件只读兜底，以及过小抓取结果不能发布为 latest 的边界。
它不访问真实网络，只用临时目录模拟运行态协同数据目录。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import processing.runtime_store as runtime_store
from scraping.full_synergy_scraper import _validate_publish_size


class SynergySnapshotStoreTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict, mtime: int = 1000) -> Path:
        """写入带固定 mtime 的 JSON，便于验证快照排序规则。"""
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_valid_latest_pointer_resolves_timestamp_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = self._write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
            self._write_json(
                Path(temp_dir) / "Champion_Synergy_latest.v1.json",
                {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
                1001,
            )

            with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
                self.assertEqual(runtime_store.get_latest_synergy_snapshot_path(), str(snapshot.resolve()))
                self.assertEqual(runtime_store.build_synergy_data_path(), str(snapshot.resolve()))

    def test_corrupt_latest_pointer_falls_back_to_newest_snapshot_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            older = self._write_json(Path(temp_dir) / "Champion_Synergy_20260518_010101.json", {"1": {}}, 1000)
            newer = self._write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"2": {}}, 2000)
            (Path(temp_dir) / "Champion_Synergy_latest.v1.json").write_text("{bad", encoding="utf-8")

            with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
                self.assertEqual(runtime_store.get_latest_synergy_snapshot_path(), str(newer))
                self.assertNotEqual(runtime_store.get_latest_synergy_snapshot_path(), str(older))

    def test_legacy_fixed_name_is_read_only_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = self._write_json(Path(temp_dir) / "Champion_Synergy.json", {"1": {}}, 1000)

            with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
                self.assertIsNone(runtime_store.get_latest_synergy_snapshot_path())
                self.assertEqual(runtime_store.build_synergy_data_path(), str(legacy))

    def test_publish_fuse_rejects_too_small_snapshot_against_existing_baseline(self):
        with self.assertRaisesRegex(ValueError, "协同数据熔断"):
            _validate_publish_size(
                {"heroes": 172, "non_empty_heroes": 100, "synergy_entries": 700},
                {"heroes": 172, "non_empty_heroes": 136, "synergy_entries": 876},
            )


if __name__ == "__main__":
    unittest.main()

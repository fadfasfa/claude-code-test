"""协同数据 freshness 判断测试。

刷新是否成功不再由旧 `Champion_Synergy.json` 的存在性决定，而是由
latest 指针和它指向的时间快照共同决定。这里覆盖 heal worker 与
orchestrator 两条调用链，避免后台自愈把陈旧、无指针或 blocked cooldown
内的数据误判为需要立刻抓取。
"""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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
    def _snapshot(
        self,
        temp_dir: str,
        name: str = "Champion_Synergy_20260519_223505.json",
        *,
        mtime: float | None = None,
    ) -> Path:
        """构造一个 freshness 测试用的最小合法协同快照。"""
        path = Path(temp_dir) / name
        path.write_text(json.dumps({"804": {"synergy_items": [{"content": "ok"}]}}), encoding="utf-8")
        timestamp = time.time() if mtime is None else mtime
        os.utime(path, (timestamp, timestamp))
        return path

    def _patch_synergy_dir(self, temp_dir: str, status: dict | None = None):
        payload = {} if status is None else status
        return (
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(heal_worker, "load_synergy_refresh_status", return_value=payload),
            patch.object(orchestrator, "load_synergy_refresh_status", return_value=payload),
        )

    def test_missing_latest_pointer_makes_synergy_stale_even_when_snapshot_mtime_is_fresh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._snapshot(temp_dir)

            patches = self._patch_synergy_dir(temp_dir)
            with patches[0], patches[1], patches[2]:
                self.assertFalse(heal_worker._synergy_data_fresh())
                self.assertTrue(orchestrator.should_refresh_synergy(False))

    def test_valid_latest_pointer_keeps_synergy_fresh_within_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            synergy_path = self._snapshot(temp_dir)

            patches = self._patch_synergy_dir(temp_dir)
            with patches[0], patches[1], patches[2]:
                write_synergy_refresh_meta(
                    target_path=synergy_path,
                    base_url="https://apexlol.info/zh",
                    resources=3,
                    mapped=1,
                    stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
                )

                self.assertTrue(heal_worker._synergy_data_fresh())
                self.assertFalse(orchestrator.should_refresh_synergy(False))

                meta_path = Path(temp_dir) / "Champion_Synergy_latest.v1.json"
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self.assertEqual(meta["version"], SYNERGY_REFRESH_META_VERSION)
                self.assertEqual(meta["filename"], synergy_path.name)
                self.assertEqual(meta["non_empty_heroes"], 1)

    def test_weekly_synergy_window_replaces_high_frequency_refresh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_mtime = time.time() - (8 * 24 * 60 * 60)
            synergy_path = self._snapshot(temp_dir, mtime=old_mtime)

            patches = self._patch_synergy_dir(temp_dir)
            with patches[0], patches[1], patches[2]:
                write_synergy_refresh_meta(
                    target_path=synergy_path,
                    base_url="https://apexlol.info/zh",
                    resources=3,
                    mapped=1,
                    stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
                )
                os.utime(synergy_path, (old_mtime, old_mtime))
                os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

                self.assertFalse(heal_worker._synergy_data_fresh())
                self.assertTrue(orchestrator.should_refresh_synergy(False))

    def test_blocked_cooldown_keeps_valid_snapshot_ready(self):
        blocked_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        status = {"last_result": "blocked", "blocked_until": blocked_until}
        with tempfile.TemporaryDirectory() as temp_dir:
            old_mtime = time.time() - (8 * 24 * 60 * 60)
            synergy_path = self._snapshot(temp_dir, mtime=old_mtime)

            patches = self._patch_synergy_dir(temp_dir, status=status)
            with patches[0], patches[1], patches[2]:
                write_synergy_refresh_meta(
                    target_path=synergy_path,
                    base_url="https://apexlol.info/zh",
                    resources=3,
                    mapped=1,
                    stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
                )
                os.utime(synergy_path, (old_mtime, old_mtime))
                os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

                self.assertTrue(heal_worker._synergy_data_fresh())
                self.assertFalse(orchestrator.should_refresh_synergy(False))


if __name__ == "__main__":
    unittest.main()

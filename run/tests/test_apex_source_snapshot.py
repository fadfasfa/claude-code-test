import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scraping import full_synergy_scraper as scraper


class ApexSourceSnapshotTests(unittest.TestCase):
    def _patch_snapshot_roots(self, root: Path, manual: Path):
        return (
            patch.object(scraper, "DEFAULT_APEX_SNAPSHOT_DIR", str(root)),
            patch.object(scraper, "DEFAULT_APEX_MANUAL_SNAPSHOT_DIR", str(manual)),
        )

    def test_default_snapshot_dir_reads_manual_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "apex_snapshot"
            manual = root / "manual"
            manual.mkdir(parents=True)
            (manual / "sample.json").write_text('{"katarina": []}', encoding="utf-8")

            root_patch, manual_patch = self._patch_snapshot_roots(root, manual)
            with root_patch, manual_patch, patch.dict(os.environ, {"APEX_SNAPSHOT_DIR": ""}):
                source = scraper.ApexSource()
                resources = source._load_snapshot_resources()

            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0].source, "snapshot")
            self.assertIn("sample.json", resources[0].url)

    def test_discover_without_snapshot_does_not_fetch_online_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "apex_snapshot"
            manual = root / "manual"
            root.mkdir(parents=True)

            root_patch, manual_patch = self._patch_snapshot_roots(root, manual)
            env = {
                "APEX_SNAPSHOT_DIR": "",
                "APEX_ALLOW_ONLINE_FETCH": "0",
                "APEX_ALLOW_BROWSER": "0",
                "APEX_SYNERGY_JSON_URL": "",
            }
            with root_patch, manual_patch, patch.dict(os.environ, env):
                source = scraper.ApexSource()
                with patch.object(source, "fetch_configured_json_resource", side_effect=AssertionError):
                    with patch.object(source, "fetch", side_effect=AssertionError):
                        self.assertEqual(source.discover_resources(), [])

    def test_browser_fallback_requires_env_flag(self):
        source = scraper.ApexSource()
        fetched = scraper.FetchedResource(url=source.base_url, text="<html></html>", source="selenium")

        with patch.dict(os.environ, {"APEX_ALLOW_BROWSER": "0"}):
            with patch.object(source, "fetch_requests", return_value=None):
                with patch.object(source, "fetch_browser", return_value=fetched) as fetch_browser:
                    self.assertIsNone(source.fetch(source.base_url, allow_browser=True))
                    fetch_browser.assert_not_called()

        with patch.dict(os.environ, {"APEX_ALLOW_BROWSER": "1"}):
            with patch.object(source, "fetch_requests", return_value=None):
                with patch.object(source, "fetch_browser", return_value=fetched) as fetch_browser:
                    self.assertIs(source.fetch(source.base_url, allow_browser=True), fetched)
                    fetch_browser.assert_called_once_with(source.base_url)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.collect.excel.import_excel import _is_greyed_strategy_font
from pipeline.collect.wiki.scrape_wiki import (
    PAGE_HASHES_VERSION_KEY,
    SCRAPER_CACHE_VERSION,
    _page_hash_value,
    _set_page_hash,
)
from pipeline.compute.update_review import _render_markdown
from pipeline.compute.publish_candidate import apply_candidate, build_diff_summary
from pipeline.common import write_json
import build_release


RUNTIME_FILES = ("classes.json", "talents.json", "meta.json")


class FakeColor:
    def __init__(self, color_type: str, rgb: str = ""):
        self.type = color_type
        self.rgb = rgb


class PublishHardeningTests(unittest.TestCase):
    def write_runtime(self, root: Path, *, meta: dict | None = None) -> None:
        root.mkdir(parents=True, exist_ok=True)
        write_json(root / "classes.json", {"classes": []})
        write_json(root / "talents.json", {"classes": []})
        write_json(root / "meta.json", meta or {"build": {"version": "13.2", "excel_version": "13.2"}})

    def test_apply_candidate_blocks_hard_degradation_until_explicit_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            candidate = temp_root / "candidate"
            target = temp_root / "app_data"
            hard_meta = {
                "build": {
                    "version": "13.2",
                    "excel_version": "13.2",
                    "degradation": {
                        "structure_degraded": True,
                        "reasons": ["required_weapon_missing:plasma-pistol"],
                    },
                }
            }
            self.write_runtime(candidate, meta=hard_meta)

            with self.assertRaisesRegex(RuntimeError, "Hard wiki degradation"):
                apply_candidate(candidate, target, cleanup=False)

            result = apply_candidate(candidate, target, cleanup=False, accept_hard_degradation=True)

            self.assertEqual(result["status"], "applied")
            for filename in RUNTIME_FILES:
                self.assertTrue((target / filename).exists())

    def test_diff_summary_reports_modifier_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            current = temp_root / "current"
            candidate = temp_root / "candidate"
            self.write_runtime(
                current,
                meta={
                    "build": {"version": "13.2", "excel_version": "13.2"},
                    "positive_modifier_pool": [{"key": "old-positive", "label": "Old"}],
                    "negative_modifier_pool": [{"key": "shared-negative", "label": "Old"}],
                    "negative_modifier_rules": {"quota_limits": {"shared-negative": 1}},
                },
            )
            self.write_runtime(
                candidate,
                meta={
                    "build": {"version": "13.2", "excel_version": "13.2"},
                    "positive_modifier_pool": [{"key": "new-positive", "label": "New"}],
                    "negative_modifier_pool": [{"key": "shared-negative", "label": "New"}],
                    "negative_modifier_rules": {"quota_limits": {"shared-negative": 2}},
                },
            )

            modifiers = build_diff_summary(candidate, current)["semantic_changes"]["modifier_changes"]

            self.assertTrue(modifiers["has_changes"])
            self.assertEqual(modifiers["positive_modifier_pool"]["added_keys"], ["new-positive"])
            self.assertEqual(modifiers["positive_modifier_pool"]["removed_keys"], ["old-positive"])
            self.assertEqual(modifiers["negative_modifier_pool"]["changed_keys"], ["shared-negative"])
            self.assertGreater(modifiers["negative_modifier_rules"]["changed_count"], 0)

    def test_greyed_strategy_font_only_matches_explicit_rgb_grey(self):
        self.assertTrue(_is_greyed_strategy_font(FakeColor("rgb", "FF767171")))
        self.assertFalse(_is_greyed_strategy_font(FakeColor("rgb", "FF000000")))
        self.assertFalse(_is_greyed_strategy_font(FakeColor("theme", "FF767171")))
        self.assertFalse(_is_greyed_strategy_font(FakeColor("indexed", "FF767171")))

    def test_build_release_apply_candidate_passes_acceptance_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            candidate = temp_root / "candidate"
            report = temp_root / "runtime_validation.json"
            self.write_runtime(candidate)
            write_json(report, {"summary": {"issue_count": 0}})
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> int:
                commands.append(command)
                return 0

            with (
                patch.object(build_release, "PIPELINE_TMP_PUBLISH_DIR", candidate),
                patch.object(build_release, "VALIDATION_REPORT_FILE", report),
                patch.object(build_release, "_run", side_effect=fake_run),
            ):
                exit_code = build_release.apply_candidate(
                    accept_version_mismatch=True,
                    accept_hard_degradation=True,
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("--accept-version-mismatch", commands[-1])
            self.assertIn("--accept-hard-degradation", commands[-1])

    def test_build_release_blocks_package_when_candidate_diff_is_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            candidate = temp_root / "candidate"
            current = temp_root / "current"
            self.write_runtime(candidate, meta={"build": {"version": "13.2", "excel_version": "13.2", "marker": "candidate"}})
            self.write_runtime(current, meta={"build": {"version": "13.2", "excel_version": "13.2", "marker": "current"}})

            with (
                patch.object(build_release, "PIPELINE_TMP_PUBLISH_DIR", candidate),
                patch.object(build_release, "APP_DATA_DIR", current),
            ):
                with self.assertRaisesRegex(RuntimeError, "尚未应用到 app/data"):
                    build_release._assert_no_stale_candidate_for_package()

    def test_update_review_renders_required_acceptance_flags(self):
        markdown = _render_markdown(
            {
                "wiki_version": "Hotfix 13.2",
                "excel_version": "13.1",
                "wiki_degraded": True,
                "hard_degraded": True,
                "wiki_skipped": True,
                "version_alignment": {
                    "aligned": False,
                    "wiki_version": "13.2",
                    "excel_version": "13.1",
                    "reason": "version_mismatch",
                },
                "validation_issue_count": 0,
                "has_diff": True,
                "excel": {},
                "wiki_incremental": {},
                "degradation": {"structure_degraded": True, "reasons": ["required_class_missing:heavy"]},
                "semantic_changes": {},
            }
        )

        self.assertIn("wiki 本轮跳过: `True`", markdown)
        self.assertIn("--accept-version-mismatch --accept-hard-degradation", markdown)
        self.assertIn("可安全 apply: `False`", markdown)

    def test_wiki_page_hash_cache_version_invalidates_old_entries(self):
        hashes = {"Tactical": "old-hash"}

        self.assertEqual(_page_hash_value(hashes, "Tactical"), "")

        _set_page_hash(hashes, "Tactical", "new-hash")

        self.assertEqual(hashes[PAGE_HASHES_VERSION_KEY], SCRAPER_CACHE_VERSION)
        self.assertEqual(_page_hash_value(hashes, "Tactical"), "new-hash")


if __name__ == "__main__":
    unittest.main()

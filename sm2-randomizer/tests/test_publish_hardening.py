from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.collect.excel.import_excel import _is_greyed_strategy_font, _is_stable_weapon_item
from pipeline.compute.update_review import _render_markdown
from pipeline.compute.publish_candidate import _extract_version_number, apply_candidate, build_diff_summary
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

            semantic = build_diff_summary(candidate, current)["semantic_changes"]
            modifiers = semantic["modifier_changes"]
            frontend = semantic["frontend_changes"]

            self.assertTrue(modifiers["has_changes"])
            self.assertEqual(modifiers["positive_modifier_pool"]["added_keys"], ["new-positive"])
            self.assertEqual(modifiers["positive_modifier_pool"]["removed_keys"], ["old-positive"])
            self.assertEqual(modifiers["negative_modifier_pool"]["changed_keys"], ["shared-negative"])
            self.assertGreater(modifiers["negative_modifier_rules"]["changed_count"], 0)
            self.assertEqual(frontend["positive_modifiers"]["added"][0]["id"], "new-positive")
            self.assertEqual(frontend["positive_modifiers"]["removed"][0]["id"], "old-positive")
            self.assertEqual(
                frontend["negative_modifiers"]["changed"][0]["fields"],
                [{"field": "label", "before": "Old", "after": "New"}],
            )
            self.assertEqual(frontend["modifier_rules"]["changed_fields"][0]["field"], "quota_limits")

    def test_frontend_diff_ignores_loadout_array_reordering(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            current = temp_root / "current"
            candidate = temp_root / "candidate"
            self.write_runtime(current)
            self.write_runtime(candidate)
            weapons = [
                {"slug": "bolt-pistol", "name": "爆弹手枪", "slot_type": "secondary"},
                {"slug": "plasma-pistol", "name": "等离子手枪", "slot_type": "secondary"},
            ]
            current_classes = {
                "classes": [
                    {
                        "slug": "tactical",
                        "name": "战术兵",
                        "loadout_pools": {"primary": [], "secondary": weapons, "melee": []},
                    }
                ]
            }
            candidate_classes = {
                "classes": [
                    {
                        "slug": "tactical",
                        "name": "战术兵",
                        "loadout_pools": {"primary": [], "secondary": list(reversed(weapons)), "melee": []},
                    }
                ]
            }
            write_json(current / "classes.json", current_classes)
            write_json(candidate / "classes.json", candidate_classes)

            frontend = build_diff_summary(candidate, current)["semantic_changes"]["frontend_changes"]

            self.assertFalse(frontend["has_changes"])
            self.assertEqual(frontend["loadouts"], {"added": [], "removed": [], "changed": []})

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

    def test_update_review_renders_only_concrete_frontend_changes(self):
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
                "semantic_changes": {
                    "frontend_changes": {
                        "classes": {"added": [], "removed": [], "changed": []},
                        "loadouts": {"added": [], "removed": [], "changed": []},
                        "talents": {"added": [], "removed": [], "changed": []},
                        "positive_modifiers": {"added": [], "removed": [], "changed": []},
                        "negative_modifiers": {
                            "added": [],
                            "removed": [],
                            "changed": [
                                {
                                    "id": "tidal-onslaught",
                                    "label": "海啸来袭",
                                    "fields": [
                                        {
                                            "field": "detail",
                                            "before": "旧说明",
                                            "after": "新说明",
                                        }
                                    ],
                                }
                            ],
                        },
                        "modifier_rules": {
                            "changed_fields": [],
                            "title_aliases": {"added": [], "removed": [], "changed": [], "replaced": []},
                        },
                        "has_changes": True,
                    }
                },
            }
        )

        self.assertIn("负向策略词条：`1` 项", markdown)
        self.assertIn("修改：海啸来袭", markdown)
        self.assertIn("说明：“旧说明” → “新说明”", markdown)
        self.assertNotIn("wiki 本轮跳过", markdown)
        self.assertNotIn("--accept-version-mismatch", markdown)
        self.assertNotIn("可安全 apply", markdown)

    def test_version_extraction_keeps_multi_part_versions(self):
        self.assertEqual(_extract_version_number("Hotfix 13.2.1"), "13.2.1")
        self.assertEqual(_extract_version_number("当前数据为13.2.1版本"), "13.2.1")

    def test_pending_review_weapon_items_are_not_stable(self):
        self.assertFalse(_is_stable_weapon_item({"slug": "excel-discovered-等离子", "pending_review": True}))
        self.assertFalse(_is_stable_weapon_item({"slug": "excel-discovered-plasma-pistol"}))
        self.assertTrue(_is_stable_weapon_item({"slug": "plasma-pistol"}))

    def test_key_pipeline_modules_have_real_module_docstrings(self):
        module_paths = [
            PROJECT_ROOT / "pipeline" / "common.py",
            PROJECT_ROOT / "build_release.py",
            PROJECT_ROOT / "pipeline" / "collect" / "excel" / "import_excel.py",
        ]

        for path in module_paths:
            with self.subTest(path=path.name):
                module = ast.parse(path.read_text(encoding="utf-8"))
                self.assertIsNotNone(ast.get_docstring(module), f"{path} should expose a module docstring")

    def test_partial_perk_refresh_preserves_unrequested_manual_actions(self):
        from pipeline.collect.wiki import scrape_perks

        existing_items = [
            {"class_name": "战术兵", "talent_name_raw": "old tactical"},
            {"class_name": "特战兵", "talent_name_raw": "old heavy"},
        ]
        new_items = [{"class_name": "战术兵", "talent_name_raw": "new tactical"}]

        merged = scrape_perks.merge_manual_action_items(
            existing_items,
            new_items,
            requested_class_titles=["Tactical"],
        )

        self.assertEqual(
            merged,
            [
                {"class_name": "特战兵", "talent_name_raw": "old heavy"},
                {"class_name": "战术兵", "talent_name_raw": "new tactical"},
            ],
        )


if __name__ == "__main__":
    unittest.main()

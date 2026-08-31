from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.collect.wiki import run as wiki_run
from pipeline.collect.wiki import scrape_perks
from pipeline.collect.wiki import scrape_wiki


def class_record(*, revision_id: int = 10, sha1: str = "same") -> dict:
    return {
        "name": "Tactical",
        "slug_candidate": "tactical",
        "image_url": "https://example.test/tactical.png",
        "class_role_text": "Role",
        "class_ability": "Ability",
        "character_name": "Character",
        "image_key": "class_tactical_img",
        "weapons": {"primary": [], "secondary": [], "melee": []},
        "notes": [],
        "mode_restriction_candidates": [],
        "source_pages": ["https://example.test/Tactical"],
        "source_type": "fandom",
        "parse_warnings": [],
        "parse_degraded": False,
        "missing_fields": [],
        "source_revision": {"id": revision_id, "timestamp": "2026-08-01T00:00:00Z", "sha1": sha1},
    }


class OfficialPatchSelectionTests(unittest.TestCase):
    def test_selects_latest_patch_or_hotfix_and_ignores_newer_community_posts(self):
        selected = scrape_wiki.select_latest_official_patch(
            [
                {"title": "August Community Update", "url": "https://example.test/community", "date": 300},
                {"title": "Hotfix 14.1", "url": "https://example.test/14.1", "date": 200},
                {"title": "Patch Notes 14.0", "url": "https://example.test/14.0", "date": 100},
            ]
        )

        self.assertEqual(selected["title"], "Hotfix 14.1")
        self.assertEqual(selected["version"], "14.1")
        self.assertEqual(selected["source"], "steam_news")

    def test_selects_multi_part_version_with_zero_width_prefix(self):
        selected = scrape_wiki.select_latest_official_patch(
            [{"title": "\u200bPatch Notes 14.1.2", "url": "https://example.test/14.1.2", "date": 200}]
        )

        self.assertEqual(selected["version"], "14.1.2")

    def test_rejects_missing_or_non_patch_news(self):
        with self.assertRaisesRegex(RuntimeError, "news item list"):
            scrape_wiki.select_latest_official_patch({})
        with self.assertRaisesRegex(RuntimeError, "Patch Notes/Hotfix"):
            scrape_wiki.select_latest_official_patch(
                [{"title": "Community Event", "url": "https://example.test/event", "date": 100}]
            )

    def test_rejects_patch_items_without_a_valid_positive_timestamp(self):
        invalid_dates = [None, 0, -1, "200", True, 10**30]
        for invalid_date in invalid_dates:
            with self.subTest(date=invalid_date):
                with self.assertRaisesRegex(RuntimeError, "Patch Notes/Hotfix"):
                    scrape_wiki.select_latest_official_patch(
                        [{"title": "Hotfix 14.1", "url": "https://example.test/14.1", "date": invalid_date}]
                    )

        selected = scrape_wiki.select_latest_official_patch(
            [
                {"title": "Hotfix 14.2", "url": "https://example.test/14.2", "date": 0},
                {"title": "Patch Notes 14.1", "url": "https://example.test/14.1", "date": 200},
            ]
        )
        self.assertEqual(selected["title"], "Patch Notes 14.1")

    def test_fetch_official_patch_rejects_malformed_payload(self):
        with patch.object(scrape_wiki, "get_json", return_value={"unexpected": {}}):
            with self.assertRaisesRegex(RuntimeError, "appnews"):
                scrape_wiki.fetch_official_patch_anchor()

    def test_fetch_official_patch_propagates_network_failure(self):
        with patch.object(scrape_wiki, "get_json", side_effect=OSError("network down")):
            with self.assertRaisesRegex(OSError, "network down"):
                scrape_wiki.fetch_official_patch_anchor()


class FandomRevisionIncrementalTests(unittest.TestCase):
    def test_unchanged_revision_reuses_raw_without_fetching_html(self):
        previous = class_record()
        stats = {"skipped": [], "refetched": []}
        with patch.object(scrape_wiki, "fetch_fandom_page") as fetch_page:
            result = scrape_wiki._scrape_with_increment(
                ["Tactical"],
                kind="class",
                scrape_fn=scrape_wiki.scrape_fandom_class,
                previous_raw={"Tactical": previous},
                revisions={"Tactical": previous["source_revision"]},
                force_refresh=False,
                stats=stats,
            )

        fetch_page.assert_not_called()
        self.assertEqual(stats["skipped"], ["Tactical"])
        self.assertEqual(stats["refetched"], [])
        self.assertTrue(result[0]["reused_unchanged"])

    def test_changed_revision_fetches_only_changed_page(self):
        previous = class_record()
        revision = {"id": 11, "timestamp": "2026-08-02T00:00:00Z", "sha1": "changed"}
        stats = {"skipped": [], "refetched": []}
        parsed = class_record(revision_id=11, sha1="changed")
        parsed.pop("source_revision")

        with patch.object(scrape_wiki, "fetch_fandom_page", return_value={"html": "x", "url": "https://example.test/Tactical"}) as fetch_page:
            result = scrape_wiki._scrape_with_increment(
                ["Tactical"],
                kind="class",
                scrape_fn=lambda _title, _page: dict(parsed),
                previous_raw={"Tactical": previous},
                revisions={"Tactical": revision},
                force_refresh=False,
                stats=stats,
            )

        fetch_page.assert_called_once_with("Tactical")
        self.assertEqual(stats["refetched"], ["Tactical"])
        self.assertEqual(result[0]["source_revision"], revision)

    def test_missing_or_incomplete_revision_on_old_raw_and_force_refresh_refetch(self):
        incomplete_revision = dict(class_record())
        incomplete_revision["source_revision"] = {"id": 10, "sha1": "same"}
        cases = (
            (False, {**class_record(), "source_revision": {}}),
            (False, incomplete_revision),
            (True, class_record()),
        )
        for force_refresh, previous in cases:
            with self.subTest(force_refresh=force_refresh):
                stats = {"skipped": [], "refetched": []}
                revision = {"id": 10, "timestamp": "2026-08-01T00:00:00Z", "sha1": "same"}
                parsed = class_record()
                parsed.pop("source_revision")
                with patch.object(scrape_wiki, "fetch_fandom_page", return_value={"html": "x", "url": "https://example.test/Tactical"}):
                    scrape_wiki._scrape_with_increment(
                        ["Tactical"],
                        kind="class",
                        scrape_fn=lambda _title, _page: dict(parsed),
                        previous_raw={"Tactical": previous},
                        revisions={"Tactical": revision},
                        force_refresh=force_refresh,
                        stats=stats,
                    )
                self.assertEqual(stats["refetched"], ["Tactical"])

    def test_new_page_without_previous_raw_is_refetched(self):
        revision = {"id": 20, "timestamp": "2026-08-03T00:00:00Z", "sha1": "new"}
        stats = {"skipped": [], "refetched": []}

        with patch.object(
            scrape_wiki,
            "fetch_fandom_page",
            return_value={"html": "x", "url": "https://example.test/New_Weapon"},
        ) as fetch_page:
            result = scrape_wiki._scrape_with_increment(
                ["New Weapon"],
                kind="weapon",
                scrape_fn=lambda _title, _page: {
                    "name": "New Weapon",
                    "slug_candidate": "new-weapon",
                    "slot_type": "primary",
                    "allowed_classes": ["Tactical"],
                },
                previous_raw={},
                revisions={"New Weapon": revision},
                force_refresh=False,
                stats=stats,
            )

        fetch_page.assert_called_once_with("New Weapon")
        self.assertEqual(stats["refetched"], ["New Weapon"])
        self.assertEqual(result[0]["source_revision"], revision)

    def test_revision_api_missing_page_fails_closed(self):
        payload = {"query": {"pages": [{"title": "Tactical", "missing": True}]}}
        with patch.object(scrape_wiki, "get_json", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "missing pages: Tactical"):
                scrape_wiki.fetch_fandom_revisions(["Tactical"])

    def test_revision_api_rejects_malformed_page_list(self):
        with patch.object(scrape_wiki, "get_json", return_value={"query": {"pages": {}}}):
            with self.assertRaisesRegex(RuntimeError, "page list"):
                scrape_wiki.fetch_fandom_revisions(["Tactical"])

    def test_preserves_existing_talents_until_partial_asset_refresh(self):
        raw = {"meta": {"degradation": {"structure_degraded": False, "soft_degraded": False}}}
        previous = {
            "talents": [{"class_slug_candidate": "tactical", "talents": [{"name": "old"}]}],
            "meta": {
                "talent_coverage": {"talent_class_count": 7},
                "talent_manual_action_items": [{"class_name": "Heavy"}],
                "degradation": {"talent_degraded": True, "talent_reasons": ["manual_action:Heavy"]},
            },
        }

        result = scrape_wiki.preserve_existing_talent_state(raw, previous)

        self.assertEqual(result["talents"], previous["talents"])
        self.assertEqual(result["meta"]["talent_coverage"], {"talent_class_count": 7})
        self.assertTrue(result["meta"]["degradation"]["talent_degraded"])
        self.assertTrue(result["meta"]["wiki_degraded"])

    def test_preserves_existing_weapon_order_and_appends_new_items(self):
        classes = [
            {
                "name": "Tactical",
                "weapons": {
                    "primary": ["New Weapon", "Bolt Rifle", "Melta Rifle"],
                    "secondary": [],
                    "melee": [],
                },
            }
        ]
        previous = {
            "Tactical": {
                "weapons": {
                    "primary": ["Melta Rifle", "Bolt Rifle", "Removed Weapon"],
                    "secondary": [],
                    "melee": [],
                }
            }
        }

        scrape_wiki.preserve_class_weapon_order(classes, previous)

        self.assertEqual(classes[0]["weapons"]["primary"], ["Melta Rifle", "Bolt Rifle", "New Weapon"])

    def test_partial_talent_refresh_recomputes_coverage_from_merged_classes(self):
        existing = [
            {
                "class_slug_candidate": "tactical",
                "talents": [
                    {"download_status": "reused-local"},
                    {"download_status": "reused-local"},
                ],
            },
            {
                "class_slug_candidate": "heavy",
                "talents": [{"download_status": "reused-local"}],
            },
        ]
        tactical_update = {
            "class_slug_candidate": "tactical",
            "talents": [
                {"download_status": "ok"},
                {"download_status": "failed-hard", "manual_action_required": True},
                {"download_status": "reused-local"},
            ],
        }
        merged = scrape_perks.merge_by_key(
            existing,
            [tactical_update],
            "class_slug_candidate",
            ["tactical", "heavy"],
        )

        coverage = scrape_perks.build_talent_coverage(
            merged,
            [{"class_name": "战术兵"}],
        )

        self.assertEqual(merged[1], existing[1])
        self.assertEqual(
            coverage,
            {
                "talent_class_count": 2,
                "talent_icon_count": 4,
                "talent_icon_downloaded_count": 3,
                "talent_manual_action_count": 1,
            },
        )


class WikiRunAssetSelectionTests(unittest.TestCase):
    def namespace(self, **overrides: object) -> argparse.Namespace:
        values = {
            "skip_structure": False,
            "skip_assets": False,
            "headless": True,
            "dump_dom": False,
            "force_download": False,
            "force_refresh": False,
            "class_titles": [],
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_explicit_class_wins_and_force_or_skip_structure_refresh_all(self):
        self.assertEqual(
            wiki_run.resolve_asset_refresh_classes(self.namespace(class_titles=["Tactical", "Tactical"])),
            ["Tactical"],
        )
        self.assertIsNone(wiki_run.resolve_asset_refresh_classes(self.namespace(force_download=True)))
        self.assertIsNone(wiki_run.resolve_asset_refresh_classes(self.namespace(skip_structure=True)))

    def test_loads_changed_classes_and_empty_list_skips_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            raw_path = Path(temp) / "raw.json"
            raw_path.write_text(
                json.dumps({"meta": {"incremental": {"changed_class_pages": []}}}),
                encoding="utf-8",
            )
            args = self.namespace()
            commands: list[list[str]] = []
            with (
                patch.object(wiki_run, "RAW_DATA_FILE", raw_path),
                patch.object(wiki_run, "parse_args", return_value=args),
                patch.object(wiki_run, "_run", side_effect=lambda command: commands.append(command) or 0),
            ):
                exit_code = wiki_run.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(commands), 1)
        self.assertIn("scrape_wiki.py", commands[0][1])


if __name__ == "__main__":
    unittest.main()

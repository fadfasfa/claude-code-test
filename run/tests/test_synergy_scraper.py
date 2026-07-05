from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class SynergyScraperTests(unittest.TestCase):
    def _extractor(self):
        from hextech.scraping.synergy import scraper

        champion = scraper.ChampionInfo(id="254", name="蔚", title="", en_name="Vi", aliases=[], slug="vi")
        return scraper.SynergyExtractor(
            champion_lookup={"vi": champion, "蔚": champion, "254": champion},
            augment_name_map={
                "易损": "易损",
                scraper.normalize_augment_name("易损"): "易损",
                "闪电打击": "闪电打击",
                scraper.normalize_augment_name("闪电打击"): "闪电打击",
            },
        )

    def test_visible_active_card_is_parsed(self):
        from hextech.scraping.synergy import scraper

        extractor = self._extractor()
        html = """
        <html><body>
          <section>
            <div>关联 1 个海克斯</div>
            <div>闪电打击</div>
            <div>黄金</div>
            <div>S 级 ?</div>
            <div>强力联动</div>
            <div>作者</div><div>9426224</div>
            <p>闪电打击让蔚的连招更稳定。</p>
          </section>
        </body></html>
        """

        entries = extractor.extract([scraper.FetchedResource("https://apexlol.info/zh/champions/Vi", html, "fixture")])

        self.assertEqual(len(entries["vi"]), 1)
        self.assertEqual(entries["vi"][0].augment_names, ["闪电打击"])
        self.assertEqual(extractor.archived_filtered_count, 0)

    def test_visible_archived_card_is_filtered(self):
        from hextech.scraping.synergy import scraper

        extractor = self._extractor()
        html = """
        <html><body>
          <h2>已弃用归档</h2>
          <article>
            <div>关联 1 个海克斯</div>
            <div>易损</div>
            <div>黄金</div>
            <div>S 级 ?</div>
            <div>强力联动</div>
            <span>已弃用</span>
            <div>作者</div><div>9426224</div>
            <p>这条归档联动不应进入 cleaned 数据。</p>
          </article>
        </body></html>
        """

        with self.assertRaises(ValueError):
            extractor.extract([scraper.FetchedResource("https://apexlol.info/zh/champions/Vi", html, "fixture")])
        self.assertEqual(extractor.archived_filtered_count, 1)
        self.assertEqual(extractor.archived_filter_samples[0]["reason"], "visible_archived_card")

    def test_json_archived_item_is_filtered_but_active_item_remains(self):
        from hextech.scraping.synergy import scraper

        extractor = self._extractor()
        payload = {
            "items": [
                {
                    "championSlug": "vi",
                    "augmentName": "易损",
                    "rating": "S",
                    "status": "deprecated",
                    "content": "归档卡片。",
                },
                {
                    "championSlug": "vi",
                    "augmentName": "闪电打击",
                    "rating": "A",
                    "tag": "强力联动",
                    "content": "活跃卡片。",
                },
            ]
        }
        resource = scraper.FetchedResource("https://apexlol.info/zh/champions/Vi", json.dumps(payload), "fixture")

        entries = extractor.extract([resource])

        self.assertEqual(len(entries["vi"]), 1)
        self.assertEqual(entries["vi"][0].augment_names, ["闪电打击"])
        self.assertEqual(extractor.archived_filtered_count, 1)

    def test_archived_marker_in_card_content_is_filtered(self):
        from hextech.scraping.synergy import scraper

        extractor = self._extractor()
        html = """
        <html><body>
          <article>
            <div>关联 1 个海克斯</div>
            <div>易损</div>
            <div>黄金</div>
            <div>S 级 ?</div>
            <div>强力联动</div>
            <div>作者</div><div>9426224</div>
            <p>该联动已弃用(Deprecated)，即将下架。</p>
          </article>
        </body></html>
        """

        with self.assertRaises(ValueError):
            extractor.extract([scraper.FetchedResource("https://apexlol.info/zh/champions/Vi", html, "fixture")])
        self.assertEqual(extractor.archived_filter_samples[0]["reason"], "visible_archived_content")

    def test_runtime_guard_requires_cloakbrowser(self):
        from hextech.support import python_runtime

        self.assertIn("cloakbrowser", python_runtime.REQUIRED_RUNTIME_PACKAGES)


class MayhemMergeTests(unittest.TestCase):
    def test_mayhem_only_adds_missing_combos_and_removes_archived_apex_items(self):
        from hextech.scraping.synergy.mayhem_merge import merge_mayhem_combos

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apex_path = root / "apex.json"
            mayhem_path = root / "mayhem.json"
            manifest_path = root / "augments.json"
            core_path = root / "core.json"
            output_path = root / "cleaned.json"

            apex_path.write_text(
                json.dumps(
                    {
                        "254": {
                            "id": "254",
                            "name": "蔚",
                            "synergy_items": [
                                {
                                    "augment_names": ["闪电打击"],
                                    "tier": "黄金",
                                    "rating": "S",
                                    "tag": "强力联动",
                                    "author": "ApexLoL",
                                    "content": "Apex 主来源。",
                                },
                                {
                                    "augment_names": ["易损"],
                                    "tier": "黄金",
                                    "rating": "S",
                                    "tag": "强力联动",
                                    "author": "ApexLoL",
                                    "content": "已弃用归档",
                                },
                            ],
                            "synergies": [],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            mayhem_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "champion": "Vi",
                                "augment_names": ["闪电打击"],
                                "body": "重复组合，不应覆盖 Apex。",
                            },
                            {
                                "champion": "Vi",
                                "augment_names": ["易损"],
                                "body": "Apex 归档项被移除后，Mayhem 可作为补缺。",
                            },
                        ],
                        "rejects": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest_path.write_text(
                json.dumps(
                    [
                        {"name": "闪电打击", "tier": "黄金"},
                        {"name": "易损", "tier": "黄金"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            core_path.write_text(
                json.dumps({"254": {"name": "蔚", "title": "", "en_name": "Vi", "aliases": []}}, ensure_ascii=False),
                encoding="utf-8",
            )

            summary = merge_mayhem_combos(
                apex_path=apex_path,
                mayhem_raw_path=mayhem_path,
                augment_manifest_path=manifest_path,
                core_data_path=core_path,
                output_path=output_path,
                write_output=True,
            )

            cleaned = json.loads(output_path.read_text(encoding="utf-8"))
            items = cleaned["254"]["synergy_items"]
            self.assertEqual(summary["removed_archived_apex_items"], 1)
            self.assertEqual(summary["skipped_duplicate_items"], 1)
            self.assertEqual(summary["added_items"], 1)
            self.assertEqual([item["source"] for item in items if item.get("source") == "arammayhem"], ["arammayhem"])
            self.assertFalse(any("已弃用" in json.dumps(item, ensure_ascii=False) for item in items))


if __name__ == "__main__":
    unittest.main()

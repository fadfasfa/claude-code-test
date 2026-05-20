import json
import unittest

from display.web_api import _normalize_synergy_items, _synergy_item_to_compat_string
from scraping.full_synergy_scraper import (
    ChampionInfo,
    FetchedResource,
    SynergyEntry,
    SynergyExtractor,
    SynergyWriter,
    build_champion_lookup,
    normalize_augment_name,
    normalize_slug,
)


class SynergyStructuredTests(unittest.TestCase):
    def _core_info(self):
        return {
            "55": ChampionInfo(
                id="55",
                name="卡特琳娜",
                title="不祥之刃",
                en_name="Katarina",
                aliases=["卡特"],
                slug=normalize_slug("Katarina"),
            )
        }

    def _augment_map(self):
        return {
            "bladewaltz": "利刃华尔兹",
            normalize_augment_name("利刃华尔兹"): "利刃华尔兹",
            "利刃华尔兹": "利刃华尔兹",
        }

    def test_extracts_json_synergy_items_with_champion_path_fallback(self):
        payload = {
            "55": {
                "synergy_items": [
                    {
                        "augment_names": ["利刃华尔兹"],
                        "tier": "黄金",
                        "rating": "A",
                        "tag": "强力联动",
                        "author": "ApexLoL",
                        "content": "卡特琳娜 R 可以触发这条联动。",
                    }
                ]
            }
        }
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(self._core_info()),
            augment_name_map=self._augment_map(),
        )

        result = extractor.extract([
            FetchedResource(
                url="https://apexlol.info/zh/snapshot/data.json",
                text=json.dumps(payload, ensure_ascii=False),
                source="test",
            )
        ])

        self.assertIn("katarina", result)
        self.assertEqual(result["katarina"][0].augment_names, ["利刃华尔兹"])
        self.assertEqual(result["katarina"][0].tier, "黄金")

    def test_writer_keeps_legacy_and_structured_payloads(self):
        entry = SynergyEntry(
            champion_slug="katarina",
            augment_names=["利刃华尔兹"],
            tier="黄金",
            rating="A",
            tag="强力联动",
            author="ApexLoL",
            is_original=True,
            content="卡特琳娜 R 可以触发这条联动。",
            upvotes=3,
            downvotes=1,
        )

        payload = SynergyWriter(self._core_info()).build_payload({"katarina": [entry]})

        self.assertEqual(payload["55"]["synergy_items"][0]["augment_names"], ["利刃华尔兹"])
        self.assertIn("利刃华尔兹 | 黄金 | 评分 A", payload["55"]["synergies"][0])

    def test_api_normalizes_legacy_strings_to_structured_items(self):
        legacy = "利刃华尔兹 | 黄金 | 评分 A | 强力联动 | A | B站晴转小雨Yy_ | 原创 | 卡特琳娜 R 可以触发这条联动。"

        items = _normalize_synergy_items([], [legacy])

        self.assertEqual(items[0]["augment_names"], ["利刃华尔兹"])
        self.assertEqual(items[0]["rating"], "A")
        self.assertTrue(items[0]["is_original"])
        self.assertEqual(_synergy_item_to_compat_string(items[0]).split(" | ")[4:6], ["0", "0"])

    def test_visible_parser_reads_tag_after_rating_line(self):
        extractor = SynergyExtractor(
            champion_lookup=build_champion_lookup(self._core_info()),
            augment_name_map=self._augment_map(),
        )
        html = """
        <html><body>
        <div>利刃华尔兹</div>
        <div>黄金</div>
        <div>D 级</div>
        <div>陷阱</div>
        <div>0</div>
        <div>0</div>
        <div>作者</div>
        <div>ApexLoL</div>
        <p>卡特琳娜 R 在这个组合里会卡手。</p>
        </body></html>
        """

        result = extractor.extract([
            FetchedResource(
                url="https://apexlol.info/zh/champions/Katarina",
                text=html,
                source="test",
            )
        ])

        self.assertEqual(result["katarina"][0].rating, "D")
        self.assertEqual(result["katarina"][0].tag, "陷阱")


if __name__ == "__main__":
    unittest.main()

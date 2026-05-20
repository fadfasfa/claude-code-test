import json
import unittest

import pandas as pd

from processing.view_adapter import process_hextechs_data
from scraping.full_hextech_scraper import extract_champion_stats


def _flight_script(payload: str) -> str:
    return f"<script>self.__next_f.push([1,{json.dumps(payload, ensure_ascii=False)}])</script>"


class HextechSourceParserTests(unittest.TestCase):
    def _morgana_maps(self):
        names = {
            "1373": "缩小引擎",
            "1420": "咏叹奏鸣",
            "1406": "祖母的辣椒油",
            "1052": "闪电打击",
            "1058": "秘术冲拳",
        }
        tiers = {
            "缩小引擎": "黄金",
            "咏叹奏鸣": "黄金",
            "祖母的辣椒油": "黄金",
            "闪电打击": "黄金",
            "秘术冲拳": "棱彩",
        }
        return names, tiers

    def _morgana_html(self):
        ref_payload = (
            '27:[["$","$L28",null,{"championId":"25",'
            '"championAugmentsStats":{"25":[["25","$29","16.10","2026-05-14"]]}}]]\n'
            "29:T123,"
        )
        stats_payload = json.dumps(
            {
                "augments": {
                    "1373": {"tier": "1", "win_rate": "0.5774767146486028", "pick_rate": "0.06533163688665154"},
                    "1420": {"tier": "1", "win_rate": "0.5656274561173696", "pick_rate": "0.05278807324224152"},
                    "1406": {"tier": "1", "win_rate": "0.5623392704067054", "pick_rate": "0.1451983183050285"},
                    "1058": {"tier": "5", "win_rate": "0.42105263157894735", "pick_rate": "0.002627705297509843"},
                }
            },
            ensure_ascii=False,
        )
        noise = '<div>{"1052":{"winRate":0.62961,"pickRate":0.078373}}</div>'
        return noise + _flight_script(ref_payload) + _flight_script(stats_payload)

    def test_parser_reads_only_referenced_react_flight_augments(self):
        aug_id_map, truth_dict = self._morgana_maps()

        rows = extract_champion_stats(
            self._morgana_html(),
            aug_id_map,
            truth_dict,
            "25",
            "堕落天使",
            {"tier": "1", "winRate": 0.5255849975106489, "pickRate": 0.011419658717905307},
        )

        names = [row["海克斯名称"] for row in rows]
        self.assertEqual(names[:3], ["缩小引擎", "咏叹奏鸣", "祖母的辣椒油"])
        self.assertNotIn("闪电打击", names)
        self.assertEqual(rows[0]["海克斯ID"], "1373")
        self.assertEqual(rows[0]["源站排名"], 1)
        self.assertEqual(rows[0]["源站层级"], "T1")
        self.assertAlmostEqual(rows[0]["海克斯胜率"], 0.5774767146486028)
        self.assertAlmostEqual(rows[0]["海克斯出场率"], 0.06533163688665154)

    def test_noise_four_digit_stats_are_not_scraped(self):
        aug_id_map, truth_dict = self._morgana_maps()
        html = self._morgana_html() + '<script>{"9999":{"win_rate":0.99,"pick_rate":0.99}}</script>'

        rows = extract_champion_stats(
            html,
            aug_id_map,
            truth_dict,
            "25",
            "堕落天使",
            {"tier": "1", "winRate": 0.52, "pickRate": 0.01},
        )

        self.assertEqual({row["海克斯ID"] for row in rows}, {"1373", "1420", "1406", "1058"})

    def test_view_adapter_uses_source_rank_for_comprehensive_only(self):
        df = pd.DataFrame(
            [
                {
                    "英雄ID": "25",
                    "英雄名称": "堕落天使",
                    "英雄评级": "T1",
                    "英雄胜率": 0.52,
                    "英雄出场率": 0.01,
                    "海克斯ID": "1",
                    "源站排名": 2,
                    "源站层级": "T1",
                    "海克斯阶级": "黄金",
                    "海克斯名称": "高胜率后排",
                    "海克斯胜率": 0.9,
                    "海克斯出场率": 0.01,
                    "胜率差": 0.38,
                    "综合得分": 100,
                },
                {
                    "英雄ID": "25",
                    "英雄名称": "堕落天使",
                    "英雄评级": "T1",
                    "英雄胜率": 0.52,
                    "英雄出场率": 0.01,
                    "海克斯ID": "2",
                    "源站排名": 1,
                    "源站层级": "T1",
                    "海克斯阶级": "黄金",
                    "海克斯名称": "源站第一",
                    "海克斯胜率": 0.5,
                    "海克斯出场率": 0.5,
                    "胜率差": -0.02,
                    "综合得分": -100,
                },
            ]
        )

        result = process_hextechs_data(df, "堕落天使", catalog_lookup={}, use_runtime_cache=False)

        self.assertEqual(result["comprehensive"][0]["海克斯名称"], "源站第一")
        self.assertEqual(result["top_10_overall"][0]["源站排名"], 1)
        self.assertEqual(result["winrate_only"][0]["海克斯名称"], "高胜率后排")


if __name__ == "__main__":
    unittest.main()

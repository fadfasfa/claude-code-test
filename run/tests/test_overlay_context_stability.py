"""测试 overlay 上下文稳定性。

调用方: pytest; 关键依赖: hextech.overlay.renderer。
"""
from __future__ import annotations

import unittest
from unittest import mock


def _hint_cache() -> dict:
    hints = {
        "a0": {
            "augment_id": "a0",
            "name": "强化 1",
            "tier": "Gold",
            "stats_by_champion_id": {
                "266": {"winrate": 0.55, "pickrate": 0.03},
            },
            "stats_by_champion_name": {
                "暗裔剑魔": {"winrate": 0.55, "pickrate": 0.03},
            },
            "synergies": [
                {"hero_id": "266", "hero_name": "暗裔剑魔", "rating": "S", "tag": "联动", "content": "稳定联动"}
            ],
        }
    }
    return {
        "schema_version": 1,
        "source": {"private_policy_stats_enabled": True},
        "hints": hints,
        "name_index": {},
    }


def _alias_hint_cache() -> dict:
    return {
        "schema_version": 1,
        "source": {"private_policy_stats_enabled": True},
        "hints": {
            "1322": {
                "augment_id": "1322",
                "name": "罪恶快感",
                "tier": "Gold",
                "stats_by_champion_id": {"266": {"winrate": 0.51, "pickrate": 0.07}},
            }
        },
        "name_index": {
            "aram_getexcited": "1322",
            "ARAM_GetExcited": "1322",
            "罪恶快感": "1322",
        },
    }


class OverlayContextStabilityTests(unittest.TestCase):
    def test_recent_context_prevents_waiting_hero_flicker_on_transient_missing(self):
        from hextech.overlay import renderer

        snapshot = {
            "ok": True,
            "visible": True,
            "source": {"selection_window_active": True},
            "slots": [{"slot": 0, "state": "ready", "augment_id": "a0", "name": "强化 1", "tier": "Gold"}],
        }
        recent_context = {"ok": True, "champion_id": "266", "champion_name": "暗裔剑魔"}
        transient_missing = {"ok": False, "error": "context_missing"}

        model = renderer.build_render_model(
            snapshot,
            hint_cache=_hint_cache(),
            context=transient_missing,
            recent_context=recent_context,
        )

        self.assertEqual(model["stats"][0]["status_code"], "READY")
        self.assertEqual(model["stats"][0]["status_text"], "")
        self.assertEqual(model["stats"][0]["stats_text"], "胜率 55.0% · 出场 3.0%")

    def test_expired_context_does_not_reuse_recent_context(self):
        from hextech.overlay import renderer

        snapshot = {
            "ok": True,
            "visible": True,
            "source": {"selection_window_active": True},
            "slots": [{"slot": 0, "state": "ready", "augment_id": "a0", "name": "强化 1", "tier": "Gold"}],
        }

        model = renderer.build_render_model(
            snapshot,
            hint_cache=_hint_cache(),
            context={"ok": False, "error": "context_expired"},
            recent_context={"ok": True, "champion_id": "266", "champion_name": "暗裔剑魔"},
        )

        self.assertEqual(model["stats"][0]["status_code"], "CONTEXT_EXPIRED")
        self.assertEqual(model["stats"][0]["status_text"], "等待当前英雄")

    def test_missing_context_status_text_explains_lcu_unavailable(self):
        from hextech.overlay import renderer

        model = renderer.build_render_model(
            {
                "ok": True,
                "visible": True,
                "slots": [{"slot": 0, "state": "ready", "augment_id": "a0", "name": "强化 1", "tier": "Gold"}],
            },
            hint_cache=_hint_cache(),
            context={"ok": False, "error": "context_missing", "source": "lcu-unavailable"},
        )

        self.assertEqual(model["stats"][0]["status_code"], "CONTEXT_MISSING")
        self.assertEqual(model["stats"][0]["status_text"], "等待 LCU")

    def test_renderer_resolves_augment_name_id_alias_without_slot_name(self):
        from hextech.overlay import renderer

        snapshot = {
            "ok": True,
            "visible": True,
            "source": {"selection_window_active": True},
            "slots": [{"slot": 0, "state": "ready", "augment_id": "aram_getexcited", "name": "", "tier": "Gold"}],
        }

        model = renderer.build_render_model(
            snapshot,
            hint_cache=_alias_hint_cache(),
            context={"ok": True, "champion_id": "266", "champion_name": "暗裔剑魔"},
        )

        self.assertEqual(model["stats"][0]["name"], "罪恶快感")
        self.assertEqual(model["stats"][0]["status_code"], "READY")
        self.assertEqual(model["stats"][0]["stats_text"], "胜率 51.0% · 出场 7.0%")

    def test_hint_cache_indexes_augment_name_id_alias(self):
        from hextech.overlay.hints import build_overlay_hint_cache

        cache = build_overlay_hint_cache(
            {
                "暗裔剑魔": {
                    "comprehensive": [
                        {
                            "英雄名称": "暗裔剑魔",
                            "英雄 ID": "266",
                            "海克斯ID": "1322",
                            "海克斯名称": "罪恶快感",
                            "海克斯阶级": "黄金",
                            "augment_name_id": "ARAM_GetExcited",
                            "海克斯胜率": 0.51,
                            "海克斯出场率": 0.07,
                        }
                    ]
                }
            },
            include_private_stats=True,
        )

        self.assertEqual(cache["name_index"]["aram_getexcited"], "1322")
        self.assertEqual(cache["name_index"]["罪恶快感"], "1322")
        self.assertIn("aram_getexcited", cache["hints"]["1322"]["aliases"])

    def test_hint_cache_indexes_aram_prefixed_alias_for_plain_augment_name_id(self):
        from hextech.overlay.hints import build_overlay_hint_cache

        cache = build_overlay_hint_cache(
            {
                "暗裔剑魔": {
                    "comprehensive": [
                        {
                            "英雄名称": "暗裔剑魔",
                            "英雄 ID": "266",
                            "海克斯ID": "1029",
                            "海克斯名称": "虚幻武器",
                            "海克斯阶级": "黄金",
                            "augment_name_id": "EtherealWeapon",
                            "海克斯胜率": 0.52,
                            "海克斯出场率": 0.08,
                        }
                    ]
                }
            },
            include_private_stats=True,
        )

        self.assertEqual(cache["name_index"]["etherealweapon"], "1029")
        self.assertEqual(cache["name_index"]["aram_etherealweapon"], "1029")

    def test_runtime_csv_cache_keeps_manifest_only_alias_as_no_stats_hint(self):
        import pandas as pd

        from hextech.catalog import runtime_store
        from hextech.overlay import renderer
        from hextech.overlay.hints import build_overlay_hint_cache_from_precomputed, query_overlay_hint

        latest_df = pd.DataFrame(
            [
                {
                    "英雄名称": "暗裔剑魔",
                    "英雄 ID": "266",
                    "海克斯ID": "1322",
                    "海克斯名称": "罪恶快感",
                    "海克斯阶级": "黄金",
                    "海克斯胜率": 0.51,
                    "海克斯出场率": 0.07,
                    "源站排名": 1,
                    "综合得分": 1.0,
                }
            ]
        )
        catalog_lookup = {
            "冰雪爆裂": {
                "name": "冰雪爆裂",
                "tier": "黄金",
                "cdragon_id": 2080,
                "augment_name_id": "Snowbomb",
                "icon_url": "/assets/snowbomb_small.png",
                "tooltip_plain": "在目标位置引爆雪球。",
            },
            "占位强化 A": {
                "name": "占位强化 A",
                "tier": "白银",
                "cdragon_id": -1,
                "augment_name_id": "PlaceholderA",
                "icon_url": "/assets/placeholdera_small.png",
            },
            "占位强化 B": {
                "name": "占位强化 B",
                "tier": "白银",
                "cdragon_id": -1,
                "augment_name_id": "PlaceholderB",
                "icon_url": "/assets/placeholderb_small.png",
            }
        }

        with (
            mock.patch.object(runtime_store, "get_latest_csv", return_value="C:/tmp/Hextech_Data_2099-01-01.csv"),
            mock.patch.object(runtime_store, "load_runtime_csv", return_value=latest_df),
            mock.patch("hextech.scraping.augment_catalog.load_augment_catalog_lookup_read_only", return_value=catalog_lookup),
        ):
            cache = build_overlay_hint_cache_from_precomputed(include_private_stats=True, source_tag="test")

        hint_result = query_overlay_hint(cache, "snowbomb")
        self.assertTrue(hint_result["ok"])
        self.assertEqual(hint_result["hint"]["augment_id"], "2080")
        self.assertEqual(hint_result["hint"]["name"], "冰雪爆裂")
        self.assertNotIn("stats_by_champion_id", hint_result["hint"])
        self.assertEqual(query_overlay_hint(cache, "placeholdera")["hint"]["name"], "占位强化 A")
        self.assertEqual(query_overlay_hint(cache, "placeholderb")["hint"]["name"], "占位强化 B")

        model = renderer.build_render_model(
            {
                "ok": True,
                "visible": True,
                "slots": [{"slot": 0, "state": "ready", "augment_id": "snowbomb", "name": "", "tier": "Gold"}],
            },
            hint_cache=cache,
            context={"ok": True, "champion_id": "266", "champion_name": "暗裔剑魔"},
        )

        self.assertEqual(model["stats"][0]["name"], "冰雪爆裂")
        self.assertEqual(model["stats"][0]["status_code"], "SOURCE_STATS_MISSING")
        self.assertEqual(model["stats"][0]["stats_text"], "源站暂无该组合统计")
        self.assertEqual(model["stats"][0]["status_text"], "源站暂无统计")


if __name__ == "__main__":
    unittest.main()

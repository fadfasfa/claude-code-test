import pytest
from hextech.bootstrap.startup_refresh import initial_auto_refresh_delay_seconds
from hextech.modules.recommendation.hints import enrich_overlay_hint_cache_with_catalog, enrich_overlay_hint_cache_with_synergy

def test_verified_seed_delays_only_automatic_startup_refresh() -> None:
    from hextech.bootstrap.startup_refresh import StartupRefreshSchedule

    seeded = {
        "state": "ready",
        "generation_id": "generation-seed",
        "source": "verified_seed",
    }

    assert initial_auto_refresh_delay_seconds(seeded) == 30.0
    assert initial_auto_refresh_delay_seconds({**seeded, "source": "runtime_current"}) == 0.0
    assert initial_auto_refresh_delay_seconds({**seeded, "generation_id": ""}) == 0.0
    schedule = StartupRefreshSchedule.create(seeded, skip=False, now=100.0)
    assert schedule.consume_if_due(129.9) is False
    assert schedule.consume_if_due(130.0) is True
    assert schedule.consume_if_due(131.0) is False



def test_brand_synergy_projects_all_known_names_and_catalog_only_hint() -> None:
    """复仇焰魂的 11 条方案必须附着到最终 hint，而不是停在 generation 原始数据。"""

    names = ["虚幻武器", "你肩上的恶魔", *[f"联动海克斯{i}" for i in range(1, 13)]]
    cache = {
        "schema_version": 1,
        "source": {"private_policy_stats_enabled": True},
        "hints": {
            "100": {
                "augment_id": "100",
                "name": "虚幻武器",
                "stats_by_champion_id": {"63": {"winrate": 0.55, "pickrate": 0.12}},
            }
        },
        "name_index": {"虚幻武器": "100"},
    }
    catalog = [
        {"augment_name_id": "illusory_weapons", "name": "虚幻武器", "tier": "Gold"},
        {"augment_name_id": "devil_on_your_shoulder", "name": "你肩上的恶魔", "tier": "Prismatic"},
        *[
            {"augment_name_id": f"synergy_{index}", "name": name, "tier": "Gold"}
            for index, name in enumerate(names[2:], start=1)
        ],
    ]
    enrich_overlay_hint_cache_with_catalog(cache, catalog)
    items = [
        {
            "augment_names": names[index : index + 2],
            "rating": "S" if index == 0 else "A",
            "content": f"复仇焰魂方案 {index + 1}",
        }
        for index in range(0, 11)
    ]
    # 保证测试口径与真机证据一致：11 条方案、14 个唯一名称。
    items[-1]["augment_names"] = names[10:]

    report = enrich_overlay_hint_cache_with_synergy(
        cache,
        {"63": {"name": "复仇焰魂", "synergy_items": items}},
    )

    assert report["hero_count"] == 1
    assert report["item_count"] == 11
    assert report["unique_augment_name_count"] == 14
    assert report["catalog_resolvable_name_count"] == 14
    assert report["projected_name_count"] == 14
    assert report["projection_coverage"] == 1.0
    virtual = cache["hints"][cache["name_index"]["虚幻武器"]]
    shoulder = cache["hints"][cache["name_index"]["你肩上的恶魔"]]
    assert virtual["synergies"][0]["hero_id"] == "63"
    assert shoulder["synergies"][0]["hero_name"] == "复仇焰魂"
    assert not shoulder.get("stats_by_champion_id")
    assert report["catalog_only_hints_with_synergy_count"] >= 1

def test_synergy_projection_rejects_empty_or_unresolvable_input() -> None:
    cache = {"hints": {"known": {"augment_id": "known", "name": "已知"}}, "name_index": {"已知": "known"}}

    with pytest.raises(ValueError, match="输入为空"):
        enrich_overlay_hint_cache_with_synergy(cache, {"63": {"synergy_items": []}})
    with pytest.raises(ValueError, match="没有可关联 Catalog"):
        enrich_overlay_hint_cache_with_synergy(
            cache,
            {"63": {"name": "复仇焰魂", "synergy_items": [{"augment_names": ["污染名称"]}]}},
        )

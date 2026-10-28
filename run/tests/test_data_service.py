from hextech.modules.recommendation.identities import build_augment_identity_payload as _build_augment_identity_payload

def test_augment_identities_map_vision_ids_without_inventing_source_stats() -> None:
    overlay_hints = {
        "hints": {
            "1027": {"name": "大地苏醒"},
            "1421": {"name": "舞会女王"},
            "2006": {"name": "飞身踢"},
            "1018": {"name": "巨像的勇气"},
        }
    }
    catalog = [
        {"name": "大地苏醒", "tier": "棱彩", "augment_name_id": "ARAM_Earthwake", "cdragon_id": 1027},
        {"name": "舞会女王", "tier": "棱彩", "augment_name_id": "PromQueen", "cdragon_id": 1421},
        {"name": "飞身踢", "tier": "棱彩", "augment_name_id": "ARAM_Dropkick", "cdragon_id": 2006},
        {"name": "巨像的勇气", "tier": "棱彩", "augment_name_id": "ARAM_CourageoftheColossus", "cdragon_id": 1018},
        {"name": "歼灭者", "tier": "白银", "augment_name_id": "Weapon_Nuke", "cdragon_id": 33220},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["aram_earthwake"] == "1027"
    assert identities["augment_aliases"]["promqueen"] == "1421"
    assert identities["augment_aliases"]["aram_dropkick"] == "2006"
    assert identities["augment_aliases"]["aram_courageofthecolossus"] == "1018"
    assert identities["catalog_augments"]["weapon_nuke"]["canonical_id"] == ""
    assert identities["catalog_augments"]["weapon_nuke"]["stats_available"] is False

def test_augment_identity_names_are_not_first_wins_when_canonical_ids_are_ambiguous() -> None:
    overlay_hints = {
        "hints": {
            "100": {"name": "同名强化"},
            "200": {"name": "同名强化"},
        }
    }
    catalog = [
        {"name": "同名强化", "tier": "黄金", "augment_name_id": "Same_Gold", "cdragon_id": 1},
        {"name": "同名强化", "tier": "棱彩", "augment_name_id": "Same_Prismatic", "cdragon_id": 2},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["100"] == "100"
    assert identities["augment_aliases"]["200"] == "200"
    assert "同名强化" not in identities["augment_aliases"]
    assert identities["catalog_augments"]["same_gold"]["canonical_id"] == ""
    assert identities["catalog_augments"]["same_prismatic"]["canonical_id"] == ""

def test_glass_cannon_visual_variants_share_unique_name_statistics() -> None:
    overlay_hints = {"hints": {"1325": {"name": "玻璃大炮"}}}
    catalog = [
        {"name": "玻璃大炮", "tier": "黄金", "augment_name_id": "Special_GlassCannon", "cdragon_id": 33307},
        {"name": "玻璃大炮", "tier": "棱彩", "augment_name_id": "GlassCannon", "cdragon_id": 1325},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["special_glasscannon"] == "1325"
    assert identities["augment_aliases"]["glasscannon"] == "1325"
    assert identities["augment_aliases"]["玻璃大炮"] == "1325"

def test_current_hard_names_resolve_to_the_unique_statistical_ids() -> None:
    overlay_hints = {
        "hints": {
            "1020": {"name": "黎明使者的坚决"},
            "2089": {"name": "哎哟，我的硬币！"},
        }
    }
    catalog = [
        {"name": "黎明使者的坚决", "tier": "黄金", "augment_name_id": "ARAM_DawnbringersResolve"},
        {"name": "黎明使者的坚决", "tier": "黄金", "augment_name_id": "DawnbringersResolve"},
        {"name": "哎哟，我的硬币！", "tier": "黄金", "augment_name_id": "ARAM_YowchMyCoins"},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["黎明使者的坚决"] == "1020"
    assert identities["augment_aliases"]["aram_dawnbringersresolve"] == "1020"
    assert identities["augment_aliases"]["dawnbringersresolve"] == "1020"
    assert identities["augment_aliases"]["哎哟，我的硬币！"] == "2089"
    assert identities["augment_aliases"]["aram_yowchmycoins"] == "2089"

"""One deterministic numerical card formula for source validation and release views."""
from .hero_rankings import standard_scores


def project_augment_cards(rows, hero_id, hero_name, catalog_by_id, *, allowed_ids=None):
    cards = []
    for raw in rows:
        identity = str(raw["id"])
        if identity not in catalog_by_id:
            raise ValueError(f"augment cannot bind Catalog: {hero_id}/{identity}")
        if allowed_ids is not None and identity not in allowed_ids:
            continue
        catalog = catalog_by_id[identity]
        cards.append({"id": identity, "hero_id": hero_id, "hero_name": hero_name, "海克斯ID": identity,
            "海克斯名称": catalog["name"], "海克斯阶级": catalog.get("tier", ""),
            "海克斯胜率": float(raw["win_rate"]), "海克斯出场率": float(raw["pick_rate"]),
            "winrate": float(raw["win_rate"]), "pickrate": float(raw["pick_rate"]),
            "sample_count": int(raw["sample_count"]), "source_rank": int(raw["source_rank"]),
            "status": "ready", "icon": catalog.get("icon_url", ""), "augment_name_id": catalog.get("augment_name_id", "")})
    wins = standard_scores([row["winrate"] for row in cards])
    picks = standard_scores([row["pickrate"] for row in cards])
    for card, win, pick in zip(cards, wins, picks, strict=True):
        card["score"] = card["综合得分"] = win * .85 + pick * .15
    cards.sort(key=lambda row: (-row["score"], -row["sample_count"], int(row["id"])))
    for rank, card in enumerate(cards, 1):
        card["rank"] = rank
    return cards

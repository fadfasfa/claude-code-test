"""Shared deterministic ARAMKit hero ranking formula (all ranking rows)."""
from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from hextech.modules.data.catalog.champion_tier import champion_tier_from_score


def standard_scores(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0 for _ in values]
    deviation = statistics.stdev(values)
    if deviation == 0:
        return [0.0 for _ in values]
    mean = statistics.fmean(values)
    return [(value - mean) / deviation for value in values]


def build_hero_rankings(rows: Sequence[Mapping[str, Any]],
                        catalog_champions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rows use the scoped champion summary schema, not UI or transport schema."""
    if not rows:
        raise ValueError("ARAMKit rankings cannot be empty")
    ids = [str(row["id"]) for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("ARAMKit rankings contain duplicate champion IDs")
    winrates = [float(row["win_rate"]) for row in rows]
    pickrates = [float(row["pick_rate"]) for row in rows]
    average = statistics.fmean(winrates)
    bayesian = [(win * pick + average * 0.00075) / (pick + 0.00075)
                for win, pick in zip(winrates, pickrates, strict=True)]
    win_scores = standard_scores(bayesian)
    pick_scores = standard_scores(pickrates)
    champions = []
    for row, bayes, win_score, pick_score in zip(rows, bayesian, win_scores, pick_scores, strict=True):
        champion_id = str(row["id"])
        catalog = catalog_champions.get(champion_id)
        if not isinstance(catalog, Mapping) or not str(catalog.get("name") or ""):
            raise ValueError(f"ARAMKit hero cannot bind Catalog: {champion_id}")
        score = win_score * 0.80 + pick_score * 0.20
        name = str(catalog["name"])
        champions.append({
            "id": champion_id, "name": name, "英雄 ID": champion_id, "英雄名称": name,
            "英文名": str(catalog.get("en_name") or ""),
            "英雄胜率": float(row["win_rate"]), "英雄出场率": float(row["pick_rate"]),
            "sample_count": int(row["sample_count"]), "贝叶斯胜率": bayes,
            "综合分数": score, "英雄评级": champion_tier_from_score(score),
            "source_rank": int(row["source_rank"]), "source_tier": str(row["source_tier"]),
            "Z_贝叶斯胜率": win_score, "Z_出场率": pick_score,
        })
    champions.sort(key=lambda item: (-float(item["综合分数"]), int(item["id"])))
    return champions

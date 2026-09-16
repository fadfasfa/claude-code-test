"""把 ARAMKit 最小统计投影适配为既有 Web/Overlay generation DTO。

本模块只负责确定性读取、Catalog 展示字段绑定和本地 score/rank 计算；不执行
网络抓取、source 发布或 generation promotion。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


from hextech.modules.recommendation.hero_rankings import build_hero_rankings, standard_scores as _standard_scores


def build_aramkit_payloads(
    pointer: Mapping[str, Any],
    *,
    catalog: Any,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    from hextech.infrastructure.sources.aramkit.service import validate_scoped_stats_artifact
    from hextech.modules.data.catalog.version_catalog import (
        load_augment_manifest_entries,
        load_champion_core_data,
    )
    from hextech.modules.data.source_runs import source_run_artifact_path

    index = validate_scoped_stats_artifact(pointer)
    artifact = pointer.get("artifact") if isinstance(pointer.get("artifact"), Mapping) else {}
    index_path = source_run_artifact_path(
        "aramkit",
        str(pointer.get("run_id") or ""),
        str(artifact.get("relative_path") or ""),
    )
    catalog_champions = load_champion_core_data(catalog.root)
    catalog_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in load_augment_manifest_entries(catalog.root):
        if not isinstance(entry, Mapping):
            continue
        try:
            canonical_id = str(int(entry.get("cdragon_id")))
        except (TypeError, ValueError):
            continue
        if int(canonical_id) <= 0:
            continue
        if canonical_id in catalog_by_id:
            raise ValueError(f"Catalog cdragon_id 不唯一：{canonical_id}")
        catalog_by_id[canonical_id] = entry

    source_rows: list[tuple[dict[str, Any], Mapping[str, Any], list[dict[str, Any]]]] = []
    for descriptor in index["files"]:
        relative = str(descriptor["relative_path"])
        try:
            payload = json.loads((index_path.parent / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"ARAMKit 英雄统计无效：{relative}") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("champion"), Mapping):
            raise ValueError(f"ARAMKit 英雄统计 schema 无效：{relative}")
        champion = dict(payload["champion"])
        champion_id = str(champion.get("id") or "")
        catalog_champion = catalog_champions.get(champion_id)
        if not isinstance(catalog_champion, Mapping):
            raise ValueError(f"ARAMKit 英雄无法绑定 Catalog：{champion_id}")
        cards: list[dict[str, Any]] = []
        all_rows = payload.get("all")
        if not isinstance(all_rows, list) or not all_rows:
            raise ValueError(f"ARAMKit 英雄统计为空：{champion_id}")
        for raw in all_rows:
            if not isinstance(raw, Mapping):
                raise ValueError(f"ARAMKit 海克斯统计 schema 无效：{champion_id}")
            augment_id = str(raw.get("id") or "")
            catalog_augment = catalog_by_id.get(augment_id)
            if not isinstance(catalog_augment, Mapping):
                raise ValueError(f"ARAMKit 海克斯无法绑定 Catalog：{champion_id}/{augment_id}")
            cards.append(
                {
                    "id": augment_id,
                    "hero_id": champion_id,
                    "hero_name": str(catalog_champion.get("name") or ""),
                    "海克斯ID": augment_id,
                    "海克斯名称": str(catalog_augment.get("name") or ""),
                    "海克斯阶级": str(catalog_augment.get("tier") or ""),
                    "海克斯胜率": float(raw["win_rate"]),
                    "海克斯出场率": float(raw["pick_rate"]),
                    "winrate": float(raw["win_rate"]),
                    "pickrate": float(raw["pick_rate"]),
                    "sample_count": int(raw["sample_count"]),
                    "source_rank": int(raw["source_rank"]),
                    "status": "ready",
                    "icon": str(catalog_augment.get("icon_url") or ""),
                    "augment_name_id": str(catalog_augment.get("augment_name_id") or ""),
                }
            )
        win_scores = _standard_scores([float(card["winrate"]) for card in cards])
        pick_scores = _standard_scores([float(card["pickrate"]) for card in cards])
        for card, win_score, pick_score in zip(cards, win_scores, pick_scores, strict=True):
            card["score"] = win_score * 0.85 + pick_score * 0.15
            card["综合得分"] = card["score"]
        cards.sort(key=lambda card: (-float(card["score"]), -int(card["sample_count"]), int(card["id"])))
        for rank, card in enumerate(cards, start=1):
            card["rank"] = rank
        source_rows.append((champion, catalog_champion, cards))

    if not source_rows:
        raise ValueError("ARAMKit generation 没有可发布英雄")
    champions = build_hero_rankings([item[0] for item in source_rows], catalog_champions)
    details: dict[str, dict[str, Any]] = {}
    for source_champion, catalog_champion, cards in source_rows:
        champion_id = str(source_champion["id"])
        name = str(catalog_champion.get("name") or "")
        details[name] = {
            "hero_id": champion_id,
            "hero_name": name,
            "source": "aramkit",
            "dataset": "all",
            "comprehensive": cards,
            "winrate_only": sorted(
                cards,
                key=lambda card: (-float(card["winrate"]), -int(card["sample_count"]), int(card["id"])),
            ),
            "augments": cards,
        }
    champions.sort(key=lambda item: (-float(item["综合分数"]), int(item["id"])))
    return champions, details


__all__ = ["build_aramkit_payloads"]

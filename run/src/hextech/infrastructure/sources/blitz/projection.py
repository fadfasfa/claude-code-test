"""把 Blitz tier 排名适配为 Web/Overlay generation DTO。

Blitz 不提供胜率、选择率或样本量。本模块只发布明确命名的全局 tier 和最多
五个英雄的专属 tier；不得把 tier 推断或伪装成百分比统计。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _production_ids(catalog_root: Path) -> set[str]:
    try:
        payload = json.loads((catalog_root / "augment_assets.v1.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Blitz generation 无法读取 production pool") from exc
    entries = payload.get("entries") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        raise ValueError("Blitz generation production pool 无效")
    return {
        str(item.get("canonical_id") or "").strip()
        for item in entries
        if isinstance(item, Mapping) and str(item.get("canonical_id") or "").strip()
    }


def build_blitz_details(pointer: Mapping[str, Any], *, catalog: Any) -> dict[str, dict[str, Any]]:
    from hextech.infrastructure.sources.blitz.service import validate_blitz_artifact
    from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_champion_core_data

    payload = validate_blitz_artifact(pointer)
    production_ids = _production_ids(Path(catalog.root))
    augments: dict[str, Mapping[str, Any]] = {}
    for item in load_augment_manifest_entries(catalog.root):
        if not isinstance(item, Mapping):
            continue
        raw_id = str(item.get("cdragon_id") or "")
        if raw_id.isdigit() and int(raw_id) > 0:
            augments[str(int(raw_id))] = item
    rows = {
        str(item["augment_id"]): item
        for item in payload["rows"]
        if str(item["augment_id"]) in production_ids
    }
    covered_ids = set(rows)
    missing_ids = sorted(production_ids - covered_ids, key=int)
    coverage_ratio = len(covered_ids) / len(production_ids)
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        if coverage_ratio < 0.95:
            raise ValueError("Blitz generation legacy production coverage 低于 95%")
    else:
        coverage_state = str(coverage.get("coverage_state") or "")
        try:
            production_count = int(coverage.get("production_count"))
            covered_count = int(coverage.get("covered_count"))
            declared_ratio = float(coverage.get("coverage_ratio"))
            source_record_count = int(coverage.get("source_record_count"))
            filtered_record_count = int(coverage.get("filtered_source_record_count"))
            previous_record_count = int(coverage.get("previous_record_count") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Blitz generation coverage 数值无效") from exc
        declared_missing = coverage.get("missing_ids")
        compatibility_filtered = coverage.get("compatibility_filtered_augment_ids")
        if (
            production_count != len(production_ids)
            or covered_count != len(covered_ids)
            or not math.isclose(declared_ratio, coverage_ratio, rel_tol=0.0, abs_tol=1e-12)
            or declared_missing != missing_ids
            or filtered_record_count != len(payload["rows"])
            or source_record_count < filtered_record_count
            or not isinstance(compatibility_filtered, list)
            or source_record_count - filtered_record_count != len(compatibility_filtered)
        ):
            raise ValueError("Blitz generation coverage 与 artifact/Catalog 不一致")
        if coverage_state == "partial_ready":
            previous_ratio = coverage.get("previous_record_ratio")
            try:
                previous_ratio_value = float(previous_ratio)
            except (TypeError, ValueError) as exc:
                raise ValueError("Blitz partial generation 缺少 previous record ratio") from exc
            expected_previous_ratio = (
                source_record_count / previous_record_count if previous_record_count > 0 else 0.0
            )
            if (
                str(coverage.get("coverage_policy") or "") != "active_partial"
                or not (0.85 <= coverage_ratio < 0.95)
                or previous_record_count <= 0
                or not math.isclose(
                    previous_ratio_value,
                    expected_previous_ratio,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                or previous_ratio_value < 0.90
            ):
                raise ValueError("Blitz partial generation 未满足 active coverage 合同")
        elif coverage_state != "ready" or coverage_ratio < 0.95:
            raise ValueError("Blitz generation coverage_state 无效")
    champions = load_champion_core_data(catalog.root)
    details: dict[str, dict[str, Any]] = {}
    for champion_id, champion in champions.items():
        champion_name = str(champion.get("name") or "").strip()
        if not champion_name:
            raise ValueError(f"Blitz generation 英雄缺少名称：{champion_id}")
        cards: list[dict[str, Any]] = []
        for augment_id, row in rows.items():
            catalog_augment = augments.get(augment_id)
            if not isinstance(catalog_augment, Mapping):
                raise ValueError(f"Blitz 海克斯无法绑定 Catalog：{augment_id}")
            champion_tiers = {
                str(item["champion_id"]): int(item["tier"])
                for item in row["top_champions"]
            }
            source_tier = int(row["tier"])
            champion_tier = champion_tiers.get(str(champion_id))
            effective_tier = champion_tier or source_tier
            cards.append(
                {
                    "id": augment_id,
                    "hero_id": str(champion_id),
                    "hero_name": champion_name,
                    "海克斯ID": augment_id,
                    "海克斯名称": str(catalog_augment.get("name") or ""),
                    "海克斯阶级": str(catalog_augment.get("tier") or ""),
                    "score": float(6 - effective_tier),
                    "source_tier": source_tier,
                    "champion_tier": champion_tier,
                    "stats_scope": "top_champion_tier" if champion_tier is not None else "global_tier",
                    "source_patch": str(payload["patch"]),
                    "source_date": str(payload["data_date"]),
                    "status": "ranking_only",
                    "icon": str(catalog_augment.get("icon_url") or ""),
                    "augment_name_id": str(catalog_augment.get("augment_name_id") or ""),
                }
            )
        cards.sort(
            key=lambda card: (
                int(card["champion_tier"] or card["source_tier"]),
                int(card["source_tier"]),
                int(card["id"]),
            )
        )
        for rank, card in enumerate(cards, start=1):
            card["rank"] = rank
            card["source_rank"] = rank
        details[champion_name] = {
            "hero_id": str(champion_id),
            "hero_name": champion_name,
            "source": "blitz",
            "dataset": "augment_ranking",
            "comprehensive": cards,
            "augments": cards,
        }
    if not details:
        raise ValueError("Blitz generation 没有可发布英雄")
    return details


__all__ = ["build_blitz_details"]

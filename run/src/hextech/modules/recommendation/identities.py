"""Shared pure identity index for snapshot projections."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any

def build_augment_identity_payload(
    overlay_hints: Mapping[str, Any],
    catalog_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """把 Vision stable ID 与源站数字统计 ID 收口到同一身份索引。"""

    from hextech.modules.recommendation.hints import normalize_augment_id, normalize_augment_name

    hint_map = overlay_hints.get("hints", {})
    if not isinstance(hint_map, Mapping):
        hint_map = {}

    augments: dict[str, str] = {}
    canonical_ids_by_name: dict[str, set[str]] = {}
    for raw_id, raw_hint in hint_map.items():
        if not isinstance(raw_hint, Mapping):
            continue
        canonical_id = str(raw_id).strip()
        name = str(raw_hint.get("name") or "").strip()
        if not canonical_id.isdecimal() or not name:
            continue
        augments[canonical_id] = name
        canonical_ids_by_name.setdefault(normalize_augment_name(name), set()).add(canonical_id)

    aliases: dict[str, str] = {}
    for canonical_id, name in augments.items():
        # 数字 ID 永远无歧义；名称只有唯一 canonical 候选时才可成为 alias。
        # 旧逻辑用 setdefault 让同名项按遍历顺序 first-wins，会静默绑定错误统计。
        for alias in (canonical_id,):
            if alias:
                aliases.setdefault(alias, canonical_id)
    for normalized_name, candidates in canonical_ids_by_name.items():
        if len(candidates) != 1:
            continue
        canonical_id = next(iter(candidates))
        name = augments.get(canonical_id, "")
        for alias in (name, normalize_augment_id(name), normalized_name):
            if alias:
                aliases[alias] = canonical_id

    catalog_augments: dict[str, dict[str, Any]] = {}
    for entry in catalog_entries:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        stable_id = normalize_augment_id(entry.get("augment_name_id"), name)
        if not name or not stable_id:
            continue
        candidates = canonical_ids_by_name.get(normalize_augment_name(name), set())
        canonical_id = next(iter(candidates)) if len(candidates) == 1 else ""
        item = {
            "vision_id": stable_id,
            "name": name,
            "tier": str(entry.get("tier") or "").strip(),
            "canonical_id": canonical_id,
            "stats_available": bool(canonical_id),
            "ambiguous": len(candidates) > 1,
        }
        existing = catalog_augments.get(stable_id)
        if existing and existing != item:
            existing["ambiguous"] = True
            existing["canonical_id"] = ""
            existing["stats_available"] = False
            continue
        catalog_augments[stable_id] = item
        if canonical_id:
            for alias in (
                stable_id,
                str(entry.get("augment_name_id") or "").strip(),
                name,
                normalize_augment_id(name),
                normalize_augment_name(name),
            ):
                if alias:
                    aliases.setdefault(alias, canonical_id)

    return {
        "schema_version": 2,
        "augments": augments,
        "augment_aliases": aliases,
        "catalog_augments": catalog_augments,
    }

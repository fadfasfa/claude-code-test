from __future__ import annotations

"""processed/indexes 写盘层。"""

import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from processing.runtime_store import resolve_runtime_file
from scraping.version_sync import (
    DATA_INDEXES_DIR,
    DATA_PROCESSED_DIR,
    load_champion_core_data,
)

CHAMPION_LIST_CACHE_FILE = os.path.join(DATA_PROCESSED_DIR, "Champion_List_Cache.json")
HEXTECH_DETAIL_CACHE_FILE = os.path.join(DATA_PROCESSED_DIR, "Champion_Hextech_Cache.json")
HEXTECH_DETAIL_CACHE_DIR = os.path.join(DATA_PROCESSED_DIR, "Champion_Hextech_Cache")

CHAMPIONS_LIST_VIEW_FILE = os.path.join(DATA_PROCESSED_DIR, "champions.list.v1.json")
CHAMPION_DETAIL_VIEW_FILE = os.path.join(DATA_PROCESSED_DIR, "champion.detail.v1.json")
CHAMPION_DETAILS_DIR = os.path.join(DATA_PROCESSED_DIR, "champions")
SYNERGY_BY_CHAMPION_FILE = os.path.join(DATA_PROCESSED_DIR, "synergy.by_champion.v1.json")
AUGMENTS_LIST_VIEW_FILE = os.path.join(DATA_PROCESSED_DIR, "augments.list.v1.json")
BUNDLE_META_FILE = os.path.join(DATA_PROCESSED_DIR, "bundle.meta.v1.json")

CHAMPION_ALIAS_INDEX_FILE = os.path.join(DATA_INDEXES_DIR, "Champion_Alias_Index.json")
CHAMPION_ALIAS_LOOKUP_FILE = os.path.join(DATA_INDEXES_DIR, "champion.alias-to-id.v1.json")
CHAMPION_DETAIL_ROUTE_FILE = os.path.join(DATA_INDEXES_DIR, "champion.id-to-detail.v1.json")
CHAMPION_ID_NAME_FILE = os.path.join(DATA_INDEXES_DIR, "champion.id-to-name.v1.json")
AUGMENT_ICON_LOOKUP_FILE = os.path.join(DATA_INDEXES_DIR, "augment.name-to-icon.v1.json")

SYNERGY_SOURCE_NAME = "Champion_Synergy.json"

LEGACY_TO_VIEW_SECTION = {
    "comprehensive": "comprehensive",
    "winrate_only": "winrateOnly",
    "Prismatic": "Prismatic",
    "Gold": "Gold",
    "Silver": "Silver",
}
VIEW_TO_LEGACY_SECTION = {value: key for key, value in LEGACY_TO_VIEW_SECTION.items()}
VIEW_SECTION_KEYS = ["comprehensive", "winrateOnly", "Prismatic", "Gold", "Silver"]
AUGMENT_LIST_LIMIT = 10
SYNERGY_EMPTY_PAYLOAD = {"synergies": []}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, path)


def _get_core_data() -> Dict[str, Any]:
    try:
        return load_champion_core_data() or {}
    except Exception:
        return {}


def _load_synergy_source() -> dict:
    synergy_source_file = resolve_runtime_file(SYNERGY_SOURCE_NAME)
    if not synergy_source_file:
        return {}
    try:
        with open(synergy_source_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _transform_synergy_payload(raw_payload: dict, core_data: Dict[str, Any]) -> dict[str, dict]:
    by_hero: dict[str, dict] = {}
    name_to_id = {}
    for hero_id, entry in core_data.items():
        if isinstance(entry, dict):
            hero_name = str(entry.get("name", "")).strip()
            if hero_name:
                name_to_id[hero_name.lower()] = str(hero_id)

    for hero_key, payload in raw_payload.items():
        if not isinstance(payload, dict):
            continue
        resolved_id = str(hero_key).strip()
        if not resolved_id.isdigit():
            resolved_id = name_to_id.get(resolved_id.lower(), resolved_id)
        synergies = payload.get("synergies", [])
        by_hero[resolved_id] = {
            "synergies": synergies if isinstance(synergies, list) else [],
        }
    return by_hero


def _to_view_champion_item(legacy_item: dict) -> dict:
    hero_id = str(legacy_item.get("英雄 ID", "")).strip()
    hero_name = str(legacy_item.get("英雄名称", "")).strip()
    en_name = str(legacy_item.get("英文名", "")).strip()
    return {
        "heroId": hero_id,
        "heroName": hero_name,
        "enName": en_name,
        "icon": f"/assets/{hero_id}.png" if hero_id else "",
        "stats": {
            "winRate": float(legacy_item.get("英雄胜率", 0) or 0),
            "pickRate": float(legacy_item.get("英雄出场率", 0) or 0),
            "bayesWinRate": float(legacy_item.get("贝叶斯胜率", 0) or 0),
            "score": float(legacy_item.get("综合分数", 0) or 0),
        },
        "search": {"aliases": []},
        "detailKey": hero_id or hero_name,
        "legacy": dict(legacy_item),
    }


def _build_hero_summary(legacy_item: Optional[dict]) -> dict:
    item = legacy_item or {}
    return {
        "winRate": float(item.get("英雄胜率", 0) or 0),
        "pickRate": float(item.get("英雄出场率", 0) or 0),
        "bayesWinRate": float(item.get("贝叶斯胜率", 0) or 0),
        "score": float(item.get("综合分数", 0) or 0),
    }


def _to_view_hextech_item(legacy_item: dict) -> dict:
    return {
        "augmentId": str(legacy_item.get("海克斯名称", "")).strip(),
        "name": str(legacy_item.get("海克斯名称", "")).strip(),
        "tier": str(legacy_item.get("海克斯阶级", "")).strip(),
        "icon": str(legacy_item.get("icon", "")).strip(),
        "stats": {
            "winRate": float(legacy_item.get("海克斯胜率", 0) or 0),
            "pickRate": float(legacy_item.get("海克斯出场率", 0) or 0),
            "deltaWinRate": float(legacy_item.get("胜率差", 0) or 0),
            "score": float(legacy_item.get("综合得分", 0) or 0),
        },
        "tooltip": {
            "plain": str(legacy_item.get("tooltip_plain", "")).strip(),
            "raw": str(legacy_item.get("tooltip", "")).strip(),
        },
        "legacy": dict(legacy_item),
    }


def _to_view_detail_payload(
    hero_name: str,
    hero_summary: Optional[dict],
    legacy_sections: dict,
    synergy_payload: Optional[dict],
    core_data: Dict[str, Any],
) -> dict:
    hero_id = ""
    en_name = ""
    for candidate_id, payload in core_data.items():
        if not isinstance(payload, dict):
            continue
        if str(payload.get("name", "")).strip() == hero_name:
            hero_id = str(candidate_id).strip()
            en_name = str(payload.get("en_name", "")).strip()
            break

    sections = {key: [] for key in VIEW_SECTION_KEYS}
    for legacy_key, view_key in LEGACY_TO_VIEW_SECTION.items():
        raw_items = legacy_sections.get(legacy_key, []) if isinstance(legacy_sections, dict) else []
        if not isinstance(raw_items, list):
            raw_items = []
        sections[view_key] = [_to_view_hextech_item(item) for item in raw_items if isinstance(item, dict)]

    return {
        "hero": {
            "heroId": hero_id,
            "heroName": hero_name,
            "enName": en_name,
            "icon": f"/assets/{hero_id}.png" if hero_id else "",
        },
        "summary": _build_hero_summary(hero_summary),
        "sections": sections,
        "synergy": list((synergy_payload or {}).get("synergies", [])),
        "meta": {"detailKey": hero_id or hero_name},
    }


def _to_legacy_detail_payload(view_payload: dict) -> dict:
    sections = view_payload.get("sections", {}) if isinstance(view_payload, dict) else {}
    legacy_payload = {
        "top_10_overall": [],
        "comprehensive": [],
        "winrate_only": [],
        "Prismatic": [],
        "Gold": [],
        "Silver": [],
    }
    comprehensive_items = sections.get("comprehensive", []) if isinstance(sections, dict) else []
    if isinstance(comprehensive_items, list):
        legacy_payload["top_10_overall"] = [
            dict(item.get("legacy", {}))
            for item in comprehensive_items[:AUGMENT_LIST_LIMIT]
            if isinstance(item, dict)
        ]
    for view_key, legacy_key in VIEW_TO_LEGACY_SECTION.items():
        raw_items = sections.get(view_key, []) if isinstance(sections, dict) else []
        if not isinstance(raw_items, list):
            raw_items = []
        legacy_payload[legacy_key] = [
            dict(item.get("legacy", {})) for item in raw_items if isinstance(item, dict)
        ]
    return legacy_payload


def _build_alias_records(core_data: Dict[str, Any]) -> list[dict]:
    records: list[dict] = []
    for hero_id, payload in core_data.items():
        if not isinstance(payload, dict):
            continue
        hero_name = str(payload.get("name", "")).strip()
        if not hero_name:
            continue
        records.append(
            {
                "heroName": hero_name,
                "title": str(payload.get("title", "")).strip(),
                "enName": str(payload.get("en_name", "")).strip(),
                "heroId": str(hero_id).strip(),
                "aliases": [],
            }
        )
    return records


def _build_alias_lookup(records: list[dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for record in records:
        hero_id = str(record.get("heroId", "")).strip()
        if not hero_id:
            continue
        for token in (record.get("heroName", ""), record.get("enName", ""), hero_id):
            normalized = str(token).strip().lower()
            if normalized:
                lookup[normalized] = hero_id
    return lookup


def _build_detail_route_map(core_data: Dict[str, Any]) -> dict[str, str]:
    route_map: dict[str, str] = {}
    for hero_id, payload in core_data.items():
        if isinstance(payload, dict):
            hero_name = str(payload.get("name", "")).strip()
            if hero_name:
                route_map[str(hero_id).strip()] = hero_name
    return route_map


def _build_id_name_map(core_data: Dict[str, Any]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for hero_id, payload in core_data.items():
        if not isinstance(payload, dict):
            continue
        hero_id_str = str(hero_id).strip()
        if not hero_id_str:
            continue
        result[hero_id_str] = {
            "heroName": str(payload.get("name", "")).strip(),
            "enName": str(payload.get("en_name", "")).strip(),
            "title": str(payload.get("title", "")).strip(),
        }
    return result


def _build_augments_payload(catalog_lookup: Dict[str, Any]) -> dict:
    items = []
    icon_lookup: dict[str, str] = {}
    seen = set()
    for key, entry in catalog_lookup.items():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or key or "").strip()
        normalized = name.lower()
        if not name or normalized in seen:
            continue
        seen.add(normalized)
        filename = str(entry.get("filename", "")).strip()
        icon_url = f"/assets/{filename}" if filename else str(entry.get("icon_url", "")).strip()
        if icon_url:
            icon_lookup[name] = icon_url
        items.append(
            {
                "augmentId": name,
                "name": name,
                "tier": str(entry.get("tier", "")).strip(),
                "icon": icon_url,
                "tooltipPlain": str(entry.get("tooltip_plain", "")).strip(),
                "tooltip": str(entry.get("tooltip", "")).strip(),
            }
        )
    items.sort(key=lambda item: (item.get("tier", ""), item.get("name", "")))
    return {"items": items, "iconLookup": icon_lookup}


def _write_detail_shards(champion_detail_payload: dict, source_tag: str) -> None:
    os.makedirs(CHAMPION_DETAILS_DIR, exist_ok=True)
    for path in os.listdir(CHAMPION_DETAILS_DIR):
        if path.endswith('.json'):
            try:
                os.remove(os.path.join(CHAMPION_DETAILS_DIR, path))
            except OSError:
                continue

    for detail_payload in champion_detail_payload.values():
        if not isinstance(detail_payload, dict):
            continue
        hero = detail_payload.get("hero", {}) if isinstance(detail_payload.get("hero"), dict) else {}
        hero_id = str(hero.get("heroId", "")).strip()
        if not hero_id:
            continue
        _atomic_write_json(
            os.path.join(CHAMPION_DETAILS_DIR, f"{hero_id}.json"),
            {
                "version": 1,
                "generatedAt": _now_iso(),
                "source": source_tag,
                "item": detail_payload,
            },
        )


def _write_processed_views(
    champions_payload: dict,
    champion_detail_payload: dict,
    synergy_payload: dict,
    augments_payload: dict,
    source_tag: str,
) -> None:
    _atomic_write_json(
        CHAMPIONS_LIST_VIEW_FILE,
        {
            "version": 1,
            "generatedAt": _now_iso(),
            "source": source_tag,
            "items": champions_payload.get("items", []),
        },
    )
    _atomic_write_json(
        CHAMPION_DETAIL_VIEW_FILE,
        {
            "version": 1,
            "generatedAt": _now_iso(),
            "source": source_tag,
            "items": champion_detail_payload,
        },
    )
    _write_detail_shards(champion_detail_payload, source_tag)
    _atomic_write_json(
        SYNERGY_BY_CHAMPION_FILE,
        {
            "version": 1,
            "generatedAt": _now_iso(),
            "source": source_tag,
            "items": synergy_payload,
        },
    )
    _atomic_write_json(
        AUGMENTS_LIST_VIEW_FILE,
        {
            "version": 1,
            "generatedAt": _now_iso(),
            "source": source_tag,
            "items": augments_payload.get("items", []),
        },
    )
    _atomic_write_json(
        BUNDLE_META_FILE,
        {
            "version": 1,
            "generatedAt": _now_iso(),
            "source": {
                "latestCsv": source_tag,
                "synergy": SYNERGY_SOURCE_NAME,
            },
        },
    )


def _write_indexes(
    alias_records: list[dict],
    alias_lookup: dict[str, str],
    detail_route: dict[str, str],
    id_name_map: dict[str, dict],
    augment_icon_lookup: dict[str, str],
) -> None:
    _atomic_write_json(CHAMPION_ALIAS_INDEX_FILE, alias_records)
    _atomic_write_json(CHAMPION_ALIAS_LOOKUP_FILE, alias_lookup)
    _atomic_write_json(CHAMPION_DETAIL_ROUTE_FILE, detail_route)
    _atomic_write_json(CHAMPION_ID_NAME_FILE, id_name_map)
    _atomic_write_json(AUGMENT_ICON_LOOKUP_FILE, augment_icon_lookup)


def write_precomputed_champion_list(champions: List[dict], source_tag: str) -> None:
    _atomic_write_json(
        CHAMPION_LIST_CACHE_FILE,
        {"meta": {"generated_at": _now_iso(), "source": source_tag}, "data": champions},
    )


def write_precomputed_hextech_map(hextech_by_hero: Dict[str, dict], source_tag: str) -> None:
    _atomic_write_json(
        HEXTECH_DETAIL_CACHE_FILE,
        {"meta": {"generated_at": _now_iso(), "source": source_tag}, "data": hextech_by_hero},
    )
    if os.path.isdir(HEXTECH_DETAIL_CACHE_DIR):
        shutil.rmtree(HEXTECH_DETAIL_CACHE_DIR, ignore_errors=True)


def build_processed_outputs_from_legacy(
    champions: List[dict],
    hextech_by_hero: Dict[str, dict],
    source_tag: str,
    catalog_lookup: Optional[Dict[str, Any]] = None,
) -> dict:
    core_data = _get_core_data()
    synergy_payload = _transform_synergy_payload(_load_synergy_source(), core_data)
    champions_payload = {"items": [_to_view_champion_item(item) for item in champions if isinstance(item, dict)]}
    summary_by_hero = {
        str(item.get("英雄名称", "")).strip(): item
        for item in champions
        if isinstance(item, dict) and str(item.get("英雄名称", "")).strip()
    }

    champion_detail_payload = {}
    for hero_name, legacy_payload in hextech_by_hero.items():
        if not isinstance(legacy_payload, dict):
            continue
        hero_name_str = str(hero_name).strip()
        hero_summary = summary_by_hero.get(hero_name_str)
        hero_id = ""
        for candidate_id, payload in core_data.items():
            if isinstance(payload, dict) and str(payload.get("name", "")).strip() == hero_name_str:
                hero_id = str(candidate_id).strip()
                break
        champion_detail_payload[hero_name_str] = _to_view_detail_payload(
            hero_name_str,
            hero_summary,
            legacy_payload,
            synergy_payload.get(hero_id, SYNERGY_EMPTY_PAYLOAD),
            core_data,
        )

    alias_records = _build_alias_records(core_data)
    alias_lookup = _build_alias_lookup(alias_records)
    detail_route = _build_detail_route_map(core_data)
    id_name_map = _build_id_name_map(core_data)
    augments_payload = _build_augments_payload(catalog_lookup or {})
    augment_icon_lookup = dict(augments_payload.get("iconLookup", {}))

    _write_processed_views(champions_payload, champion_detail_payload, synergy_payload, augments_payload, source_tag)
    _write_indexes(alias_records, alias_lookup, detail_route, id_name_map, augment_icon_lookup)

    legacy_hextech_payload = {
        hero_name: _to_legacy_detail_payload(detail_payload)
        for hero_name, detail_payload in champion_detail_payload.items()
    }
    write_precomputed_champion_list(champions, source_tag)
    write_precomputed_hextech_map(legacy_hextech_payload, source_tag)

    return {
        "champions": champions_payload,
        "details": champion_detail_payload,
        "synergy": synergy_payload,
        "augments": {"items": augments_payload.get("items", [])},
        "aliasRecords": alias_records,
        "aliasLookup": alias_lookup,
        "detailRoutes": detail_route,
        "idNameMap": id_name_map,
        "augmentIconLookup": augment_icon_lookup,
        "legacyHextech": legacy_hextech_payload,
    }


__all__ = [
    "CHAMPION_LIST_CACHE_FILE",
    "HEXTECH_DETAIL_CACHE_FILE",
    "CHAMPIONS_LIST_VIEW_FILE",
    "CHAMPION_DETAIL_VIEW_FILE",
    "SYNERGY_BY_CHAMPION_FILE",
    "AUGMENTS_LIST_VIEW_FILE",
    "CHAMPION_ALIAS_INDEX_FILE",
    "CHAMPION_ALIAS_LOOKUP_FILE",
    "CHAMPION_DETAIL_ROUTE_FILE",
    "CHAMPION_ID_NAME_FILE",
    "AUGMENT_ICON_LOOKUP_FILE",
    "BUNDLE_META_FILE",
    "build_processed_outputs_from_legacy",
    "write_precomputed_champion_list",
    "write_precomputed_hextech_map",
]

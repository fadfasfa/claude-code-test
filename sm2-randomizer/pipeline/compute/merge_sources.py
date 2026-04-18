from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import (
    APP_DATA_DIR,
    EXTRACTION_RULES_FILE,
    FIELD_SOURCE_POLICY_FILE,
    MANUAL_SOURCE_FILE,
    PIPELINE_STORE_RAW_EXCEL_DIR,
    PIPELINE_STORE_CATALOG_DIR,
    WIKI_RAW_FILE,
    ensure_directories,
    build_weapon_asset_path,
    load_weapon_image_name_overrides,
    read_json,
    relative_asset_path,
    resolve_weapon_asset_name,
    slugify,
    write_json,
)


def _index_by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_key = str(item.get(key, "")).strip()
        if item_key:
            result[item_key] = item
    return result


def _load_catalog_file(name: str, default: Any) -> Any:
    return read_json(PIPELINE_STORE_CATALOG_DIR / name, default)


def _load_excel_file(name: str, default: Any) -> Any:
    return read_json(PIPELINE_STORE_RAW_EXCEL_DIR / name, default)


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _clean_placeholder(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text == "/" else text


def _clean_optional_text(value: Any) -> str | None:
    text = _clean_placeholder(value)
    return text or None


def _clean_talent_name_zh(zh_name: Any, en_name: Any) -> str | None:
    zh_text = _clean_placeholder(zh_name)
    en_text = _clean_placeholder(en_name)
    if not zh_text:
        return None
    if zh_text == en_text and zh_text.isascii():
        return None
    return zh_text


def _resolve_negative_modifier_detail(item: dict[str, Any], strategy_terms: list[str]) -> str:
    explicit_detail = _clean_placeholder(item.get("detail"))
    if explicit_detail:
        return explicit_detail
    aliases = [str(alias).strip() for alias in _safe_list(item.get("aliases")) if str(alias).strip()]
    matched = next(
        (
            alias
            for alias in sorted(aliases, key=len, reverse=True)
            if alias in strategy_terms
        ),
        "",
    )
    return matched or _clean_placeholder(item.get("label")) or "暂无详细信息"


def _negative_title_part(value: Any) -> str:
    return _clean_placeholder(value).split("：")[0].strip()


def _normalize_manual_negative_aliases(item: dict[str, Any]) -> list[str]:
    label = _clean_placeholder(item.get("label"))
    derived = {
        _clean_placeholder(item.get("name")),
        label,
        _negative_title_part(label),
        _clean_placeholder(item.get("detail")),
    }
    seen: set[str] = set()
    aliases: list[str] = []
    for raw in _safe_list(item.get("aliases")):
        value = str(raw).strip()
        if not value or value in derived or value in seen:
            continue
        seen.add(value)
        aliases.append(value)
    return aliases


def _load_positive_modifier_pool(manual: dict[str, Any], runtime_meta_fallback: dict[str, Any]) -> list[dict[str, Any]]:
    manual_pool = _safe_list(manual.get("positive_modifier_pool"))
    if manual_pool:
        source = manual_pool
    else:
        runtime_pool = _safe_list(runtime_meta_fallback.get("positive_modifier_pool"))
        if runtime_pool:
            source = runtime_pool
        else:
            strategy_pools = _safe_dict(runtime_meta_fallback.get("strategy_modifier_pools"))
            source = _safe_list(_safe_dict(strategy_pools).get("positive"))

    pool: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        key = _clean_placeholder(item.get("key"))
        name = _clean_placeholder(item.get("name"))
        detail = _clean_placeholder(item.get("detail"))
        if not key or not name or not detail:
            continue
        pool.append(
            {
                "key": key,
                "name": name,
                "detail": detail,
                "type": "positive",
            }
        )
    return pool


def _build_weapon_lookup(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for candidate in (
            item.get("slug"),
            item.get("name"),
            item.get("original_name"),
            item.get("excel_name"),
        ):
            key = str(candidate or "").strip()
            if key:
                lookup[key] = item
    return lookup


def _slot_type_from_source_sheet(source_sheet: str) -> str:
    normalized = str(source_sheet or "").strip()
    if normalized == "主武器":
        return "primary"
    if normalized == "副武器":
        return "secondary"
    if normalized == "主页-近战武器":
        return "melee"
    return ""


def _append_unique_loadout_entry(entries: list[dict[str, str]], entry: dict[str, str]) -> list[dict[str, str]]:
    target_slug = str(entry.get("slug", "")).strip()
    if not target_slug:
        return entries
    if any(str(item.get("slug", "")).strip() == target_slug for item in entries if isinstance(item, dict)):
        return entries
    return [*entries, entry]


CLASS_LOADOUT_OVERRIDES: dict[str, dict[str, list[str]]] = {
    "sniper": {
        "primary": ["marksman-bolt-carbine"],
    },
}
EXCLUDED_HERO_WEAPON_SLUGS = {"twin-linked-melta-gun"}


def _class_slug_to_available_name(class_slug: str) -> str:
    mapping = {
        "assault": "Assault",
        "bulwark": "Bulwark",
        "heavy": "Heavy",
        "sniper": "Sniper",
        "tactical": "Tactical",
        "techmarine": "Techmarine",
        "vanguard": "Vanguard",
    }
    return mapping.get(str(class_slug or "").strip(), str(class_slug or "").strip().title())


def _to_class_loadout(entries: list[dict[str, Any]], weapon_lookup: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            item = {"name": str(item or "").strip(), "original_name": str(item or "").strip()}
        name = _first_text(item.get("name"), item.get("original_name"))
        slug = _first_text(item.get("slug"))
        matched: dict[str, Any] = {}
        if slug and slug in weapon_lookup:
            candidate = weapon_lookup.get(slug, {})
            if isinstance(candidate, dict):
                matched = candidate
        if not slug and name:
            candidate = weapon_lookup.get(name) or weapon_lookup.get(name.lower())
            if isinstance(candidate, dict):
                matched = candidate
            slug = _first_text(matched.get("slug") if matched else "")
        if not slug:
            slug = slugify(name)
        if not matched and slug:
            candidate = weapon_lookup.get(slug, {})
            if isinstance(candidate, dict):
                matched = candidate
        if not slug or slug in seen:
            continue
        seen.add(slug)
        result.append(
            {
                "slug": slug,
                "name": _first_text(matched.get("name"), item.get("name"), item.get("original_name"), name),
                "original_name": _first_text(matched.get("original_name"), item.get("original_name"), item.get("name")),
            }
        )
    return result


def _build_negative_modifier_rules(manual: dict[str, Any], strategy_terms: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pool_raw = _safe_list(manual.get("negative_modifier_pool"))
    rules_raw = _safe_dict(manual.get("negative_modifier_rules"))

    pool: list[dict[str, Any]] = []
    title_aliases: dict[str, str] = {}
    for item in pool_raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        if not key or not label:
            continue
        name = str(item.get("name", "")).strip() or _negative_title_part(label)
        aliases = _normalize_manual_negative_aliases(item)
        title_aliases[label] = key
        title_aliases[_negative_title_part(label)] = key
        title_aliases[name] = key
        for alias in aliases:
            title_aliases[alias] = key
        pool.append(
            {
                "key": key,
                "label": label,
                "name": name,
                "detail": _resolve_negative_modifier_detail(item, strategy_terms),
                "risk_level": str(item.get("risk_level", "")).strip() or "advisory",
                "core_tags": [str(tag).strip() for tag in _safe_list(item.get("core_tags")) if str(tag).strip()],
                "aliases": aliases,
                "type": "negative",
            }
        )

    exact_conflicts: list[dict[str, Any]] = []
    for rule in _safe_list(rules_raw.get("exact_conflicts")):
        if not isinstance(rule, dict):
            continue
        keys = [str(key).strip() for key in _safe_list(rule.get("keys")) if str(key).strip()]
        if len(keys) < 2:
            continue
        exact_conflicts.append(
            {
                "keys": keys,
                "risk_level": str(rule.get("risk_level", "")).strip() or "high_risk",
                "message": str(rule.get("message", "")).strip(),
            }
        )

    quota_limits = {
        str(tag).strip(): int(limit)
        for tag, limit in _safe_dict(rules_raw.get("quota_limits")).items()
        if str(tag).strip()
    }

    return pool, {
        "exact_conflicts": exact_conflicts,
        "quota_limits": quota_limits,
        "title_aliases": title_aliases,
    }


def merge_sources() -> dict[str, Any]:
    ensure_directories()
    manual = read_json(
        MANUAL_SOURCE_FILE,
        {
            "classes": [],
            "weapons": [],
            "strategy_terms": [],
            "negative_modifier_pool": [],
            "negative_modifier_rules": {},
        },
    )
    wiki_raw = read_json(WIKI_RAW_FILE, {"classes": [], "weapons": [], "talents": [], "meta": {}})
    class_manifest = _load_catalog_file("职业图片清单.json", {"classes": []})
    weapon_manifest = _load_catalog_file("武器图标清单.json", {"weapons": []})
    class_weapon_map = _load_catalog_file("职业武器映射.json", {"class_weapon_map": {}})
    talent_manifest = _load_catalog_file("按职业分组天赋图标.json", {"classes": []})
    excel_weapon_image_map = _load_excel_file("武器图片映射.json", {"items": []})
    excel_strategy_terms = _load_excel_file("策略词条清单.json", {"items": []})
    runtime_meta_fallback = read_json(APP_DATA_DIR / "meta.json", {})
    weapon_name_overrides = load_weapon_image_name_overrides()
    field_policy = read_json(FIELD_SOURCE_POLICY_FILE, {})
    extraction_rules = read_json(EXTRACTION_RULES_FILE, {})

    manual_class_by_slug = _index_by(_safe_list(manual.get("classes")), "slug")
    manual_weapon_by_slug = _index_by(_safe_list(manual.get("weapons")), "slug")
    wiki_class_by_slug = _index_by(_safe_list(wiki_raw.get("classes")), "slug_candidate")
    wiki_weapon_by_slug = _index_by(_safe_list(wiki_raw.get("weapons")), "slug_candidate")
    class_manifest_by_slug = _index_by(_safe_list(class_manifest.get("classes")), "slug")
    weapon_manifest_by_slug = _index_by(_safe_list(weapon_manifest.get("weapons")), "slug")
    excel_weapon_image_by_slug = _index_by(_safe_list(excel_weapon_image_map.get("items")), "slug")
    talent_manifest_by_class = _index_by(_safe_list(talent_manifest.get("classes")), "class_slug")
    wiki_talent_by_class = _index_by(_safe_list(wiki_raw.get("talents")), "class_slug_candidate")

    strategy_terms = (
        _safe_list(manual.get("strategy_terms"))
        or _safe_list(excel_strategy_terms.get("items"))
        or _safe_list(runtime_meta_fallback.get("strategy_terms"))
    )
    positive_modifier_pool = _load_positive_modifier_pool(manual, runtime_meta_fallback)
    negative_modifier_pool, negative_modifier_rules = _build_negative_modifier_rules(manual, strategy_terms)

    all_weapon_slugs = sorted(
        {
            *manual_weapon_by_slug.keys(),
            *wiki_weapon_by_slug.keys(),
            *weapon_manifest_by_slug.keys(),
            *excel_weapon_image_by_slug.keys(),
        }
        - EXCLUDED_HERO_WEAPON_SLUGS
    )

    merged_weapons: list[dict[str, Any]] = []
    weapon_lookup: dict[str, dict[str, Any]] = {}
    for slug in all_weapon_slugs:
        manual_weapon = manual_weapon_by_slug.get(slug, {})
        wiki_weapon = wiki_weapon_by_slug.get(slug, {})
        weapon_image = excel_weapon_image_by_slug.get(slug, {})
        manifest_weapon = weapon_manifest_by_slug.get(slug, {})
        excel_slot_type = _slot_type_from_source_sheet(str(weapon_image.get("source_sheet", "")).strip())
        slot_type = _first_text(excel_slot_type, manual_weapon.get("slot_type"), wiki_weapon.get("slot_type"))
        fallback_display_name = _first_text(
            manual_weapon.get("name"),
            wiki_weapon.get("name"),
        )
        display_name = resolve_weapon_asset_name(
            slug=slug,
            excel_name=str(weapon_image.get("excel_name", "")).strip(),
            default_name=fallback_display_name,
            overrides=weapon_name_overrides,
        )
        fallback_asset_name = resolve_weapon_asset_name(
            slug=slug,
            excel_name=str(weapon_image.get("excel_name", "")).strip(),
            default_name=fallback_display_name,
            overrides=weapon_name_overrides,
        )
        fallback_asset_path = build_weapon_asset_path(
            slot_type=slot_type or _slot_type_from_source_sheet(str(weapon_image.get("source_sheet", "")).strip()),
            source_sheet=str(weapon_image.get("source_sheet", "")).strip(),
            asset_name=fallback_asset_name,
        )

        available_classes = _safe_list(wiki_weapon.get("allowed_classes"))

        merged_weapon = {
            "slug": slug,
            "name": display_name,
            "original_name": _first_text(manual_weapon.get("original_name"), wiki_weapon.get("name")),
            "slot_type": slot_type,
            "available_classes": available_classes,
            "mode_restriction": _first_text(manual_weapon.get("mode_restriction")),
            "description_short": _first_text(manual_weapon.get("description_short"), wiki_weapon.get("description_short")),
            "image": {
                "asset_path": relative_asset_path(_first_text(weapon_image.get("asset_path"), fallback_asset_path, manifest_weapon.get("asset_path"))),
                "excel_name": _first_text(weapon_image.get("excel_name")),
                "category": _first_text(weapon_image.get("category")),
                "directory_label": _first_text(weapon_image.get("directory_label")),
                "source": "excel" if weapon_image else "catalog",
            },
            "notes": _safe_list(manual_weapon.get("notes")) or _safe_list(wiki_weapon.get("notes")),
            "source_meta": {
                "structure": "wiki",
                "weapon_image": "excel" if weapon_image else "catalog",
                "available_classes": "wiki" if _safe_list(wiki_weapon.get("allowed_classes")) else "catalog_fallback",
                "naming": "override" if slug in weapon_name_overrides else ("excel" if weapon_image.get("excel_name") else ("manual" if manual_weapon.get("name") else "wiki")),
            },
        }
        merged_weapons.append(merged_weapon)
        weapon_lookup[slug] = merged_weapon
        for alias in filter(None, [merged_weapon["name"], merged_weapon["original_name"], weapon_image.get("excel_name")]):
            weapon_lookup[str(alias)] = merged_weapon

    all_class_slugs = sorted(
        {
            *manual_class_by_slug.keys(),
            *wiki_class_by_slug.keys(),
            *class_manifest_by_slug.keys(),
            *talent_manifest_by_class.keys(),
        }
    )

    merged_classes: list[dict[str, Any]] = []
    for slug in all_class_slugs:
        manual_class = manual_class_by_slug.get(slug, {})
        wiki_class = wiki_class_by_slug.get(slug, {})
        manifest_class = class_manifest_by_slug.get(slug, {})
        talent_class = talent_manifest_by_class.get(slug, {})
        wiki_talent_class = wiki_talent_by_class.get(slug, {})

        display_name = _first_text(manual_class.get("name"), wiki_class.get("name"))
        class_weapon_entry = class_weapon_map.get("class_weapon_map", {}).get(slug, {})
        wiki_loadouts = wiki_class.get("weapons") if isinstance(wiki_class.get("weapons"), dict) else {}
        loadouts = class_weapon_entry if isinstance(class_weapon_entry, dict) and class_weapon_entry else wiki_loadouts

        normalized_loadouts = {
            "primary": _to_class_loadout(_safe_list(loadouts.get("primary")), weapon_lookup),
            "secondary": _to_class_loadout(_safe_list(loadouts.get("secondary")), weapon_lookup),
            "melee": _to_class_loadout(_safe_list(loadouts.get("melee")), weapon_lookup),
        }
        loadout_overrides = CLASS_LOADOUT_OVERRIDES.get(slug, {})
        for slot, override_slugs in loadout_overrides.items():
            for override_slug in override_slugs:
                override_weapon = weapon_lookup.get(override_slug)
                if not isinstance(override_weapon, dict):
                    continue
                normalized_loadouts[slot] = _append_unique_loadout_entry(
                    normalized_loadouts[slot],
                    {
                        "slug": override_slug,
                        "name": str(override_weapon.get("name", "")).strip(),
                        "original_name": _first_text(override_weapon.get("original_name"), str(override_weapon.get("name", "")).strip()),
                    },
                )

        talents = _safe_list(talent_class.get("talents"))
        if not talents and isinstance(wiki_talent_class.get("talents"), list):
            talents = _safe_list(wiki_talent_class.get("talents"))

        merged_classes.append(
            {
                "slug": slug,
                "name": display_name,
                "role": _first_text(manual_class.get("role"), wiki_class.get("class_role_text")),
                "tagline": _first_text(manual_class.get("tagline"), wiki_class.get("class_summary_plain")),
                "class_ability": _first_text(manual_class.get("class_ability"), wiki_class.get("class_ability")),
                "summary": _first_text(wiki_class.get("class_summary_plain"), manual_class.get("tagline")),
                "images": {
                    "asset_dir": relative_asset_path(manifest_class.get("asset_dir")),
                    "local_images": _safe_list(manifest_class.get("local_images")),
                    "default_image": _first_text(manifest_class.get("default_image")),
                },
                "loadouts": normalized_loadouts,
                "talents": talents,
                "strategy_terms": _safe_list(manual_class.get("strategy_terms")),
                "notes": _safe_list(manual_class.get("notes")) or _safe_list(wiki_class.get("notes")),
                "source_meta": {
                    "structure": "wiki",
                    "display_name": "manual" if manual_class.get("name") else "wiki",
                    "weapon_image": "excel",
                    "loadouts": "wiki" if loadouts else "missing",
                    "talents": "wiki_preferred" if talents else "missing",
                },
            }
        )

    weapon_to_classes: dict[str, list[str]] = {}
    for class_entry in merged_classes:
        class_slug = str(class_entry.get("slug", "")).strip()
        available_name = _class_slug_to_available_name(class_slug)
        loadouts = class_entry.get("loadouts", {})
        if not isinstance(loadouts, dict):
            continue
        for slot_items in loadouts.values():
            if not isinstance(slot_items, list):
                continue
            for item in slot_items:
                if not isinstance(item, dict):
                    continue
                weapon_slug = str(item.get("slug", "")).strip()
                if not weapon_slug:
                    continue
                names = weapon_to_classes.setdefault(weapon_slug, [])
                if available_name not in names:
                    names.append(available_name)

    for weapon_entry in merged_weapons:
        weapon_slug = str(weapon_entry.get("slug", "")).strip()
        weapon_entry["available_classes"] = weapon_to_classes.get(weapon_slug, [])
        weapon_entry["source_meta"]["available_classes"] = "class_loadout_projection" if weapon_entry["available_classes"] else weapon_entry["source_meta"].get("available_classes")

    coverage = {
        "classes": len(merged_classes),
        "weapons": len(merged_weapons),
        "talent_class_count": len([item for item in merged_classes if item.get("talents")]),
        "class_images_with_local_assets": len([item for item in merged_classes if item["images"]["local_images"]]),
        "weapon_images_from_excel": len([item for item in merged_weapons if item["image"]["asset_path"]]),
    }

    merged = {
        "meta": {
            "generated_from": "merge_sources.py",
            "generated_at": datetime.now(UTC).isoformat(),
            "source_mode": "hybrid_now_wiki_ready",
            "version_anchor": _first_text(wiki_raw.get("meta", {}).get("version_anchor"), extraction_rules.get("version_anchor")),
            "positive_modifier_pool": positive_modifier_pool,
            "strategy_terms": strategy_terms,
            "negative_modifier_pool": negative_modifier_pool,
            "negative_modifier_rules": negative_modifier_rules,
            "source_coverage": coverage,
            "field_source_policy": field_policy,
        },
        "classes": merged_classes,
        "weapons": merged_weapons,
        "talents": [
            {
                "class_slug": item["slug"],
                "class_name": item["name"],
                "talents": item["talents"],
            }
            for item in merged_classes
        ],
    }
    return merged


if __name__ == "__main__":
    write_json(PROJECT_ROOT / "pipeline" / "store" / "reports" / "source" / "merge_preview.json", merge_sources())

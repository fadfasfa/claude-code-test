from __future__ import annotations

"""构建运行期候选数据。

将 merge 后的多源结果收敛为 app/data 契约下的最小三文件，并清理历史遗留运行文件。
"""

import argparse
from pathlib import Path
import re
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.common import APP_DATA_DIR, MANUAL_SOURCE_FILE, PIPELINE_TMP_PUBLISH_DIR, TALENT_DESCRIPTION_ZH_FILE, TALENT_NAME_ZH_FILE, ensure_directories, read_json, write_json
from pipeline.compute.merge_sources import merge_sources

RUNTIME_TALENT_GRID_SPEC = {
    "cols": 8,
    "rows": 3,
    "label_format": "col/row",
    "order": "column-major",
}
MAX_NEGATIVE_LABEL_LENGTH = 14


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


def _load_talent_name_zh_map() -> dict[str, str]:
    payload = read_json(TALENT_NAME_ZH_FILE, {"items": {}})
    items = payload.get("items", {}) if isinstance(payload, dict) else {}
    if not isinstance(items, dict):
        return {}
    return {
        str(slug).strip(): str(name).strip()
        for slug, name in items.items()
        if str(slug).strip() and str(name).strip()
    }


def _load_talent_description_zh_map() -> dict[tuple[str, str], str]:
    payload = read_json(TALENT_DESCRIPTION_ZH_FILE, {"classes": {}})
    classes = payload.get("classes", {}) if isinstance(payload, dict) else {}
    result: dict[tuple[str, str], str] = {}
    if not isinstance(classes, dict):
        return result
    for class_slug, class_payload in classes.items():
        if not isinstance(class_payload, dict):
            continue
        items = class_payload.get("items", {})
        if not isinstance(items, dict):
            continue
        clean_class_slug = str(class_slug).strip()
        if not clean_class_slug:
            continue
        for talent_slug, description in items.items():
            clean_talent_slug = str(talent_slug).strip()
            clean_description = _clean_placeholder(description)
            if clean_talent_slug and clean_description:
                result[(clean_class_slug, clean_talent_slug)] = clean_description
    return result


def _resolve_class_image_path(images: dict[str, Any]) -> str | None:
    if not isinstance(images, dict):
        return None
    asset_dir = _clean_placeholder(images.get("asset_dir"))
    if not asset_dir:
        return None
    selected = _clean_placeholder(images.get("default_image"))
    if not selected:
        local_images = images.get("local_images", [])
        if isinstance(local_images, list):
            selected = next((str(item).strip() for item in local_images if str(item).strip()), "")
    if not selected:
        return None
    return f"{asset_dir}/{selected}"


def _sanitize_weapon(item: Any, weapon_lookup: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    slug = _clean_placeholder(item.get("slug"))
    if not slug:
        return None
    source = weapon_lookup.get(slug, {})
    image = source.get("image", {}) if isinstance(source, dict) else {}
    return {
        "slug": slug,
        "name": _clean_placeholder(item.get("name")) or _clean_placeholder(source.get("name")),
        "slot_type": _clean_optional_text(source.get("slot_type")),
        "image_path": _clean_optional_text(image.get("asset_path")) if isinstance(image, dict) else None,
    }


def _build_loadout_pools(entry: dict[str, Any], weapon_lookup: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    source_pools = entry.get("loadouts", {}) if isinstance(entry.get("loadouts"), dict) else {}
    pools: dict[str, list[dict[str, Any]]] = {}
    for slot in ("primary", "secondary", "melee"):
        clean_items = []
        for item in source_pools.get(slot, []):
            clean_item = _sanitize_weapon(item, weapon_lookup)
            if clean_item:
                clean_items.append(clean_item)
        pools[slot] = clean_items

    return pools


def _manual_skill_description_set() -> set[str]:
    manual = read_json(MANUAL_SOURCE_FILE, {"class_skill_entries": {}})
    entries = manual.get("class_skill_entries", {}) if isinstance(manual, dict) else {}
    if not isinstance(entries, dict):
        return set()
    values: set[str] = set()
    for class_items in entries.values():
        if not isinstance(class_items, list):
            continue
        for skill in class_items:
            if not isinstance(skill, dict):
                continue
            description = _clean_placeholder(skill.get("description"))
            if description:
                values.add(description)
    return values


def _skill_description_set(classes: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        skills = entry.get("skills", [])
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if not isinstance(skill, dict):
                continue
            description = _clean_placeholder(skill.get("description"))
            if description:
                values.add(description)
    return values


def _negative_title_part(value: Any) -> str:
    return _clean_placeholder(value).split("：")[0].strip()


def _normalize_negative_aliases(item: dict[str, Any], short_label: str) -> list[str]:
    seen: set[str] = set()
    aliases: list[str] = []
    derived = {
        _clean_placeholder(item.get("name")),
        _clean_placeholder(item.get("label")),
        short_label,
        _negative_title_part(item.get("label")),
        _negative_title_part(short_label),
        _clean_placeholder(item.get("detail")),
    }
    source_aliases = item.get("aliases", []) if isinstance(item.get("aliases"), list) else []
    for raw in source_aliases:
        value = _clean_placeholder(raw)
        if not value or value in seen or value in derived:
            continue
        seen.add(value)
        aliases.append(value)
    return aliases


def _shorten_negative_label(value: Any, max_len: int = MAX_NEGATIVE_LABEL_LENGTH) -> str:
    label = _clean_placeholder(value)
    if not label:
        return ""
    if len(label) <= max_len:
        return label
    title, sep, detail = label.partition("：")
    title = title.strip()
    detail = detail.strip()
    if not sep:
        return label[:max_len]
    if len(title) >= max_len:
        return title[:max_len]
    remaining = max_len - len(title) - 1
    compact = re.sub(r"[，。、“”\"'（）()\\s]+", "", detail)
    compact = compact.replace("显著", "").replace("进一步", "").replace("持续", "").replace("状态", "")
    if not compact:
        compact = detail
    short_detail = compact[:remaining] if remaining > 0 else ""
    return f"{title}：{short_detail}" if short_detail else title


def _sanitize_meta(meta: dict[str, Any]) -> dict[str, Any]:
    positive = meta.get("positive_modifier_pool", []) if isinstance(meta.get("positive_modifier_pool"), list) else []
    negative_input = meta.get("negative_modifier_pool", []) if isinstance(meta.get("negative_modifier_pool"), list) else []
    rules_input = meta.get("negative_modifier_rules", {}) if isinstance(meta.get("negative_modifier_rules"), dict) else {}
    negative_pool: list[dict[str, Any]] = []
    title_aliases = rules_input.get("title_aliases", {}) if isinstance(rules_input.get("title_aliases"), dict) else {}
    normalized_aliases = {str(k).strip(): str(v).strip() for k, v in title_aliases.items() if str(k).strip() and str(v).strip()}
    for item in negative_input:
        if not isinstance(item, dict):
            continue
        raw_label = _clean_placeholder(item.get("label"))
        short_label = _shorten_negative_label(item.get("label"))
        if not short_label:
            continue
        key = _clean_placeholder(item.get("key"))
        name = _clean_placeholder(item.get("name"))
        aliases = _normalize_negative_aliases(item, short_label)
        normalized = {**item, "label": short_label, "aliases": aliases}
        negative_pool.append(normalized)
        if key:
            candidates = [
                short_label,
                _negative_title_part(short_label),
                raw_label,
                _negative_title_part(raw_label),
                name,
            ]
            for alias in aliases:
                candidates.append(alias)
            for candidate in candidates:
                candidate = _clean_placeholder(candidate)
                if candidate:
                    normalized_aliases[candidate] = key
    negative_rules = {
        **rules_input,
        "title_aliases": normalized_aliases,
    }
    return {
        "build": {
            "version": _clean_optional_text(meta.get("version_anchor")),
            "generated_at": _clean_optional_text(meta.get("generated_at")),
            "source_mode": _clean_optional_text(meta.get("source_mode")) or "hybrid_now_wiki_ready",
            "source_coverage": meta.get("source_coverage", {}) if isinstance(meta.get("source_coverage"), dict) else {},
        },
        "positive_modifier_pool": positive,
        "negative_modifier_pool": negative_pool,
        "negative_modifier_rules": negative_rules,
    }


def _sanitize_talent_node(
    item: Any,
    class_slug: str,
    blocked_descriptions: set[str],
    talent_name_zh_map: dict[str, str],
    talent_description_zh_map: dict[tuple[str, str], str],
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    talent_slug = _clean_placeholder(item.get("talent_slug"))
    if not talent_slug:
        return None
    try:
        col = int(item.get("col"))
        row = int(item.get("row"))
    except (TypeError, ValueError):
        return None
    mapped_zh_name = talent_name_zh_map.get(talent_slug, "")
    talent_name_en = _clean_placeholder(item.get("talent_name_en")) or _clean_placeholder(item.get("talent_name"))
    talent_name_zh = _clean_talent_name_zh(
        mapped_zh_name or item.get("talent_name_zh"),
        talent_name_en,
    )
    description = _clean_optional_text(item.get("description"))
    if description and description in blocked_descriptions:
        description = None
    description = talent_description_zh_map.get((class_slug, talent_slug), description)
    icon_path = _clean_optional_text(item.get("icon_path"))
    if icon_path:
        icon_path = icon_path.replace("\\", "/")
    return {
        "talent_slug": talent_slug,
        "col": col,
        "row": row,
        "grid_label": _clean_placeholder(item.get("grid_label")) or f"{col}/{row}",
        "talent_name": talent_name_zh or talent_name_en or talent_slug,
        "talent_name_en": talent_name_en or None,
        "talent_name_zh": talent_name_zh,
        "icon_path": icon_path,
        "description": description,
    }


def build_runtime_payloads(merged: dict[str, Any]) -> dict[str, dict[str, Any]]:
    classes = merged.get("classes", [])
    weapons = merged.get("weapons", [])
    talents = merged.get("talents", [])
    meta = merged.get("meta", {})

    weapon_lookup = {
        str(item.get("slug", "")).strip(): item
        for item in weapons
        if isinstance(item, dict) and str(item.get("slug", "")).strip()
    }

    blocked_descriptions = _manual_skill_description_set() | _skill_description_set(classes)
    talent_name_zh_map = _load_talent_name_zh_map()
    talent_description_zh_map = _load_talent_description_zh_map()

    class_payload = {
        "classes": [
            {
                "slug": _clean_placeholder(entry.get("slug")),
                "name": _clean_placeholder(entry.get("name")),
                "role": _clean_optional_text(entry.get("role")),
                "tagline": _clean_optional_text(entry.get("tagline")),
                "summary": _clean_optional_text(entry.get("summary")),
                "class_ability": _clean_optional_text(entry.get("class_ability")),
                "image_path": _resolve_class_image_path(entry.get("images", {})),
                "loadout_pools": _build_loadout_pools(entry, weapon_lookup),
            }
            for entry in classes
            if isinstance(entry, dict) and _clean_placeholder(entry.get("slug")) and _clean_placeholder(entry.get("name"))
        ],
    }

    talent_payload = {
        "classes": [
            {
                "class_slug": _clean_placeholder(entry.get("class_slug")),
                "class_name": _clean_placeholder(entry.get("class_name")),
                "grid_spec": entry.get("grid_spec", {}) if isinstance(entry.get("grid_spec"), dict) and entry.get("grid_spec") else dict(RUNTIME_TALENT_GRID_SPEC),
                "nodes": [
                    item
                    for item in (
                        _sanitize_talent_node(
                            node,
                            _clean_placeholder(entry.get("class_slug")),
                            blocked_descriptions,
                            talent_name_zh_map,
                            talent_description_zh_map,
                        )
                        for node in entry.get("talents", [])
                    )
                    if item
                ],
            }
            for entry in talents
            if isinstance(entry, dict) and _clean_placeholder(entry.get("class_slug"))
        ],
    }

    meta_payload = _sanitize_meta(meta)

    return {
        "classes.json": class_payload,
        "talents.json": talent_payload,
        "meta.json": meta_payload,
    }


def write_runtime_payloads(output_dir: Path, payloads: dict[str, dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in payloads.items():
        write_json(output_dir / filename, payload)
    for legacy_name in ("loadouts.json", "talent_details.json"):
        (output_dir / legacy_name).unlink(missing_ok=True)


def build_runtime_data(output_dir: Path | None = None) -> dict:
    ensure_directories()
    merged = merge_sources()
    payloads = build_runtime_payloads(merged)
    target_dir = output_dir or PIPELINE_TMP_PUBLISH_DIR
    write_runtime_payloads(target_dir, payloads)

    return {
        "classes": len(payloads["classes.json"]["classes"]),
        "weapons": len(merged.get("weapons", [])),
        "talent_classes": len(payloads["talents.json"]["classes"]),
        "output_dir": target_dir.as_posix(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build runtime candidate data using the app/data contract.")
    parser.add_argument("--output-dir", default=str(PIPELINE_TMP_PUBLISH_DIR), help="Directory to write runtime contract JSON files into.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    build_runtime_data(Path(args.output_dir).resolve())

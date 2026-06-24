"""清洗并合并 ARAMMayhem combo 到前端协同数据。

本脚本保持 ApexLoL raw synergy 作为基底，只把 ARAMMayhem 中 Apex 没有的
「英雄 + 官方中文 augment 组合」增量追加到 ``synergy_items``。Mayhem 原始
文件为空或全部被拒绝时不会覆盖 ``Champion_Synergy_Cleaned.json``。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.catalog.runtime_store import build_raw_synergy_data_path, build_synergy_cleaned_data_path
from hextech.scraping.icon_resolver import normalize_augment_name
from hextech.support.atomic_io import atomic_write_json

DEFAULT_MAYHEM_RAW = RUN_DIR / "data" / "raw" / "mayhem_combos.raw.json"
DEFAULT_AUGMENT_MANIFEST = RUN_DIR / "data" / "static" / "Augment_Icon_Manifest.json"
DEFAULT_CORE_DATA = RUN_DIR / "data" / "static" / "Champion_Core_Data.json"

RETIRED_MARKERS = (
    "Retired in live Mayhem",
    "已移除",
)
LEGACY_DEPENDENCY_PATTERNS = (
    re.compile(r"\btraits?\b", re.IGNORECASE),
    re.compile(r"\baugment\s+sets?\b", re.IGNORECASE),
    re.compile(r"\bold\s+sets?\b", re.IGNORECASE),
    re.compile(r"\bsets?\s*\d+\b", re.IGNORECASE),
    re.compile(r"旧\s*(?:Trait|Set|强化|特质|羁绊)", re.IGNORECASE),
    re.compile(r"(?:旧|历史|已移除).{0,8}(?:特质|羁绊|套装|强化组)", re.IGNORECASE),
)
SPLIT_AUGMENT_RE = re.compile(r"\s*(?:[，,、/+]|&|\band\b)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class AugmentRecord:
    name: str
    tier: str


@dataclass(frozen=True)
class ChampionRecord:
    id: str
    name: str
    title: str
    en_name: str
    aliases: list[str]


def _configure_stdio() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _load_json(path: str | os.PathLike[str], expected_type: type) -> Any:
    target = Path(path)
    with target.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, expected_type):
        raise ValueError(f"JSON schema mismatch: {target} expected={expected_type.__name__}")
    return payload


def _lookup_key(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    basename = os.path.basename(text)
    stem = os.path.splitext(basename)[0] if "." in basename else basename
    stem = re.sub(r"(?:_?mayhem_augment|_?small|_?large)$", "", stem, flags=re.IGNORECASE)
    return normalize_augment_name(stem)


def _add_augment_lookup(lookup: dict[str, AugmentRecord], key: Any, record: AugmentRecord) -> None:
    for candidate in {_text(key), _lookup_key(key)}:
        if candidate:
            lookup.setdefault(candidate, record)


def build_augment_lookup(manifest: list[dict[str, Any]]) -> dict[str, AugmentRecord]:
    """构建官方 augment 名称闭集，兼容英文 id、图标名和 CDragon 字段。"""
    lookup: dict[str, AugmentRecord] = {}
    for item in manifest:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        record = AugmentRecord(name=name, tier=_text(item.get("tier")) or "未知")
        _add_augment_lookup(lookup, name, record)
        _add_augment_lookup(lookup, item.get("augment_name_id"), record)
        _add_augment_lookup(lookup, item.get("filename"), record)
        _add_augment_lookup(lookup, item.get("source_icon_path"), record)
        _add_augment_lookup(lookup, item.get("source_icon_url"), record)
        cdragon_id = item.get("cdragon_id")
        if cdragon_id not in (None, ""):
            _add_augment_lookup(lookup, str(cdragon_id), record)
    return lookup


def _champion_key(value: Any) -> str:
    return re.sub(r"[\s'._-]+", "", _text(value).lower())


def build_champion_lookup(core_data: dict[str, Any]) -> tuple[dict[str, ChampionRecord], dict[str, ChampionRecord]]:
    by_id: dict[str, ChampionRecord] = {}
    lookup: dict[str, ChampionRecord] = {}
    for champ_id, raw_info in core_data.items():
        info = raw_info if isinstance(raw_info, dict) else {}
        aliases = info.get("aliases") if isinstance(info.get("aliases"), list) else []
        record = ChampionRecord(
            id=str(champ_id),
            name=_text(info.get("name")),
            title=_text(info.get("title")),
            en_name=_text(info.get("en_name")),
            aliases=[_text(item) for item in aliases if _text(item)],
        )
        by_id[record.id] = record
        for candidate in (record.id, record.name, record.title, record.en_name, *record.aliases):
            key = _champion_key(candidate)
            if key:
                lookup.setdefault(key, record)
    return lookup, by_id


def _split_augment_names(value: Any) -> list[str]:
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("name") or item.get("displayName") or item.get("id")
            result.extend(_split_augment_names(item))
        return list(dict.fromkeys(result))
    text = _text(value)
    if not text:
        return []
    parts = [part for part in SPLIT_AUGMENT_RE.split(text) if part]
    return list(dict.fromkeys(parts or [text]))


def _raw_augment_values(raw_item: dict[str, Any]) -> list[str]:
    values = _split_augment_names(
        raw_item.get("augment_names")
        or raw_item.get("augmentNames")
        or raw_item.get("combo_augment_names")
        or raw_item.get("augment_name")
        or raw_item.get("name")
    )
    augment_id = _text(raw_item.get("augment_id") or raw_item.get("augmentId"))
    if augment_id:
        values.append(augment_id)
    return list(dict.fromkeys(item for item in values if item))


def resolve_augment_names(raw_item: dict[str, Any], lookup: dict[str, AugmentRecord]) -> tuple[list[str], list[str], list[str]]:
    names: list[str] = []
    tiers: list[str] = []
    unknown: list[str] = []
    raw_values = _raw_augment_values(raw_item)
    for raw_name in raw_values:
        record = lookup.get(raw_name) or lookup.get(_lookup_key(raw_name))
        if not record:
            unknown.append(raw_name)
            continue
        if record.name not in names:
            names.append(record.name)
            tiers.append(record.tier)

    if names:
        return names, tiers, []
    return [], [], unknown or raw_values


def _combo_key(champion_id: str, augment_names: Iterable[str]) -> str:
    names = sorted(_text(name) for name in augment_names if _text(name))
    return json.dumps([str(champion_id), names], ensure_ascii=False, separators=(",", ":"))


def _apex_item_names(item: Any) -> list[str]:
    if isinstance(item, dict):
        return _split_augment_names(item.get("augment_names") or item.get("name") or item.get("augments"))
    if isinstance(item, str):
        return _split_augment_names(item.split("|", 1)[0])
    return []


def build_apex_key_set(apex_payload: dict[str, Any], augment_lookup: dict[str, AugmentRecord]) -> set[str]:
    keys: set[str] = set()
    for champ_id, hero_payload in apex_payload.items():
        if not isinstance(hero_payload, dict):
            continue
        raw_items = hero_payload.get("synergy_items")
        if not isinstance(raw_items, list) or not raw_items:
            raw_items = hero_payload.get("synergies") if isinstance(hero_payload.get("synergies"), list) else []
        for item in raw_items:
            official_names = []
            for raw_name in _apex_item_names(item):
                record = augment_lookup.get(raw_name) or augment_lookup.get(_lookup_key(raw_name))
                official_names.append(record.name if record else raw_name)
            if official_names:
                keys.add(_combo_key(str(champ_id), official_names))
    return keys


def _contains_retired_marker(raw_item: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            _text(raw_item.get("body")),
            _text(raw_item.get("content")),
            _text(raw_item.get("description")),
            " ".join(_raw_augment_values(raw_item)),
        ]
    )
    return any(marker.lower() in haystack.lower() for marker in RETIRED_MARKERS)


def _contains_legacy_dependency(raw_item: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            _text(raw_item.get("body")),
            _text(raw_item.get("content")),
            _text(raw_item.get("description")),
            " ".join(_raw_augment_values(raw_item)),
        ]
    )
    return any(pattern.search(haystack) for pattern in LEGACY_DEPENDENCY_PATTERNS)


def _resolve_champion(raw_item: dict[str, Any], champion_lookup: dict[str, ChampionRecord]) -> ChampionRecord | None:
    for value in (raw_item.get("champion_id"), raw_item.get("championId"), raw_item.get("champion"), raw_item.get("champion_name")):
        key = _champion_key(value)
        if key and key in champion_lookup:
            return champion_lookup[key]
    return None


def _mayhem_body(raw_item: dict[str, Any]) -> str:
    return _text(raw_item.get("body") or raw_item.get("content") or raw_item.get("description"))


def _mayhem_rating(raw_item: dict[str, Any]) -> str:
    return _text(raw_item.get("mayhem_rating") or raw_item.get("rating") or "未知") or "未知"


def _mayhem_tier(raw_item: dict[str, Any]) -> str:
    return _text(raw_item.get("mayhem_tier") or raw_item.get("tier"))


def _build_synergy_item(raw_item: dict[str, Any], augment_names: list[str], augment_tiers: list[str]) -> dict[str, Any]:
    source_url = _text(raw_item.get("source_url"))
    source_tier = _mayhem_tier(raw_item)
    source_rating = _mayhem_rating(raw_item)
    item = {
        "augment_names": augment_names,
        "tier": next((tier for tier in augment_tiers if tier), source_tier or "未知"),
        "rating": source_rating,
        "tag": "强力联动",
        "author": "ARAMMayhem",
        "is_original": False,
        "content": _mayhem_body(raw_item),
        "upvotes": 0,
        "downvotes": 0,
        "source": "arammayhem",
        "source_url": source_url,
        "source_rating": source_rating,
    }
    if source_tier:
        item["source_tier"] = source_tier
    return item


def _compat_string(item: dict[str, Any]) -> str:
    rating = _text(item.get("rating")) or "未知"
    if not rating.startswith("评分"):
        rating = f"评分 {rating}"
    return " | ".join(
        [
            ", ".join(item.get("augment_names") or []) or "未知联动",
            _text(item.get("tier")) or "未知",
            rating,
            _text(item.get("tag")) or "强力联动",
            str(int(item.get("upvotes") or 0)),
            str(int(item.get("downvotes") or 0)),
            "作者：ARAMMayhem",
            "非原创",
            _text(item.get("content")),
        ]
    )


def _ensure_hero_payload(cleaned: dict[str, Any], record: ChampionRecord) -> dict[str, Any]:
    payload = cleaned.setdefault(
        record.id,
        {
            "id": record.id,
            "name": record.name,
            "title": record.title,
            "en_name": record.en_name,
            "aliases": record.aliases,
            "synergies": [],
            "synergy_items": [],
        },
    )
    if not isinstance(payload, dict):
        payload = {
            "id": record.id,
            "name": record.name,
            "title": record.title,
            "en_name": record.en_name,
            "aliases": record.aliases,
            "synergies": [],
            "synergy_items": [],
        }
        cleaned[record.id] = payload
    if not isinstance(payload.get("synergies"), list):
        payload["synergies"] = []
    if not isinstance(payload.get("synergy_items"), list):
        payload["synergy_items"] = []
    return payload


def merge_mayhem_combos(
    *,
    apex_path: str | os.PathLike[str] | None = None,
    mayhem_raw_path: str | os.PathLike[str] = DEFAULT_MAYHEM_RAW,
    augment_manifest_path: str | os.PathLike[str] = DEFAULT_AUGMENT_MANIFEST,
    core_data_path: str | os.PathLike[str] = DEFAULT_CORE_DATA,
    output_path: str | os.PathLike[str] | None = None,
    write_output: bool = True,
) -> dict[str, Any]:
    apex_target = Path(apex_path or build_raw_synergy_data_path())
    output_target = Path(output_path or build_synergy_cleaned_data_path())
    apex_payload = _load_json(apex_target, dict)
    mayhem_payload = _load_json(mayhem_raw_path, dict)
    manifest_payload = _load_json(augment_manifest_path, list)
    core_payload = _load_json(core_data_path, dict)

    raw_items = mayhem_payload.get("items") if isinstance(mayhem_payload.get("items"), list) else []
    raw_rejects = mayhem_payload.get("rejects") if isinstance(mayhem_payload.get("rejects"), list) else []

    augment_lookup = build_augment_lookup(manifest_payload)
    champion_lookup, _champions_by_id = build_champion_lookup(core_payload)
    existing_keys = build_apex_key_set(apex_payload, augment_lookup)
    seen_mayhem_keys: set[str] = set()
    cleaned = copy.deepcopy(apex_payload)

    added = 0
    skipped_duplicates = 0
    clean_rejects: list[dict[str, Any]] = []
    valid_mayhem_items = 0
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            clean_rejects.append({"index": index, "reason": "raw_item_schema_mismatch"})
            continue
        if _contains_retired_marker(raw_item):
            clean_rejects.append({"index": index, "reason": "retired_combo", "source_url": raw_item.get("source_url", "")})
            continue
        if _contains_legacy_dependency(raw_item):
            clean_rejects.append({"index": index, "reason": "legacy_dependency", "source_url": raw_item.get("source_url", "")})
            continue
        body = _mayhem_body(raw_item)
        if not body:
            clean_rejects.append({"index": index, "reason": "missing_body", "source_url": raw_item.get("source_url", "")})
            continue
        champion = _resolve_champion(raw_item, champion_lookup)
        if not champion:
            clean_rejects.append({"index": index, "reason": "unknown_champion", "champion": raw_item.get("champion"), "source_url": raw_item.get("source_url", "")})
            continue
        augment_names, augment_tiers, unknown_augments = resolve_augment_names(raw_item, augment_lookup)
        if not augment_names:
            clean_rejects.append({"index": index, "reason": "unknown_augment", "augment_names": unknown_augments, "source_url": raw_item.get("source_url", "")})
            continue

        valid_mayhem_items += 1
        key = _combo_key(champion.id, augment_names)
        if key in existing_keys or key in seen_mayhem_keys:
            skipped_duplicates += 1
            continue

        hero_payload = _ensure_hero_payload(cleaned, champion)
        synergy_item = _build_synergy_item(raw_item, augment_names, augment_tiers)
        hero_payload["synergy_items"].append(synergy_item)
        hero_payload["synergies"].append(_compat_string(synergy_item))
        existing_keys.add(key)
        seen_mayhem_keys.add(key)
        added += 1

    reject_count = len(raw_rejects) + len(clean_rejects)
    should_write = bool(write_output and valid_mayhem_items > 0)
    if should_write:
        atomic_write_json(output_target, cleaned, ensure_ascii=False, indent=2)

    return {
        "apex_path": str(apex_target),
        "mayhem_raw_path": str(mayhem_raw_path),
        "output_path": str(output_target),
        "written": should_write,
        "apex_heroes": len(apex_payload),
        "apex_items": sum(
            len(value.get("synergy_items") or value.get("synergies") or [])
            for value in apex_payload.values()
            if isinstance(value, dict)
        ),
        "mayhem_raw_items": len(raw_items),
        "mayhem_valid_items": valid_mayhem_items,
        "added_items": added,
        "skipped_duplicate_items": skipped_duplicates,
        "reject_items": reject_count,
        "raw_reject_items": len(raw_rejects),
        "clean_reject_items": len(clean_rejects),
        "clean_rejects": clean_rejects[:20],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="清洗并合并 ARAMMayhem combo 协同数据。")
    parser.add_argument("--apex-input", default="", help="Apex 基底 JSON；默认读取 current latest pointer 指向文件。")
    parser.add_argument("--mayhem-raw", default=os.fspath(DEFAULT_MAYHEM_RAW), help="Mayhem raw JSON。")
    parser.add_argument("--augment-manifest", default=os.fspath(DEFAULT_AUGMENT_MANIFEST), help="官方 Augment_Icon_Manifest.json。")
    parser.add_argument("--core-data", default=os.fspath(DEFAULT_CORE_DATA), help="Champion_Core_Data.json。")
    parser.add_argument("--output", default="", help="输出 Champion_Synergy_Cleaned.json；默认写 data/static。")
    parser.add_argument("--dry-run", action="store_true", help="只输出统计，不写 cleaned 文件。")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    summary = merge_mayhem_combos(
        apex_path=args.apex_input or None,
        mayhem_raw_path=args.mayhem_raw,
        augment_manifest_path=args.augment_manifest,
        core_data_path=args.core_data,
        output_path=args.output or None,
        write_output=not args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

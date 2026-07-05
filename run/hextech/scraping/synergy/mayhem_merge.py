"""清洗并合并 ARAMMayhem combo 到前端协同数据。

默认优先使用 ApexLoL raw latest 作为高置信度基底；缺少 raw latest 时才回退到当前
cleaned 协同数据。合并前会先移除旧 ``source="arammayhem"`` 条目，再用本次
ARAMMayhem 数据只补 Apex 缺失组合。Mayhem 原始文件为空或全部被拒绝时不会覆盖
``Champion_Synergy_Cleaned.json``。
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

RUN_DIR = Path(__file__).resolve().parents[3]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.catalog.runtime_store import build_raw_synergy_data_path, build_synergy_cleaned_data_path
from hextech.catalog.version_catalog import get_augment_resource_catalog_path, load_augment_manifest_entries, load_champion_core_data
from hextech.scraping.icon_resolver import normalize_augment_name
from hextech.support.atomic_io import atomic_write_json

DATA_DIR = RUN_DIR / "data"
STATIC_VERSION_DIR = DATA_DIR / "static" / "version"
EVIDENCE_DIR = DATA_DIR / "evidence"
DEFAULT_MAYHEM_RAW = EVIDENCE_DIR / "mayhem_combos.raw.json"
DEFAULT_AUGMENT_MANIFEST = get_augment_resource_catalog_path(STATIC_VERSION_DIR)
DEFAULT_CORE_DATA = STATIC_VERSION_DIR / "英雄目录.v1.json"
MAYHEM_COMPAT_AUTHOR_RE = re.compile(
    r"(?:^|[|｜])\s*作者\s*[:：]\s*ARAMMayhem\s*(?=[|｜]|$)",
    re.IGNORECASE,
)

RETIRED_MARKERS = (
    "Retired in live Mayhem",
    "已移除",
)
ARCHIVED_APEX_MARKERS = (
    "已弃用归档",
    "已弃用",
    "deprecated",
    "archived",
    "retired",
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


def _contains_archived_apex_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_archived_apex_marker(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_archived_apex_marker(child) for child in value)
    text = _text(value).lower()
    return bool(text and any(marker.lower() in text for marker in ARCHIVED_APEX_MARKERS))


def _load_json(path: str | os.PathLike[str], expected_type: type) -> Any:
    target = Path(path)
    with target.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, expected_type):
        raise ValueError(f"JSON schema mismatch: {target} expected={expected_type.__name__}")
    return payload


def _load_augment_manifest(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    target = Path(path)
    if target.name == "海克斯资源目录.v1.json":
        return load_augment_manifest_entries(target.parent)
    payload = _load_json(target, list)
    return [item for item in payload if isinstance(item, dict)]


def _load_core_data(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = Path(path)
    if target.name == "英雄目录.v1.json":
        return load_champion_core_data(target.parent)
    if target.name == "Champion_Core_Data.json" and not target.exists():
        return load_champion_core_data(target.parent)
    return _load_json(target, dict)


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


def _resolve_base_payload(apex_path: str | os.PathLike[str] | None) -> tuple[dict[str, Any], Path, str]:
    """选择合并基底；默认以 Apex raw latest 为主，cleaned 只作兼容兜底。"""

    if apex_path:
        target = Path(apex_path)
        return _load_json(target, dict), target, "apex_input"

    raw_target = Path(build_raw_synergy_data_path())
    if raw_target.exists() and raw_target.name != "Champion_Synergy.json":
        return _load_json(raw_target, dict), raw_target, "raw_latest"
    if raw_target.exists():
        return _load_json(raw_target, dict), raw_target, "raw_legacy"

    cleaned_target = Path(build_synergy_cleaned_data_path())
    if cleaned_target.exists():
        return _load_json(cleaned_target, dict), cleaned_target, "cleaned_without_mayhem"

    raise FileNotFoundError(
        "找不到 Apex raw latest 或现有 Champion_Synergy_Cleaned.json；"
        "请传入 --apex-input，或先准备 cleaned 基底。"
    )


def remove_mayhem_items(cleaned: dict[str, Any]) -> int:
    """从基底中移除旧 Mayhem 条目，防止低频刷新长期堆积过期组合。"""

    removed = 0
    for hero_payload in cleaned.values():
        if not isinstance(hero_payload, dict):
            continue
        items = hero_payload.get("synergy_items")
        kept_items: list[Any] = []
        removed_compat_strings: set[str] = set()
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and _text(item.get("source")).lower() == "arammayhem":
                    removed += 1
                    removed_compat_strings.add(_compat_string(item))
                    continue
                kept_items.append(item)
            hero_payload["synergy_items"] = kept_items

        synergies = hero_payload.get("synergies")
        if isinstance(synergies, list):
            hero_payload["synergies"] = [
                value
                for value in synergies
                if not (
                    isinstance(value, str)
                    and (value in removed_compat_strings or MAYHEM_COMPAT_AUTHOR_RE.search(value))
                )
            ]
    return removed


def remove_archived_apex_items(cleaned: dict[str, Any]) -> int:
    """清理旧 Apex latest 中已经混入的归档卡片，避免 cleaned 继续污染 UI。"""

    removed = 0
    for hero_payload in cleaned.values():
        if not isinstance(hero_payload, dict):
            continue
        items = hero_payload.get("synergy_items")
        kept_items: list[Any] = []
        removed_compat_strings: set[str] = set()
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and _contains_archived_apex_marker(item):
                    removed += 1
                    removed_compat_strings.add(_compat_string(item))
                    continue
                kept_items.append(item)
            hero_payload["synergy_items"] = kept_items

        synergies = hero_payload.get("synergies")
        if isinstance(synergies, list):
            hero_payload["synergies"] = [
                value
                for value in synergies
                if not (
                    isinstance(value, str)
                    and (value in removed_compat_strings or _contains_archived_apex_marker(value))
                )
            ]
    return removed


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
    apex_payload, apex_target, base_mode = _resolve_base_payload(apex_path)
    output_target = Path(output_path or build_synergy_cleaned_data_path())
    mayhem_payload = _load_json(mayhem_raw_path, dict)
    manifest_payload = _load_augment_manifest(augment_manifest_path)
    core_payload = _load_core_data(core_data_path)

    raw_items = mayhem_payload.get("items") if isinstance(mayhem_payload.get("items"), list) else []
    raw_rejects = mayhem_payload.get("rejects") if isinstance(mayhem_payload.get("rejects"), list) else []

    augment_lookup = build_augment_lookup(manifest_payload)
    champion_lookup, _champions_by_id = build_champion_lookup(core_payload)
    seen_mayhem_keys: set[str] = set()
    cleaned = copy.deepcopy(apex_payload)
    removed_archived_apex_items = remove_archived_apex_items(cleaned)
    removed_existing_mayhem_items = remove_mayhem_items(cleaned)
    existing_keys = build_apex_key_set(cleaned, augment_lookup)

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
        "base_mode": base_mode,
        "mayhem_raw_path": str(mayhem_raw_path),
        "output_path": str(output_target),
        "written": should_write,
        "removed_archived_apex_items": removed_archived_apex_items,
        "removed_existing_mayhem_items": removed_existing_mayhem_items,
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
    parser.add_argument("--augment-manifest", default=os.fspath(DEFAULT_AUGMENT_MANIFEST), help="官方海克斯资源目录或旧 Augment_Icon_Manifest.json。")
    parser.add_argument("--core-data", default=os.fspath(DEFAULT_CORE_DATA), help="英雄目录或旧 Champion_Core_Data.json。")
    parser.add_argument("--output", default="", help="输出 Champion_Synergy_Cleaned.json；默认写 data/static/version。")
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

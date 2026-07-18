"""Web 联动 DTO 的归一化、去重与污染隔离。"""

from __future__ import annotations

import json
import re

from . import runtime as web_runtime
def _normalize_synergy_entry(raw_entry: str) -> str:
    """把历史联动字符串归一成前端稳定解析格式。"""
    parts = [part.strip() for part in str(raw_entry or "").split("|")]
    if len(parts) < 8:
        return str(raw_entry or "")

    name, tier, grade, tag = parts[:4]
    sixth = parts[5] if len(parts) > 5 else ""
    seventh = parts[6] if len(parts) > 6 else ""

    # 新格式已经是：name | tier | grade | tag | up | down | 作者：x | 原创 | content
    if len(parts) >= 9 and sixth.isdigit() and (seventh.startswith("作者：") or seventh.startswith("作者:")):
        return " | ".join(parts)

    # 旧格式是：name | tier | grade | tag | rating | author | 原创/非原创 | content
    if seventh in {"原创", "非原创"} and not (sixth.startswith("作者：") or sixth.startswith("作者:")):
        author = sixth or "ApexLoL"
        content = " | ".join(parts[7:]).strip()
        return " | ".join([
            name,
            tier,
            grade,
            tag,
            "0",
            "0",
            f"作者：{author}",
            seventh,
            content,
        ])

    return " | ".join(parts)


def _normalize_synergy_entries(raw_entries) -> list[str]:
    if not isinstance(raw_entries, list):
        return []
    return [_normalize_synergy_entry(entry) for entry in raw_entries if str(entry or "").strip()]


def _split_augment_names(raw_name: str) -> list[str]:
    names = [part.strip() for part in re.split(r"[，,、/+]", str(raw_name or "")) if part.strip()]
    if names:
        return list(dict.fromkeys(names))
    text = str(raw_name or "").strip()
    return [text] if text else []


def _int_field(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _strip_rating_prefix(value: str) -> str:
    text = str(value or "").strip()
    return re.sub(r"^评分\s*", "", text, flags=re.IGNORECASE).strip() or text


def _bool_field(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "原创", "original"}
    return bool(value)


def _normalize_synergy_item(raw_item) -> dict | None:
    """把新旧联动协议都收口成前端稳定消费的结构化对象。"""
    if isinstance(raw_item, str):
        return _normalize_synergy_item_from_string(raw_item)
    if not isinstance(raw_item, dict):
        return None

    raw_names = raw_item.get("augment_names") or raw_item.get("augmentNames") or raw_item.get("augments") or raw_item.get("name")
    if isinstance(raw_names, list):
        augment_names = []
        for value in raw_names:
            if isinstance(value, dict):
                value = value.get("name") or value.get("displayName") or value.get("id") or value.get("slug")
            augment_names.extend(_split_augment_names(str(value or "")))
        augment_names = list(dict.fromkeys(augment_names))
    else:
        augment_names = _split_augment_names(str(raw_names or ""))
    if not augment_names:
        return None

    tags = raw_item.get("tags") or raw_item.get("tag") or "强力联动"
    if isinstance(tags, list):
        tag = " ".join(str(item).strip() for item in tags if str(item).strip()) or "强力联动"
    else:
        tag = str(tags or "强力联动").strip() or "强力联动"

    normalized = {
        "augment_names": augment_names,
        "name": ", ".join(augment_names),
        "tier": str(raw_item.get("tier") or raw_item.get("rarity") or raw_item.get("rank") or "未知").strip() or "未知",
        "rating": _strip_rating_prefix(str(raw_item.get("rating") or raw_item.get("grade") or raw_item.get("score") or "未知")),
        "tag": tag,
        "author": str(raw_item.get("author") or raw_item.get("contributor") or raw_item.get("user") or "ApexLoL").strip() or "ApexLoL",
        "is_original": _bool_field(raw_item.get("is_original") if "is_original" in raw_item else raw_item.get("isOriginal") or raw_item.get("original")),
        "content": str(raw_item.get("content") or raw_item.get("note") or raw_item.get("text") or raw_item.get("description") or "").strip(),
        "upvotes": _int_field(raw_item.get("upvotes") or raw_item.get("upVotes") or raw_item.get("likes")),
        "downvotes": _int_field(raw_item.get("downvotes") or raw_item.get("downVotes") or raw_item.get("dislikes")),
    }
    for key in ("source", "source_url", "source_rating"):
        value = raw_item.get(key)
        if value is not None and str(value).strip():
            normalized[key] = str(value).strip()
    return normalized


def _normalize_synergy_item_from_string(raw_entry: str) -> dict | None:
    normalized = _normalize_synergy_entry(raw_entry)
    parts = [part.strip() for part in normalized.split("|")]
    if len(parts) < 4:
        return None

    name, tier, grade, tag = parts[:4]
    upvotes = _int_field(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
    downvotes = _int_field(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
    author = "ApexLoL"
    is_original = False
    content_start = 6 if len(parts) > 5 and parts[4].isdigit() and parts[5].isdigit() else 4

    if len(parts) > content_start:
        maybe_author = parts[content_start]
        if maybe_author.startswith(("作者：", "作者:")):
            author = maybe_author.split("：", 1)[-1].split(":", 1)[-1].strip() or author
            content_start += 1
    if len(parts) > content_start and parts[content_start] in {"原创", "非原创"}:
        is_original = parts[content_start] == "原创"
        content_start += 1

    return {
        "augment_names": _split_augment_names(name),
        "name": name,
        "tier": tier or "未知",
        "rating": _strip_rating_prefix(grade),
        "tag": tag or "强力联动",
        "author": author,
        "is_original": is_original,
        "content": " | ".join(parts[content_start:]).strip(),
        "upvotes": upvotes,
        "downvotes": downvotes,
    }


def _normalize_synergy_items(raw_items, raw_entries=None) -> list[dict]:
    source_items = raw_items if isinstance(raw_items, list) else []
    if not source_items and isinstance(raw_entries, list):
        source_items = raw_entries
    result = []
    for item in source_items:
        normalized = _normalize_synergy_item(item)
        if normalized and normalized.get("content"):
            result.append(normalized)
    return result


def _synergy_item_to_display_string(item: dict) -> str:
    rating = str(item.get("rating") or "未知").strip()
    if rating and not rating.startswith("评分"):
        rating = f"评分 {rating}"
    return " | ".join([
        str(item.get("name") or ", ".join(item.get("augment_names") or []) or "未知联动"),
        str(item.get("tier") or "未知"),
        rating or "评分 未知",
        str(item.get("tag") or "强力联动"),
        str(_int_field(item.get("upvotes"))),
        str(_int_field(item.get("downvotes"))),
        f"作者：{item.get('author') or 'ApexLoL'}",
        "原创" if item.get("is_original") else "非原创",
        str(item.get("content") or ""),
    ])


_SHORT_ALIAS_TERMS = {"q", "w", "e", "r", "ad", "ap", "aa"}
_PARTIAL_SYNERGY_OVERLAP_MIN_SHARED = 2


def _normalize_match_text(value) -> str:
    return "".join(str(value or "").lower().split())


def _champion_terms(champ_id: str, *, include_short_chinese: bool = False) -> list[str]:
    cache = web_runtime.ensure_champion_cache()
    record = cache.get(str(champ_id), {}) if isinstance(cache, dict) else {}
    if not isinstance(record, dict):
        return []

    raw_terms = [
        record.get("name"),
        record.get("title"),
        record.get("en_name"),
        *(record.get("aliases") or []),
    ]
    terms: list[str] = []
    for value in raw_terms:
        text = str(value or "").strip()
        normalized = _normalize_match_text(text)
        if not normalized or normalized in _SHORT_ALIAS_TERMS:
            continue
        if normalized.isascii() and len(normalized) < 3:
            continue
        if not normalized.isascii() and len(normalized) < 2 and not include_short_chinese:
            continue
        if text not in terms:
            terms.append(text)
    return terms


def _synergy_items_signature(items: list[dict]) -> str:
    if not items:
        return ""
    return json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _synergy_item_overlap_key(item: dict) -> str:
    names = sorted(
        _normalize_match_text(name)
        for name in (item.get("augment_names") or [])
        if _normalize_match_text(name)
    )
    return json.dumps(
        {
            "names": names,
            "rating": _normalize_match_text(item.get("rating") or ""),
            "tag": _normalize_match_text(item.get("tag") or ""),
            "content": _normalize_match_text(item.get("content") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _synergy_item_overlap_keys(items: list[dict]) -> set[str]:
    return {_synergy_item_overlap_key(item) for item in items if isinstance(item, dict) and item.get("content")}


def _synergy_overlap_matches(data: dict, target_items: list[dict]) -> dict[str, str]:
    """查找整组重复或局部重叠的协同条目，作为 API 侧污染兜底。"""
    target_signature = _synergy_items_signature(target_items)
    if not target_signature:
        return {}

    target_keys = _synergy_item_overlap_keys(target_items)
    matches: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        raw_synergies = value.get("synergies", [])
        items = _normalize_synergy_items(value.get("synergy_items", []), raw_synergies)
        if _synergy_items_signature(items) == target_signature:
            matches[str(key)] = "exact"
            continue

        item_keys = _synergy_item_overlap_keys(items)
        shared_count = len(target_keys & item_keys)
        if shared_count >= _PARTIAL_SYNERGY_OVERLAP_MIN_SHARED:
            matches[str(key)] = "overlap"
    return matches if len(matches) > 1 else {}


def _find_term_hits(text: str, terms: list[str]) -> list[str]:
    normalized_text = _normalize_match_text(text)
    hits = []
    for term in terms:
        normalized_term = _normalize_match_text(term)
        if normalized_term and normalized_term in normalized_text:
            hits.append(term)
    return hits


def _synergy_quarantine_reason(champ_id: str, synergy_items: list[dict], overlap_matches: dict[str, str]) -> dict:
    """用重复/重叠条目与英雄名词命中判断是否应隐藏污染数据。"""
    peers = [item for item in overlap_matches if item != str(champ_id)]
    if not peers:
        return {}

    content_text = " ".join(str(item.get("content") or "") for item in synergy_items if isinstance(item, dict))
    own_hits = _find_term_hits(content_text, _champion_terms(str(champ_id), include_short_chinese=True))
    foreign_hits = []
    for other_id in peers:
        hits = _find_term_hits(content_text, _champion_terms(other_id, include_short_chinese=True))
        if hits:
            foreign_hits.append({"id": other_id, "terms": hits[:5]})

    if foreign_hits and not own_hits:
        return {
            "reason": "foreign_champion_terms",
            "duplicate_with": peers,
            "match_types": {peer: overlap_matches.get(peer) for peer in peers},
            "foreign_hits": foreign_hits,
        }
    if any(overlap_matches.get(peer) == "exact" for peer in peers) and not own_hits:
        return {
            "reason": "ambiguous_duplicate_synergy_items",
            "duplicate_with": peers,
            "match_types": {peer: overlap_matches.get(peer) for peer in peers},
            "foreign_hits": foreign_hits,
        }
    return {}


def _empty_synergy_payload(*, status_text: str = "empty", message: str = "暂无联动数据") -> dict:
    return {
        "synergies": [],
        "synergy_items": [],
        "status": status_text,
        "message": message,
    }


def _build_synergy_api_payload(data: dict, champ_id: str) -> dict:
    if not data:
        return _empty_synergy_payload()

    resolved_champ_id = web_runtime.resolve_champion_id(champ_id)
    canonical_name = web_runtime.resolve_canonical_hero_name(champ_id).lower()

    lookup_id = resolved_champ_id or str(champ_id)
    synergy_data = data.get(lookup_id, {})
    if not synergy_data:
        for key, value in data.items():
            key_text = str(key).lower()
            if (
                str(champ_id).lower() == key_text
                or str(resolved_champ_id).lower() == key_text
                or (canonical_name and canonical_name == key_text)
            ):
                synergy_data = value
                lookup_id = str(key)
                break

    if not synergy_data:
        return _empty_synergy_payload()

    raw_synergies = synergy_data.get("synergies", []) if synergy_data else []
    synergy_items = _normalize_synergy_items(
        synergy_data.get("synergy_items", []) if synergy_data else [],
        raw_synergies,
    )
    overlap_matches = _synergy_overlap_matches(data, synergy_items)
    if overlap_matches:
        quarantine = _synergy_quarantine_reason(str(lookup_id), synergy_items, overlap_matches)
        if quarantine:
            return {
                "synergies": [],
                "synergy_items": [],
                "status": "quarantined",
                "message": "联动数据待校准",
                **quarantine,
            }

    synergies = _normalize_synergy_entries(raw_synergies)
    if not synergies and synergy_items:
        synergies = [_synergy_item_to_display_string(item) for item in synergy_items]
    if not synergies and not synergy_items:
        return _empty_synergy_payload()
    return {"synergies": synergies, "synergy_items": synergy_items, "status": "ok"}

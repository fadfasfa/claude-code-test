"""游戏内 overlay 轻量提示缓存。

本模块把现有海克斯详情 payload 收口成 overlay 可直接按 `augment_id` 查询的
本地 JSON 结构。它只消费已经可用的本地数据，不触发抓取、远端访问或 Web 服务。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from processing.runtime_store import build_runtime_cache_path
from tools.atomic_io import atomic_write_json


SCHEMA_VERSION = 1
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
OVERLAY_HINT_CACHE_FILE = Path(build_runtime_cache_path("overlay_hint_cache.v1.json"))
# \w 含 CJK：海克斯名是中文，ASCII-only 正则会把整个名字滤成空串，导致模板索引几乎为空。
_AUGMENT_ID_SAFE_RE = re.compile(r"[^\w.:-]+")


def normalize_augment_id(value: Any, fallback_name: str = "") -> str:
    """生成 overlay 查询用稳定 ID；优先使用源数据 ID，缺失时用名称兜底。"""

    raw_text = str(value or "").strip() or str(fallback_name or "").strip()
    if not raw_text:
        return ""
    return _AUGMENT_ID_SAFE_RE.sub("_", raw_text).strip("_").lower()


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _empty_cache_payload(error: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": 0.0,
        "source": {"tag": "local", "private_policy_stats_enabled": False},
        "hints": {},
        "name_index": {},
        "error": error,
    }


def _clean_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text


def _summary_from_card(card: Mapping[str, Any]) -> str:
    summary = _clean_text(card.get("tooltip_plain") or card.get("summary") or card.get("海克斯描述"))
    if not summary:
        return "暂无机制说明"
    return summary[:120]


def _iter_hextech_cards(hextech_by_hero: Mapping[str, Any]):
    for hero_name, payload in hextech_by_hero.items():
        if isinstance(payload, Mapping):
            cards = payload.get("comprehensive") or payload.get("top_10_overall") or []
        elif isinstance(payload, list):
            cards = payload
        else:
            cards = []
        for card in cards:
            if isinstance(card, Mapping):
                yield str(hero_name or ""), card


def _build_hint(card: Mapping[str, Any], hero_name: str, *, include_private_stats: bool) -> dict[str, Any] | None:
    name = _clean_text(card.get("海克斯名称") or card.get("name"))
    augment_id = normalize_augment_id(card.get("海克斯ID") or card.get("augment_id"), name)
    if not augment_id or not name:
        return None

    tier = _clean_text(card.get("海克斯阶级") or card.get("tier") or "Unknown") or "Unknown"
    hint: dict[str, Any] = {
        "augment_id": augment_id,
        "name": name,
        "tier": tier,
        "summary": _summary_from_card(card),
        "condition": _clean_text(card.get("condition") or "根据当前英雄与海克斯机制判断"),
        "tags": [tier],
        "icon": _clean_text(card.get("icon")),
        "source_heroes": [hero_name] if hero_name else [],
    }

    if include_private_stats:
        rank = _coerce_int(card.get("源站排名") or card.get("rank"))
        score = _coerce_float(card.get("综合得分") or card.get("score"))
        winrate = _coerce_float(card.get("海克斯胜率") or card.get("winrate"))
        if rank is not None:
            hint["rank"] = rank
        if score is not None:
            hint["score"] = score
        if winrate is not None:
            hint["winrate"] = winrate
    return hint


def build_overlay_hint_cache(
    hextech_by_hero: Mapping[str, Any],
    *,
    include_private_stats: bool = False,
    source_tag: str = "",
) -> dict[str, Any]:
    """从已加载的本地海克斯 payload 生成 overlay hint cache。"""

    hints: dict[str, dict[str, Any]] = {}
    name_index: dict[str, str] = {}
    for hero_name, card in _iter_hextech_cards(hextech_by_hero):
        hint = _build_hint(card, hero_name, include_private_stats=include_private_stats)
        if not hint:
            continue
        existing = hints.get(hint["augment_id"])
        if existing:
            for source_hero in hint.get("source_heroes", []):
                if source_hero and source_hero not in existing["source_heroes"]:
                    existing["source_heroes"].append(source_hero)
            if hint.get("name"):
                name_index.setdefault(normalize_augment_id(hint["name"]), hint["augment_id"])
                name_index.setdefault(str(hint["name"]), hint["augment_id"])
            continue
        hints[hint["augment_id"]] = hint
        name_index.setdefault(hint["augment_id"], hint["augment_id"])
        if hint.get("name"):
            name_index.setdefault(normalize_augment_id(hint["name"]), hint["augment_id"])
            name_index.setdefault(str(hint["name"]), hint["augment_id"])

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "source": {
            "tag": str(source_tag or "local"),
            "private_policy_stats_enabled": bool(include_private_stats),
        },
        "hints": hints,
        "name_index": name_index,
    }


def build_overlay_hint_cache_from_precomputed(
    *,
    include_private_stats: bool = False,
    source_tag: str = "precomputed",
) -> dict[str, Any]:
    """从本地预计算详情缓存生成 overlay hint cache，不触发数据刷新。"""

    from processing.precomputed_cache import (
        load_precomputed_champion_list,
        load_precomputed_hextech_for_hero,
        warm_precomputed_hextech_cache,
    )

    if not warm_precomputed_hextech_cache():
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": time.time(),
            "source": {
                "tag": source_tag,
                "private_policy_stats_enabled": bool(include_private_stats),
            },
            "hints": {},
            "name_index": {},
            "error": "cache_missing",
        }

    hextech_by_hero: dict[str, Any] = {}
    for champion in load_precomputed_champion_list():
        if not isinstance(champion, Mapping):
            continue
        hero_name = _clean_text(champion.get("英雄名称") or champion.get("hero_name") or champion.get("name"))
        if not hero_name:
            continue
        payload = load_precomputed_hextech_for_hero(hero_name)
        if isinstance(payload, Mapping):
            hextech_by_hero[hero_name] = payload

    cache_payload = build_overlay_hint_cache(
        hextech_by_hero,
        include_private_stats=include_private_stats,
        source_tag=source_tag,
    )
    if not cache_payload["hints"]:
        cache_payload["error"] = "cache_missing"
    return cache_payload


def _resolve_cache_error(cache_payload: Mapping[str, Any], *, now: float | None = None) -> str:
    if not isinstance(cache_payload, Mapping):
        return "cache_missing"
    explicit_error = str(cache_payload.get("error") or "").strip()
    if explicit_error:
        return explicit_error
    hints = cache_payload.get("hints")
    if not isinstance(hints, Mapping):
        return "cache_missing"
    if cache_payload.get("schema_version") != SCHEMA_VERSION:
        return "schema_mismatch"
    generated_at = _coerce_float(cache_payload.get("generated_at"))
    if generated_at is None or generated_at <= 0:
        return "cache_missing"
    if ((now if now is not None else time.time()) - generated_at) > CACHE_MAX_AGE_SECONDS:
        return "cache_expired"
    return ""


def query_overlay_hint(cache_payload: Mapping[str, Any], augment_id: Any) -> dict[str, Any]:
    """按 augment_id 或规范化名称查询提示；缓存缺失和条目缺失给出明确错误码。"""

    normalized_id = normalize_augment_id(augment_id)
    cache_error = _resolve_cache_error(cache_payload)
    if cache_error:
        return {"ok": False, "error": cache_error, "augment_id": normalized_id}
    hints = cache_payload.get("hints") if isinstance(cache_payload, Mapping) else None
    if not isinstance(hints, Mapping):
        return {"ok": False, "error": "cache_missing", "augment_id": normalized_id}
    hint = hints.get(normalized_id)
    if not isinstance(hint, Mapping):
        name_index = cache_payload.get("name_index")
        if isinstance(name_index, Mapping):
            hinted_id = name_index.get(normalized_id) or name_index.get(str(augment_id or "").strip())
            if hinted_id:
                normalized_hint_id = normalize_augment_id(hinted_id)
                hint = hints.get(normalized_hint_id)
    if not isinstance(hint, Mapping):
        return {"ok": False, "error": "hint_missing", "augment_id": normalized_id}
    return {"ok": True, "hint": dict(hint)}


def write_overlay_hint_cache(cache_payload: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """写入 overlay hint cache 到运行态 cache 目录。"""

    target = Path(path) if path is not None else OVERLAY_HINT_CACHE_FILE
    atomic_write_json(target, cache_payload, ensure_ascii=False, indent=2)
    return target


def load_overlay_hint_cache(path: str | Path | None = None) -> dict[str, Any]:
    """读取 overlay hint cache；缺失或损坏时返回空缓存错误结构。"""

    target = Path(path) if path is not None else OVERLAY_HINT_CACHE_FILE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return _empty_cache_payload("cache_missing")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return _empty_cache_payload("cache_damaged")
    return payload if isinstance(payload, dict) else _empty_cache_payload("cache_damaged")

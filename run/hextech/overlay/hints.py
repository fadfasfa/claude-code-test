"""游戏内 overlay 轻量提示缓存。

本模块把现有海克斯详情 payload 收口成 overlay 可直接按 `augment_id` 查询的
本地 JSON 结构。它只消费已经可用的本地数据（含 synergy 快照），不触发抓取、
远端访问或 Web 服务；synergy 与私用胜率/出场率字段都按 `include_private_stats`
标记控制是否写入。

调用方: display.desktop.app、overlay.data_source、overlay.events; 关键依赖: catalog.runtime_store、support.atomic_io、catalog。
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from hextech.catalog.runtime_store import (
    build_synergy_data_path,
    build_runtime_cache_path,
    get_latest_synergy_snapshot_path,
)
from hextech.support.atomic_io import atomic_write_json


SCHEMA_VERSION = 1
CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
OVERLAY_HINT_CACHE_FILE = Path(build_runtime_cache_path("overlay_hint_cache.v1.json"))
# \w 含 CJK：海克斯名是中文，ASCII-only 正则会把整个名字滤成空串，导致模板索引几乎为空。
_AUGMENT_ID_SAFE_RE = re.compile(r"[^\w.:-]+")
_AUGMENT_NAME_NORMALIZE_TOKENS = (" ", "\t", "\n", "-", "_", "(", ")", "[", "]", "'", '"', ".", ":", "：", "，", ",", "、", "/", "／")


def normalize_augment_id(value: Any, fallback_name: str = "") -> str:
    """生成 overlay 查询用稳定 ID；优先使用源数据 ID，缺失时用名称兜底。"""

    if value is None or (isinstance(value, float) and math.isnan(value)):
        raw_text = ""
    else:
        raw_text = str(value).strip()
        if raw_text.lower() in {"nan", "<na>", "nat"}:
            raw_text = ""
    raw_text = raw_text or str(fallback_name or "").strip()
    if not raw_text:
        return ""
    return _AUGMENT_ID_SAFE_RE.sub("_", raw_text).strip("_").lower()


def normalize_augment_name(value: Any) -> str:
    """轻量名称规范化；避免 overlay host 顶层导入抓取侧资源解析模块。"""

    text = str(value or "").lower()
    for token in _AUGMENT_NAME_NORMALIZE_TOKENS:
        text = text.replace(token, "")
    return text


def _coerce_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _coerce_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_synergy_item(
    item: Mapping[str, Any],
    *,
    hero_id: str,
    hero_name: str,
) -> dict[str, Any] | None:
    """规范单条 synergy_item，仅保留 overlay 渲染需要的字段。"""

    augment_names = item.get("augment_names")
    if isinstance(augment_names, list):
        names = [_clean_text(value) for value in augment_names if _clean_text(value)]
    elif augment_names:
        names = [_clean_text(augment_names)]
    else:
        names = []
    if not names:
        return None
    normalized = {
        "hero_id": str(hero_id or "").strip(),
        "hero_name": _clean_text(hero_name),
        "rating": _clean_text(item.get("rating")),
        "tag": _clean_text(item.get("tag")),
        "tier": _clean_text(item.get("tier")),
        "content": _clean_text(item.get("content")),
        "augment_names": names,
    }
    for key in ("source", "source_url", "source_rating", "source_tier"):
        value = _clean_text(item.get(key))
        if value:
            normalized[key] = value
    return normalized


def _load_synergy_by_augment_name(
    snapshot_path: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """把本地 synergy 快照按 augment 中文名展开成索引；缺失或损坏时返回空表。"""

    target = Path(snapshot_path) if snapshot_path else None
    if target is None:
        for resolver in (build_synergy_data_path, get_latest_synergy_snapshot_path):
            resolved = resolver()
            if resolved:
                target = Path(resolved)
                break
    if target is None:
        return {}
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}

    canonical_names: dict[str, str] = {}
    try:
        from hextech.catalog.version_catalog import load_augment_manifest_entries

        canonical_names = {
            normalize_augment_name(item.get("name")): str(item.get("name") or "")
            for item in load_augment_manifest_entries()
            if isinstance(item, Mapping) and str(item.get("name") or "")
        }
    except Exception:
        canonical_names = {}

    index: dict[str, list[dict[str, Any]]] = {}
    for hero_id, hero_payload in payload.items():
        if not isinstance(hero_payload, Mapping):
            continue
        hero_name = hero_payload.get("name") or hero_payload.get("title") or ""
        items = hero_payload.get("synergy_items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            normalized = _normalize_synergy_item(item, hero_id=str(hero_id or ""), hero_name=str(hero_name))
            if not normalized:
                continue
            for augment_name in normalized["augment_names"]:
                normalized_name = normalize_augment_name(augment_name)
                canonical_name = canonical_names.get(normalized_name) or augment_name
                index.setdefault(canonical_name, []).append(normalized)
                if normalized_name and normalized_name != canonical_name and normalized_name not in canonical_names:
                    index.setdefault(normalized_name, []).append(normalized)
    return index


def _empty_cache_payload(error: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": 0.0,
        "source": {"tag": "local", "private_policy_stats_enabled": False},
        "hints": {},
        "name_index": {},
        "error": error,
    }


def _read_wrapped_json_file(path: str | Path, default: Any) -> Any:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default
    if isinstance(payload, Mapping) and "data" in payload:
        return payload.get("data", default)
    return payload


def _load_stale_precomputed_payloads() -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """读取本地已有预计算快照；只作为 overlay 降级数据源，不改变正式 API freshness。"""

    from hextech.catalog import precomputed_cache

    champions = _read_wrapped_json_file(precomputed_cache.CHAMPION_LIST_CACHE_FILE, [])
    hextech_by_hero = _read_wrapped_json_file(precomputed_cache.HEXTECH_DETAIL_CACHE_FILE, {})
    champion_rows = [item for item in champions if isinstance(item, Mapping)] if isinstance(champions, list) else []
    hextech_map = {str(name): payload for name, payload in hextech_by_hero.items()} if isinstance(hextech_by_hero, Mapping) else {}
    return champion_rows, hextech_map


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if text.lower() in {"nan", "<na>", "nat"}:
        return ""
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


def _hero_id_from_card(card: Mapping[str, Any], fallback: Any = "") -> str:
    for key in ("英雄 ID", "英雄ID", "champion_id", "championId", "hero_id", "heroId"):
        text = _clean_text(card.get(key))
        if text:
            return text
    return _clean_text(fallback)


def _hero_name_from_card(card: Mapping[str, Any], fallback: str = "") -> str:
    for key in ("英雄名称", "hero_name", "champion_name"):
        text = _clean_text(card.get(key))
        if text:
            return text
    return _clean_text(fallback)


def _private_stats_from_card(card: Mapping[str, Any], *, hero_id: str, hero_name: str) -> dict[str, Any]:
    """提取单英雄海克斯统计；这些值只代表 hero_id/hero_name 对应英雄。"""

    stats: dict[str, Any] = {}
    rank = _coerce_int(card.get("源站排名") or card.get("rank"))
    score = _coerce_float(card.get("综合得分") or card.get("score"))
    winrate = _coerce_float(card.get("海克斯胜率") or card.get("winrate"))
    pickrate = _coerce_float(card.get("海克斯出场率") or card.get("pickrate"))
    if rank is not None:
        stats["rank"] = rank
    if score is not None:
        stats["score"] = score
    if winrate is not None:
        stats["winrate"] = winrate
    if pickrate is not None:
        stats["pickrate"] = pickrate
    if hero_id:
        stats["champion_id"] = hero_id
    if hero_name:
        stats["source_hero_name"] = hero_name
    return stats


def _merge_hint(existing: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for source_hero in incoming.get("source_heroes", []):
        if source_hero and source_hero not in existing["source_heroes"]:
            existing["source_heroes"].append(source_hero)
    for index_key in ("stats_by_champion_id", "stats_by_champion_name"):
        incoming_index = incoming.get(index_key)
        if not isinstance(incoming_index, Mapping):
            continue
        existing_index = existing.setdefault(index_key, {})
        if not isinstance(existing_index, dict):
            continue
        for key, value in incoming_index.items():
            if key and isinstance(value, Mapping):
                existing_index[str(key)] = dict(value)
    if "synergies" not in existing and isinstance(incoming.get("synergies"), list):
        existing["synergies"] = [dict(item) for item in incoming["synergies"] if isinstance(item, Mapping)]
    aliases = existing.setdefault("aliases", [])
    if isinstance(aliases, list):
        for alias in incoming.get("aliases", []):
            if alias and alias not in aliases:
                aliases.append(alias)


def _card_aliases(card: Mapping[str, Any]) -> list[str]:
    aliases: list[str] = []
    for key in ("augment_name_id", "augmentNameId", "cdragon_id", "cdragonId"):
        raw = _clean_text(card.get(key))
        for value in (raw, normalize_augment_id(raw), normalize_augment_name(raw)):
            if value and value not in aliases:
                aliases.append(value)
                if key in {"augment_name_id", "augmentNameId"} and not value.lower().startswith("aram_"):
                    aram_value = f"aram_{value}"
                    if aram_value not in aliases:
                        aliases.append(aram_value)
    return aliases


def _index_hint_aliases(name_index: dict[str, str], hint: Mapping[str, Any]) -> None:
    augment_id = _clean_text(hint.get("augment_id"))
    if not augment_id:
        return
    name_index.setdefault(augment_id, augment_id)
    for alias in hint.get("aliases", []):
        raw = _clean_text(alias)
        for key in (raw, normalize_augment_id(raw), normalize_augment_name(raw)):
            if key:
                name_index.setdefault(key, augment_id)
    name = _clean_text(hint.get("name"))
    if name:
        name_index.setdefault(normalize_augment_id(name), augment_id)
        name_index.setdefault(normalize_augment_name(name), augment_id)
        name_index.setdefault(name, augment_id)


def _catalog_display_augment_id(entry: Mapping[str, Any], name: str) -> str:
    """正数 CDragon ID 优先；无效占位 ID 退回名称 ID，避免多个 -1 条目互相覆盖。"""

    cdragon_id = _clean_text(entry.get("cdragon_id") or entry.get("cdragonId"))
    try:
        if cdragon_id and int(float(cdragon_id)) > 0:
            return normalize_augment_id(cdragon_id, name)
    except ValueError:
        pass
    for key in ("augment_id", "augmentId", "augment_name_id", "augmentNameId"):
        candidate = normalize_augment_id(entry.get(key), name)
        if candidate:
            return candidate
    return normalize_augment_id(name)


def _merge_display_only_catalog_hints(
    cache_payload: dict[str, Any],
    catalog_lookup: Mapping[str, Any],
) -> int:
    """把 manifest 中有身份但 CSV 暂无统计的海克斯补进 hint cache。

    这些条目只负责名称、阶级、图标和别名解析，不伪造胜率/出场率；renderer
    仍会对当前英雄显示 NO_STATS，避免把源数据覆盖缺口误报成 alias 断链。
    """

    hints = cache_payload.get("hints")
    name_index = cache_payload.get("name_index")
    if not isinstance(hints, dict) or not isinstance(name_index, dict):
        return 0

    seen_ids: set[str] = set()
    added = 0
    for entry in catalog_lookup.values():
        if not isinstance(entry, Mapping):
            continue
        name = _clean_text(entry.get("name"))
        if not name:
            continue
        augment_id = _catalog_display_augment_id(entry, name)
        if not augment_id or augment_id in seen_ids:
            continue
        seen_ids.add(augment_id)
        card = {
            "海克斯ID": augment_id,
            "海克斯名称": name,
            "海克斯阶级": _clean_text(entry.get("tier")),
            "tooltip_plain": _clean_text(entry.get("tooltip_plain")),
            "summary": _clean_text(entry.get("tooltip_plain") or entry.get("description")),
            "icon": _clean_text(entry.get("icon_url")),
            "augment_name_id": _clean_text(entry.get("augment_name_id")),
            "cdragon_id": _clean_text(entry.get("cdragon_id")),
        }
        hint = _build_hint(card, "", include_private_stats=False)
        if not hint:
            continue
        existing = hints.get(hint["augment_id"])
        if isinstance(existing, dict):
            _merge_hint(existing, hint)
            _index_hint_aliases(name_index, existing)
            continue
        hints[hint["augment_id"]] = hint
        _index_hint_aliases(name_index, hint)
        added += 1
    return added


def _build_hint(
    card: Mapping[str, Any],
    hero_name: str,
    *,
    include_private_stats: bool,
    synergy_by_name: Mapping[str, list[dict[str, Any]]] | None = None,
    hero_id: str = "",
) -> dict[str, Any] | None:
    name = _clean_text(card.get("海克斯名称") or card.get("name"))
    augment_id = normalize_augment_id(card.get("海克斯ID") or card.get("augment_id"), name)
    if not augment_id or not name:
        return None

    resolved_hero_name = _hero_name_from_card(card, hero_name)
    resolved_hero_id = _hero_id_from_card(card, hero_id)
    tier = _clean_text(card.get("海克斯阶级") or card.get("tier") or "Unknown") or "Unknown"
    hint: dict[str, Any] = {
        "augment_id": augment_id,
        "name": name,
        "tier": tier,
        "summary": _summary_from_card(card),
        "condition": _clean_text(card.get("condition") or "根据当前英雄与海克斯机制判断"),
        "tags": [tier],
        "icon": _clean_text(card.get("icon")),
        "source_heroes": [resolved_hero_name] if resolved_hero_name else [],
    }
    aliases = _card_aliases(card)
    if aliases:
        hint["aliases"] = aliases

    if include_private_stats:
        stats = _private_stats_from_card(card, hero_id=resolved_hero_id, hero_name=resolved_hero_name)
        for key in ("rank", "score", "winrate", "pickrate"):
            if key in stats:
                hint[key] = stats[key]
        if stats:
            if resolved_hero_id:
                hint["stats_by_champion_id"] = {resolved_hero_id: dict(stats)}
            if resolved_hero_name:
                hint["stats_by_champion_name"] = {resolved_hero_name: dict(stats)}

    if synergy_by_name:
        matched = synergy_by_name.get(name) or synergy_by_name.get(normalize_augment_name(name))
        if matched:
            hint["synergies"] = [dict(item) for item in matched]
    return hint


def build_overlay_hint_cache(
    hextech_by_hero: Mapping[str, Any],
    *,
    include_private_stats: bool = False,
    source_tag: str = "",
    synergy_by_name: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    synergy_snapshot_path: str | Path | None = None,
    champion_id_by_name: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """从已加载的本地海克斯 payload 生成 overlay hint cache。"""

    if synergy_by_name is None:
        synergy_index = _load_synergy_by_augment_name(synergy_snapshot_path)
    else:
        synergy_index: dict[str, list[dict[str, Any]]] = {}
        for name, items in synergy_by_name.items():
            normalized_items = [dict(entry) for entry in items if isinstance(entry, Mapping)]
            raw_name = str(name)
            if raw_name:
                synergy_index.setdefault(raw_name, []).extend(normalized_items)
            normalized_name = normalize_augment_name(raw_name)
            if normalized_name and normalized_name != raw_name:
                synergy_index.setdefault(normalized_name, []).extend(normalized_items)

    hints: dict[str, dict[str, Any]] = {}
    name_index: dict[str, str] = {}
    hero_id_index = {str(name): str(value or "").strip() for name, value in (champion_id_by_name or {}).items()}
    for hero_name, card in _iter_hextech_cards(hextech_by_hero):
        hint = _build_hint(
            card,
            hero_name,
            include_private_stats=include_private_stats,
            synergy_by_name=synergy_index,
            hero_id=hero_id_index.get(hero_name, ""),
        )
        if not hint:
            continue
        existing = hints.get(hint["augment_id"])
        if existing:
            _merge_hint(existing, hint)
            _index_hint_aliases(name_index, existing)
            continue
        hints[hint["augment_id"]] = hint
        _index_hint_aliases(name_index, hint)

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


def _build_overlay_hint_cache_from_latest_runtime_csv(
    *,
    include_private_stats: bool,
    source_tag: str,
) -> dict[str, Any] | None:
    """优先复用 Web/UI 的最新 CSV 数据源构建 overlay cache。

    旧 overlay 入口只读预计算详情缓存；该缓存可能落后于最新胜率 CSV，导致
    Web 已有统计但游戏内 overlay 查不到。这里不写 precomputed，只按 Web 相同
    视图适配器即时收口一份轻量 hint cache；失败时由调用方降级到旧缓存。
    """

    from hextech.catalog import runtime_store
    from hextech.scraping.augment_catalog import load_augment_catalog_lookup_read_only
    from hextech.scraping.icon_resolver import build_local_augment_icon_url

    latest_csv = runtime_store.get_latest_csv()
    if not latest_csv:
        return None
    try:
        df = runtime_store.load_runtime_csv(latest_csv)
    except Exception:
        return None
    if df.empty or "英雄名称" not in df.columns:
        return None

    catalog_lookup = load_augment_catalog_lookup_read_only()
    hextech_by_hero: dict[str, Any] = {}
    champion_id_by_name: dict[str, str] = {}
    for _, row in df.iterrows():
        clean_hero_name = _clean_text(row.get("英雄名称"))
        if not clean_hero_name:
            continue
        augment_name = _clean_text(row.get("海克斯名称"))
        if not augment_name:
            continue
        card = dict(row.to_dict())
        catalog_entry = catalog_lookup.get(augment_name) or catalog_lookup.get(normalize_augment_name(augment_name))
        if isinstance(catalog_entry, Mapping):
            card.setdefault("tooltip_plain", _clean_text(catalog_entry.get("tooltip_plain")))
            card.setdefault("海克斯描述", _clean_text(catalog_entry.get("description") or catalog_entry.get("tooltip")))
            card.setdefault("summary", _clean_text(catalog_entry.get("tooltip_plain") or catalog_entry.get("description")))
            for alias_key in ("augment_name_id", "cdragon_id"):
                if catalog_entry.get(alias_key):
                    card[alias_key] = _clean_text(catalog_entry.get(alias_key))
            if catalog_entry.get("tier"):
                card["海克斯阶级"] = _clean_text(catalog_entry.get("tier"))
            card["icon"] = _clean_text(catalog_entry.get("icon_url")) or build_local_augment_icon_url(augment_name)
        else:
            card["icon"] = build_local_augment_icon_url(augment_name)
        bucket = hextech_by_hero.setdefault(clean_hero_name, {"comprehensive": []})
        if isinstance(bucket, dict):
            bucket.setdefault("comprehensive", []).append(card)
        for key in ("英雄 ID", "英雄ID", "champion_id", "heroId"):
            if key in row.index:
                hero_id = _clean_text(row.get(key))
                if hero_id:
                    champion_id_by_name[clean_hero_name] = hero_id
                break

    if not hextech_by_hero:
        return None
    cache_payload = build_overlay_hint_cache(
        hextech_by_hero,
        include_private_stats=include_private_stats,
        source_tag=source_tag,
        champion_id_by_name=champion_id_by_name,
    )
    if not cache_payload["hints"]:
        return None
    display_hint_count = _merge_display_only_catalog_hints(cache_payload, catalog_lookup)
    cache_payload["source"].update(
        {
            "data_source": "runtime-csv",
            "runtime_csv": Path(latest_csv).name,
            "hero_count": len(hextech_by_hero),
            "catalog_display_hint_count": display_hint_count,
        }
    )
    return cache_payload


def build_overlay_hint_cache_from_precomputed(
    *,
    include_private_stats: bool = False,
    source_tag: str = "precomputed",
) -> dict[str, Any]:
    """生成 overlay hint cache；优先采用最新 CSV，旧预计算缓存仅作降级。"""

    latest_payload = _build_overlay_hint_cache_from_latest_runtime_csv(
        include_private_stats=include_private_stats,
        source_tag=source_tag,
    )
    if latest_payload is not None:
        return latest_payload

    from hextech.catalog.precomputed_cache import (
        load_precomputed_champion_list,
        load_precomputed_hextech_for_hero,
        warm_precomputed_hextech_cache,
    )

    hextech_by_hero: dict[str, Any] = {}
    champion_id_by_name: dict[str, str] = {}
    if not warm_precomputed_hextech_cache():
        # overlay 是选择阶段实时展示，不能在启动路径里同步重建 170+ 英雄详情。
        # 正式预计算 cache stale 时，仅降级消费本地已有快照；是否刷新仍交给后台流程。
        stale_champions, stale_hextech_by_hero = _load_stale_precomputed_payloads()
        if stale_hextech_by_hero:
            if stale_champions:
                for champion in stale_champions:
                    hero_name = _clean_text(champion.get("英雄名称") or champion.get("hero_name") or champion.get("name"))
                    hero_id = _clean_text(champion.get("英雄 ID") or champion.get("英雄ID") or champion.get("champion_id") or champion.get("heroId"))
                    if hero_name and hero_id:
                        champion_id_by_name[hero_name] = hero_id
                    payload = stale_hextech_by_hero.get(hero_name)
                    if isinstance(payload, Mapping):
                        hextech_by_hero[hero_name] = payload
            else:
                hextech_by_hero = {
                    hero_name: payload
                    for hero_name, payload in stale_hextech_by_hero.items()
                    if isinstance(payload, Mapping)
                }
        if not hextech_by_hero:
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
    else:
        for champion in load_precomputed_champion_list():
            if not isinstance(champion, Mapping):
                continue
            hero_name = _clean_text(champion.get("英雄名称") or champion.get("hero_name") or champion.get("name"))
            if not hero_name:
                continue
            hero_id = _clean_text(champion.get("英雄 ID") or champion.get("英雄ID") or champion.get("champion_id") or champion.get("heroId"))
            if hero_id:
                champion_id_by_name[hero_name] = hero_id
            payload = load_precomputed_hextech_for_hero(hero_name)
            if isinstance(payload, Mapping):
                hextech_by_hero[hero_name] = payload

    cache_payload = build_overlay_hint_cache(
        hextech_by_hero,
        include_private_stats=include_private_stats,
        source_tag=source_tag,
        champion_id_by_name=champion_id_by_name,
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
            raw_key = str(augment_id or "").strip()
            hinted_id = name_index.get(normalized_id) or name_index.get(normalize_augment_name(raw_key)) or name_index.get(raw_key)
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

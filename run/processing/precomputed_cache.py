from __future__ import annotations

"""预计算缓存兼容桥。"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

from processing.processed_writer import (
    AUGMENTS_LIST_VIEW_FILE,
    AUGMENT_ICON_LOOKUP_FILE,
    BUNDLE_META_FILE,
    CHAMPION_ALIAS_INDEX_FILE,
    CHAMPION_ALIAS_LOOKUP_FILE,
    CHAMPION_DETAIL_ROUTE_FILE,
    CHAMPION_DETAIL_VIEW_FILE,
    CHAMPION_DETAILS_DIR,
    CHAMPION_ID_NAME_FILE,
    CHAMPION_LIST_CACHE_FILE,
    CHAMPIONS_LIST_VIEW_FILE,
    HEXTECH_DETAIL_CACHE_FILE,
    SYNERGY_BY_CHAMPION_FILE,
    build_processed_outputs_from_legacy,
)
from processing.runtime_store import get_latest_csv, normalize_runtime_df
from processing.view_adapter import process_champions_data, process_hextechs_data

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CHAMPION_CACHE_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": []}
_HEXTECH_CACHE_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_LIST_VIEW_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": []}
_DETAIL_VIEW_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_SYNERGY_VIEW_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_ALIAS_RECORD_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": []}
_ALIAS_LOOKUP_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_DETAIL_ROUTE_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_ID_NAME_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_AUGMENT_ICON_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_AUGMENTS_VIEW_STATE: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {"items": []}}
_BUNDLE_META_STATE: Dict[str, Any] = {
    "path": "",
    "mtime": 0.0,
    "data": {"version": 1, "generatedAt": "", "source": {}},
}

VIEW_TO_LEGACY_SECTION = {
    "comprehensive": "comprehensive",
    "winrateOnly": "winrate_only",
    "Prismatic": "Prismatic",
    "Gold": "Gold",
    "Silver": "Silver",
}
SYNERGY_EMPTY_PAYLOAD = {"synergies": []}
META_EMPTY_PAYLOAD = {"version": 1, "generatedAt": "", "source": {}}


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def _read_wrapped_data(path: str, default: Any) -> Any:
    payload = _read_json(path, default)
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data", default)
    return payload


def _read_wrapped_items(path: str, default: Any) -> Any:
    payload = _read_json(path, default)
    if isinstance(payload, dict) and "items" in payload:
        return payload.get("items", default)
    return payload


def _load_cached(path: str, state: Dict[str, Any], default: Any, reader) -> Any:
    with _CACHE_LOCK:
        mtime = _safe_mtime(path)
        if mtime and state["path"] == path and state["mtime"] == mtime:
            data = state["data"]
        else:
            data = reader(path, default)
            state.update({"path": path, "mtime": mtime, "data": data})

    if isinstance(default, list):
        return list(data) if isinstance(data, list) else list(default)
    if isinstance(default, dict):
        return dict(data) if isinstance(data, dict) else dict(default)
    return data


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
            for item in comprehensive_items[:10]
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


def load_processed_champions_list() -> dict:
    items = _load_cached(CHAMPIONS_LIST_VIEW_FILE, _LIST_VIEW_STATE, [], _read_wrapped_items)
    return {"items": items if isinstance(items, list) else []}


def load_processed_champion_detail_map() -> dict:
    items = _load_cached(CHAMPION_DETAIL_VIEW_FILE, _DETAIL_VIEW_STATE, {}, _read_wrapped_items)
    return items if isinstance(items, dict) else {}


def _load_processed_detail_shard(hero_id: str) -> Optional[dict]:
    normalized = str(hero_id or "").strip()
    if not normalized:
        return None
    payload = _read_json(os.path.join(CHAMPION_DETAILS_DIR, f"{normalized}.json"), {})
    if isinstance(payload, dict) and isinstance(payload.get("item"), dict):
        return payload.get("item")
    return None


def load_processed_champion_detail(hero_name: str) -> Optional[dict]:
    normalized = str(hero_name or "").strip()
    if not normalized:
        return None

    detail_routes = load_champion_detail_routes()
    alias_lookup = load_champion_alias_lookup()
    hero_id = alias_lookup.get(normalized.lower(), "")
    if hero_id:
        shard_payload = _load_processed_detail_shard(hero_id)
        if isinstance(shard_payload, dict):
            return shard_payload
        routed_name = str(detail_routes.get(hero_id, "")).strip()
        if routed_name:
            normalized = routed_name

    payload = load_processed_champion_detail_map().get(normalized)
    return payload if isinstance(payload, dict) else None


def load_processed_champion_detail_by_id(hero_id: str) -> Optional[dict]:
    shard_payload = _load_processed_detail_shard(hero_id)
    if isinstance(shard_payload, dict):
        return shard_payload
    routed_name = str(load_champion_detail_routes().get(str(hero_id).strip(), "")).strip()
    if routed_name:
        return load_processed_champion_detail(routed_name)
    return None


def load_precomputed_hextech_for_hero_id(hero_id: str) -> Optional[dict]:
    detail_payload = load_processed_champion_detail_by_id(hero_id)
    if isinstance(detail_payload, dict):
        return _to_legacy_detail_payload(detail_payload)
    routed_name = str(load_champion_detail_routes().get(str(hero_id).strip(), "")).strip()
    if routed_name:
        return _load_legacy_hextech_cache_for_hero(routed_name)
    return None




def load_processed_synergy_by_champion() -> dict:
    items = _load_cached(SYNERGY_BY_CHAMPION_FILE, _SYNERGY_VIEW_STATE, {}, _read_wrapped_items)
    return items if isinstance(items, dict) else {}


def load_processed_synergy_for_champion(champ_id: str) -> dict:
    normalized = str(champ_id or "").strip()
    if not normalized:
        return dict(SYNERGY_EMPTY_PAYLOAD)
    payload = load_processed_synergy_by_champion().get(normalized)
    return dict(payload) if isinstance(payload, dict) else dict(SYNERGY_EMPTY_PAYLOAD)


def load_processed_alias_records() -> list[dict]:
    payload = _load_cached(CHAMPION_ALIAS_INDEX_FILE, _ALIAS_RECORD_STATE, [], _read_json)
    return payload if isinstance(payload, list) else []


def load_champion_alias_lookup() -> dict:
    payload = _load_cached(CHAMPION_ALIAS_LOOKUP_FILE, _ALIAS_LOOKUP_STATE, {}, _read_json)
    return payload if isinstance(payload, dict) else {}


def load_champion_detail_routes() -> dict:
    payload = _load_cached(CHAMPION_DETAIL_ROUTE_FILE, _DETAIL_ROUTE_STATE, {}, _read_json)
    return payload if isinstance(payload, dict) else {}


def load_champion_id_name_map() -> dict:
    payload = _load_cached(CHAMPION_ID_NAME_FILE, _ID_NAME_STATE, {}, _read_json)
    return payload if isinstance(payload, dict) else {}


def load_augment_icon_lookup() -> dict:
    payload = _load_cached(AUGMENT_ICON_LOOKUP_FILE, _AUGMENT_ICON_STATE, {}, _read_json)
    return payload if isinstance(payload, dict) else {}


def load_processed_augments_list() -> dict:
    payload = _load_cached(AUGMENTS_LIST_VIEW_FILE, _AUGMENTS_VIEW_STATE, {"items": []}, _read_json)
    if isinstance(payload, dict) and "items" in payload:
        return payload
    if isinstance(payload, list):
        return {"items": payload}
    return {"items": []}


def load_bundle_meta() -> dict:
    payload = _load_cached(BUNDLE_META_FILE, _BUNDLE_META_STATE, META_EMPTY_PAYLOAD, _read_json)
    return payload if isinstance(payload, dict) else dict(META_EMPTY_PAYLOAD)


def resolve_champion_detail_name(query: str) -> str:
    normalized = str(query or "").strip()
    if not normalized:
        return ""
    detail_routes = load_champion_detail_routes()
    alias_lookup = load_champion_alias_lookup()
    hero_id = alias_lookup.get(normalized.lower())
    if hero_id and hero_id in detail_routes:
        return str(detail_routes.get(hero_id, "")).strip()
    if normalized in load_processed_champion_detail_map():
        return normalized
    return normalized


def resolve_synergy_key(query: str) -> str:
    normalized = str(query or "").strip()
    if not normalized:
        return ""
    if normalized.isdigit():
        return normalized
    alias_lookup = load_champion_alias_lookup()
    return str(alias_lookup.get(normalized.lower(), normalized)).strip()


def _load_legacy_champion_cache_list() -> List[dict]:
    return _load_cached(CHAMPION_LIST_CACHE_FILE, _CHAMPION_CACHE_STATE, [], _read_wrapped_data)


def _load_legacy_hextech_cache_for_hero(hero_name: str) -> Optional[dict]:
    payload = _load_cached(HEXTECH_DETAIL_CACHE_FILE, _HEXTECH_CACHE_STATE, {}, _read_wrapped_data)
    result = payload.get(str(hero_name or "").strip()) if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else None


def load_legacy_champion_list() -> List[dict]:
    processed = load_processed_champions_list().get("items", [])
    legacy_items = [dict(item.get("legacy", {})) for item in processed if isinstance(item, dict)]
    if legacy_items:
        return legacy_items
    return _load_legacy_champion_cache_list()


def load_legacy_hextech_payload(hero_name: str) -> Optional[dict]:
    detail_payload = load_processed_champion_detail(hero_name)
    if isinstance(detail_payload, dict):
        return _to_legacy_detail_payload(detail_payload)
    return _load_legacy_hextech_cache_for_hero(hero_name)


def load_precomputed_champion_list() -> List[dict]:
    return load_legacy_champion_list()


def load_precomputed_hextech_for_hero(hero_name: str) -> Optional[dict]:
    return load_legacy_hextech_payload(hero_name)


def has_precomputed_hextech_cache() -> bool:
    return bool(load_processed_champion_detail_map()) or bool(_safe_mtime(HEXTECH_DETAIL_CACHE_FILE))


def rebuild_precomputed_api_cache_from_latest_csv() -> bool:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return False

    try:
        df = normalize_runtime_df(pd.read_csv(latest_csv))
    except Exception as exc:
        logger.warning("读取最新 CSV 失败，无法重建本地 API 缓存：%s", exc)
        return False

    if df.empty or "英雄名称" not in df.columns:
        return False

    champions = process_champions_data(df, use_runtime_cache=False, log_columns=False)
    try:
        from scraping.augment_catalog import build_augment_catalog_lookup
        catalog_lookup = build_augment_catalog_lookup()
    except Exception:
        catalog_lookup = {}

    hextech_by_hero: Dict[str, dict] = {}
    for hero_name, group in df.groupby("英雄名称", sort=False):
        if pd.isna(hero_name):
            continue
        hextech_by_hero[str(hero_name)] = process_hextechs_data(
            normalize_runtime_df(group.copy()),
            str(hero_name),
            catalog_lookup=catalog_lookup,
            use_runtime_cache=False,
            log_columns=False,
        )

    if not champions:
        return False

    build_processed_outputs_from_legacy(
        champions=champions,
        hextech_by_hero=hextech_by_hero,
        source_tag=os.path.basename(latest_csv),
        catalog_lookup=catalog_lookup,
    )
    clear_processed_cache_states()
    return True


def ensure_processed_views() -> bool:
    if processed_views_ready():
        return True
    return rebuild_precomputed_api_cache_from_latest_csv()


def ensure_processed_synergy() -> bool:
    if processed_synergy_ready():
        return True
    return ensure_processed_views()


def processed_views_ready() -> bool:
    return bool(load_processed_champions_list().get("items")) and bool(load_processed_champion_detail_map())


def processed_synergy_ready() -> bool:
    return bool(load_processed_synergy_by_champion())


def clear_processed_cache_states() -> None:
    with _CACHE_LOCK:
        for state in (
            _CHAMPION_CACHE_STATE,
            _HEXTECH_CACHE_STATE,
            _LIST_VIEW_STATE,
            _DETAIL_VIEW_STATE,
            _SYNERGY_VIEW_STATE,
            _ALIAS_RECORD_STATE,
            _ALIAS_LOOKUP_STATE,
            _DETAIL_ROUTE_STATE,
            _ID_NAME_STATE,
            _AUGMENT_ICON_STATE,
            _AUGMENTS_VIEW_STATE,
            _BUNDLE_META_STATE,
        ):
            state.update({
                "path": "",
                "mtime": 0.0,
                "data": [] if isinstance(state.get("data"), list) else {},
            })
        _AUGMENTS_VIEW_STATE.update({"data": {"items": []}})
        _BUNDLE_META_STATE.update({"data": dict(META_EMPTY_PAYLOAD)})


__all__ = [
    "load_precomputed_champion_list",
    "load_precomputed_hextech_for_hero",
    "load_precomputed_hextech_for_hero_id",
    "has_precomputed_hextech_cache",
    "rebuild_precomputed_api_cache_from_latest_csv",
    "load_processed_champions_list",
    "load_processed_champion_detail",
    "load_processed_champion_detail_by_id",
    "load_processed_champion_detail_map",
    "load_processed_synergy_for_champion",
    "load_processed_synergy_by_champion",
    "load_processed_alias_records",
    "load_champion_alias_lookup",
    "load_champion_detail_routes",
    "load_champion_id_name_map",
    "load_augment_icon_lookup",
    "load_processed_augments_list",
    "load_bundle_meta",
    "resolve_champion_detail_name",
    "resolve_synergy_key",
    "ensure_processed_views",
    "ensure_processed_synergy",
    "processed_views_ready",
    "processed_synergy_ready",
    "clear_processed_cache_states",
]

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from data_processor import process_champions_data, process_hextechs_data
from hero_sync import CONFIG_DIR, load_champion_core_data
from hextech_query import get_latest_csv

logger = logging.getLogger(__name__)

CHAMPION_LIST_CACHE_FILE = os.path.join(CONFIG_DIR, "Champion_List_Cache.json")
HEXTECH_DETAIL_CACHE_FILE = os.path.join(CONFIG_DIR, "Champion_Hextech_Cache.json")
HEXTECH_DETAIL_CACHE_DIR = os.path.join(CONFIG_DIR, "Champion_Hextech_Cache")

_cache_lock = threading.Lock()
_champion_cache_state: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": []}
_hextech_cache_state: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, path)


def _read_wrapped_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data", default)
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def load_precomputed_champion_list() -> List[dict]:
    with _cache_lock:
        mtime = _safe_mtime(CHAMPION_LIST_CACHE_FILE)
        if (
            mtime
            and _champion_cache_state["path"] == CHAMPION_LIST_CACHE_FILE
            and _champion_cache_state["mtime"] == mtime
        ):
            return list(_champion_cache_state["data"])

        data = _read_wrapped_json(CHAMPION_LIST_CACHE_FILE, [])
        if isinstance(data, list):
            _champion_cache_state.update(
                {"path": CHAMPION_LIST_CACHE_FILE, "mtime": mtime, "data": data}
            )
            return list(data)
        return []


def has_precomputed_hextech_cache() -> bool:
    return bool(_safe_mtime(HEXTECH_DETAIL_CACHE_FILE))


def load_precomputed_hextech_for_hero(hero_name: str) -> Optional[dict]:
    normalized = str(hero_name or "").strip()
    if not normalized:
        return None

    with _cache_lock:
        mtime = _safe_mtime(HEXTECH_DETAIL_CACHE_FILE)
        if (
            mtime
            and _hextech_cache_state["path"] == HEXTECH_DETAIL_CACHE_FILE
            and _hextech_cache_state["mtime"] == mtime
        ):
            payload = _hextech_cache_state["data"]
        else:
            payload = _read_wrapped_json(HEXTECH_DETAIL_CACHE_FILE, {})
            if isinstance(payload, dict):
                _hextech_cache_state.update(
                    {
                        "path": HEXTECH_DETAIL_CACHE_FILE,
                        "mtime": mtime,
                        "data": payload,
                    }
                )
            else:
                payload = {}

    result = payload.get(normalized) if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else None


def write_precomputed_champion_list(champions: List[dict], source_tag: str) -> None:
    payload = {
        "meta": {"generated_at": _now_iso(), "source": source_tag},
        "data": champions,
    }
    _atomic_write_json(CHAMPION_LIST_CACHE_FILE, payload)
    with _cache_lock:
        _champion_cache_state.update(
            {
                "path": CHAMPION_LIST_CACHE_FILE,
                "mtime": _safe_mtime(CHAMPION_LIST_CACHE_FILE),
                "data": list(champions),
            }
        )


def write_precomputed_hextech_map(hextech_by_hero: Dict[str, dict], source_tag: str) -> None:
    payload = {
        "meta": {"generated_at": _now_iso(), "source": source_tag},
        "data": hextech_by_hero,
    }
    _atomic_write_json(HEXTECH_DETAIL_CACHE_FILE, payload)
    if os.path.isdir(HEXTECH_DETAIL_CACHE_DIR):
        shutil.rmtree(HEXTECH_DETAIL_CACHE_DIR, ignore_errors=True)
    with _cache_lock:
        _hextech_cache_state.update(
            {
                "path": HEXTECH_DETAIL_CACHE_FILE,
                "mtime": _safe_mtime(HEXTECH_DETAIL_CACHE_FILE),
                "data": dict(hextech_by_hero),
            }
        )


def build_precomputed_champion_list_from_stats(
    stats_list: List[dict],
    core_data: Optional[Dict[str, dict]] = None,
) -> List[dict]:
    core = core_data or load_champion_core_data()
    rows: List[dict] = []
    for item in stats_list or []:
        if not isinstance(item, dict):
            continue
        champ_id = str(item.get("championId", "")).strip()
        hero_info = core.get(champ_id, {})
        hero_name = str(hero_info.get("name", "")).strip()
        if not hero_name:
            continue
        try:
            rows.append(
                {
                    "英雄名称": hero_name,
                    "英雄胜率": float(item.get("winRate", 0) or 0),
                    "英雄出场率": float(item.get("pickRate", 0) or 0),
                }
            )
        except (TypeError, ValueError):
            continue

    if not rows:
        return []

    df = pd.DataFrame(rows)
    return process_champions_data(df, use_runtime_cache=False, log_columns=False)


def build_precomputed_hextech_map_from_rows(
    rows_by_hero: Dict[str, List[dict]],
) -> Dict[str, dict]:
    from augment_icon_refresh import build_augment_catalog_lookup

    catalog_lookup = build_augment_catalog_lookup()
    result: Dict[str, dict] = {}
    for hero_name, rows in rows_by_hero.items():
        if not rows:
            continue
        df = pd.DataFrame(rows)
        payload = process_hextechs_data(
            df,
            hero_name,
            catalog_lookup=catalog_lookup,
            use_runtime_cache=False,
            log_columns=False,
        )
        result[str(hero_name)] = payload
    return result


def rebuild_precomputed_api_cache_from_latest_csv() -> bool:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return False

    try:
        df = pd.read_csv(latest_csv)
    except Exception as exc:
        logger.warning("读取最新 CSV 失败，无法重建本地 API 缓存：%s", exc)
        return False

    if df.empty or "英雄名称" not in df.columns:
        return False

    champions = process_champions_data(df, use_runtime_cache=False, log_columns=False)
    write_precomputed_champion_list(champions, os.path.basename(latest_csv))

    rows_by_hero: Dict[str, List[dict]] = {}
    for hero_name, group in df.groupby("英雄名称", sort=False):
        if pd.isna(hero_name):
            continue
        rows_by_hero[str(hero_name)] = group.to_dict("records")

    hextech_by_hero = build_precomputed_hextech_map_from_rows(rows_by_hero)
    if hextech_by_hero:
        write_precomputed_hextech_map(hextech_by_hero, os.path.basename(latest_csv))
    return bool(champions) and bool(hextech_by_hero)

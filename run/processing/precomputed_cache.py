from __future__ import annotations

"""预计算 API 缓存。

负责把最新 CSV 转换成首页榜单和单英雄海克斯详情缓存，
降低冷启动和无实时数据场景下的接口延迟。
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from processing.runtime_store import (
    build_runtime_cache_path,
    get_latest_csv,
    load_runtime_csv,
    normalize_runtime_df,
    resolve_runtime_data_file,
)
from processing.view_adapter import process_champions_data, process_hextechs_data

logger = logging.getLogger(__name__)
CHAMPION_LIST_CACHE_FILE = build_runtime_cache_path("Champion_List_Cache.json")
HEXTECH_DETAIL_CACHE_FILE = build_runtime_cache_path("Champion_Hextech_Cache.json")
HEXTECH_DETAIL_CACHE_DIR = build_runtime_cache_path("Champion_Hextech_Cache")

_cache_lock = threading.Lock()
_champion_cache_state: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": []}
_hextech_cache_state: Dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}
_cache_match_state: Dict[str, Dict[str, Any]] = {}


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


def _resolve_cache_file(path: str, legacy_relative_name: str) -> str:
    return resolve_runtime_data_file(path, legacy_relative_name) or path


def _read_wrapped_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data", default)
        return payload
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def _read_cache_payload(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _latest_csv_signature() -> dict:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return {"source": "", "source_mtime": 0.0}
    return {
        "source": os.path.basename(latest_csv),
        "source_mtime": _safe_mtime(latest_csv),
    }


def _cache_matches_latest_csv(cache_file: str) -> bool:
    cache_mtime = _safe_mtime(cache_file)
    latest = _latest_csv_signature()
    if not cache_mtime or not latest["source"]:
        return False

    cached = _cache_match_state.get(cache_file)
    if (
        cached
        and cached.get("cache_mtime") == cache_mtime
        and cached.get("source") == latest["source"]
        and float(cached.get("source_mtime") or 0.0) == float(latest["source_mtime"] or 0.0)
    ):
        return bool(cached.get("matches"))

    payload = _read_cache_payload(cache_file)
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    matches = (
        isinstance(meta, dict)
        and meta.get("source") == latest["source"]
        and float(meta.get("source_mtime") or 0.0) == float(latest["source_mtime"] or 0.0)
    )
    _cache_match_state[cache_file] = {
        "cache_mtime": cache_mtime,
        "source": latest["source"],
        "source_mtime": latest["source_mtime"],
        "matches": matches,
    }
    return bool(matches)


def load_precomputed_champion_list() -> List[dict]:
    with _cache_lock:
        cache_file = _resolve_cache_file(CHAMPION_LIST_CACHE_FILE, "Champion_List_Cache.json")
        mtime = _safe_mtime(cache_file)
        if mtime and not _cache_matches_latest_csv(cache_file):
            _champion_cache_state.update({"path": cache_file, "mtime": 0.0, "data": []})
            return []
        if (
            mtime
            and _champion_cache_state["path"] == cache_file
            and _champion_cache_state["mtime"] == mtime
        ):
            return list(_champion_cache_state["data"])

        data = _read_wrapped_json(cache_file, [])
        if isinstance(data, list):
            _champion_cache_state.update(
                {"path": cache_file, "mtime": mtime, "data": data}
            )
            return list(data)
        return []


def has_precomputed_hextech_cache() -> bool:
    cache_file = _resolve_cache_file(HEXTECH_DETAIL_CACHE_FILE, "Champion_Hextech_Cache.json")
    return bool(_safe_mtime(cache_file) and _cache_matches_latest_csv(cache_file))


def is_precomputed_hextech_cache_loaded() -> bool:
    """只检查进程内详情缓存是否已暖好，不触发磁盘 JSON 读取。"""
    return bool(_hextech_cache_state.get("data"))


def warm_precomputed_hextech_cache() -> bool:
    """后台暖机整份详情缓存，避免首个详情请求承担大 JSON 读取。"""
    with _cache_lock:
        cache_file = _resolve_cache_file(HEXTECH_DETAIL_CACHE_FILE, "Champion_Hextech_Cache.json")
        mtime = _safe_mtime(cache_file)
        if not mtime or not _cache_matches_latest_csv(cache_file):
            _hextech_cache_state.update({"path": cache_file, "mtime": 0.0, "data": {}})
            return False
        if _hextech_cache_state["path"] == cache_file and _hextech_cache_state["mtime"] == mtime:
            return bool(_hextech_cache_state["data"])
        payload = _read_wrapped_json(cache_file, {})
        if isinstance(payload, dict):
            _hextech_cache_state.update({"path": cache_file, "mtime": mtime, "data": payload})
            return bool(payload)
        _hextech_cache_state.update({"path": cache_file, "mtime": 0.0, "data": {}})
        return False


def load_precomputed_hextech_for_hero(hero_name: str) -> Optional[dict]:
    normalized = str(hero_name or "").strip()
    if not normalized:
        return None

    with _cache_lock:
        cache_file = _resolve_cache_file(HEXTECH_DETAIL_CACHE_FILE, "Champion_Hextech_Cache.json")
        mtime = _safe_mtime(cache_file)
        if mtime and not _cache_matches_latest_csv(cache_file):
            _hextech_cache_state.update({"path": cache_file, "mtime": 0.0, "data": {}})
            return None
        if (
            mtime
            and _hextech_cache_state["path"] == cache_file
            and _hextech_cache_state["mtime"] == mtime
        ):
            payload = _hextech_cache_state["data"]
        else:
            payload = _read_wrapped_json(cache_file, {})
            if isinstance(payload, dict):
                _hextech_cache_state.update(
                    {"path": cache_file, "mtime": mtime, "data": payload}
                )
            else:
                payload = {}

    result = payload.get(normalized) if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else None


def write_precomputed_champion_list(champions: List[dict], source_tag: str) -> None:
    signature = _latest_csv_signature()
    _atomic_write_json(
        CHAMPION_LIST_CACHE_FILE,
        {
            "meta": {
                "generated_at": _now_iso(),
                "source": signature["source"] or source_tag,
                "source_mtime": signature["source_mtime"],
            },
            "data": champions,
        },
    )


def write_precomputed_hextech_map(hextech_by_hero: Dict[str, dict], source_tag: str) -> None:
    signature = _latest_csv_signature()
    _atomic_write_json(
        HEXTECH_DETAIL_CACHE_FILE,
        {
            "meta": {
                "generated_at": _now_iso(),
                "source": signature["source"] or source_tag,
                "source_mtime": signature["source_mtime"],
            },
            "data": hextech_by_hero,
        },
    )
    if os.path.isdir(HEXTECH_DETAIL_CACHE_DIR):
        shutil.rmtree(HEXTECH_DETAIL_CACHE_DIR, ignore_errors=True)


def rebuild_precomputed_api_cache_from_latest_csv() -> bool:
    latest_csv = get_latest_csv()
    if not latest_csv or not os.path.exists(latest_csv):
        return False

    try:
        df = load_runtime_csv(latest_csv)
    except Exception as exc:
        logger.warning("读取最新 CSV 失败，无法重建本地 API 缓存：%s", exc)
        return False

    if df.empty or "英雄名称" not in df.columns:
        return False

    champions = process_champions_data(df, use_runtime_cache=False, log_columns=False)
    write_precomputed_champion_list(champions, os.path.basename(latest_csv))

    from scraping.augment_catalog import build_augment_catalog_lookup

    catalog_lookup = build_augment_catalog_lookup()
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

    if hextech_by_hero:
        write_precomputed_hextech_map(hextech_by_hero, os.path.basename(latest_csv))
    return True

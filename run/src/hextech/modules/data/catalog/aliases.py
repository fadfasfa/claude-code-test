"""首页搜索专用的英雄别名索引读取与归一。

调用方: catalog.query_terminal、display.web.api、tooling.checks.dev; 关键依赖: alias_utils、runtime_store、version_catalog。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Dict, List, Optional

from hextech.modules.data.catalog.alias_utils import dedupe_alias_texts, normalize_alias_token
from hextech.modules.data.catalog.runtime_store import (
    build_runtime_user_preference_path,
    ensure_private_runtime_dir,
    get_runtime_root_dir,
)
from hextech.modules.data.catalog.version_catalog import HERO_CATALOG_FILENAME, get_hero_catalog_path, load_champion_alias_records

_ALIAS_INDEX_CACHE: tuple[str, float, list[dict]] = ("", 0.0, [])
CHAMPION_ALIAS_INDEX_FILE = str(get_hero_catalog_path())
RUNTIME_ALIAS_FILE = build_runtime_user_preference_path("aliases.json")
_DEFAULT_RUNTIME_ALIAS_FILE = RUNTIME_ALIAS_FILE
_LEGACY_RUNTIME_ALIAS_FILE = str(get_runtime_root_dir() / "snapshots" / "aliases.json")
_RUNTIME_ALIAS_LOCK = threading.Lock()


def _normalize_record(record: dict) -> dict:
    hero_name = str(record.get("heroName") or record.get("title") or "").strip()
    title = str(record.get("title") or "").strip()
    en_name = str(record.get("enName") or record.get("en_name") or "").strip()
    hero_id = str(record.get("heroId") or record.get("id") or "").strip()
    aliases = dedupe_alias_texts(
        record.get("aliases", []),
        excluded_tokens=[hero_name, title, en_name, hero_id],
    )
    return {
        "heroName": hero_name,
        "title": title,
        "enName": en_name,
        "heroId": hero_id,
        "aliases": aliases,
    }


def _load_json_file(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _load_stable_alias_index() -> tuple[float, list[dict]]:
    stable_path = CHAMPION_ALIAS_INDEX_FILE
    if not os.path.exists(stable_path):
        return 0.0, []
    current_mtime = os.path.getmtime(stable_path)
    if os.path.basename(stable_path) == HERO_CATALOG_FILENAME:
        payload = load_champion_alias_records(os.path.dirname(stable_path))
    else:
        payload = _load_json_file(stable_path)
        if not isinstance(payload, list):
            payload = []
    return current_mtime, [_normalize_record(item) for item in payload if isinstance(item, dict)]


def _coerce_runtime_alias_payload(payload) -> list[dict]:
    if isinstance(payload, dict):
        raw_records = payload.get("aliases", [])
    elif isinstance(payload, list):
        raw_records = payload
    else:
        raw_records = []
    if not isinstance(raw_records, list):
        return []
    return [_normalize_record(item) for item in raw_records if isinstance(item, dict)]


def _active_runtime_alias_file() -> str:
    """一次性迁移旧 snapshot 根文件；测试覆盖可继续替换公开路径常量。"""

    if RUNTIME_ALIAS_FILE != _DEFAULT_RUNTIME_ALIAS_FILE or os.path.exists(RUNTIME_ALIAS_FILE):
        return RUNTIME_ALIAS_FILE
    if not os.path.isfile(_LEGACY_RUNTIME_ALIAS_FILE):
        return RUNTIME_ALIAS_FILE
    ensure_private_runtime_dir(os.path.dirname(RUNTIME_ALIAS_FILE))
    try:
        os.replace(_LEGACY_RUNTIME_ALIAS_FILE, RUNTIME_ALIAS_FILE)
        return RUNTIME_ALIAS_FILE
    except OSError:
        return _LEGACY_RUNTIME_ALIAS_FILE


def _load_runtime_alias_index() -> tuple[float, list[dict]]:
    active_file = _active_runtime_alias_file()
    if not os.path.exists(active_file):
        return 0.0, []
    current_mtime = os.path.getmtime(active_file)
    return current_mtime, _coerce_runtime_alias_payload(_load_json_file(active_file))


def _merge_alias_records(stable_records: list[dict], runtime_records: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for record in [*stable_records, *runtime_records]:
        hero_name = str(record.get("heroName", "")).strip()
        if not hero_name:
            continue
        if hero_name not in merged:
            merged[hero_name] = dict(record)
            order.append(hero_name)
            continue
        current = merged[hero_name]
        current["title"] = current.get("title") or record.get("title", "")
        current["enName"] = current.get("enName") or record.get("enName", "")
        current["heroId"] = current.get("heroId") or record.get("heroId", "")
        current["aliases"] = dedupe_alias_texts(
            current.get("aliases", []),
            record.get("aliases", []),
            excluded_tokens=[
                current.get("heroName", ""),
                current.get("title", ""),
                current.get("enName", ""),
                current.get("heroId", ""),
            ],
        )
    return [merged[name] for name in order]


def load_champion_alias_index(force_refresh: bool = False) -> list[dict]:
    """读取首页搜索专用别名索引，并合并运行态新增别名。"""
    global _ALIAS_INDEX_CACHE

    try:
        stable_mtime, stable_records = _load_stable_alias_index()
        runtime_mtime, runtime_records = _load_runtime_alias_index()
        cache_key = f"{CHAMPION_ALIAS_INDEX_FILE}|{RUNTIME_ALIAS_FILE}"
        cache_mtime = max(stable_mtime, runtime_mtime)
        if (
            not force_refresh
            and _ALIAS_INDEX_CACHE[0] == cache_key
            and _ALIAS_INDEX_CACHE[1] == cache_mtime
            and _ALIAS_INDEX_CACHE[2]
        ):
            return _ALIAS_INDEX_CACHE[2]

        records = _merge_alias_records(stable_records, runtime_records)
        _ALIAS_INDEX_CACHE = (cache_key, cache_mtime, records)
        return records
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def add_runtime_champion_alias(record: dict, alias: str) -> bool:
    """把用户新增别名写入运行态持久化文件，不污染稳定 bundle 数据。"""
    global _ALIAS_INDEX_CACHE
    normalized_record = _normalize_record(record)
    hero_name = str(normalized_record.get("heroName", "")).strip()
    alias_text = str(alias or "").strip()
    if not hero_name or not alias_text:
        return False

    with _RUNTIME_ALIAS_LOCK:
        _, runtime_records = _load_runtime_alias_index()
        by_name = {str(item.get("heroName", "")).strip(): dict(item) for item in runtime_records if str(item.get("heroName", "")).strip()}
        current = by_name.get(hero_name, normalized_record)
        current["heroName"] = hero_name
        current["title"] = current.get("title") or normalized_record.get("title", "")
        current["enName"] = current.get("enName") or normalized_record.get("enName", "")
        current["heroId"] = current.get("heroId") or normalized_record.get("heroId", "")
        current["aliases"] = dedupe_alias_texts(
            current.get("aliases", []),
            [alias_text],
            excluded_tokens=[hero_name, current.get("title", ""), current.get("enName", ""), current.get("heroId", "")],
        )
        by_name[hero_name] = current
        payload = {
            "schema_version": 1,
            "aliases": sorted(by_name.values(), key=lambda item: str(item.get("heroName", ""))),
        }

        target_dir = ensure_private_runtime_dir(os.path.dirname(RUNTIME_ALIAS_FILE))
        fd, tmp_path = tempfile.mkstemp(prefix="aliases-", suffix=".tmp", dir=str(target_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, RUNTIME_ALIAS_FILE)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise
        _ALIAS_INDEX_CACHE = ("", 0.0, [])
        return True


def load_manual_alias_index(force_refresh: bool = False) -> list[dict]:
    return load_champion_alias_index(force_refresh=force_refresh)


def load_champion_alias_map(force_refresh: bool = False) -> Dict[str, List[str]]:
    """返回 `{英雄名: [别名...]}` 的映射，供首页搜索构建索引。"""
    records = load_champion_alias_index(force_refresh=force_refresh)
    return {
        str(record.get("heroName", "")).strip(): list(record.get("aliases", []))
        for record in records
        if str(record.get("heroName", "")).strip()
    }


def resolve_champion_record(query: str, force_refresh: bool = False) -> Optional[dict]:
    """按英雄名、称号、英文名、ID 或别名解析到索引记录。"""
    normalized_query = normalize_alias_token(query)
    if not normalized_query:
        return None

    records = load_champion_alias_index(force_refresh=force_refresh)
    exact_map: dict[str, dict] = {}
    fuzzy_candidates: list[tuple[int, dict]] = []

    for record in records:
        hero_name = str(record.get("heroName", "")).strip()
        title = str(record.get("title", "")).strip()
        en_name = str(record.get("enName", "")).strip()
        hero_id = str(record.get("heroId", "")).strip()
        aliases = list(record.get("aliases", []))

        tokens = dedupe_alias_texts([hero_name, title, en_name, hero_id], aliases)
        for token in tokens:
            normalized_token = normalize_alias_token(token)
            if not normalized_token:
                continue
            exact_map.setdefault(normalized_token, record)
            if normalized_query in normalized_token or normalized_token in normalized_query:
                fuzzy_candidates.append((len(normalized_token), record))

    if normalized_query in exact_map:
        return exact_map[normalized_query]

    if fuzzy_candidates:
        fuzzy_candidates.sort(key=lambda item: item[0])
        return fuzzy_candidates[0][1]

    return None


def resolve_champion_name(query: str, force_refresh: bool = False) -> Optional[str]:
    record = resolve_champion_record(query, force_refresh=force_refresh)
    if not record:
        return None
    return str(record.get("heroName", "")).strip() or None

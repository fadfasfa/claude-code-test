"""Web 服务路由层。

这个模块只负责定义页面路由、API 路由和 WebSocket 入口。
所有与端口、LCU、缓存、资源定位、浏览器托管相关的细节都委托给 `web_runtime`，
从而让接口层保持稳定且方便后续扩展。

调用方: display.web.app、dev_checks、verify_data_source_integrity; 关键依赖: catalog.aliases、catalog.precomputed_cache、catalog.runtime_store。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from hextech.catalog.aliases import load_manual_alias_index
from hextech.catalog.precomputed_cache import is_precomputed_hextech_cache_loaded, warm_precomputed_hextech_cache
from hextech.catalog.runtime_store import load_precomputed_hextech_for_hero
from hextech.catalog.version_catalog import (
    AUGMENT_RESOURCE_CATALOG_FILENAME,
    HERO_CATALOG_FILENAME,
    legacy_index_payload,
    legacy_static_payload,
)
from hextech.catalog.view_adapter import process_champions_data, process_hextechs_data
from hextech.core.refresh import rebuild_api_cache_if_needed
from hextech.scraping.augment_catalog import load_augment_icon_manifest
from hextech.scraping._paths import INDEX_DATA_DIR, STATIC_DATA_DIR
from . import runtime as web_runtime

_api_cache_rebuild_lock = threading.Lock()
_api_cache_rebuild_inflight = False
_api_cache_warm_lock = threading.Lock()
_api_cache_warm_inflight = False
_STATIC_DATA_FILE_ALLOWLIST = frozenset({
    HERO_CATALOG_FILENAME,
    AUGMENT_RESOURCE_CATALOG_FILENAME,
    "Champion_Synergy_Cleaned.json",
    "Champion_Core_Data.json",
    "Augment_Full_Map.json",
    "Augment_Icon_Map.json",
    "hero_version.txt",
})
_STATIC_DATA_LEGACY_ALLOWLIST = frozenset({
    "Augment_Apexlol_Map.json",
    "Augment_Icon_Manifest.json",
    "Champion_Alias_Index.json",
    "Champion_Core_Data.json",
})
_INDEX_DATA_LEGACY_ALLOWLIST = frozenset({
    "Champion_Alias_Index.json",
    "augment.name-to-icon.v1.json",
    "champion.alias-to-id.v1.json",
    "champion.id-to-detail.v1.json",
    "champion.id-to-name.v1.json",
})


def _normalize_synergy_entry(raw_entry: str) -> str:
    """把历史联动字符串归一成前端稳定解析格式。"""
    parts = [part.strip() for part in str(raw_entry or "").split("|")]
    if len(parts) < 8:
        return str(raw_entry or "")

    name, tier, grade, tag = parts[:4]
    fifth = parts[4]
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


def _synergy_item_to_compat_string(item: dict) -> str:
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


def _build_synergy_api_payload(data: dict, champ_id: str) -> dict:
    if not data:
        return {"synergies": [], "synergy_items": []}

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
        synergies = [_synergy_item_to_compat_string(item) for item in synergy_items]
    return {"synergies": synergies, "synergy_items": synergy_items}


class RedirectRequest(BaseModel):
    """前端点击英雄后发送的跳转请求体。"""

    hero_id: str
    hero_name: str


def _loading_hextech_payload() -> dict:
    payload = {
        "top_10_overall": [],
        "comprehensive": [],
        "winrate_only": [],
        "Prismatic": [],
        "Gold": [],
        "Silver": [],
        "loading": True,
        "ready": False,
        "startup_status": web_runtime.get_startup_status(),
    }
    return payload


def _attach_local_auth_cookie(response: Response) -> Response:
    response.set_cookie(
        key=web_runtime.HTTP_SESSION_COOKIE,
        value=web_runtime.get_request_auth_token(),
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return response


def _extract_request_token(request: Request) -> str:
    header_token = str(request.headers.get(web_runtime.REQUEST_TOKEN_HEADER, "")).strip()
    if header_token:
        return header_token
    return str(request.cookies.get(web_runtime.HTTP_SESSION_COOKIE, "")).strip()


def _safe_asset_response(assets_dir: str, filename: str):
    safe_path = web_runtime.safe_join_under_dir(assets_dir, filename)
    if not safe_path or not os.path.exists(safe_path):
        return JSONResponse(content={"error": "禁止访问"}, status_code=403)
    return FileResponse(safe_path)


def _route_file_name(filename: str) -> str:
    value = str(filename or "").strip()
    if not value or "/" in value or "\\" in value:
        return ""
    if os.path.basename(value) != value:
        return ""
    return value


def _stable_data_response(
    base_dir: str,
    filename: str,
    legacy_loader,
    *,
    allowed_files: frozenset[str] = frozenset(),
    legacy_files: frozenset[str] = frozenset(),
):
    route_name = _route_file_name(filename)
    if not route_name:
        return JSONResponse(content={"error": "禁止访问"}, status_code=403)

    safe_path = web_runtime.safe_join_under_dir(base_dir, route_name)
    if not safe_path:
        return JSONResponse(content={"error": "禁止访问"}, status_code=403)
    if route_name in allowed_files and os.path.exists(safe_path) and os.path.isfile(safe_path):
        return FileResponse(safe_path)
    if route_name in legacy_files:
        payload = legacy_loader(route_name, base_dir)
        if payload is not None:
            return JSONResponse(content=payload)
    return JSONResponse(content={"error": "资源未找到"}, status_code=404)


def _request_precomputed_hextech_rebuild() -> bool:
    global _api_cache_rebuild_inflight
    with _api_cache_rebuild_lock:
        if _api_cache_rebuild_inflight:
            return False
        _api_cache_rebuild_inflight = True

    def _worker() -> None:
        global _api_cache_rebuild_inflight
        try:
            rebuild_api_cache_if_needed(force=False)
        except Exception:
            web_runtime.logger.exception("预计算海克斯缓存后台重建失败。")
        finally:
            with _api_cache_rebuild_lock:
                _api_cache_rebuild_inflight = False

    threading.Thread(
        target=_worker,
        daemon=True,
        name="precomputed-hextech-cache-rebuild",
    ).start()
    return True


def _request_precomputed_hextech_warm() -> bool:
    global _api_cache_warm_inflight
    with _api_cache_warm_lock:
        if _api_cache_warm_inflight:
            return False
        _api_cache_warm_inflight = True

    def _worker() -> None:
        global _api_cache_warm_inflight
        try:
            warm_precomputed_hextech_cache()
        except Exception:
            web_runtime.logger.exception("预计算海克斯详情缓存后台暖机失败。")
        finally:
            with _api_cache_warm_lock:
                _api_cache_warm_inflight = False

    threading.Thread(
        target=_worker,
        daemon=True,
        name="precomputed-hextech-cache-warm",
    ).start()
    return True


def _html_file_response(filename: str) -> FileResponse:
    return _attach_local_auth_cookie(FileResponse(
        os.path.join(web_runtime.get_static_dir(), filename),
        media_type="text/html; charset=utf-8",
    ))


def register_routes(app: FastAPI) -> None:
    @app.get("/")
    async def read_index():
        return _html_file_response("index.html")

    @app.get("/index.html")
    async def read_index_explicit():
        return _html_file_response("index.html")

    @app.get("/detail.html")
    async def read_detail():
        return _html_file_response("detail.html")

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/data/static/{filename:path}")
    async def stable_static_file(filename: str):
        return _stable_data_response(
            STATIC_DATA_DIR,
            filename,
            legacy_static_payload,
            allowed_files=_STATIC_DATA_FILE_ALLOWLIST,
            legacy_files=_STATIC_DATA_LEGACY_ALLOWLIST,
        )

    @app.get("/data/indexes/{filename:path}")
    async def stable_index_file(filename: str):
        return _stable_data_response(
            INDEX_DATA_DIR,
            filename,
            legacy_index_payload,
            legacy_files=_INDEX_DATA_LEGACY_ALLOWLIST,
        )

    @app.get("/assets/{filename}")
    async def get_asset(filename: str):
        if not str(filename or "").lower().endswith(".png"):
            return JSONResponse(content={"error": "资源未找到"}, status_code=404)
        assets_dir = web_runtime.get_assets_dir()
        safe_path = web_runtime.safe_join_under_dir(assets_dir, filename)
        if not safe_path:
            web_runtime.logger.warning("已阻止目录遍历：%s", filename)
            return JSONResponse(content={"error": "禁止访问"}, status_code=403)
        if os.path.exists(safe_path):
            return FileResponse(safe_path)

        if filename.endswith(".png") and not filename[:-4].isdigit():
            catalog_entry = None
            try:
                file_stem = unquote(filename[:-4])
                catalog_entry = web_runtime.find_augment_catalog_entry(file_stem, STATIC_DATA_DIR)
                if catalog_entry:
                    augment_name = str(catalog_entry.get("name", "")).strip() or file_stem
                    mapped_filename = str(catalog_entry.get("filename", "")).strip()
                    if mapped_filename:
                        local_mapped = web_runtime.find_existing_augment_asset_filename(assets_dir, mapped_filename)
                        if local_mapped:
                            return _safe_asset_response(assets_dir, local_mapped)
                        web_runtime.queue_augment_icon_cache(mapped_filename, augment_name)

                    remote_icon_url = web_runtime.resolve_remote_augment_icon_url(catalog_entry, augment_name)
                    if remote_icon_url:
                        return RedirectResponse(url=remote_icon_url, status_code=307)

                local_fallback = web_runtime.find_existing_augment_asset_filename(assets_dir, filename)
                if local_fallback:
                    return _safe_asset_response(assets_dir, local_fallback)
                if re.fullmatch(r"[A-Za-z0-9._-]+", file_stem):
                    web_runtime.queue_augment_icon_cache(filename, file_stem)
                remote_icon_url = web_runtime.resolve_remote_augment_icon_url(catalog_entry, file_stem)
                if remote_icon_url:
                    return RedirectResponse(url=remote_icon_url, status_code=307)
            except Exception as exc:
                web_runtime.logger.warning("远程资源缓存失败：%s", exc)

        if filename.endswith(".png"):
            file_stem = filename[:-4]
            hero_name = web_runtime.get_champion_name(file_stem)
            if hero_name:
                _, en_name = web_runtime.get_champion_info(file_stem)
                if en_name:
                    version = web_runtime.get_ddragon_version()
                    if version:
                        ddragon_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{en_name}.png"
                        return RedirectResponse(url=ddragon_url, status_code=307)
            web_runtime.logger.debug("资源本地不存在，DDragon 回退也失败：%s", filename)

        return JSONResponse(content={"error": "资源未找到"}, status_code=404)

    @app.get("/api/champions")
    async def api_champions():
        df = web_runtime.get_df()
        if not df.empty:
            champions = process_champions_data(df)
            if champions:
                return JSONResponse(content=champions)

        stable_df = await asyncio.to_thread(web_runtime.get_stable_champion_catalog_df)
        if not stable_df.empty:
            champions = process_champions_data(stable_df, use_runtime_cache=False, log_columns=False)
            if champions:
                return JSONResponse(content=champions)

        snapshot_df = await asyncio.to_thread(web_runtime.get_live_champion_snapshot_df)
        if not snapshot_df.empty:
            champions = process_champions_data(snapshot_df)
            if champions:
                return JSONResponse(content=champions)

        return JSONResponse(content=[])

    @app.get("/api/startup_status")
    async def api_startup_status():
        return JSONResponse(content=web_runtime.get_startup_status())

    @app.get("/api/live_state")
    async def api_live_state():
        return JSONResponse(content=web_runtime.get_live_state_payload())

    @app.get("/api/champion_aliases")
    async def api_champion_aliases():
        try:
            payload = load_manual_alias_index()
            payload.sort(key=lambda item: item.get("heroName", ""))
            return JSONResponse(content=payload)
        except Exception as exc:
            web_runtime.logger.warning("英雄别名索引读取失败：%s", exc)
            return JSONResponse(content=[])

    @app.get("/api/champion/{name}/hextechs")
    async def api_champion_hextechs(name: str):
        canonical_name = web_runtime.resolve_canonical_hero_name(name)
        preloaded_payload = web_runtime.get_preloaded_hextech_payload(canonical_name)
        if isinstance(preloaded_payload, dict) and preloaded_payload.get("comprehensive"):
            return JSONResponse(content=preloaded_payload)

        if is_precomputed_hextech_cache_loaded():
            precomputed_payload = load_precomputed_hextech_for_hero(canonical_name)
            if isinstance(precomputed_payload, dict) and precomputed_payload.get("comprehensive"):
                return JSONResponse(content=precomputed_payload)
        else:
            _request_precomputed_hextech_warm()

        preload_status = web_runtime.get_preload_hextech_status(canonical_name)
        if preload_status.get("pending"):
            return JSONResponse(content=_loading_hextech_payload())

        _request_precomputed_hextech_rebuild()
        web_runtime.request_preload_hextech_payload_async(canonical_name)
        return JSONResponse(content=_loading_hextech_payload())

    @app.post("/api/champion/{name}/preload")
    async def api_preload_champion(name: str, request: Request):
        origin = request.headers.get("origin")
        if not web_runtime.is_allowed_local_origin(origin):
            web_runtime.logger.warning("已拒绝非本机来源的 preload 请求：origin=%s", origin)
            return JSONResponse(content={"error": "forbidden_origin"}, status_code=status.HTTP_403_FORBIDDEN)
        request_token = _extract_request_token(request)
        if request_token != web_runtime.get_request_auth_token():
            web_runtime.logger.warning("已拒绝缺少有效 token 的 preload 请求")
            return JSONResponse(content={"error": "forbidden_token"}, status_code=status.HTTP_403_FORBIDDEN)

        canonical_name = web_runtime.resolve_canonical_hero_name(unquote(name))
        queued = web_runtime.request_preload_hextech_payload_async(canonical_name)
        preload_status = web_runtime.get_preload_hextech_status(canonical_name)
        return JSONResponse(content={"queued": queued, **preload_status})

    @app.get("/api/champion/{name}/preload_status")
    async def api_preload_status(name: str, request: Request):
        origin = request.headers.get("origin")
        if not web_runtime.is_allowed_local_origin(origin):
            web_runtime.logger.warning("已拒绝非本机来源的 preload_status 请求：origin=%s", origin)
            return JSONResponse(content={"error": "forbidden_origin"}, status_code=status.HTTP_403_FORBIDDEN)
        request_token = _extract_request_token(request)
        if request_token != web_runtime.get_request_auth_token():
            web_runtime.logger.warning("已拒绝缺少有效 token 的 preload_status 请求")
            return JSONResponse(content={"error": "forbidden_token"}, status_code=status.HTTP_403_FORBIDDEN)

        canonical_name = web_runtime.resolve_canonical_hero_name(unquote(name))
        return JSONResponse(content=web_runtime.get_preload_hextech_status(canonical_name))

    @app.get("/api/augment_icon_map")
    async def api_augment_icon_map():
        try:
            manifest = load_augment_icon_manifest()
            data = {}
            for item in manifest:
                name = str(item.get("name", "")).strip()
                filename = str(item.get("filename", "")).strip()
                remote_icon_url = str(item.get("icon_url", "")).strip()
                if not name:
                    continue
                if filename and web_runtime.is_safe_png_asset_name(filename):
                    data[name] = f"/assets/{quote(filename, safe='')}"
                elif remote_icon_url and web_runtime.is_safe_redirect_url(remote_icon_url):
                    data[name] = remote_icon_url
            return JSONResponse(content=data)
        except Exception as exc:
            web_runtime.logger.warning("统一海克斯目录图标映射读取失败：%s", exc)
            return JSONResponse(content={})

    @app.get("/api/synergies/{champ_id}")
    async def api_synergies(champ_id: str):
        try:
            data = web_runtime.get_synergy_data()
            return JSONResponse(content=_build_synergy_api_payload(data, champ_id))
        except Exception as exc:
            web_runtime.logger.warning("协同数据查询失败：%s", exc)
            return JSONResponse(content={"synergies": [], "synergy_items": []})

    @app.post("/api/redirect")
    async def api_redirect(req: RedirectRequest, request: Request):
        origin = request.headers.get("origin")
        if not web_runtime.is_allowed_local_origin(origin):
            web_runtime.logger.warning("已拒绝非本地来源的 redirect 请求：origin=%s", origin)
            return JSONResponse(content={"error": "forbidden_origin"}, status_code=status.HTTP_403_FORBIDDEN)
        request_token = _extract_request_token(request)
        if request_token != web_runtime.get_request_auth_token():
            web_runtime.logger.warning("已拒绝缺少有效 token 的 redirect 请求。")
            return JSONResponse(content={"error": "forbidden_token"}, status_code=status.HTTP_403_FORBIDDEN)

        try:
            hero_name, en_name = web_runtime.get_champion_info(req.hero_id)
        except (ValueError, TypeError):
            hero_name, en_name = "", ""

        if not hero_name:
            hero_name = req.hero_name

        canonical_name = web_runtime.resolve_canonical_hero_name(hero_name or req.hero_name)
        try:
            web_runtime.request_preload_hextech_payload_async(canonical_name)
        except Exception:
            web_runtime.logger.warning("英雄详情异步预加载请求失败：hero=%s", canonical_name, exc_info=True)

        standardized_hero_name = hero_name or req.hero_name

        if len(web_runtime.manager.active) == 0:
            url = web_runtime.build_detail_url(req.hero_id, standardized_hero_name, en_name)
            if web_runtime.request_open_managed_browser_async(url, replace_existing=True):
                return JSONResponse(content={"status": "opening_browser", "detail_first": True})
            return JSONResponse(content={"status": "浏览器打开失败"}, status_code=500)

        await web_runtime.manager.broadcast(
            {
                "type": "local_player_locked",
                "champion_id": req.hero_id,
                "hero_name": standardized_hero_name,
                "en_name": en_name,
                "detail_first": True,
            }
        )
        return JSONResponse(content={"status": "broadcast_sent", "detail_first": True})

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin")
        if not web_runtime.is_allowed_local_origin(origin):
            web_runtime.logger.warning("已拒绝非本地来源的 WebSocket 连接：origin=%s", origin)
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        request_token = str(ws.headers.get(web_runtime.REQUEST_TOKEN_HEADER, "")).strip() or str(
            ws.cookies.get(web_runtime.HTTP_SESSION_COOKIE, "")
        ).strip()
        if request_token != web_runtime.get_request_auth_token():
            web_runtime.logger.warning("已拒绝缺少或无效 token 的 WebSocket 连接。")
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await web_runtime.manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await web_runtime.manager.disconnect(ws)

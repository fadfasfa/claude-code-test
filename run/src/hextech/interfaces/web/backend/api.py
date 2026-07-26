"""Web 服务路由层。

这个模块只负责定义页面路由、API 路由和 WebSocket 入口。
所有与端口、LCU、缓存、资源定位、浏览器托管相关的细节都委托给 `web_runtime`，
从而让接口层保持稳定且方便后续扩展。

调用方: display.web.app、dev_checks、verify_data_source_integrity; 关键依赖: catalog.aliases、catalog.precomputed_cache、catalog.runtime_store。
"""

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from hextech.modules.data.catalog.aliases import load_manual_alias_index
from hextech.modules.data.generation import DataSnapshotClient, SnapshotValidationError
from hextech.modules.data.catalog.version_catalog import (
    AUGMENT_RESOURCE_CATALOG_FILENAME,
    HERO_CATALOG_FILENAME,
    catalog_index_payload,
    catalog_payload,
    load_augment_name_to_icon_map,
)
from hextech.modules.session.diagnostics import redact_diagnostic_text
from . import runtime as web_runtime

_snapshot_client = DataSnapshotClient()
_STATIC_DATA_FILE_ALLOWLIST = frozenset({
    HERO_CATALOG_FILENAME,
    AUGMENT_RESOURCE_CATALOG_FILENAME,
    "Champion_Core_Data.json",
    "Augment_Full_Map.json",
    "Augment_Icon_Map.json",
    "hero_version.txt",
})
_CATALOG_PROJECTION_ALLOWLIST = frozenset({
    "Augment_Apexlol_Map.json",
    "Augment_Icon_Manifest.json",
    "Champion_Alias_Index.json",
    "Champion_Core_Data.json",
})
_CATALOG_INDEX_ALLOWLIST = frozenset({
    "Champion_Alias_Index.json",
    "augment.name-to-icon.v1.json",
    "champion.alias-to-id.v1.json",
    "champion.id-to-detail.v1.json",
    "champion.id-to-name.v1.json",
})
_PUBLIC_STARTUP_STATUS_BOOL_KEYS = frozenset({
    "first_run",
    "hero_ready",
    "hextech_ready",
    "hextech_degraded",
    "synergy_ready",
    "augment_icons_prefetched",
})
_PUBLIC_STARTUP_STATUS_TEXT_KEYS = frozenset({
    "last_error",
    "hextech_warning",
    "updated_at",
})
_PUBLIC_STARTUP_STATUS_LIST_KEYS = frozenset({
    "in_progress_tasks",
})


from .synergy_api import _build_synergy_api_payload, _empty_synergy_payload  # noqa: E402

class RedirectRequest(BaseModel):
    """前端点击英雄后发送的跳转请求体。"""

    hero_id: str
    hero_name: str


def _public_startup_status() -> dict:
    raw_status = web_runtime.get_startup_status()
    if not isinstance(raw_status, dict):
        return {}

    public: dict = {}
    for key in _PUBLIC_STARTUP_STATUS_BOOL_KEYS:
        if key in raw_status:
            public[key] = bool(raw_status.get(key))
    for key in _PUBLIC_STARTUP_STATUS_TEXT_KEYS:
        if key in raw_status:
            public[key] = redact_diagnostic_text(raw_status.get(key))
    for key in _PUBLIC_STARTUP_STATUS_LIST_KEYS:
        if key not in raw_status:
            continue
        value = raw_status.get(key)
        if isinstance(value, (list, tuple)):
            public[key] = [redact_diagnostic_text(item) for item in value]
        elif value:
            public[key] = [redact_diagnostic_text(value)]

    manifest = raw_status.get("bundle_manifest")
    if isinstance(manifest, dict) and manifest.get("warning"):
        public["bundle_manifest"] = {"warning": redact_diagnostic_text(manifest.get("warning"))}

    return public


def _loading_hextech_payload(preload_status: dict | None = None) -> dict:
    payload = {
        "top_10_overall": [],
        "comprehensive": [],
        "winrate_only": [],
        "Prismatic": [],
        "Gold": [],
        "Silver": [],
        "loading": True,
        "ready": False,
        "startup_status": _public_startup_status(),
        "preload_status": preload_status or {},
    }
    return payload


def _display_private_stats_enabled() -> bool:
    from hextech.modules.session.settings import load_ui_feature_flags

    return bool(load_ui_feature_flags().get("private_policy_stats_enabled", False))


def _open_snapshot_view(generation_id: str | None = None):
    requested = str(generation_id or "").strip()
    return _snapshot_client.open_generation(requested) if requested else _snapshot_client.open_view()


def _generation_conflict(generation_id: str) -> JSONResponse:
    return JSONResponse(
        content={
            "error": "generation_conflict",
            "requested_generation_id": str(generation_id or ""),
            "current_generation_id": str(_snapshot_client.status().get("generation_id") or ""),
        },
        status_code=status.HTTP_409_CONFLICT,
    )


def _snapshot_hextech_payload(champion_name: str, *, generation_id: str | None = None) -> dict:
    """只读查询一代快照，并保持数据未就绪与真实无统计的语义分离。"""

    try:
        snapshot_view = _open_snapshot_view(generation_id)
    except SnapshotValidationError:
        snapshot_view = None
    snapshot_status = snapshot_view.status() if snapshot_view is not None else _snapshot_client.status()
    state = str(snapshot_status.get("state") or "unavailable")
    generation_id = str(snapshot_status.get("generation_id") or "")
    if state == "unavailable":
        return {
            **_loading_hextech_payload(),
            "status": "DATA_NOT_READY",
            "generation_id": generation_id,
        }

    private_stats_enabled = _display_private_stats_enabled()
    if not private_stats_enabled:
        return {
            "comprehensive": [],
            "ready": True,
            "loading": False,
            "status": "PRIVACY_OFF",
            "data_status": "PRIVACY_OFF",
            "generation_state": state,
            "generation_id": generation_id,
            "private_stats_enabled": False,
        }

    detail = snapshot_view.get_champion_detail(champion_name)
    augments = snapshot_view.get_champion_augments(champion_name)
    if detail is not None and augments:
        payload = dict(detail)
        payload.setdefault("comprehensive", augments)
        payload["generation_id"] = generation_id
        payload["status"] = "GENERATION_DEGRADED" if state == "degraded" else "READY"
        payload["data_status"] = "READY"
        payload["generation_state"] = state
        payload["private_stats_enabled"] = True
        return payload

    if state == "degraded":
        status_code = "GENERATION_DEGRADED"
    else:
        status_code = "NO_STATS"
    return {
        "comprehensive": [],
        "ready": True,
        "loading": False,
        "status": status_code,
        "data_status": "NO_STATS",
        "generation_state": state,
        "generation_id": generation_id,
        "private_stats_enabled": private_stats_enabled,
    }


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


def _require_local_api_access(request: Request, route_label: str) -> JSONResponse | None:
    """保护本机状态 API：同时要求本机 Origin 和当前 Web token。"""

    origin = request.headers.get("origin")
    if not web_runtime.is_allowed_local_origin(origin):
        web_runtime.logger.warning("已拒绝非本机来源的 %s 请求：origin=%s", route_label, origin)
        return JSONResponse(content={"error": "forbidden_origin"}, status_code=status.HTTP_403_FORBIDDEN)
    request_token = _extract_request_token(request)
    if request_token != web_runtime.get_request_auth_token():
        web_runtime.logger.warning("已拒绝缺少有效 token 的 %s 请求", route_label)
        return JSONResponse(content={"error": "forbidden_token"}, status_code=status.HTTP_403_FORBIDDEN)
    return None


# 与 /assets 路由的前缀白名单保持一致；目录 name_to_icon 现存 augments 与 modes 两类。
_ICON_MAP_LOCAL_PREFIXES = frozenset({"champions", "augments", "modes", "ui"})


def _is_safe_icon_map_local_url(url: str) -> bool:
    """校验 name_to_icon 的本地路径：必须落在 /assets/ 白名单前缀下且为安全 png 文件名。"""

    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if not parsed.path.startswith("/assets/"):
        return False
    parts = parsed.path[len("/assets/"):].split("/")
    if len(parts) < 2 or parts[0] not in _ICON_MAP_LOCAL_PREFIXES:
        return False
    if any(not part or part in {".", ".."} for part in parts):
        return False
    return web_runtime.is_safe_png_asset_name(unquote(parts[-1]))


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
    projection_loader,
    *,
    allowed_files: frozenset[str] = frozenset(),
    projection_files: frozenset[str] = frozenset(),
):
    route_name = _route_file_name(filename)
    if not route_name:
        return JSONResponse(content={"error": "禁止访问"}, status_code=403)

    safe_path = web_runtime.safe_join_under_dir(base_dir, route_name)
    if not safe_path:
        return JSONResponse(content={"error": "禁止访问"}, status_code=403)
    if route_name in allowed_files and os.path.exists(safe_path) and os.path.isfile(safe_path):
        return FileResponse(safe_path)
    if route_name in projection_files:
        payload = projection_loader(route_name, base_dir)
        if payload is not None:
            return JSONResponse(content=payload)
    return JSONResponse(content={"error": "资源未找到"}, status_code=404)


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

    @app.get("/catalog/{filename:path}")
    async def catalog_file(filename: str):
        projection_files = _CATALOG_PROJECTION_ALLOWLIST | _CATALOG_INDEX_ALLOWLIST
        catalog_dir = web_runtime.get_catalog_dir()
        return _stable_data_response(
            catalog_dir,
            filename,
            lambda name, config_dir: (
                catalog_payload(name, config_dir)
                if name in _CATALOG_PROJECTION_ALLOWLIST
                else catalog_index_payload(name, config_dir)
            ),
            allowed_files=_STATIC_DATA_FILE_ALLOWLIST,
            projection_files=projection_files,
        )

    @app.get("/assets/{asset_path:path}")
    async def get_asset(asset_path: str):
        normalized = str(asset_path or "").replace("\\", "/").strip("/")
        parts = normalized.split("/")
        if len(parts) < 2 or parts[0] not in {"champions", "augments", "modes", "ui"}:
            return JSONResponse(content={"error": "资源未找到"}, status_code=404)
        filename = parts[-1]
        if not filename.lower().endswith(".png"):
            return JSONResponse(content={"error": "资源未找到"}, status_code=404)
        assets_dir = web_runtime.get_assets_dir()
        safe_path = web_runtime.safe_join_under_dir(assets_dir, normalized)
        if not safe_path:
            web_runtime.logger.warning("已阻止目录遍历：%s", asset_path)
            return JSONResponse(content={"error": "禁止访问"}, status_code=403)
        local_path = web_runtime.find_local_asset_path(normalized)
        if local_path:
            return FileResponse(local_path)

        if parts[0] == "augments" and not filename[:-4].isdigit():
            catalog_entry = None
            try:
                file_stem = unquote(filename[:-4])
                catalog_entry = web_runtime.find_augment_catalog_entry(file_stem, web_runtime.get_catalog_dir())
                if catalog_entry:
                    augment_name = str(catalog_entry.get("name", "")).strip() or file_stem
                    mapped_filename = str(catalog_entry.get("filename", "")).strip()
                    if mapped_filename:
                        for search_dir in web_runtime.get_asset_search_dirs():
                            local_mapped = web_runtime.find_existing_augment_asset_filename(search_dir, mapped_filename)
                            if local_mapped:
                                return _safe_asset_response(search_dir, f"augments/{local_mapped}")

                    remote_icon_url = web_runtime.resolve_remote_augment_icon_url(catalog_entry, augment_name)
                    if remote_icon_url:
                        return RedirectResponse(url=remote_icon_url, status_code=307)

                for search_dir in web_runtime.get_asset_search_dirs():
                    local_fallback = web_runtime.find_existing_augment_asset_filename(search_dir, filename)
                    if local_fallback:
                        return _safe_asset_response(search_dir, f"augments/{local_fallback}")
                remote_icon_url = web_runtime.resolve_remote_augment_icon_url(catalog_entry, file_stem)
                if remote_icon_url:
                    return RedirectResponse(url=remote_icon_url, status_code=307)
            except Exception as exc:
                web_runtime.logger.warning("远程资源解析失败：%s", exc)

        if parts[0] == "champions":
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
    async def api_champions(generation_id: str | None = None):
        try:
            snapshot_view = _open_snapshot_view(generation_id)
        except SnapshotValidationError:
            if generation_id:
                return _generation_conflict(generation_id)
            return JSONResponse(
                content={"error": "snapshot_unavailable", "generation_id": ""},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return JSONResponse(
            content={
                "generation_id": str(snapshot_view.status().get("generation_id") or ""),
                "champions": snapshot_view.get_champions(),
            }
            if generation_id
            else snapshot_view.get_champions()
        )

    @app.get("/api/startup_status")
    async def api_startup_status(request: Request):
        denied = _require_local_api_access(request, "startup_status")
        if denied is not None:
            return denied
        return JSONResponse(content=web_runtime.get_startup_status())

    @app.get("/api/live_state")
    async def api_live_state(request: Request):
        denied = _require_local_api_access(request, "live_state")
        if denied is not None:
            return denied
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
    async def api_champion_hextechs(name: str, generation_id: str | None = None):
        canonical_name = web_runtime.resolve_canonical_hero_name(name)
        if generation_id:
            try:
                _open_snapshot_view(generation_id)
            except SnapshotValidationError:
                return _generation_conflict(generation_id)
        return JSONResponse(content=_snapshot_hextech_payload(canonical_name, generation_id=generation_id))

    @app.post("/api/champion/{name}/preload")
    async def api_preload_champion(name: str, request: Request):
        denied = _require_local_api_access(request, "preload")
        if denied is not None:
            return denied

        canonical_name = web_runtime.resolve_canonical_hero_name(unquote(name))
        queued = web_runtime.request_preload_hextech_payload_async(canonical_name)
        preload_status = web_runtime.get_preload_hextech_status(canonical_name)
        return JSONResponse(content={"queued": queued, **preload_status})

    @app.get("/api/champion/{name}/preload_status")
    async def api_preload_status(name: str, request: Request):
        denied = _require_local_api_access(request, "preload_status")
        if denied is not None:
            return denied

        canonical_name = web_runtime.resolve_canonical_hero_name(unquote(name))
        return JSONResponse(content=web_runtime.get_preload_hextech_status(canonical_name))

    @app.get("/api/augment_icon_map")
    async def api_augment_icon_map():
        # 直接下发目录 name_to_icon 的完整本地路径（/assets/augments/... 等）。
        # 旧实现用扁平 filename 拼 /assets/{filename}，缺子目录前缀，资产路由必 404。
        try:
            payload = load_augment_name_to_icon_map(web_runtime.get_catalog_dir())
            data = {}
            for name, icon_url in payload.items():
                normalized_name = str(name or "").strip()
                url = str(icon_url or "").strip()
                if not normalized_name or not url:
                    continue
                if _is_safe_icon_map_local_url(url) or web_runtime.is_safe_redirect_url(url):
                    data[normalized_name] = url
            return JSONResponse(content=data)
        except Exception as exc:
            web_runtime.logger.warning("统一海克斯目录图标映射读取失败：%s", exc)
            return JSONResponse(content={})

    @app.get("/api/synergies/{champ_id}")
    async def api_synergies(champ_id: str, generation_id: str | None = None):
        try:
            snapshot_view = _open_snapshot_view(generation_id)
            data = snapshot_view.get_synergy_data()
            payload = _build_synergy_api_payload(data, champ_id)
            payload["generation_id"] = str(snapshot_view.status().get("generation_id") or "")
            return JSONResponse(content=payload)
        except SnapshotValidationError:
            return _generation_conflict(generation_id) if generation_id else JSONResponse(
                content=_empty_synergy_payload(status_text="source_unavailable", message="协同数据未就绪")
            )
        except Exception as exc:
            web_runtime.logger.warning("协同数据查询失败：%s", exc)
            return JSONResponse(content=_empty_synergy_payload(status_text="error", message="协同数据读取失败"))

    @app.post("/api/redirect")
    async def api_redirect(req: RedirectRequest, request: Request):
        denied = _require_local_api_access(request, "redirect")
        if denied is not None:
            return denied

        try:
            hero_name, en_name = web_runtime.get_champion_info(req.hero_id)
        except (ValueError, TypeError):
            hero_name, en_name = "", ""

        if not hero_name:
            hero_name = req.hero_name

        try:
            canonical_name = web_runtime.resolve_canonical_hero_name(hero_name or req.hero_name)
        except (ValueError, TypeError):
            return JSONResponse(content={"error": "invalid_champion"}, status_code=400)

        standardized_hero_name = hero_name or req.hero_name
        try:
            url = web_runtime.build_detail_url(req.hero_id, standardized_hero_name, en_name)
        except (ValueError, TypeError):
            return JSONResponse(content={"error": "invalid_champion"}, status_code=400)

        try:
            web_runtime.request_preload_hextech_payload_async(canonical_name)
        except Exception:
            web_runtime.logger.warning("英雄详情异步预加载请求失败：hero=%s", canonical_name, exc_info=True)

        if len(web_runtime.manager.active) == 0:
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

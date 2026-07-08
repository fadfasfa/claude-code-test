"""测试 Web 本机 API 的 token 边界和只读 GET 契约。

调用方: pytest; 关键依赖: hextech.display.web.api。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client():
    from hextech.display.web import api as web_api

    app = FastAPI()
    web_api.register_routes(app)
    return TestClient(app), web_api


def test_local_state_gets_require_origin_and_token(monkeypatch):
    client, web_api = _client()

    monkeypatch.setattr(web_api.web_runtime, "get_request_auth_token", lambda: "local-secret")
    monkeypatch.setattr(web_api.web_runtime, "get_active_web_port", lambda: 8211)
    monkeypatch.setattr(web_api.web_runtime, "get_startup_status", lambda: {"status": "ready"})
    monkeypatch.setattr(web_api.web_runtime, "get_live_state_payload", lambda: {"state": "ready"})

    assert client.get("/api/startup_status", headers={"origin": "http://127.0.0.1:8211"}).status_code == 403
    assert (
        client.get(
            "/api/live_state",
            headers={"origin": "http://127.0.0.1:9000", web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
        ).status_code
        == 403
    )
    for malformed_origin in ("http://127.0.0.1:bad", "http://127.0.0.1:99999"):
        response = client.get(
            "/api/live_state",
            headers={"origin": malformed_origin, web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
        )
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden_origin"}
    assert (
        client.get(
            "/api/live_state",
            headers={"origin": "https://attacker.example", web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
        ).status_code
        == 403
    )

    authorized = client.get(
        "/api/live_state",
        headers={"origin": "http://127.0.0.1:8211", web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
    )
    assert authorized.status_code == 200
    assert authorized.json() == {"state": "ready"}


def test_public_hextech_get_does_not_trigger_rebuild_or_preload(monkeypatch):
    client, web_api = _client()

    def fail_side_effect(*_args, **_kwargs):
        raise AssertionError("public GET must not start rebuild, warmup, or preload side effects")

    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(web_api.web_runtime, "get_preloaded_hextech_payload", lambda name: None)
    monkeypatch.setattr(web_api.web_runtime, "get_preload_hextech_status", lambda name: {"pending": False})
    monkeypatch.setattr(web_api.web_runtime, "get_startup_status", lambda: {"hextech_ready": False})
    monkeypatch.setattr(web_api, "is_precomputed_hextech_cache_loaded", lambda: False)
    monkeypatch.setattr(web_api, "_request_precomputed_hextech_warm", fail_side_effect)
    monkeypatch.setattr(web_api, "_request_precomputed_hextech_rebuild", fail_side_effect)
    monkeypatch.setattr(web_api.web_runtime, "request_preload_hextech_payload_async", fail_side_effect)

    response = client.get("/api/champion/Garen/hextechs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["loading"] is True
    assert payload["ready"] is False
    assert payload["startup_status"] == {"hextech_ready": False}
    assert payload["preload_status"] == {"pending": False}


def test_public_hextech_loading_payload_redacts_startup_status_paths(monkeypatch):
    client, web_api = _client()

    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(web_api.web_runtime, "get_preloaded_hextech_payload", lambda name: None)
    monkeypatch.setattr(web_api.web_runtime, "get_preload_hextech_status", lambda name: {"pending": False})
    monkeypatch.setattr(web_api, "is_precomputed_hextech_cache_loaded", lambda: False)
    monkeypatch.setattr(
        web_api.web_runtime,
        "get_startup_status",
        lambda: {
            "hero_ready": True,
            "hextech_ready": False,
            "last_error": "failed at C:/Users/apple/claudecode/run/data/runtime/raw/hextech/Hextech_Data.csv",
            "active_hextech_csv": "C:/Users/apple/claudecode/run/data/runtime/raw/hextech/Hextech_Data.csv",
            "hextech_refresh": {
                "reason": "fallback",
                "active_csv": "C:/Users/apple/claudecode/run/data/seed/startup/hextech/Hextech_Data.csv",
            },
            "bundle_manifest": {
                "status": "error",
                "warning": "manifest at C:/Users/apple/claudecode/run/bundle_manifest.json",
                "manifest_path": "C:/Users/apple/claudecode/run/bundle_manifest.json",
            },
        },
    )

    response = client.get("/api/champion/Garen/hextechs")

    assert response.status_code == 200
    payload = response.json()
    startup = payload["startup_status"]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["loading"] is True
    assert startup["hero_ready"] is True
    assert startup["hextech_ready"] is False
    assert startup["last_error"] == "failed at <local-path>"
    assert startup["bundle_manifest"] == {"warning": "manifest at <local-path>"}
    assert "active_hextech_csv" not in startup
    assert "hextech_refresh" not in startup
    assert "manifest_path" not in startup["bundle_manifest"]
    assert "C:/Users/apple" not in serialized
    assert "Hextech_Data.csv" not in serialized


def test_detail_loading_branch_requests_authenticated_preload_from_page():
    detail_js = Path("hextech/display/web/static/js/detail.js").read_text(encoding="utf-8")

    assert "function requestDetailPreload" in detail_js
    assert "fetch(`${API_BASE}/api/champion/${encodeURIComponent(hero)}/preload`" in detail_js
    assert "method: 'POST'" in detail_js
    assert "credentials: 'same-origin'" in detail_js
    assert "requestDetailPreload();" in detail_js


def test_synergy_api_distinguishes_empty_error_and_quarantined(monkeypatch):
    client, web_api = _client()

    monkeypatch.setattr(web_api.web_runtime, "resolve_champion_id", lambda champ_id: str(champ_id))
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda champ_id: str(champ_id))

    monkeypatch.setattr(web_api.web_runtime, "get_synergy_data", lambda: {})
    empty = client.get("/api/synergies/86").json()
    assert empty["status"] == "empty"
    assert empty["synergy_items"] == []

    def boom():
        raise RuntimeError("broken")

    monkeypatch.setattr(web_api.web_runtime, "get_synergy_data", boom)
    error = client.get("/api/synergies/86").json()
    assert error["status"] == "error"
    assert error["message"] == "协同数据读取失败"

    duplicated = {
        "86": {
            "synergy_items": [
                {"augment_names": ["A"], "content": "same"},
                {"augment_names": ["B"], "content": "same2"},
            ]
        },
        "99": {
            "synergy_items": [
                {"augment_names": ["A"], "content": "same"},
                {"augment_names": ["B"], "content": "same2"},
            ]
        },
    }
    monkeypatch.setattr(web_api.web_runtime, "get_synergy_data", lambda: duplicated)
    quarantined = client.get("/api/synergies/86").json()
    assert quarantined["status"] == "quarantined"
    assert quarantined["synergy_items"] == []


def test_preload_worker_failure_is_visible_in_detail_loading_payload(monkeypatch):
    client, web_api = _client()
    runtime = web_api.web_runtime

    class InlineExecutor:
        def submit(self, func, *args, **kwargs):
            func(*args, **kwargs)
            return None

    runtime.clear_preloaded_hextech_payloads()
    monkeypatch.setattr(runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(runtime, "_get_runtime_df_signature", lambda: ("csv", 1.0))
    monkeypatch.setattr(runtime, "get_df", lambda: pd.DataFrame([{"hero": "Garen"}]))
    monkeypatch.setattr(runtime, "_get_preloaded_hextech_executor", lambda: InlineExecutor())

    def fail_process(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "process_hextechs_data", fail_process)
    monkeypatch.setattr(runtime, "get_startup_status", lambda: {"hextech_ready": False})
    monkeypatch.setattr(web_api, "is_precomputed_hextech_cache_loaded", lambda: False)

    try:
        assert runtime.request_preload_hextech_payload_async("Garen") is True
        response = client.get("/api/champion/Garen/hextechs")
        payload = response.json()
    finally:
        runtime.clear_preloaded_hextech_payloads()

    assert response.status_code == 200
    assert payload["loading"] is True
    assert payload["preload_status"]["error"] == "preload_failed"
    assert payload["preload_status"]["reason"] == "worker_exception"
    assert payload["preload_status"]["last_error"] == "海克斯预加载失败"


def test_preloaded_hextech_payload_returns_unpolluted_copy(monkeypatch):
    from hextech.display.web import runtime

    runtime.clear_preloaded_hextech_payloads()
    monkeypatch.setattr(runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(runtime, "_get_runtime_df_signature", lambda: ("csv", 1.0))
    try:
        with runtime._preloaded_hextech_lock:
            runtime._preloaded_hextech_signature = ("csv", 1.0)
            runtime._preloaded_hextech_payloads["Garen"] = {
                "comprehensive": True,
                "items": [{"name": "original"}],
            }

        first = runtime.get_preloaded_hextech_payload("Garen")
        assert first is not None
        first["items"][0]["name"] = "polluted"

        second = runtime.get_preloaded_hextech_payload("Garen")
    finally:
        runtime.clear_preloaded_hextech_payloads()

    assert second is not None
    assert second["items"][0]["name"] == "original"


def test_redirect_invalid_champion_input_returns_400(monkeypatch):
    client, web_api = _client()

    monkeypatch.setattr(web_api.web_runtime, "get_request_auth_token", lambda: "local-secret")
    monkeypatch.setattr(web_api.web_runtime, "get_active_web_port", lambda: 8211)
    monkeypatch.setattr(web_api.web_runtime, "get_champion_info", lambda _hero_id: ("", ""))
    monkeypatch.setattr(
        web_api.web_runtime,
        "resolve_canonical_hero_name",
        lambda _hero_name: (_ for _ in ()).throw(ValueError("bad champion")),
    )

    response = client.post(
        "/api/redirect",
        json={"hero_id": "%", "hero_name": "%"},
        headers={"origin": "http://127.0.0.1:8211", web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_champion"}


def test_redirect_invalid_detail_url_returns_400(monkeypatch):
    client, web_api = _client()

    class DummyManager:
        active = []

    monkeypatch.setattr(web_api.web_runtime, "get_request_auth_token", lambda: "local-secret")
    monkeypatch.setattr(web_api.web_runtime, "get_active_web_port", lambda: 8211)
    monkeypatch.setattr(web_api.web_runtime, "get_champion_info", lambda _hero_id: ("德玛西亚之力", "Garen"))
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda _hero_name: "德玛西亚之力")
    monkeypatch.setattr(web_api.web_runtime, "request_preload_hextech_payload_async", lambda _hero_name: True)
    monkeypatch.setattr(web_api.web_runtime, "manager", DummyManager())

    def fail_build_detail_url(*_args, **_kwargs):
        raise ValueError("invalid_hero_id")

    monkeypatch.setattr(web_api.web_runtime, "build_detail_url", fail_build_detail_url)

    response = client.post(
        "/api/redirect",
        json={"hero_id": "%", "hero_name": "德玛西亚之力"},
        headers={"origin": "http://127.0.0.1:8211", web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_champion"}


def test_redirect_invalid_detail_url_with_active_clients_does_not_preload_or_broadcast(monkeypatch):
    client, web_api = _client()

    class DummyManager:
        active = [object()]

        async def broadcast(self, _message):
            raise AssertionError("invalid redirect must not broadcast")

    def fail_side_effect(*_args, **_kwargs):
        raise AssertionError("invalid redirect must not preload or open browser")

    monkeypatch.setattr(web_api.web_runtime, "get_request_auth_token", lambda: "local-secret")
    monkeypatch.setattr(web_api.web_runtime, "get_active_web_port", lambda: 8211)
    monkeypatch.setattr(web_api.web_runtime, "get_champion_info", lambda _hero_id: ("德玛西亚之力", "Garen"))
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda _hero_name: "德玛西亚之力")
    monkeypatch.setattr(web_api.web_runtime, "request_preload_hextech_payload_async", fail_side_effect)
    monkeypatch.setattr(web_api.web_runtime, "request_open_managed_browser_async", fail_side_effect)
    monkeypatch.setattr(web_api.web_runtime, "manager", DummyManager())

    def fail_build_detail_url(*_args, **_kwargs):
        raise ValueError("invalid_hero_id")

    monkeypatch.setattr(web_api.web_runtime, "build_detail_url", fail_build_detail_url)

    response = client.post(
        "/api/redirect",
        json={"hero_id": "%", "hero_name": "德玛西亚之力"},
        headers={"origin": "http://127.0.0.1:8211", web_api.web_runtime.REQUEST_TOKEN_HEADER: "local-secret"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_champion"}

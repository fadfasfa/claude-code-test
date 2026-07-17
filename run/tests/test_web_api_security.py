"""测试 Web 本机 API 的 token 边界和只读 GET 契约。

调用方: pytest; 关键依赖: hextech.interfaces.web.backend.api。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


RUN_ROOT = Path(__file__).resolve().parents[1]


class _SnapshotView:
    def __init__(self, status: dict, *, detail: dict | None = None, synergy: dict | None = None) -> None:
        self._status = dict(status)
        self._detail = detail
        self._synergy = synergy or {}

    def status(self) -> dict:
        return dict(self._status)

    def get_champion_detail(self, _name: str) -> dict | None:
        return self._detail

    def get_champion_augments(self, _name: str) -> list[dict]:
        return list((self._detail or {}).get("augments", []))

    def get_synergy_data(self) -> dict:
        return dict(self._synergy)


def _raise_snapshot_unavailable():
    from hextech.modules.data.generation import SnapshotValidationError

    raise SnapshotValidationError("unavailable")


def _client():
    from hextech.interfaces.web.backend import api as web_api

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
    monkeypatch.setattr(web_api.web_runtime, "get_startup_status", lambda: {"hextech_ready": False})
    monkeypatch.setattr(web_api.web_runtime, "request_preload_hextech_payload_async", fail_side_effect)
    monkeypatch.setattr(web_api._snapshot_client, "status", lambda: {"state": "unavailable"})
    monkeypatch.setattr(web_api._snapshot_client, "open_view", _raise_snapshot_unavailable)

    response = client.get("/api/champion/Garen/hextechs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["loading"] is True
    assert payload["ready"] is False
    assert payload["status"] == "DATA_NOT_READY"
    assert payload["startup_status"] == {"hextech_ready": False}
    assert payload["preload_status"] == {}


def test_public_hextech_loading_payload_redacts_startup_status_paths(monkeypatch):
    client, web_api = _client()

    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(web_api._snapshot_client, "status", lambda: {"state": "unavailable"})
    monkeypatch.setattr(web_api._snapshot_client, "open_view", _raise_snapshot_unavailable)
    monkeypatch.setattr(
        web_api.web_runtime,
        "get_startup_status",
        lambda: {
            "hero_ready": True,
            "hextech_ready": False,
            "last_error": "failed at C:/Users/apple/claudecode/run/var/sources/hextech/runs/x/stats.csv",
            "hextech_warning": "check auth_token.txt local.yaml proxies.json accounts.json",
            "active_hextech_csv": "C:/Users/apple/claudecode/run/var/sources/hextech/runs/x/stats.csv",
            "hextech_refresh": {
                "reason": "fallback",
                "active_csv": "C:/Users/apple/claudecode/run/resources/seeds/generations/x/champion_hextech.json",
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
    assert startup["hextech_warning"] == "check <sensitive-file> <sensitive-file> <sensitive-file> <sensitive-file>"
    assert startup["bundle_manifest"] == {"warning": "manifest at <local-path>"}
    assert "active_hextech_csv" not in startup
    assert "hextech_refresh" not in startup
    assert "manifest_path" not in startup["bundle_manifest"]
    assert "C:/Users/apple" not in serialized
    assert "Hextech_Data.csv" not in serialized
    assert "auth_token.txt" not in serialized
    assert "local.yaml" not in serialized
    assert "proxies.json" not in serialized
    assert "accounts.json" not in serialized


@pytest.mark.parametrize(
    ("snapshot_status", "expected_status"),
    [
        ({"state": "ready", "generation_id": "g1", "private_stats_enabled": True}, "NO_STATS"),
        ({"state": "ready", "generation_id": "g1", "private_stats_enabled": False}, "PRIVATE_STATS_DISABLED"),
        ({"state": "degraded", "generation_id": "g0", "private_stats_enabled": True}, "GENERATION_DEGRADED"),
    ],
)
def test_public_hextech_empty_states_are_not_conflated(monkeypatch, snapshot_status, expected_status):
    client, web_api = _client()
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(web_api._snapshot_client, "status", lambda: snapshot_status)
    monkeypatch.setattr(
        web_api._snapshot_client,
        "open_view",
        lambda: _SnapshotView(snapshot_status),
    )

    payload = client.get("/api/champion/Garen/hextechs").json()

    assert payload["status"] == expected_status
    assert payload["generation_id"] == ("g1" if snapshot_status["state"] == "ready" else "g0")


def test_private_stats_disabled_does_not_expose_published_detail(monkeypatch):
    client, web_api = _client()
    status = {"state": "ready", "generation_id": "g1", "private_stats_enabled": False}
    view = _SnapshotView(status, detail={"augments": [{"id": "a1", "win_rate": 0.9}]})
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(web_api._snapshot_client, "open_view", lambda: view)

    payload = client.get("/api/champion/Garen/hextechs").json()

    assert payload["status"] == "PRIVATE_STATS_DISABLED"
    assert payload["comprehensive"] == []
    assert payload["generation_state"] == "ready"


def test_detail_loading_branch_requests_authenticated_preload_from_page():
    detail_js = (RUN_ROOT / "src/hextech/interfaces/web/backend/static/js/detail.js").read_text(encoding="utf-8")

    assert "function requestDetailPreload" in detail_js
    assert "fetch(`${API_BASE}/api/champion/${encodeURIComponent(hero)}/preload`" in detail_js
    assert "method: 'POST'" in detail_js
    assert "credentials: 'same-origin'" in detail_js
    assert "requestDetailPreload();" in detail_js


def test_detail_loading_retry_stops_after_max_attempts(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node required for detail.js retry behavior test")

    detail_path = (RUN_ROOT / "src/hextech/interfaces/web/backend/static/js/detail.js").as_posix()
    script = tmp_path / "detail_retry_test.cjs"
    script.write_text(
        f"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync({json.dumps(detail_path)}, 'utf8');
const tip = {{ textContent: '' }};
let scheduled = 0;
const fakeElement = () => ({{
  textContent: '',
  innerHTML: '',
  className: '',
  style: {{}},
  dataset: {{}},
  classList: {{ add: () => {{}}, remove: () => {{}}, contains: () => false }},
  removeAttribute: () => {{}},
  appendChild: () => {{}},
  remove: () => {{}},
  addEventListener: () => {{}},
  querySelector: () => fakeElement(),
}});
const context = {{
  console,
  window: {{
    location: {{ origin: 'http://127.0.0.1:8211', protocol: 'http:', host: '127.0.0.1:8211', search: '?hero=Garen' }},
    setTimeout: (_fn, _delay) => {{ scheduled += 1; return scheduled; }},
    clearTimeout: () => {{}},
    addEventListener: () => {{}},
  }},
  document: {{
    readyState: 'loading',
    body: {{ appendChild: () => {{}} }},
    createElement: () => fakeElement(),
    getElementById: () => fakeElement(),
    querySelector: (selector) => {{
      if (selector === '#noDataTip .text-sm') return tip;
      return fakeElement();
    }},
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
  fetch: () => Promise.resolve({{ json: () => Promise.resolve([]) }}),
  BroadcastChannel: class {{ constructor() {{}} postMessage() {{}} addEventListener() {{}} }},
  WebSocket: class {{}},
  URLSearchParams,
  setTimeout: (_fn, _delay) => {{ scheduled += 1; return scheduled; }},
  clearTimeout: () => {{}},
}};
context.window.window = context.window;
vm.createContext(context);
vm.runInContext(source, context);
context.scheduleDetailRetry(12);
if (scheduled !== 0) throw new Error(`scheduled after max retry: ${{scheduled}}`);
if (tip.textContent !== '数据准备时间较长，请稍后刷新页面') throw new Error(`unexpected tip: ${{tip.textContent}}`);
context.scheduleDetailRetry(11);
if (scheduled !== 1) throw new Error(`expected one scheduled retry, got ${{scheduled}}`);
""",
        encoding="utf-8",
    )

    subprocess.run([node, str(script)], check=True, cwd=RUN_ROOT)


def test_missing_asset_get_redirects_without_queueing_cache_write(monkeypatch, tmp_path):
    client, web_api = _client()
    queued: list[tuple[str, str]] = []

    monkeypatch.setattr(web_api.web_runtime, "get_assets_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        web_api.web_runtime,
        "find_augment_catalog_entry",
        lambda _name, _base_dir: {
            "name": "测试海克斯",
            "filename": "mapped.png",
            "icon_url": "https://raw.communitydragon.org/latest/game/mapped.png",
        },
    )
    monkeypatch.setattr(web_api.web_runtime, "find_existing_augment_asset_filename", lambda *_args: "")
    monkeypatch.setattr(
        web_api.web_runtime,
        "queue_augment_icon_cache",
        lambda filename, name="": queued.append((filename, name)),
    )
    monkeypatch.setattr(
        web_api.web_runtime,
        "resolve_remote_augment_icon_url",
        lambda _entry, _name: "https://raw.communitydragon.org/latest/game/mapped.png",
    )

    response = client.get("/assets/augments/requested.png", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://raw.communitydragon.org/latest/game/mapped.png"
    assert queued == []


def test_uncatalogued_asset_get_redirects_without_queueing_cache_write(monkeypatch, tmp_path):
    client, web_api = _client()
    queued: list[tuple[str, str]] = []

    monkeypatch.setattr(web_api.web_runtime, "get_assets_dir", lambda: str(tmp_path))
    monkeypatch.setattr(web_api.web_runtime, "find_augment_catalog_entry", lambda *_args: None)
    monkeypatch.setattr(web_api.web_runtime, "find_existing_augment_asset_filename", lambda *_args: "")
    monkeypatch.setattr(
        web_api.web_runtime,
        "queue_augment_icon_cache",
        lambda filename, name="": queued.append((filename, name)),
    )
    monkeypatch.setattr(
        web_api.web_runtime,
        "resolve_remote_augment_icon_url",
        lambda _entry, _name: "https://raw.communitydragon.org/latest/game/fallback.png",
    )

    response = client.get("/assets/augments/fallback.png", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://raw.communitydragon.org/latest/game/fallback.png"
    assert queued == []


def test_authenticated_preload_only_reads_snapshot_and_does_not_queue_assets(monkeypatch):
    from hextech.interfaces.web.backend import runtime

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(runtime, "resolve_canonical_hero_name", lambda _name: "Garen")
    monkeypatch.setattr(runtime._snapshot_client, "status", lambda: {"state": "ready", "generation_id": "g1"})
    monkeypatch.setattr(
        runtime._snapshot_client,
        "open_view",
        lambda: _SnapshotView(
            {"state": "ready", "generation_id": "g1"},
            detail={"augments": [{"id": "a1", "icon": "/assets/augments/mapped.png"}]},
        ),
    )
    monkeypatch.setattr(
        runtime,
        "queue_augment_icon_cache",
        lambda filename, name="": queued.append((filename, name)),
    )

    assert runtime.request_preload_hextech_payload_async("Garen") is True

    assert queued == []


def test_synergy_api_distinguishes_empty_error_and_quarantined(monkeypatch):
    client, web_api = _client()

    monkeypatch.setattr(web_api.web_runtime, "resolve_champion_id", lambda champ_id: str(champ_id))
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda champ_id: str(champ_id))

    monkeypatch.setattr(
        web_api._snapshot_client,
        "open_view",
        lambda: _SnapshotView({"state": "ready", "generation_id": "g1"}),
    )
    empty = client.get("/api/synergies/86").json()
    assert empty["status"] == "empty"
    assert empty["synergy_items"] == []

    def boom():
        raise RuntimeError("broken")

    broken_view = _SnapshotView({"state": "ready", "generation_id": "g1"})
    broken_view.get_synergy_data = boom
    monkeypatch.setattr(web_api._snapshot_client, "open_view", lambda: broken_view)
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
    monkeypatch.setattr(
        web_api._snapshot_client,
        "open_view",
        lambda: _SnapshotView({"state": "ready", "generation_id": "g1"}, synergy=duplicated),
    )
    quarantined = client.get("/api/synergies/86").json()
    assert quarantined["status"] == "quarantined"
    assert quarantined["synergy_items"] == []


def test_web_runtime_has_no_detail_builder_or_preload_executor(monkeypatch):
    client, web_api = _client()
    runtime = web_api.web_runtime
    monkeypatch.setattr(runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(runtime, "get_startup_status", lambda: {"hextech_ready": False})
    monkeypatch.setattr(web_api._snapshot_client, "status", lambda: {"state": "unavailable"})
    monkeypatch.setattr(web_api._snapshot_client, "open_view", _raise_snapshot_unavailable)
    monkeypatch.setattr(runtime._snapshot_client, "status", lambda: {"state": "unavailable"})
    monkeypatch.setattr(runtime._snapshot_client, "open_view", _raise_snapshot_unavailable)
    monkeypatch.setattr(runtime._snapshot_client, "get_champion_detail", lambda name: None)

    assert not hasattr(runtime, "process_hextechs_data")
    assert not hasattr(runtime, "_get_preloaded_hextech_executor")
    assert runtime.request_preload_hextech_payload_async("Garen") is False
    response = client.get("/api/champion/Garen/hextechs")
    payload = response.json()

    assert response.status_code == 200
    assert payload["loading"] is True
    assert payload["status"] == "DATA_NOT_READY"
    assert payload["preload_status"] == {}


def test_preloaded_reader_returns_snapshot_copy(monkeypatch, tmp_path):
    from hextech.interfaces.web.backend import runtime
    from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher

    DataSnapshotPublisher(tmp_path).publish(
        {
            "champions": [{"id": "86", "name": "Garen"}],
            "champion_hextech": {"Garen": {"hero_id": "86", "augments": [{"id": "a1", "name": "original"}]}},
            "overlay_hints": {"hints": {}, "augments": {}},
            "identities": {"champions": {"86": "Garen"}, "augments": {}},
        },
        private_stats_enabled=True,
    )
    monkeypatch.setattr(runtime, "resolve_canonical_hero_name", lambda name: "Garen")
    monkeypatch.setattr(runtime, "_snapshot_client", DataSnapshotClient(tmp_path))
    first = runtime.get_preloaded_hextech_payload("Garen")
    assert first is not None
    first["augments"][0]["name"] = "polluted"
    second = runtime.get_preloaded_hextech_payload("Garen")

    assert second is not None
    assert second["augments"][0]["name"] == "original"


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

"""web 域 pytest 开发门禁。"""

from types import SimpleNamespace

import pytest

from tests._dev_gate_support import (
    RUN_DIR,
    WEB_STATIC_DIR,
    _build_synergy_api_payload,
    load_augment_name_to_icon_map,
    patch,
    re,
    web_runtime,
)

pytestmark = pytest.mark.dev_gate

def test_safe_detail_name_regex() -> None:
    safe_names = ["德玛西亚之力", "Kai'Sa", "Lee Sin", "Dr. Mundo", "K-Sante"]
    unsafe_names = ["Kai_Sa", "<script>alert(1)</script>", "bad/name", "bad&name"]
    for value in safe_names:
        assert web_runtime._SAFE_NAME_RE.fullmatch(value), value
    for value in unsafe_names:
        assert not web_runtime._SAFE_NAME_RE.fullmatch(value), value

def test_detail_hero_param_uses_text_content() -> None:
    detail_text = (WEB_STATIC_DIR / "detail.html").read_text(encoding="utf-8")
    detail_script = (WEB_STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")

    assert '<script defer src="/static/js/detail.js"></script>' in detail_text
    assert "const urlParams = new URLSearchParams(window.location.search);" in detail_script
    assert "const hero = urlParams.get('hero');" in detail_script
    assert "document.getElementById('heroName').textContent = hero" in detail_script
    forbidden_patterns = [
        r"heroName['\"]\)\.innerHTML\s*=\s*hero",
        r"innerHTML\s*=\s*`[^`]*\$\{hero\}",
        r"innerHTML\s*=\s*hero\b",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, detail_text), pattern
        assert not re.search(pattern, detail_script), pattern

def test_detail_question_mark_augment_guard() -> None:
    detail_text = (WEB_STATIC_DIR / "detail.html").read_text(encoding="utf-8")
    detail_script = (WEB_STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")
    assert "function isQuestionMarkAugmentName(text)" in detail_script
    assert "/^[?？]{3,}$/" in detail_script
    assert "if (isQuestionMarkAugmentName(original))" in detail_script
    assert '<span class="${badgeText} opacity-70' not in detail_text
    assert '<span class="${badgeText} opacity-70' not in detail_script
    assert "dataset.synergyLoaded" in detail_script
    assert re.fullmatch(r"[?？]{3,}", "？？？")
    assert not re.fullmatch(r"[?？]{3,}", "？？？ 提升攻速 25%")

    icon_map = load_augment_name_to_icon_map()
    assert icon_map.get("？？？") == "/assets/missingping_small.png"

def test_detail_hextech_card_layout_contract() -> None:
    detail_script = (WEB_STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")
    style_source = (RUN_DIR / "frontend" / "src" / "styles" / "input.css").read_text(encoding="utf-8")
    compiled_style = (WEB_STATIC_DIR / "css" / "tailwind-compiled.css").read_text(encoding="utf-8")

    # 列表卡片的胜率数字必须独立居中；趋势箭头不能参与数字本身的中轴线计算。
    assert "hextech-card-rate--win" in detail_script
    assert "hextech-card-rate-value" in detail_script
    assert "hextech-card-rate-trend" in detail_script
    assert "w-16 text-right" not in detail_script
    assert "w-14 text-right" not in detail_script

    # 长海克斯文案（例如“高压锅”）必须由稳定行盒承载，不能依赖浏览器默认 normal 行高。
    for css in (style_source, compiled_style):
        assert ".hextech-list-card" in css and ("display: grid" in css or "display:grid" in css)
        assert ".hextech-card-rate" in css and ("justify-content: center" in css or "justify-content:center" in css)
        assert ".hextech-article-content" in css and ("line-height: 1.72" in css or "line-height:1.72" in css)
        assert ".hextech-tooltip-body" in css and ("line-height: 1.65" in css or "line-height:1.65" in css)
        assert "word-break: break-word" in css or "word-break:break-word" in css

    icon_map = load_augment_name_to_icon_map()
    assert icon_map.get("高压锅") == "/assets/questpressurecooker_small.png"

def test_web_bootstrap_avoids_load_event_gate() -> None:
    index_text = (WEB_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    detail_text = (WEB_STATIC_DIR / "detail.html").read_text(encoding="utf-8")
    index_script = (WEB_STATIC_DIR / "js" / "index.js").read_text(encoding="utf-8")
    detail_script = (WEB_STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")

    assert "window.onload =" not in index_text
    assert "window.onload =" not in detail_text
    assert "window.onload =" not in index_script
    assert "window.onload =" not in detail_script
    assert "cdn.tailwindcss.com" not in index_text
    assert "cdn.tailwindcss.com" not in detail_text
    assert 'href="/static/css/tailwind-compiled.css"' in index_text
    assert 'href="/static/css/tailwind-compiled.css"' in detail_text
    assert '<script defer src="/static/js/index.js"></script>' in index_text
    assert '<script defer src="/static/js/detail.js"></script>' in detail_text
    assert "function bootstrapIndexPage()" in index_script
    assert "function bootstrapDetailPage()" in detail_script
    assert "bootstrapIndexPage();" in index_script
    assert "bootstrapDetailPage();" in detail_script

def test_api_champions_reads_published_snapshot_without_web_rebuild() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

    app = FastAPI()
    web_api.register_routes(app)
    expected_payload = [{"英雄名称": "德玛西亚之力", "综合分数": 0.0}]

    class SnapshotClient:
        @staticmethod
        def open_view():
            return SimpleNamespace(get_champions=lambda: expected_payload)

    with (
        patch.object(web_api, "_snapshot_client", SnapshotClient()),
        patch.object(web_api.web_runtime, "get_df", side_effect=AssertionError("Web 不得读取运行时 CSV")),
        patch.object(web_api.web_runtime, "request_background_refresh", side_effect=AssertionError("Web 不得发起刷新")),
    ):
        response = TestClient(app).get("/api/champions")

    assert response.status_code == 200
    assert response.json() == expected_payload

def test_redirect_api_does_not_sync_preload_before_response() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

    class DummyManager:
        active = [object()]

        async def broadcast(self, message: dict) -> None:
            self.message = message

    app = FastAPI()
    web_api.register_routes(app)
    dummy_manager = DummyManager()

    with (
        patch.object(web_api.web_runtime, "get_request_auth_token", return_value="token"),
        patch.object(web_api.web_runtime, "get_champion_info", return_value=("德玛西亚之力", "Garen")),
        patch.object(web_api.web_runtime, "resolve_canonical_hero_name", side_effect=lambda value: str(value or "")),
        patch.object(web_api.web_runtime, "request_preload_hextech_payload", side_effect=AssertionError("redirect 热路径不应同步预加载")),
        patch.object(web_api.web_runtime, "manager", dummy_manager),
    ):
        response = TestClient(app).post(
            "/api/redirect",
            json={"hero_id": "86", "hero_name": "德玛西亚之力"},
            headers={"Origin": "http://127.0.0.1:8000", "X-Hextech-Token": "token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "broadcast_sent"
    assert dummy_manager.message["champion_id"] == "86"

def test_redirect_api_defers_browser_open_before_response() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

    class DummyManager:
        active = []

    app = FastAPI()
    web_api.register_routes(app)

    with (
        patch.object(web_api.web_runtime, "get_request_auth_token", return_value="token"),
        patch.object(web_api.web_runtime, "get_champion_info", return_value=("德玛西亚之力", "Garen")),
        patch.object(web_api.web_runtime, "resolve_canonical_hero_name", side_effect=lambda value: str(value or "")),
        patch.object(web_api.web_runtime, "request_preload_hextech_payload_async", return_value=True),
        patch.object(web_api.web_runtime, "manager", DummyManager()),
        patch.object(web_api.web_runtime, "build_detail_url", return_value="http://127.0.0.1:8000/detail.html?hero=x&id=86&en=Garen&auto=1"),
        patch.object(web_api.web_runtime, "request_open_managed_browser_async", return_value=True) as async_open,
        patch.object(web_api.web_runtime, "open_managed_browser", side_effect=AssertionError("redirect 热路径不应同步打开浏览器")),
    ):
        response = TestClient(app).post(
            "/api/redirect",
            json={"hero_id": "86", "hero_name": "德玛西亚之力"},
            headers={"Origin": "http://127.0.0.1:8000", "X-Hextech-Token": "token"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "opening_browser"
    assert response.json()["detail_first"] is True
    async_open.assert_called_once_with("http://127.0.0.1:8000/detail.html?hero=x&id=86&en=Garen&auto=1", replace_existing=True)

def test_redirect_api_handles_invalid_champion_input() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

    app = FastAPI()
    web_api.register_routes(app)

    with (
        patch.object(web_api.web_runtime, "get_request_auth_token", return_value="token"),
        patch.object(web_api.web_runtime, "get_champion_info", return_value=("", "")),
        patch.object(web_api.web_runtime, "resolve_canonical_hero_name", side_effect=ValueError("bad champion")),
    ):
        response = TestClient(app).post(
            "/api/redirect",
            json={"hero_id": "%", "hero_name": "%"},
            headers={"Origin": "http://127.0.0.1:8000", "X-Hextech-Token": "token"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_champion"

    with (
        patch.object(web_api.web_runtime, "get_request_auth_token", return_value="token"),
        patch.object(web_api.web_runtime, "get_champion_info", return_value=("德玛西亚之力", "Garen")),
        patch.object(web_api.web_runtime, "resolve_canonical_hero_name", return_value="德玛西亚之力"),
        patch.object(web_api.web_runtime, "request_preload_hextech_payload_async", return_value=True),
        patch.object(web_api.web_runtime, "manager", type("DummyManager", (), {"active": []})()),
        patch.object(web_api.web_runtime, "build_detail_url", side_effect=ValueError("invalid_hero_id")),
    ):
        response = TestClient(app).post(
            "/api/redirect",
            json={"hero_id": "%", "hero_name": "德玛西亚之力"},
            headers={"Origin": "http://127.0.0.1:8000", "X-Hextech-Token": "token"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_champion"

def test_detail_renders_before_deferred_icon_catalog() -> None:
    detail_script = (WEB_STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")
    load_start = detail_script.index("async function loadHextechs")
    load_end = detail_script.index("function connectWS()", load_start)
    load_body = detail_script[load_start:load_end]

    assert "renderCurrentView();" in load_body
    assert "loadAugmentIconMap().then" in load_body
    assert "await loadAugmentIconMap();" not in load_body
    assert load_body.index("renderCurrentView();") < load_body.index("loadAugmentIconMap().then")
    assert "DETAIL_LOADING_RETRY_BASE_MS" in detail_script
    assert "DETAIL_LOADING_RETRY_MAX_MS" in detail_script
    assert "scheduleDetailRetry" in detail_script
    assert "describeLoadingStatus" in detail_script
    assert "startup_status" in detail_script
    assert "preload_status" in detail_script
    dormant_start = detail_script.index("function dormancyDormant()")
    dormant_end = detail_script.index("function reactivateTab()", dormant_start)
    assert "clearDetailRetry();" in detail_script[dormant_start:dormant_end]

def test_synergy_api_quarantines_duplicate_pollution() -> None:
    polluted_items = [
        {
            "augment_names": ["蛋白粉奶昔"],
            "tier": "棱彩",
            "rating": "SS",
            "tag": "娱乐",
            "author": "ApexLoL",
            "is_original": True,
            "content": "蔚的护盾玩法，楚雨荨专属联动",
            "upvotes": 0,
            "downvotes": 0,
        }
    ]
    data = {
        "254": {"synergy_items": polluted_items},
        "112": {"synergy_items": polluted_items},
    }
    core = {
        "254": {"name": "皮城执法官", "title": "蔚", "en_name": "Vi", "aliases": ["楚雨荨"]},
        "112": {"name": "奥术先驱", "title": "维克托", "en_name": "Viktor", "aliases": ["vi"]},
    }

    with patch.object(web_runtime, "ensure_champion_cache", return_value=core):
        vi_payload = _build_synergy_api_payload(data, "254")
        viktor_payload = _build_synergy_api_payload(data, "112")

    assert len(vi_payload["synergy_items"]) == 1
    assert viktor_payload["status"] == "quarantined"
    assert viktor_payload["synergy_items"] == []
    assert viktor_payload["reason"] == "foreign_champion_terms"
    assert viktor_payload["match_types"] == {"254": "exact"}

    partial_viktor_items = [
        *polluted_items,
        {
            "augment_names": ["珠光护手"],
            "tier": "黄金",
            "rating": "S",
            "tag": "强力联动",
            "author": "ApexLoL",
            "is_original": True,
            "content": "蔚的爆发玩法",
            "upvotes": 0,
            "downvotes": 0,
        },
        {
            "augment_names": ["机械飞升"],
            "tier": "黄金",
            "rating": "A",
            "tag": "强力联动",
            "author": "ApexLoL",
            "is_original": True,
            "content": "普通法师联动",
            "upvotes": 0,
            "downvotes": 0,
        },
    ]
    partial_data = {
        "254": {"synergy_items": partial_viktor_items[:2]},
        "112": {"synergy_items": partial_viktor_items},
    }
    with patch.object(web_runtime, "ensure_champion_cache", return_value=core):
        partial_payload = _build_synergy_api_payload(partial_data, "112")

    assert partial_payload["status"] == "quarantined"
    assert partial_payload["reason"] == "foreign_champion_terms"
    assert partial_payload["match_types"] == {"254": "overlap"}

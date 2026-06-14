from __future__ import annotations

"""开发自检与手动验收入口。

这个模块是 `run/` 的统一验证入口：
- 默认模式执行离线、无外网、无浏览器依赖的结构与回归自检。
- `--bundle-manifest` 输出 bundle manifest 明细并校验关键字段。
- `--manual-web-synergy` 执行 Web/UI 详情页联动人工验收辅助检查。

它替代原先散落的 `run/tests/` 临时测试目录，以及独立的
`accept_web_synergy.py` / `verify_bundle_manifest.py` 工具入口。
"""

import argparse
import ast
import io
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from typing import Any
from unittest.mock import patch
from urllib.parse import quote

import requests
import pandas as pd

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

import processing.orchestrator as orchestrator
import processing.alias_search as alias_search
import processing.precomputed_cache as precomputed_cache
import processing.runtime_store as runtime_store
import scraping.full_synergy_scraper as synergy_scraper
import scraping.heal_worker as heal_worker
import scraping.icon_resolver as icon_resolver
import display.web_runtime as web_runtime
from display.web_api import _build_synergy_api_payload, _normalize_synergy_items, _synergy_item_to_compat_string
from processing.alias_search import load_manual_alias_index
from processing.view_adapter import process_hextechs_data
from scraping.full_hextech_scraper import extract_champion_stats
from scraping.full_synergy_scraper import (
    SYNERGY_REFRESH_META_VERSION,
    ApexSource,
    ChampionInfo,
    FetchedResource,
    SynergyEntry,
    SynergyExtractor,
    SynergyWriter,
    _load_json_file,
    _validate_publish_size,
    build_augment_name_map_from_static,
    build_champion_lookup,
    build_core_info,
    normalize_augment_name,
    normalize_slug,
    write_synergy_refresh_meta,
)
from tools.bundle_manifest import build_bundle_manifest
from tools.log_utils import install_summary_logging


TIER_IDS = ("Prismatic", "Gold", "Silver")


def check_root_entrypoints() -> None:
    root_scripts = {
        path.name
        for path in RUN_DIR.iterdir()
        if path.is_file() and path.suffix == ".py"
    }

    assert {"build.py", "hextech_ui.py", "web_server.py"}.issubset(root_scripts)
    assert (RUN_DIR / "display").exists()
    assert (RUN_DIR / "processing").exists()
    assert (RUN_DIR / "tools").exists()


def check_manual_alias_index() -> None:
    alias_file = RUN_DIR / "data" / "indexes" / "Champion_Alias_Index.json"
    payload = json.loads(alias_file.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert payload, "Champion_Alias_Index.json 应至少包含一条手工索引"
    first = payload[0]
    assert isinstance(first, dict)
    assert "heroName" in first
    assert load_manual_alias_index()


def check_manifest_icon_url_safety() -> None:
    assert icon_resolver.sanitize_augment_icon_url("/assets/safe-name_01.png") == "/assets/safe-name_01.png"
    assert icon_resolver.sanitize_augment_icon_url("https://raw.communitydragon.org/latest/game/assets/x.webp")
    assert icon_resolver.sanitize_augment_icon_url("https://cdn.communitydragon.org/latest/game/assets/x.webp")
    assert icon_resolver.sanitize_augment_icon_url("https://ddragon.leagueoflegends.com/cdn/1/img/champion/Aatrox.png")
    assert icon_resolver.sanitize_augment_icon_url("https://apexlol.info/images/hextech/example.webp")
    assert not icon_resolver.sanitize_augment_icon_url("http://raw.communitydragon.org/latest/game/assets/x.png")
    assert not icon_resolver.sanitize_augment_icon_url("https://evil.com/assets/x.png")
    assert not icon_resolver.sanitize_augment_icon_url("/assets/not-png.webp")
    assert not icon_resolver.sanitize_augment_icon_url("/assets/../secret.png")


def check_safe_detail_name_regex() -> None:
    safe_names = ["德玛西亚之力", "Kai'Sa", "Lee Sin", "Dr. Mundo", "K-Sante"]
    unsafe_names = ["Kai_Sa", "<script>alert(1)</script>", "bad/name", "bad&name"]
    for value in safe_names:
        assert web_runtime._SAFE_NAME_RE.fullmatch(value), value
    for value in unsafe_names:
        assert not web_runtime._SAFE_NAME_RE.fullmatch(value), value


def check_apexlol_hextech_map_size_limit() -> None:
    class OversizeResponse:
        headers = {"Content-Length": str(icon_resolver.MAX_APEXLOL_HEXTECH_MAP_BYTES + 1)}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int = 65536):
            yield b""

        def close(self) -> None:
            return None

    original_cache = icon_resolver._APEXLOL_MAP_CACHE
    try:
        with TemporaryDirectory() as tmp_dir:
            cached = {"cached": "slug"}
            icon_resolver._APEXLOL_MAP_CACHE = ("cached-path", 1.0, cached)
            with patch("scraping.icon_resolver.requests.get", return_value=OversizeResponse()):
                result = icon_resolver.load_apexlol_hextech_map(config_dir=tmp_dir, force_refresh=True)
            assert result == cached
            assert not (Path(tmp_dir) / "Augment_Apexlol_Map.json").exists()
    finally:
        icon_resolver._APEXLOL_MAP_CACHE = original_cache


def check_runtime_alias_persistence() -> None:
    original_runtime_alias_file = alias_search.RUNTIME_ALIAS_FILE
    original_alias_index_file = alias_search.CHAMPION_ALIAS_INDEX_FILE
    original_cache = alias_search._ALIAS_INDEX_CACHE
    try:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stable_alias_file = tmp_path / "Champion_Alias_Index.json"
            stable_alias_payload = [
                {
                    "heroName": "德玛西亚之力",
                    "title": "盖伦",
                    "enName": "Garen",
                    "heroId": "86",
                    "aliases": ["盖伦"],
                }
            ]
            stable_alias_file.write_text(json.dumps(stable_alias_payload, ensure_ascii=False), encoding="utf-8")
            core_file = tmp_path / "Champion_Core_Data.json"
            core_payload = {"86": {"name": "德玛西亚之力", "title": "盖伦", "en_name": "Garen", "aliases": ["盖伦"]}}
            core_file.write_text(json.dumps(core_payload, ensure_ascii=False), encoding="utf-8")
            stable_before = stable_alias_file.read_text(encoding="utf-8")
            core_before = core_file.read_text(encoding="utf-8")

            alias_search.CHAMPION_ALIAS_INDEX_FILE = str(stable_alias_file)
            alias_search.RUNTIME_ALIAS_FILE = str(tmp_path / "runtime" / "aliases.json")
            alias_search._ALIAS_INDEX_CACHE = ("", 0.0, [])
            added = alias_search.add_runtime_champion_alias(stable_alias_payload[0], "大宝剑")
            merged = alias_search.load_champion_alias_map(force_refresh=True)

            assert added
            assert "大宝剑" in merged["德玛西亚之力"]
            assert Path(alias_search.RUNTIME_ALIAS_FILE).exists()
            assert stable_alias_file.read_text(encoding="utf-8") == stable_before
            assert core_file.read_text(encoding="utf-8") == core_before
    finally:
        alias_search.RUNTIME_ALIAS_FILE = original_runtime_alias_file
        alias_search.CHAMPION_ALIAS_INDEX_FILE = original_alias_index_file
        alias_search._ALIAS_INDEX_CACHE = original_cache


def check_detail_hero_param_uses_text_content() -> None:
    detail_text = (RUN_DIR / "display" / "static" / "detail.html").read_text(encoding="utf-8")
    detail_script = (RUN_DIR / "display" / "static" / "js" / "detail.js").read_text(encoding="utf-8")

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


def check_detail_question_mark_augment_guard() -> None:
    detail_text = (RUN_DIR / "display" / "static" / "detail.html").read_text(encoding="utf-8")
    detail_script = (RUN_DIR / "display" / "static" / "js" / "detail.js").read_text(encoding="utf-8")
    assert "function isQuestionMarkAugmentName(text)" in detail_script
    assert "/^[?？]{3,}$/" in detail_script
    assert "if (isQuestionMarkAugmentName(original))" in detail_script
    assert '<span class="${badgeText} opacity-70' not in detail_text
    assert '<span class="${badgeText} opacity-70' not in detail_script
    assert "dataset.synergyLoaded" in detail_script
    assert re.fullmatch(r"[?？]{3,}", "？？？")
    assert not re.fullmatch(r"[?？]{3,}", "？？？ 提升攻速 25%")

    icon_map = json.loads((RUN_DIR / "data" / "indexes" / "augment.name-to-icon.v1.json").read_text(encoding="utf-8"))
    assert icon_map.get("？？？") == "/assets/missingping_small.png"


def check_static_css_single_mount_contract() -> None:
    index_text = (RUN_DIR / "display" / "static" / "index.html").read_text(encoding="utf-8")
    detail_text = (RUN_DIR / "display" / "static" / "detail.html").read_text(encoding="utf-8")
    web_server_text = (RUN_DIR / "display" / "web_server.py").read_text(encoding="utf-8")

    assert 'href="/static/css/hextech-theme.css"' in index_text
    assert 'href="/static/css/hextech-theme.css"' in detail_text
    assert 'app.mount("/css"' not in web_server_text


def check_web_bootstrap_avoids_load_event_gate() -> None:
    index_text = (RUN_DIR / "display" / "static" / "index.html").read_text(encoding="utf-8")
    detail_text = (RUN_DIR / "display" / "static" / "detail.html").read_text(encoding="utf-8")
    index_script = (RUN_DIR / "display" / "static" / "js" / "index.js").read_text(encoding="utf-8")
    detail_script = (RUN_DIR / "display" / "static" / "js" / "detail.js").read_text(encoding="utf-8")

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


def check_api_champions_uses_stable_catalog_before_network_snapshot() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import display.web_api as web_api

    app = FastAPI()
    web_api.register_routes(app)
    stable_df = pd.DataFrame([{"英雄名称": "德玛西亚之力"}])
    expected_payload = [{"英雄名称": "德玛西亚之力", "综合分数": 0.0}]

    with (
        patch.object(web_api.web_runtime, "get_df", return_value=pd.DataFrame()),
        patch.object(web_api.web_runtime, "request_background_refresh", return_value=True),
        patch.object(web_api.web_runtime, "get_stable_champion_catalog_df", return_value=stable_df),
        patch.object(web_api.web_runtime, "get_live_champion_snapshot_df", side_effect=AssertionError("不应在稳定目录可用前等待远端快照")),
        patch.object(web_api, "process_champions_data", return_value=expected_payload),
    ):
        response = TestClient(app).get("/api/champions")

    assert response.status_code == 200
    assert response.json() == expected_payload


def check_redirect_api_does_not_sync_preload_before_response() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import display.web_api as web_api

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


def check_redirect_api_defers_browser_open_before_response() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import display.web_api as web_api

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


def check_detail_api_defers_cold_local_processing() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import display.web_api as web_api

    app = FastAPI()
    web_api.register_routes(app)

    with (
        patch.object(web_api.web_runtime, "resolve_canonical_hero_name", return_value="德玛西亚之力"),
        patch.object(web_api.web_runtime, "get_preloaded_hextech_payload", return_value=None),
        patch.object(web_api.web_runtime, "get_preload_hextech_status", return_value={"ready": False, "pending": False}),
        patch.object(web_api, "is_precomputed_hextech_cache_loaded", return_value=False),
        patch.object(web_api, "load_precomputed_hextech_for_hero", side_effect=AssertionError("详情冷路径不应同步读取预计算详情缓存")),
        patch.object(web_api, "_request_precomputed_hextech_warm", return_value=True) as warm_cache,
        patch.object(web_api, "_request_precomputed_hextech_rebuild", return_value=True),
        patch.object(web_api.web_runtime, "request_background_refresh", return_value=True),
        patch.object(web_api.web_runtime, "request_preload_hextech_payload_async", return_value=True) as async_preload,
        patch.object(web_api.web_runtime, "get_live_hextech_snapshot_df", side_effect=AssertionError("本地数据可用时不应等待远端详情快照")),
        patch.object(web_api, "process_hextechs_data", side_effect=AssertionError("详情冷路径不应同步计算海克斯")),
    ):
        response = TestClient(app).get("/api/champion/%E5%BE%B7%E7%8E%9B%E8%A5%BF%E4%BA%9A%E4%B9%8B%E5%8A%9B/hextechs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["loading"] is True
    assert payload["ready"] is False
    assert payload["comprehensive"] == []
    warm_cache.assert_called_once()
    async_preload.assert_called_once_with("德玛西亚之力")


def check_detail_renders_before_deferred_icon_catalog() -> None:
    detail_script = (RUN_DIR / "display" / "static" / "js" / "detail.js").read_text(encoding="utf-8")
    load_start = detail_script.index("async function loadHextechs")
    load_end = detail_script.index("function connectWS()", load_start)
    load_body = detail_script[load_start:load_end]

    assert "renderCurrentView();" in load_body
    assert "loadAugmentIconMap().then" in load_body
    assert "await loadAugmentIconMap();" not in load_body
    assert load_body.index("renderCurrentView();") < load_body.index("loadAugmentIconMap().then")
    assert "DETAIL_LOADING_RETRY_MS" in detail_script
    assert "scheduleDetailRetry" in detail_script


def check_heal_worker_contract() -> None:
    assert hasattr(heal_worker, "heal_missing_artifacts")
    assert hasattr(heal_worker, "detect_missing_artifacts")


def check_logging_contract() -> None:
    fd, tmp_name = mkstemp(prefix="hextech-dev-", suffix=".log")
    os.close(fd)
    file_handler = None
    try:
        file_handler = logging.FileHandler(tmp_name, encoding="utf-8")
        stream_buffer = io.StringIO()
        stream_handler = logging.StreamHandler(stream_buffer)

        install_summary_logging(handlers=[file_handler, stream_handler])

        assert file_handler.level == logging.ERROR
        assert stream_handler.level == logging.WARNING
    finally:
        if file_handler is not None:
            file_handler.close()
        try:
            os.remove(tmp_name)
        except OSError:
            pass


def check_packaging_config() -> None:
    build_script = (RUN_DIR / "tools" / "build_bundle.py").read_text(encoding="utf-8")
    spec_text = (RUN_DIR / "Hextech伴生终端.spec").read_text(encoding="utf-8")

    assert "--hidden-import\", \"filelock\"" in build_script
    assert "filelock" in spec_text
    assert "display" in (RUN_DIR / "tools" / "bundle_manifest.py").read_text(encoding="utf-8")


def check_ui_feature_flags_contract() -> None:
    """验证双开关运行态配置的默认值、持久化和未知字段收口。"""
    import processing.ui_feature_flags as ui_feature_flags

    with TemporaryDirectory() as tmp_dir:
        flags_path = Path(tmp_dir) / "ui_feature_flags.json"
        defaults = ui_feature_flags.load_ui_feature_flags(flags_path)

        assert defaults["web_frontend_enabled"] is False
        assert defaults["game_overlay_enabled"] is False
        assert defaults["auto_open_browser"] is True
        assert defaults["private_policy_stats_enabled"] is False
        assert defaults["low_frequency_listener_enabled"] is True

        ui_feature_flags.save_ui_feature_flags(
            {
                "web_frontend_enabled": True,
                "game_overlay_enabled": True,
                "private_policy_stats_enabled": True,
                "unknown": "ignored",
            },
            flags_path,
        )
        loaded = ui_feature_flags.load_ui_feature_flags(flags_path)

        assert loaded["web_frontend_enabled"] is True
        assert loaded["game_overlay_enabled"] is True
        assert loaded["auto_open_browser"] is True
        assert loaded["private_policy_stats_enabled"] is True
        assert "unknown" not in loaded


def check_overlay_hint_cache_contract() -> None:
    """验证 overlay hint cache 可直接查询，且默认不暴露私用统计字段。"""
    import processing.overlay_hint_cache as overlay_hint_cache

    sample_payload = {
        "德玛西亚之力": {
            "comprehensive": [
                {
                    "海克斯ID": "augment_001",
                    "海克斯名称": "珠光护手",
                    "海克斯阶级": "Gold",
                    "tooltip_plain": "技能可以暴击。",
                    "源站排名": 2,
                    "综合得分": 1.25,
                    "海克斯胜率": 0.551,
                }
            ]
        }
    }

    public_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=False,
        source_tag="dev-check",
    )
    public_hint = overlay_hint_cache.query_overlay_hint(public_cache, "augment_001")

    assert public_cache["schema_version"] == 1
    assert public_cache["source"]["tag"] == "dev-check"
    assert public_hint["ok"] is True
    assert public_hint["hint"]["name"] == "珠光护手"
    assert public_hint["hint"]["summary"] == "技能可以暴击。"
    assert "winrate" not in public_hint["hint"]
    assert "rank" not in public_hint["hint"]
    assert "score" not in public_hint["hint"]

    private_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=True,
        source_tag="dev-check",
    )
    private_hint = overlay_hint_cache.query_overlay_hint(private_cache, "augment_001")
    assert private_hint["hint"]["winrate"] == 0.551
    assert private_hint["hint"]["rank"] == 2
    assert private_hint["hint"]["score"] == 1.25

    missing = overlay_hint_cache.query_overlay_hint({}, "augment_404")
    assert missing == {"ok": False, "error": "cache_missing", "augment_id": "augment_404"}
    expired_cache = dict(public_cache)
    expired_cache["generated_at"] = time.time() - overlay_hint_cache.CACHE_MAX_AGE_SECONDS - 1
    expired = overlay_hint_cache.query_overlay_hint(expired_cache, "augment_001")
    assert expired == {"ok": False, "error": "cache_expired", "augment_id": "augment_001"}

    with TemporaryDirectory() as tmp_dir:
        missing_path = Path(tmp_dir) / "missing.json"
        damaged_path = Path(tmp_dir) / "damaged.json"
        damaged_path.write_text("{bad-json", encoding="utf-8")
        missing_payload = overlay_hint_cache.load_overlay_hint_cache(missing_path)
        damaged_payload = overlay_hint_cache.load_overlay_hint_cache(damaged_path)
        assert overlay_hint_cache.query_overlay_hint(missing_payload, "augment_404")["error"] == "cache_missing"
        assert overlay_hint_cache.query_overlay_hint(damaged_payload, "augment_404")["error"] == "cache_damaged"

    module_text = (RUN_DIR / "processing" / "overlay_hint_cache.py").read_text(encoding="utf-8")
    assert "requests" not in module_text
    assert "full_hextech_scraper" not in module_text


def check_overlay_event_channel_contract() -> None:
    """验证 overlay 本地事件通道可写、可读、可诊断，且固定为三槽位。"""
    import processing.overlay_event_channel as overlay_event_channel

    hint_cache = {
        "schema_version": 1,
        "generated_at": time.time(),
        "source": {"tag": "dev-check", "private_policy_stats_enabled": False},
        "hints": {
            "augment_001": {
                "augment_id": "augment_001",
                "name": "珠光护手",
                "tier": "Gold",
                "summary": "技能可以暴击。",
            }
        },
    }
    event = overlay_event_channel.build_overlay_event(
        [{"slot": 0, "augment_id": "augment_001"}],
        source_tag="dev-check",
        hint_cache=hint_cache,
    )
    assert event["schema_version"] == overlay_event_channel.SCHEMA_VERSION
    assert event["source"]["tag"] == "dev-check"
    assert event["active"] is True
    assert event["selection_type"] == "hextech"
    assert len(event["slots"]) == 3
    assert event["slots"][0]["name"] == "珠光护手"
    assert event["slots"][0]["summary"] == "技能可以暴击。"
    assert event["slots"][1]["state"] == "empty"

    with TemporaryDirectory() as tmp_dir:
        event_path = Path(tmp_dir) / "overlay-event.json"
        overlay_event_channel.write_overlay_event(event, event_path)
        snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert snapshot["ok"] is True
        assert snapshot["visible"] is True
        assert snapshot["selection_type"] == "hextech"
        assert len(snapshot["slots"]) == 3
        assert snapshot["slots"][0]["name"] == "珠光护手"

        fake_path = Path(tmp_dir) / "fake-detection.json"
        fake_written = overlay_event_channel.write_fake_detection_overlay_event(fake_path)
        assert fake_written == fake_path
        fake_snapshot = overlay_event_channel.read_overlay_event(fake_path)
        assert fake_snapshot["ok"] is True
        assert fake_snapshot["visible"] is True
        assert fake_snapshot["source"]["tag"] == "fake-detection"
        assert [slot["state"] for slot in fake_snapshot["slots"]] == ["ready", "ready", "ready"]
        assert fake_snapshot["slots"][0]["name"] == "假识别海克斯 A"

        body_shard_event = overlay_event_channel.build_overlay_event(
            [{"slot": 0, "name": "锻体样例", "summary": "锻体碎片选择使用同一通道，不复用海克斯文案。"}],
            source_tag="dev-check",
            selection_type="锻体碎片选择",
            active=True,
        )
        body_shard_path = Path(tmp_dir) / "body-shard.json"
        overlay_event_channel.write_overlay_event(body_shard_event, body_shard_path)
        body_shard_snapshot = overlay_event_channel.read_overlay_event(body_shard_path)
        assert body_shard_snapshot["ok"] is True
        assert body_shard_snapshot["visible"] is False
        assert body_shard_snapshot["selection_type"] == "body_shard"
        assert body_shard_snapshot["selection_label"] == "锻体碎片选择"

        inactive_path = Path(tmp_dir) / "inactive.json"
        overlay_event_channel.write_inactive_overlay_event(inactive_path)
        inactive_snapshot = overlay_event_channel.read_overlay_event(inactive_path)
        assert inactive_snapshot["ok"] is True
        assert inactive_snapshot["visible"] is False

        missing_snapshot = overlay_event_channel.read_overlay_event(Path(tmp_dir) / "missing.json")
        assert missing_snapshot["ok"] is False
        assert missing_snapshot["visible"] is False
        assert missing_snapshot["error"] == "event_missing"

        damaged_path = Path(tmp_dir) / "damaged.json"
        damaged_path.write_text("{bad-json", encoding="utf-8")
        damaged_snapshot = overlay_event_channel.read_overlay_event(damaged_path)
        assert damaged_snapshot["ok"] is False
        assert damaged_snapshot["error"] == "event_damaged"

        expired_event = dict(event)
        expired_event["generated_at"] = time.time() - overlay_event_channel.EVENT_MAX_AGE_SECONDS - 1
        expired_path = Path(tmp_dir) / "expired.json"
        overlay_event_channel.write_overlay_event(expired_event, expired_path)
        expired_snapshot = overlay_event_channel.read_overlay_event(expired_path)
        assert expired_snapshot["ok"] is False
        assert expired_snapshot["error"] == "event_expired"

    module_text = (RUN_DIR / "processing" / "overlay_event_channel.py").read_text(encoding="utf-8")
    assert "requests" not in module_text
    assert "cv2" not in module_text
    assert "from processing.runtime_store" not in module_text
    assert "import processing.runtime_store" not in module_text
    assert "from processing.overlay_hint_cache" not in module_text


def check_official_overlay_provider_contract() -> None:
    """验证官方接口 provider 只做本地接口归一化，并通过现有 overlay 事件协议输出。"""
    import processing.official_overlay_provider as official_overlay_provider
    import processing.overlay_event_channel as overlay_event_channel
    import tools.probe_official_overlay_provider as probe_official_overlay_provider

    direct_payload = {
        "augments": {
            "augment_1": {"id": "augment_a", "name": "官方海克斯 A"},
            "augment_2": {"augmentId": "augment_b", "displayName": "官方海克斯 B"},
            "augment_3": {"augment_id": "augment_c", "title": "官方海克斯 C"},
        }
    }
    direct_snapshot = official_overlay_provider.extract_official_augment_candidates(direct_payload)
    assert direct_snapshot["status"] == "candidates_ready"
    assert [choice["augment_id"] for choice in direct_snapshot["choices"]] == ["augment_a", "augment_b", "augment_c"]
    assert [choice["slot"] for choice in direct_snapshot["choices"]] == [0, 1, 2]

    nested_payloads = [
        {
            "gameData": {
                "augments": {
                    "availableAugments": [
                        {"id": "aa", "name": "可选 A"},
                        {"id": "bb", "name": "可选 B"},
                        {"id": "cc", "name": "可选 C"},
                    ]
                }
            }
        },
        {
            "selection": {
                "choices": [
                    {"hextechId": "choice_a", "name": "选择 A"},
                    {"hextechId": "choice_b", "name": "选择 B"},
                    {"hextechId": "choice_c", "name": "选择 C"},
                ]
            }
        },
        {
            "selection": {
                "options": [
                    "选项 A",
                    "选项 B",
                    "选项 C",
                ]
            }
        },
    ]
    for payload in nested_payloads:
        snapshot = official_overlay_provider.extract_official_augment_candidates(payload)
        assert snapshot["status"] == "candidates_ready"
        assert len(snapshot["choices"]) == 3
        assert snapshot["diagnostics"]["field_paths"]

    picked_only = official_overlay_provider.extract_official_augment_candidates(
        {"picked_augment": {"id": "already_selected", "name": "已选海克斯"}}
    )
    assert picked_only["status"] == "active_no_candidates"
    assert len(picked_only["choices"]) == 3
    assert all(choice["state"] == "empty" for choice in picked_only["choices"])

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self) -> dict:
            return self._payload

    def fake_unauthorized_fetch(_url: str, _headers: dict) -> FakeResponse:
        return FakeResponse(401, {"message": "secret-token must never leak"})

    lcu_unauthorized = official_overlay_provider.LcuSnapshotClient(
        credential_provider=lambda: ("1234", "secret-token"),
        fetch_response=fake_unauthorized_fetch,
    ).get_snapshot()
    serialized_lcu = json.dumps(lcu_unauthorized, ensure_ascii=False)
    assert lcu_unauthorized["status"] == "error"
    assert "secret-token" not in serialized_lcu
    assert "Authorization" not in serialized_lcu

    lcu_missing = official_overlay_provider.LcuSnapshotClient(
        credential_provider=lambda: ("1234", "secret-token"),
        fetch_response=lambda _url, _headers: FakeResponse(404, {}),
    ).get_snapshot()
    assert lcu_missing["status"] == "unavailable"

    def fake_connection_failure(_url: str, _headers: dict) -> FakeResponse:
        raise RuntimeError("connection failed with secret-token")

    lcu_connection_failure = official_overlay_provider.LcuSnapshotClient(
        credential_provider=lambda: ("1234", "secret-token"),
        fetch_response=fake_connection_failure,
    ).get_snapshot()
    serialized_failure = json.dumps(lcu_connection_failure, ensure_ascii=False)
    assert lcu_connection_failure["status"] == "unavailable"
    assert "secret-token" not in serialized_failure

    with TemporaryDirectory() as tmp_dir:
        event_path = Path(tmp_dir) / "official-overlay-event.json"
        written = probe_official_overlay_provider.write_official_overlay_event(direct_snapshot, event_path=event_path)
        assert written == event_path
        event_snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert event_snapshot["visible"] is True
        assert event_snapshot["source"]["tag"] == "official-api"
        assert [slot["augment_id"] for slot in event_snapshot["slots"]] == ["augment_a", "augment_b", "augment_c"]

        class FakeProvider:
            def __init__(self) -> None:
                self._snapshots = [direct_snapshot, picked_only]

            def get_snapshot(self) -> dict:
                return self._snapshots.pop(0) if self._snapshots else picked_only

        now = [0.0]

        def fake_time() -> float:
            return now[0]

        def fake_sleep(seconds: float) -> None:
            now[0] += seconds

        summary = probe_official_overlay_provider.run_probe(
            duration_seconds=0.05,
            interval_ms=50,
            dump_runtime_json=False,
            write_event=True,
            provider=FakeProvider(),
            event_path=event_path,
            time_func=fake_time,
            sleep_func=fake_sleep,
            emit_snapshots=False,
        )
        assert summary["statuses"] == ["candidates_ready", "active_no_candidates"]
        assert len(summary["event_writes"]) == 2
        inactive_snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert inactive_snapshot["visible"] is False
        assert inactive_snapshot["source"]["tag"] == "official-api"


def check_overlay_vision_sidecar_contract() -> None:
    """验证 Vision MVP 的 ROI 预设、Pillow 指纹识别和低置信度保护。"""
    from PIL import Image, ImageDraw

    import processing.overlay_vision_sidecar as overlay_vision_sidecar

    required_presets = {
        "1920x1080": (1920, 1080),
        "2560x1440": (2560, 1440),
        "2560x1600": (2560, 1600),
    }
    for preset_name, size in required_presets.items():
        preset = overlay_vision_sidecar.resolve_roi_preset(*size, preset="auto")
        assert preset.name == preset_name
        assert len(preset.slots) == 3
        for box in preset.slot_boxes(size):
            left, top, right, bottom = box
            assert 0 <= left < right <= size[0]
            assert 0 <= top < bottom <= size[1]

    assert overlay_vision_sidecar.resolve_roi_preset(1707, 1067, preset="auto").name == "2560x1600"

    def _paint_selection_button(image: Image.Image) -> tuple[int, int, int, int]:
        draw = ImageDraw.Draw(image)
        box = (
            int(image.size[0] * 0.45),
            int(image.size[1] * 0.80),
            int(image.size[0] * 0.55),
            int(image.size[1] * 0.84),
        )
        draw.rounded_rectangle(box, radius=14, fill="#168fcf", outline="#54d5ff", width=4)
        return box

    def _paint_diagonal_blue_noise(image: Image.Image) -> None:
        draw = ImageDraw.Draw(image)
        width, height = image.size
        run_width = int(width * 0.07)
        start_x = int(width * 0.38)
        start_y = int(height * 0.76)
        for row in range(int(height * 0.04)):
            x = start_x + row * 8
            y = start_y + row
            draw.line((x, y, x + run_width, y), fill="#168fcf")

    # 模板必须带形状：归一化指纹只看形状，纯色模板是平坦图会被正确剔除。
    def _make_glyph_template(shape: str) -> Image.Image:
        image = Image.new("RGB", (72, 72), "#10131a")
        glyph = ImageDraw.Draw(image)
        if shape == "ellipse":
            glyph.ellipse((12, 12, 60, 60), fill="#e8e2cf")
        elif shape == "bars":
            for offset in (10, 30, 50):
                glyph.rectangle((offset, 8, offset + 10, 64), fill="#e8e2cf")
        else:
            glyph.polygon((36, 8, 64, 60, 8, 60), fill="#e8e2cf")
        return image

    templates = {
        "augment_a": _make_glyph_template("ellipse"),
        "augment_b": _make_glyph_template("bars"),
        "augment_c": _make_glyph_template("triangle"),
    }
    template_index = overlay_vision_sidecar.build_template_index(
        {
            "augment_a": {"name": "合成海克斯 A", "tier": "Gold", "summary": "合成测试 A", "image": templates["augment_a"]},
            "augment_b": {"name": "合成海克斯 B", "tier": "Prismatic", "summary": "合成测试 B", "image": templates["augment_b"]},
            "augment_c": {"name": "合成海克斯 C", "tier": "Silver", "summary": "合成测试 C", "image": templates["augment_c"]},
        }
    )
    assert len(template_index) == 3
    # 纯色模板没有形状指纹，应被剔除而不是留下假阳性源。
    assert overlay_vision_sidecar.build_template_index(
        {"flat": {"name": "平坦模板", "image": Image.new("RGB", (72, 72), "#2f6fed")}}
    ) == []

    text_template_index = overlay_vision_sidecar.build_template_index(
        {
            "eureka": {"name": "尤里卡", "tier": "Prismatic", "summary": "文字通道测试", "image": templates["augment_a"]},
            "fey": {"name": "精怪魔法", "tier": "Gold", "summary": "文字通道测试", "image": templates["augment_b"]},
            "noise": {"name": "重量级打击手", "tier": "Gold", "summary": "文字通道测试", "image": templates["augment_c"]},
        }
    )
    decorated_name = Image.new("RGB", (295, 48), "#001010")
    name_mask = overlay_vision_sidecar._render_name_mask("尤里卡")
    assert name_mask is not None
    decorated_name.paste(Image.merge("RGB", (name_mask, name_mask, name_mask)), (42, 0))
    ImageDraw.Draw(decorated_name).rectangle((280, 0, 283, 47), fill="#d8b36f")
    _, name_ranked = overlay_vision_sidecar._rank_name_templates(decorated_name, text_template_index)
    assert name_ranked[0][0].name == "尤里卡"
    assert name_ranked[0][1] >= 0.80

    frame = Image.new("RGB", (2560, 1600), "#070b12")
    _paint_selection_button(frame)
    preset = overlay_vision_sidecar.resolve_roi_preset(2560, 1600, preset="auto")
    for slot_index, box in enumerate(preset.slot_boxes(frame.size)):
        left, top, right, bottom = box
        # ROI 即图标框：模板按整框贴入，与真实几何（crop ≈ 图标整图）一致。
        frame.paste(templates[f"augment_{chr(ord('a') + slot_index)}"].resize((right - left, bottom - top)), (left, top))

    with TemporaryDirectory() as tmp_dir:
        calibration_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        detection = overlay_vision_sidecar.detect_overlay_choices(
            frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
        assert calibration_path.is_file()
        cached_detection = overlay_vision_sidecar.detect_overlay_choices(
            frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
        assert cached_detection["source"]["calibration"] == "cached"
    assert detection["active"] is True
    assert detection["selection_type"] == "hextech"
    assert detection["source"]["tag"] == "vision-sidecar"
    assert detection["source"]["preset"] == "2560x1600"
    assert detection["source"]["capture_size"] == [2560, 1600]
    assert detection["source"]["calibration"] == "calibrated"
    assert [slot["augment_id"] for slot in detection["slots"]] == ["augment_a", "augment_b", "augment_c"]

    blank = Image.new("RGB", (2560, 1600), "#070b12")
    _paint_selection_button(blank)
    with TemporaryDirectory() as tmp_dir:
        blank_detection = overlay_vision_sidecar.detect_overlay_choices(
            blank,
            template_index,
            preset_name="2560x1600",
            min_confidence=0.80,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    assert blank_detection["active"] is False
    assert all(slot["state"] != "ready" for slot in blank_detection["slots"])

    no_button = Image.new("RGB", (2560, 1600), "#070b12")
    with TemporaryDirectory() as tmp_dir:
        calibration_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        no_button_detection = overlay_vision_sidecar.detect_overlay_choices(
            no_button,
            template_index,
            preset_name="2560x1600",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
    assert no_button_detection["active"] is False
    assert no_button_detection["source"]["reason"] == "anchor_missing"

    scattered_blue = Image.new("RGB", (2560, 1600), "#070b12")
    _paint_diagonal_blue_noise(scattered_blue)
    assert overlay_vision_sidecar.detect_selection_button_box(scattered_blue) is None
    with TemporaryDirectory() as tmp_dir:
        calibration_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        scattered_detection = overlay_vision_sidecar.detect_overlay_choices(
            scattered_blue,
            template_index,
            preset_name="2560x1600",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
        assert not calibration_path.exists()
    assert scattered_detection["active"] is False
    assert scattered_detection["source"]["reason"] == "anchor_missing"

    missing_button = frame.copy()
    button_payload = overlay_vision_sidecar.build_anchor_calibration_payload(frame, preset_name="auto")
    assert button_payload is not None
    with TemporaryDirectory() as tmp_dir:
        calibration_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        overlay_vision_sidecar.write_anchor_calibration(button_payload, calibration_path)
        calibration = overlay_vision_sidecar._coerce_anchor_calibration(button_payload, frame.size)
        assert calibration is not None
        button_box = calibration.button_box
        ImageDraw.Draw(missing_button).rectangle(button_box, fill="#070b12")
        missing_button_detection = overlay_vision_sidecar.detect_overlay_choices(
            missing_button,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
    assert missing_button_detection["active"] is False
    assert missing_button_detection["source"]["reason"] == "selection_button_missing"

    poisoned_payload = dict(button_payload)
    poisoned_payload["button_box"] = [0.585, 0.80, 0.665, 0.84]
    with TemporaryDirectory() as tmp_dir:
        calibration_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        overlay_vision_sidecar.write_anchor_calibration(poisoned_payload, calibration_path)
        healed_detection = overlay_vision_sidecar.detect_overlay_choices(
            frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
        healed_payload = overlay_vision_sidecar.load_anchor_calibration(calibration_path)
    assert healed_detection["active"] is True
    assert healed_detection["source"]["calibration"] == "recalibrated"
    assert healed_payload is not None
    healed_button_box = healed_payload["button_box"]
    healed_center_x = (float(healed_button_box[0]) + float(healed_button_box[2])) / 2.0
    assert 0.42 <= healed_center_x <= 0.58

    # 载入画面杂乱内容也能蹭到 low_confidence；空占位框不得触发 active 显示。
    assert (
        overlay_vision_sidecar._scene_active_from_slots(
            [{"state": "low_confidence", "confidence": 0.7} for _ in range(3)]
        )
        is False
    )
    assert (
        overlay_vision_sidecar._scene_active_from_slots(
            [{"state": "ready", "confidence": 0.9}, {"state": "low_confidence", "confidence": 0.6}, {"state": "empty"}]
        )
        is True
    )

    # 搜索区内但偏上的实心蓝条（卡片描述区高度）不是底部按钮，必须被垂直下带约束拒绝。
    mid_band = Image.new("RGB", (2560, 1600), "#070b12")
    ImageDraw.Draw(mid_band).rounded_rectangle(
        (int(2560 * 0.45), int(1600 * 0.56), int(2560 * 0.55), int(1600 * 0.60)),
        radius=14,
        fill="#168fcf",
    )
    assert overlay_vision_sidecar.detect_selection_button_box(mid_band) is None

    body_shard_frame = Image.new("RGB", (2560, 1600), "#070b12")
    _paint_selection_button(body_shard_frame)
    shard_icon = _make_glyph_template("bars")
    for box in preset.slot_boxes(body_shard_frame.size):
        left, top, right, bottom = box
        body_shard_frame.paste(shard_icon.resize((right - left, bottom - top)), (left, top))
    with TemporaryDirectory() as tmp_dir:
        body_shard_detection = overlay_vision_sidecar.detect_overlay_choices(
            body_shard_frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    assert body_shard_detection["active"] is False
    assert body_shard_detection["selection_type"] == "body_shard"
    assert body_shard_detection["source"]["reason"] == "body_shard_only"

    # ESC 暗色菜单场景：接近平坦的深色面板必须被方差门槛拒绝，不得误报 active。
    dark_panel = Image.new("RGB", (2560, 1600), "#1d2026")
    _paint_selection_button(dark_panel)
    with TemporaryDirectory() as tmp_dir:
        dark_detection = overlay_vision_sidecar.detect_overlay_choices(
            dark_panel,
            template_index,
            preset_name="2560x1600",
            min_confidence=0.80,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    assert dark_detection["active"] is False
    assert all(slot["state"] != "ready" for slot in dark_detection["slots"])

    # 孪生图标：margin 归零但置信度极高，应走槽位豁免判 ready 而不是永远识别不出。
    twin_index = overlay_vision_sidecar.build_template_index(
        {
            "twin_a": {"name": "孪生 A", "image": _make_glyph_template("ellipse")},
            "twin_b": {"name": "孪生 B", "image": _make_glyph_template("ellipse")},
        }
    )
    twin_slot = overlay_vision_sidecar._detect_slot(frame, preset.slot_boxes(frame.size)[0], 0, twin_index, min_confidence=0.80)
    assert twin_slot["state"] == "ready"
    assert twin_slot["name"] in {"孪生 A", "孪生 B"}

    # 槽位判定真值表：平坦拒绝、低置信度拒绝、margin 不足只接受极高置信度。
    assert overlay_vision_sidecar._slot_match_decision(5.0, 0.99, 0.5, min_confidence=0.80) is False
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.70, 0.5, min_confidence=0.80) is False
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.85, 0.001, min_confidence=0.80) is False
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.85, 0.05, min_confidence=0.80) is True
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.95, 0.0, min_confidence=0.80) is True

    # 中文名必须能生成稳定 ID；ASCII-only 归一化曾把 206/208 个模板滤成空导致识别不可用。
    assert overlay_vision_sidecar.normalize_augment_id("魄罗爆破手") == "魄罗爆破手"
    real_index = overlay_vision_sidecar.load_default_template_index()
    assert len(real_index) >= 100, f"真实模板索引过小: {len(real_index)}"

    # 退出防抖：active 掉到 unstable 的前几帧延迟写事件，其余情况不延迟。
    assert overlay_vision_sidecar.should_defer_unstable_event(("active", "a", "b", "c"), 1) is True
    assert overlay_vision_sidecar.should_defer_unstable_event(("active", "a", "b", "c"), 2) is True
    assert overlay_vision_sidecar.should_defer_unstable_event(("active", "a", "b", "c"), 3) is False
    assert overlay_vision_sidecar.should_defer_unstable_event(("inactive", "unstable"), 1) is False
    assert overlay_vision_sidecar.should_defer_unstable_event(None, 1) is False

    stable = overlay_vision_sidecar.stabilize_detections([detection, detection], required_frames=2)
    assert stable["active"] is True
    assert stable["slots"][1]["augment_id"] == "augment_b"
    unstable = overlay_vision_sidecar.stabilize_detections([detection, blank_detection], required_frames=2)
    assert unstable["active"] is False
    active_signature = overlay_vision_sidecar._loop_event_signature(stable)
    inactive_signature = overlay_vision_sidecar._loop_event_signature(unstable)
    assert active_signature == ("active", "ready:augment_a", "ready:augment_b", "ready:augment_c")
    assert inactive_signature == ("inactive", "unstable")
    assert overlay_vision_sidecar.should_write_loop_event(
        stable,
        last_signature=None,
        last_write_at=0.0,
        now=1000.0,
        heartbeat_seconds=60.0,
    ) is True
    assert overlay_vision_sidecar.should_write_loop_event(
        stable,
        last_signature=active_signature,
        last_write_at=1000.0,
        now=1030.0,
        heartbeat_seconds=60.0,
    ) is False
    assert overlay_vision_sidecar.should_write_loop_event(
        stable,
        last_signature=active_signature,
        last_write_at=1000.0,
        now=1061.0,
        heartbeat_seconds=60.0,
    ) is True
    assert overlay_vision_sidecar.should_write_loop_event(
        unstable,
        last_signature=active_signature,
        last_write_at=1000.0,
        now=1001.0,
        heartbeat_seconds=60.0,
    ) is True
    assert overlay_vision_sidecar.should_write_loop_event(
        unstable,
        last_signature=None,
        last_write_at=0.0,
        now=1001.0,
        heartbeat_seconds=60.0,
    ) is True
    assert overlay_vision_sidecar.should_write_loop_event(
        unstable,
        last_signature=inactive_signature,
        last_write_at=1001.0,
        now=1002.0,
        heartbeat_seconds=60.0,
    ) is False

    class StopLoop(Exception):
        pass

    def stop_after_first_idle(_seconds: float) -> None:
        raise StopLoop()

    with TemporaryDirectory() as tmp_dir:
        missing_event_path = Path(tmp_dir) / "missing-window.json"
        with (
            patch.object(overlay_vision_sidecar, "load_default_template_index", return_value=template_index),
            patch.object(overlay_vision_sidecar, "_find_lol_game_window", return_value=None),
            patch.object(overlay_vision_sidecar.time, "sleep", side_effect=stop_after_first_idle),
        ):
            try:
                overlay_vision_sidecar.run_loop(write_event=True, event_path=missing_event_path)
            except StopLoop:
                pass
        missing_payload = json.loads(missing_event_path.read_text(encoding="utf-8"))
        assert missing_payload["active"] is False
        assert missing_payload["source"]["reason"] == "game_window_missing"

        background_event_path = Path(tmp_dir) / "background-window.json"
        with (
            patch.object(overlay_vision_sidecar, "load_default_template_index", return_value=template_index),
            patch.object(overlay_vision_sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 100, 100))),
            patch.object(overlay_vision_sidecar, "_is_lol_game_foreground", return_value=False),
            patch.object(overlay_vision_sidecar.time, "sleep", side_effect=stop_after_first_idle),
        ):
            try:
                overlay_vision_sidecar.run_loop(write_event=True, event_path=background_event_path)
            except StopLoop:
                pass
        background_payload = json.loads(background_event_path.read_text(encoding="utf-8"))
        assert background_payload["active"] is False
        assert background_payload["source"]["reason"] == "game_not_foreground"

    module_text = (RUN_DIR / "processing" / "overlay_vision_sidecar.py").read_text(encoding="utf-8").lower()
    assert "requests" not in module_text
    assert "opencv" not in module_text
    assert "cv2" not in module_text
    assert "pyautogui" not in module_text


def check_service_manager_lifecycle_contract() -> None:
    """验证 Web 与游戏内 overlay 两套生命周期互不强依赖。"""
    import display.service_manager as service_manager

    class DummyProcess:
        def __init__(self, pid: int):
            self.pid = pid
            self.terminated = False
            self.killed = False

        def poll(self):
            return None if not (self.terminated or self.killed) else 0

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):
            return 0

    calls: list[str] = []

    manager = service_manager.ServiceManager(
        start_web_func=lambda: calls.append("web") or DummyProcess(11),
        start_overlay_func=lambda: calls.append("overlay") or DummyProcess(22),
        start_vision_sidecar_func=lambda: calls.append("vision-sidecar") or DummyProcess(23),
        prepare_overlay_hint_cache_func=lambda: calls.append("hint-cache"),
        write_inactive_overlay_event_func=lambda: calls.append("inactive-event"),
        listener_interval_seconds=3.0,
    )

    assert manager.get_status_snapshot()["web"]["status"] == "stopped"
    assert manager.get_status_snapshot()["game_overlay"]["status"] == "stopped"
    assert manager.get_status_snapshot()["low_frequency_listener"]["interval_seconds"] == 3.0

    manager.start_game_overlay()
    assert calls == ["hint-cache", "overlay", "vision-sidecar"]
    snapshot = manager.get_status_snapshot()
    assert snapshot["game_overlay"]["status"] == "running"
    assert snapshot["game_overlay"]["pid"] == 22
    assert snapshot["vision_sidecar"]["status"] == "running"
    assert snapshot["vision_sidecar"]["pid"] == 23
    assert snapshot["web"]["status"] == "stopped"

    manager.start_web()
    assert calls == ["hint-cache", "overlay", "vision-sidecar", "web"]
    snapshot = manager.get_status_snapshot()
    assert snapshot["web"]["status"] == "running"
    assert snapshot["web"]["pid"] == 11

    manager.stop_game_overlay()
    snapshot = manager.get_status_snapshot()
    assert snapshot["game_overlay"]["status"] == "stopped"
    assert snapshot["vision_sidecar"]["status"] == "stopped"
    assert snapshot["web"]["status"] == "running"
    assert calls[-1] == "inactive-event"

    # 从未启动过 overlay 的实例关闭时不得写隐藏事件，避免覆盖其他实例的事件文件。
    inactive_count_before = calls.count("inactive-event")
    manager.stop_game_overlay()
    assert calls.count("inactive-event") == inactive_count_before

    manager.set_low_frequency_listener_enabled(False)
    assert manager.get_status_snapshot()["low_frequency_listener"]["enabled"] is False

    manager.stop_web()
    assert manager.get_status_snapshot()["web"]["status"] == "stopped"

    class ExitedProcess(DummyProcess):
        def poll(self):
            return 1

    failing_manager = service_manager.ServiceManager(
        start_web_func=lambda: ExitedProcess(33),
        start_overlay_func=lambda: DummyProcess(44),
        start_vision_sidecar_func=lambda: DummyProcess(45),
    )
    try:
        failing_manager.start_web()
    except RuntimeError as exc:
        assert "进程已退出" in str(exc)
    else:
        raise AssertionError("已退出的 Web 子进程不应标记为 running")
    assert failing_manager.get_status_snapshot()["web"]["status"] == "error"

    orphan_probe: dict[str, Any] = {}

    def start_overlay_then_sidecar_fails() -> DummyProcess:
        process = DummyProcess(46)
        orphan_probe["overlay"] = process
        return process

    rollback_manager = service_manager.ServiceManager(
        start_web_func=lambda: DummyProcess(47),
        start_overlay_func=start_overlay_then_sidecar_fails,
        start_vision_sidecar_func=lambda: (_ for _ in ()).throw(RuntimeError("vision failed")),
        write_inactive_overlay_event_func=lambda: calls.append("rollback-inactive-event"),
    )
    try:
        rollback_manager.start_game_overlay()
    except RuntimeError as exc:
        assert "vision failed" in str(exc)
    else:
        raise AssertionError("Vision sidecar 启动失败时不应保留 running 状态")
    assert orphan_probe["overlay"].terminated or orphan_probe["overlay"].killed
    rollback_snapshot = rollback_manager.get_status_snapshot()
    assert rollback_snapshot["game_overlay"]["status"] == "error"
    assert rollback_snapshot["game_overlay"]["pid"] is None
    assert rollback_snapshot["vision_sidecar"]["status"] == "error"

    class KillRequiredProcess(DummyProcess):
        def terminate(self) -> None:
            self.terminated = False

        def wait(self, timeout=None):
            if not self.killed:
                raise TimeoutError("still running")
            return 0

    kill_manager = service_manager.ServiceManager(
        start_web_func=lambda: KillRequiredProcess(55),
        start_overlay_func=lambda: DummyProcess(66),
        start_vision_sidecar_func=lambda: DummyProcess(67),
    )
    kill_manager.start_web()
    kill_manager.stop_web()
    assert kill_manager.get_status_snapshot()["web"]["status"] == "stopped"

    class UnstoppableProcess(DummyProcess):
        def terminate(self) -> None:
            self.terminated = False

        def kill(self) -> None:
            self.killed = False

        def wait(self, timeout=None):
            raise TimeoutError("still running")

    stuck_manager = service_manager.ServiceManager(
        start_web_func=lambda: UnstoppableProcess(77),
        start_overlay_func=lambda: DummyProcess(88),
        start_vision_sidecar_func=lambda: DummyProcess(89),
    )
    stuck_manager.start_web()
    stuck_manager.stop_web()
    stuck_snapshot = stuck_manager.get_status_snapshot()["web"]
    assert stuck_snapshot["status"] == "error"
    assert stuck_snapshot["pid"] == 77

    captured: dict[str, Any] = {}

    def fake_popen(command, cwd=None, startupinfo=None):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["startupinfo"] = startupinfo
        return DummyProcess(90)

    with patch.object(service_manager.subprocess, "Popen", side_effect=fake_popen):
        sidecar_process = service_manager.start_vision_sidecar_process()
    assert sidecar_process.pid == 90
    assert captured["cwd"] == service_manager.BASE_DIR
    assert "processing.overlay_vision_sidecar" in captured["command"]
    assert "--loop" in captured["command"]
    assert "--once" not in captured["command"]
    assert "--write-event" in captured["command"]


def _top_level_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            imports.add(f"{prefix}{node.module or ''}")
    return imports


def _probe_clean_import(script_name: str) -> set[str]:
    code = f"""
import json
import runpy
import sys
runpy.run_path({script_name!r}, run_name='__hextech_probe__')
watched = [
    'display',
    'display.web_server',
    'display.web_api',
    'fastapi',
    'uvicorn',
    'webbrowser',
    'requests',
]
print(json.dumps([name for name in watched if name in sys.modules]))
"""
    output = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=str(RUN_DIR),
        text=True,
        encoding="utf-8",
    )
    return set(json.loads(output))


def _probe_module_import(module_name: str) -> set[str]:
    code = f"""
import importlib
import json
import sys
importlib.import_module({module_name!r})
watched = [
    'display.web_server',
    'display.web_api',
    'fastapi',
    'uvicorn',
    'webbrowser',
    'requests',
]
print(json.dumps([name for name in watched if name in sys.modules]))
"""
    output = subprocess.check_output(
        [sys.executable, "-c", code],
        cwd=str(RUN_DIR),
        text=True,
        encoding="utf-8",
    )
    return set(json.loads(output))


def check_desktop_ui_toggle_rollback_contract() -> None:
    """验证开关后续步骤失败时不会留下状态关闭但进程仍运行的残留。"""

    from display.hextech_ui import HextechUI
    import display.hextech_ui as hextech_ui_module
    import display.service_manager as service_manager

    class FakeVar:
        def __init__(self, value: bool):
            self.value = value

        def get(self) -> bool:
            return self.value

        def set(self, value: bool) -> None:
            self.value = bool(value)

    class DummyProcess:
        pid = 101

        def __init__(self):
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def make_ui(*, web_enabled: bool = False, overlay_enabled: bool = False):
        ui = HextechUI.__new__(HextechUI)
        ui.web_frontend_var = FakeVar(web_enabled)
        ui.game_overlay_var = FakeVar(overlay_enabled)
        ui.private_stats_var = FakeVar(False)
        ui.low_frequency_listener_var = FakeVar(True)
        ui.feature_flags = {
            "web_frontend_enabled": web_enabled,
            "game_overlay_enabled": overlay_enabled,
            "auto_open_browser": False,
            "private_policy_stats_enabled": False,
            "low_frequency_listener_enabled": True,
        }
        ui.web_port_file = "unused-port-file.txt"
        ui.web_process = None
        ui.service_manager = service_manager.ServiceManager(
            start_web_func=DummyProcess,
            start_overlay_func=DummyProcess,
        )
        ui._set_status = lambda _text, _color: None
        ui._refresh_feature_status = lambda: None
        return ui

    with patch.object(hextech_ui_module, "save_ui_feature_flags", side_effect=OSError("persist failed")):
        web_ui = make_ui(web_enabled=True)
        HextechUI._toggle_web_frontend(web_ui)
        assert web_ui.web_frontend_var.get() is False
        assert web_ui.service_manager.is_web_running() is False
        assert web_ui.web_process is None

        overlay_ui = make_ui(overlay_enabled=True)
        HextechUI._toggle_game_overlay(overlay_ui)
        assert overlay_ui.game_overlay_var.get() is False
        assert overlay_ui.service_manager.is_game_overlay_running() is False


def check_game_overlay_host_contract() -> None:
    """验证 overlay host 只显示 active 选择事件，且必须要求游戏窗口前台。"""
    import display.game_overlay_host as game_overlay_host

    config = game_overlay_host.build_overlay_window_config()
    assert config["title"] == "Hextech Game Overlay"
    assert config["topmost"] is True
    assert config["click_through"] is True
    assert config["hotkey"] == "Alt+H"
    assert "League of Legends (TM) Client" in config["follow_window_titles"]
    assert 250 <= config["follow_poll_ms"] <= 1000
    assert 100 <= config["event_poll_ms"] <= 500

    module_text = (RUN_DIR / "display" / "game_overlay_host.py").read_text(encoding="utf-8").lower()
    forbidden_terms = ["opencv", "cv2", "screenshot", "template match", "phash", "orb"]
    assert not any(term in module_text for term in forbidden_terms)
    assert game_overlay_host._should_show_overlay(user_enabled=True, event_visible=True, game_foreground=True) is True
    assert game_overlay_host._should_show_overlay(user_enabled=False, event_visible=True, game_foreground=True) is False
    assert game_overlay_host._should_show_overlay(user_enabled=True, event_visible=False, game_foreground=True) is False
    assert game_overlay_host._should_show_overlay(user_enabled=True, event_visible=True, game_foreground=False) is False

    class DummyRoot:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def deiconify(self) -> None:
            self.calls.append(("deiconify",))

        def withdraw(self) -> None:
            self.calls.append(("withdraw",))

        def attributes(self, *args: Any) -> None:
            self.calls.append(("attributes", args))

    active_snapshot = {
        "ok": True,
        "error": "",
        "visible": True,
        "slots": [{"name": "珠光护手"}],
    }
    missing_snapshot = {
        "ok": False,
        "error": "event_missing",
        "visible": False,
        "slots": [],
    }

    root = DummyRoot()
    visibility = {"user_enabled": True, "target_hwnd": 123, "window_visible": True}
    with (
        patch.object(game_overlay_host, "_is_game_window_foreground", return_value=True),
        patch.object(game_overlay_host, "_apply_overlay_window_styles", return_value=None) as apply_styles,
    ):
        game_overlay_host._sync_event_visibility(root, config, visibility, missing_snapshot)
    assert ("withdraw",) in root.calls
    assert ("deiconify",) not in root.calls
    assert visibility["event_visible"] is False
    assert visibility["window_visible"] is False
    apply_styles.assert_not_called()

    root = DummyRoot()
    visibility = {"user_enabled": True, "target_hwnd": 123, "window_visible": False}
    with (
        patch.object(game_overlay_host, "_is_game_window_foreground", return_value=True),
        patch.object(game_overlay_host, "_apply_overlay_window_styles", return_value=None) as apply_styles,
    ):
        game_overlay_host._sync_event_visibility(root, config, visibility, active_snapshot)
    assert ("deiconify",) in root.calls
    assert ("withdraw",) not in root.calls
    assert visibility["event_visible"] is True
    assert visibility["game_foreground"] is True
    assert visibility["window_visible"] is True
    apply_styles.assert_called_once()

    root = DummyRoot()
    visibility = {"user_enabled": True, "target_hwnd": 123, "window_visible": False}
    with (
        patch.object(game_overlay_host, "_is_game_window_foreground", return_value=False),
        patch.object(game_overlay_host, "_apply_overlay_window_styles", return_value=None) as apply_styles,
    ):
        game_overlay_host._sync_event_visibility(root, config, visibility, active_snapshot)
    assert root.calls == []
    assert visibility["game_foreground"] is False
    apply_styles.assert_not_called()


def check_desktop_ui_feature_switch_contract() -> None:
    """验证桌面 UI 不再初始化时无条件启动 Web 服务。"""
    import display.ui_runtime as ui_runtime

    ui_text = (RUN_DIR / "display" / "hextech_ui.py").read_text(encoding="utf-8")
    runtime_text = (RUN_DIR / "display" / "ui_runtime.py").read_text(encoding="utf-8")
    init_start = ui_text.index("    def __init__(self):")
    init_end = ui_text.index("    def _start_web_server", init_start)
    init_body = ui_text[init_start:init_end]

    assert "ServiceManager" in ui_text
    assert "Web 前端" in ui_text
    assert "游戏内显示" in ui_text
    assert "start_vision_sidecar_process" in ui_text
    assert "start_vision_sidecar_func=start_vision_sidecar_process" in ui_text
    assert "self._start_web_server()" not in init_body

    root_entry_imports = _top_level_import_names(RUN_DIR / "hextech_ui.py")
    display_init_imports = _top_level_import_names(RUN_DIR / "display" / "__init__.py")
    assert not any(name.startswith("display") for name in root_entry_imports)
    assert ".web_server" not in display_init_imports
    assert ".hextech_ui" not in display_init_imports

    assert _probe_clean_import("hextech_ui.py") == set()
    assert _probe_clean_import("game_overlay_host.py") == set()
    assert _probe_module_import("display.game_overlay_host") == set()

    captured: dict[str, Any] = {}

    class DummyProcess:
        def poll(self):
            return None

    def fake_popen(command, startupinfo=None, cwd=None, env=None):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})
        return DummyProcess()

    with (
        patch.object(ui_runtime.subprocess, "Popen", side_effect=fake_popen),
        patch.object(ui_runtime, "_clear_web_readiness_files", return_value=None),
        patch.object(ui_runtime, "_wait_for_web_startup", return_value=None),
    ):
        ui_runtime.start_web_server_process("unused-port-file.txt", auto_open_browser=True)
    assert captured["env"]["HEXTECH_OPEN_BROWSER"] == "0"

    assert hasattr(ui_runtime, "open_companion_browser")
    assert hasattr(ui_runtime, "close_companion_browser")
    private_toggle_body = ui_text.split("    def _toggle_private_policy_stats", 1)[1].split("    def _toggle_low_frequency_listener", 1)[0]
    assert "self._prepare_overlay_hint_cache()" in private_toggle_body
    assert "if not _web_frontend_available(ui):\n            time.sleep(3)\n            continue" not in runtime_text


def check_no_legacy_imports() -> None:
    legacy_hits = []
    for path in RUN_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == "dev_checks.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "from app." in text or "from services." in text or "import app." in text or "import services." in text:
            legacy_hits.append(path)
    assert not legacy_hits, f"仍存在旧导入: {legacy_hits}"


def check_bundle_manifest(*, verbose: bool = False) -> None:
    manifest = build_bundle_manifest(RUN_DIR)
    summary = {
        key: len(value) if isinstance(value, list) else value
        for key, value in manifest.items()
    }

    if verbose:
        print(summary)

    assert "hextech_snapshot_files" in manifest
    hextech_files = manifest["hextech_snapshot_files"]
    assert isinstance(hextech_files, list)

    assert "synergy_data_file" in manifest
    assert manifest["synergy_data_file"]

    assert "synergy_data_files" in manifest
    synergy_files = manifest["synergy_data_files"]
    assert isinstance(synergy_files, list)

    has_latest_pointer = any(Path(item).name == "Champion_Synergy_latest.v1.json" for item in synergy_files)
    has_timestamp_snapshot = any(
        Path(item).name.startswith("Champion_Synergy_")
        and Path(item).name != "Champion_Synergy_latest.v1.json"
        and Path(item).name.endswith(".json")
        for item in synergy_files
    )
    assert has_latest_pointer
    assert has_timestamp_snapshot
    source_files = manifest.get("source_files", [])
    assert "processing/overlay_vision_sidecar.py" in source_files
    assert "processing/official_overlay_provider.py" in source_files
    assert "display/game_overlay_host.py" in source_files
    assert "tools/probe_official_overlay_provider.py" in source_files
    serialized_manifest = json.dumps(manifest, ensure_ascii=False)
    assert not any("data/runtime" in str(item) for item in source_files)
    assert not any("data/raw" in str(item) for item in source_files)
    assert "data/runtime" not in serialized_manifest.replace("\\", "/")
    assert "overlay_anchor_calibration.v1.json" not in serialized_manifest

    if verbose:
        print("has_hextech_snapshot_files", True)
        print("hextech_snapshot_files_count", len(hextech_files))
        print("hextech_snapshot_files_sample", hextech_files[:5])
        print("has_synergy_data_file", True)
        print("synergy_data_file", manifest["synergy_data_file"])
        print("has_synergy_data_files", True)
        print("synergy_data_files_count", len(synergy_files))
        print("synergy_data_files_sample", synergy_files[:5])
        print("has_synergy_latest_pointer", has_latest_pointer)
        print("has_synergy_timestamp_snapshot", has_timestamp_snapshot)


def check_overlay_performance_probe_contract() -> None:
    """验证游戏内显示性能记录结构可用于阶段 5 手动验收。"""
    import tools.overlay_performance_probe as overlay_performance_probe

    sample = overlay_performance_probe.build_overlay_performance_report(
        service_samples={
            "all_off": {"rss_mb": 90.0, "cpu_percent": 0.2},
            "web_only": {"rss_mb": 145.0, "cpu_percent": 1.4},
            "game_overlay_only": {"rss_mb": 130.0, "cpu_percent": 2.0},
            "web_and_overlay": {"rss_mb": 185.0, "cpu_percent": 3.2},
        },
        latency_samples_ms=[120.0, 240.0, 510.0],
        source_tag="dev-check",
    )
    assert sample["source"]["tag"] == "dev-check"
    assert set(sample["service_states"]) == {
        "all_off",
        "web_only",
        "game_overlay_only",
        "web_and_overlay",
    }
    assert sample["latency"]["p50_ms"] == 240.0
    assert sample["latency"]["p95_ms"] == 510.0
    assert sample["targets"]["recognition_p95_ms"] == 300.0
    assert sample["targets"]["overlay_p95_ms"] == 500.0
    assert sample["manual_acceptance_required"] is True

    module_text = (RUN_DIR / "tools" / "overlay_performance_probe.py").read_text(encoding="utf-8").lower()
    assert "requests" not in module_text
    assert "data/runtime" not in module_text


def check_game_overlay_documentation_contract() -> None:
    """验证阶段 3R-5 文档口径与当前实现一致。"""

    readme_text = (RUN_DIR / "README.md").read_text(encoding="utf-8")
    project_text = (RUN_DIR / "PROJECT.md").read_text(encoding="utf-8")
    design_text = (RUN_DIR / "hextech_game_overlay_design.md").read_text(encoding="utf-8")
    assert "阶段 3R" in readme_text
    assert "2560x1600" in readme_text
    assert "python -m processing.overlay_vision_sidecar --once --preset auto --write-event" in readme_text
    assert "python -m processing.overlay_vision_sidecar --loop --preset auto --write-event" in readme_text
    assert "默认不显示占位框" in readme_text
    assert "开关开 + active 海克斯选择事件 + 游戏窗口在前台" in readme_text
    assert "蓝色选择按钮" in readme_text
    assert "overlay_anchor_calibration.v1.json" in readme_text
    assert "body_shard_only" in readme_text
    assert "probe_official_overlay_provider.py" in readme_text
    assert "官方接口优先" in readme_text
    assert "P95 <= 500ms" in readme_text
    assert "不承诺独占全屏" in readme_text
    assert "阶段 0-5" in project_text
    assert "processing/overlay_vision_sidecar.py" in project_text
    assert "processing/official_overlay_provider.py" in project_text
    assert "tools/overlay_performance_probe.py" in project_text
    assert "tools/probe_official_overlay_provider.py" in project_text
    assert "默认不显示占位框" in project_text
    assert "蓝色按钮场景门控" in project_text
    assert "overlay_anchor_calibration.v1.json" in project_text
    assert "body_shard` 只作为诊断类型不显示" in project_text
    assert "蓝色选择按钮是游戏内显示的主场景门控" in design_text
    assert "官方接口优先验证顺序" in design_text
    assert "python tools/probe_official_overlay_provider.py --duration-seconds 120 --interval-ms 500 --dump-runtime-json" in design_text
    assert "data/runtime/state/overlay_anchor_calibration.v1.json" in design_text
    assert "打包后首次启动必须重新校准" in design_text
    assert "不验证真实 Vision 识别" not in project_text


def check_packaged_smoke_uses_explicit_feature_flags() -> None:
    """验证空仓烟测不依赖桌面 UI 默认开关状态。"""

    smoke_text = (RUN_DIR / "tools" / "smoke_packaged_startup.py").read_text(encoding="utf-8")
    assert '"web_frontend_enabled": True' in smoke_text
    assert '"game_overlay_enabled": False' in smoke_text
    assert '"auto_open_browser": False' in smoke_text
    assert "_write_smoke_feature_flags(runtime_root)" in smoke_text
    assert "OVERLAY_ANCHOR_CALIBRATION_FILENAME" in smoke_text
    assert "package:data/runtime absent" in smoke_text
    assert "overlay_anchor_calibration.v1.json" in smoke_text


def check_packaged_smoke_extracts_representative_champion_id_variants() -> None:
    """验证打包烟测代表英雄提取兼容 Web API 的真实字段名。"""

    import tools.smoke_packaged_startup as smoke_packaged_startup

    champion_name, champion_id = smoke_packaged_startup._extract_representative_champion(
        [{"英雄名称": "德玛西亚之力", "英雄 ID": "86", "英文名": "Garen"}]
    )

    assert champion_name == "德玛西亚之力"
    assert champion_id == "86"

    legacy_name, legacy_id = smoke_packaged_startup._extract_representative_champion(
        [{"hero_name": "Garen", "hero_id": "86"}]
    )
    assert legacy_name == "Garen"
    assert legacy_id == "86"


def check_atomic_json_write_retries_transient_replace_conflict() -> None:
    """验证 Windows 下瞬时 replace 冲突不会让 startup_status 类 JSON 写入失败。"""

    import tools.atomic_io as atomic_io

    with TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "startup_status.json"
        calls = {"replace": 0}
        original_replace = atomic_io.os.replace

        def flaky_replace(src: str, dst: str) -> None:
            calls["replace"] += 1
            if calls["replace"] == 1:
                raise PermissionError("simulated Windows replace race")
            original_replace(src, dst)

        with patch.object(atomic_io.os, "replace", side_effect=flaky_replace):
            atomic_io.atomic_write_json(target, {"ok": True}, ensure_ascii=False, indent=2)

        assert calls["replace"] == 2
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
        assert not list(target.parent.glob(f".{target.name}-*.tmp"))


def check_precomputed_cache_freshness() -> None:
    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (1000, 1000))

        cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "source": latest_csv.name,
                        "source_mtime": 999,
                    },
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "旧数据"}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)):
            assert not precomputed_cache._cache_matches_latest_csv(str(cache_file))

    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (2000, 2000))

        cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "source": latest_csv.name,
                        "source_mtime": 1000,
                    },
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "旧数据"}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        precomputed_cache._hextech_cache_state.update({"path": "", "mtime": 0.0, "data": {}})
        with (
            patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)),
            patch.object(precomputed_cache, "_resolve_cache_file", return_value=str(cache_file)),
        ):
            assert precomputed_cache.load_precomputed_hextech_for_hero("酒桶") is None

    with TemporaryDirectory() as temp_dir:
        latest_csv = Path(temp_dir) / "Hextech_Data_20260518.csv"
        latest_csv.write_text("英雄名称\n酒桶\n", encoding="utf-8")
        os.utime(latest_csv, (3000, 3000))

        cache_file = Path(temp_dir) / "Champion_Hextech_Cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "meta": {
                        "source": latest_csv.name,
                        "source_mtime": 3000,
                    },
                    "data": {"酒桶": {"comprehensive": [{"海克斯名称": "可用数据"}]}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        precomputed_cache._cache_match_state.pop(str(cache_file), None)
        read_count = {"value": 0}
        original_read_cache_payload = precomputed_cache._read_cache_payload

        def counted_read_cache_payload(path: str) -> dict:
            read_count["value"] += 1
            return original_read_cache_payload(path)

        with (
            patch.object(precomputed_cache, "get_latest_csv", return_value=str(latest_csv)),
            patch.object(precomputed_cache, "_read_cache_payload", side_effect=counted_read_cache_payload),
        ):
            assert precomputed_cache._cache_matches_latest_csv(str(cache_file))
            assert precomputed_cache._cache_matches_latest_csv(str(cache_file))
            assert read_count["value"] == 1
            os.utime(cache_file, (4000, 4000))
            assert precomputed_cache._cache_matches_latest_csv(str(cache_file))
            assert read_count["value"] == 2


def check_apex_source_snapshot_policy() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "apex_snapshot"
        manual = root / "manual"
        manual.mkdir(parents=True)
        (manual / "sample.json").write_text('{"katarina": []}', encoding="utf-8")

        with (
            patch.object(synergy_scraper, "DEFAULT_APEX_SNAPSHOT_DIR", str(root)),
            patch.object(synergy_scraper, "DEFAULT_APEX_MANUAL_SNAPSHOT_DIR", str(manual)),
            patch.dict(os.environ, {"APEX_SNAPSHOT_DIR": ""}),
        ):
            source = synergy_scraper.ApexSource()
            resources = source._load_snapshot_resources()
            source.close()

        assert len(resources) == 1
        assert resources[0].source == "snapshot"
        assert "sample.json" in resources[0].url

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "apex_snapshot"
        manual = root / "manual"
        root.mkdir(parents=True)
        env = {
            "APEX_SNAPSHOT_DIR": "",
            "APEX_ALLOW_ONLINE_FETCH": "0",
            "APEX_ALLOW_BROWSER": "0",
            "APEX_SYNERGY_JSON_URL": "",
        }

        with (
            patch.object(synergy_scraper, "DEFAULT_APEX_SNAPSHOT_DIR", str(root)),
            patch.object(synergy_scraper, "DEFAULT_APEX_MANUAL_SNAPSHOT_DIR", str(manual)),
            patch.dict(os.environ, env),
        ):
            source = synergy_scraper.ApexSource()
            with (
                patch.object(source, "fetch_configured_json_resource", side_effect=AssertionError),
                patch.object(source, "fetch", side_effect=AssertionError),
            ):
                assert source.discover_resources() == []
            source.close()

    source = synergy_scraper.ApexSource()
    fetched = synergy_scraper.FetchedResource(url=source.base_url, text="<html></html>", source="selenium")
    try:
        with patch.dict(os.environ, {"APEX_ALLOW_BROWSER": "0"}):
            with patch.object(source, "fetch_requests", return_value=None):
                with patch.object(source, "fetch_browser", return_value=fetched) as fetch_browser:
                    assert source.fetch(source.base_url, allow_browser=True) is None
                    fetch_browser.assert_not_called()

        with patch.dict(os.environ, {"APEX_ALLOW_BROWSER": "1"}):
            with patch.object(source, "fetch_requests", return_value=None):
                with patch.object(source, "fetch_browser", return_value=fetched) as fetch_browser:
                    assert source.fetch(source.base_url, allow_browser=True) is fetched
                    fetch_browser.assert_called_once_with(source.base_url)
    finally:
        source.close()


def _flight_script(payload: str) -> str:
    return f"<script>self.__next_f.push([1,{json.dumps(payload, ensure_ascii=False)}])</script>"


def _morgana_maps() -> tuple[dict[str, str], dict[str, str]]:
    names = {
        "1373": "缩小引擎",
        "1420": "咏叹奏鸣",
        "1406": "祖母的辣椒油",
        "1052": "闪电打击",
        "1058": "秘术冲拳",
    }
    tiers = {
        "缩小引擎": "黄金",
        "咏叹奏鸣": "黄金",
        "祖母的辣椒油": "黄金",
        "闪电打击": "黄金",
        "秘术冲拳": "棱彩",
    }
    return names, tiers


def _morgana_html() -> str:
    ref_payload = (
        '27:[["$","$L28",null,{"championId":"25",'
        '"championAugmentsStats":{"25":[["25","$29","16.10","2026-05-14"]]}}]]\n'
        "29:T123,"
    )
    stats_payload = json.dumps(
        {
            "augments": {
                "1373": {"tier": "1", "win_rate": "0.5774767146486028", "pick_rate": "0.06533163688665154"},
                "1420": {"tier": "1", "win_rate": "0.5656274561173696", "pick_rate": "0.05278807324224152"},
                "1406": {"tier": "1", "win_rate": "0.5623392704067054", "pick_rate": "0.1451983183050285"},
                "1058": {"tier": "5", "win_rate": "0.42105263157894735", "pick_rate": "0.002627705297509843"},
            }
        },
        ensure_ascii=False,
    )
    noise = '<div>{"1052":{"winRate":0.62961,"pickRate":0.078373}}</div>'
    return noise + _flight_script(ref_payload) + _flight_script(stats_payload)


def check_hextech_source_parser() -> None:
    aug_id_map, truth_dict = _morgana_maps()

    rows = extract_champion_stats(
        _morgana_html(),
        aug_id_map,
        truth_dict,
        "25",
        "堕落天使",
        {"tier": "1", "winRate": 0.5255849975106489, "pickRate": 0.011419658717905307},
    )

    names = [row["海克斯名称"] for row in rows]
    assert names[:3] == ["缩小引擎", "咏叹奏鸣", "祖母的辣椒油"]
    assert "闪电打击" not in names
    assert rows[0]["海克斯ID"] == "1373"
    assert rows[0]["源站排名"] == 1
    assert rows[0]["源站层级"] == "T1"
    assert abs(rows[0]["海克斯胜率"] - 0.5774767146486028) < 1e-12
    assert abs(rows[0]["海克斯出场率"] - 0.06533163688665154) < 1e-12

    html = _morgana_html() + '<script>{"9999":{"win_rate":0.99,"pick_rate":0.99}}</script>'
    noisy_rows = extract_champion_stats(
        html,
        aug_id_map,
        truth_dict,
        "25",
        "堕落天使",
        {"tier": "1", "winRate": 0.52, "pickRate": 0.01},
    )
    assert {row["海克斯ID"] for row in noisy_rows} == {"1373", "1420", "1406", "1058"}

    df = pd.DataFrame(
        [
            {
                "英雄ID": "25",
                "英雄名称": "堕落天使",
                "英雄评级": "T1",
                "英雄胜率": 0.52,
                "英雄出场率": 0.01,
                "海克斯ID": "1",
                "源站排名": 2,
                "源站层级": "T1",
                "海克斯阶级": "黄金",
                "海克斯名称": "高胜率后排",
                "海克斯胜率": 0.9,
                "海克斯出场率": 0.01,
                "胜率差": 0.38,
                "综合得分": 100,
            },
            {
                "英雄ID": "25",
                "英雄名称": "堕落天使",
                "英雄评级": "T1",
                "英雄胜率": 0.52,
                "英雄出场率": 0.01,
                "海克斯ID": "2",
                "源站排名": 1,
                "源站层级": "T1",
                "海克斯阶级": "黄金",
                "海克斯名称": "源站第一",
                "海克斯胜率": 0.5,
                "海克斯出场率": 0.5,
                "胜率差": -0.02,
                "综合得分": -100,
            },
        ]
    )

    result = process_hextechs_data(df, "堕落天使", catalog_lookup={}, use_runtime_cache=False)
    assert result["comprehensive"][0]["海克斯名称"] == "源站第一"
    assert result["top_10_overall"][0]["源站排名"] == 1
    assert result["winrate_only"][0]["海克斯名称"] == "高胜率后排"

    missing_derived_df = df.drop(columns=["胜率差", "综合得分"]).rename(columns={"英雄ID": "英雄 ID"})
    missing_result = process_hextechs_data(
        missing_derived_df,
        "堕落天使",
        catalog_lookup={},
        use_runtime_cache=False,
    )
    assert missing_result["comprehensive"]
    assert missing_result["comprehensive"][0]["海克斯名称"] == "源站第一"


def _write_json(path: Path, payload: dict, mtime: int = 1000) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _snapshot(
    temp_dir: str,
    name: str = "Champion_Synergy_20260519_223505.json",
    *,
    mtime: float | None = None,
) -> Path:
    path = Path(temp_dir) / name
    path.write_text(json.dumps({"804": {"synergy_items": [{"content": "ok"}]}}), encoding="utf-8")
    timestamp = time.time() if mtime is None else mtime
    os.utime(path, (timestamp, timestamp))
    return path


def _patch_synergy_dir(temp_dir: str, status: dict | None = None):
    payload = {} if status is None else status
    return (
        patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
        patch.object(heal_worker, "load_synergy_refresh_status", return_value=payload),
        patch.object(orchestrator, "load_synergy_refresh_status", return_value=payload),
    )


def check_synergy_refresh_freshness() -> None:
    with TemporaryDirectory() as temp_dir:
        _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            assert not heal_worker._synergy_data_fresh()
            assert not orchestrator.should_refresh_synergy(False)
            assert not heal_worker.detect_missing_artifacts()["synergy_data"]

    with TemporaryDirectory() as temp_dir:
        synergy_path = _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )

            assert heal_worker._synergy_data_fresh()
            assert not orchestrator.should_refresh_synergy(False)
            assert not heal_worker.detect_missing_artifacts()["synergy_data"]

            meta_path = Path(temp_dir) / "Champion_Synergy_latest.v1.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            assert meta["version"] == SYNERGY_REFRESH_META_VERSION
            assert meta["filename"] == synergy_path.name
            assert meta["non_empty_heroes"] == 1

    with TemporaryDirectory() as temp_dir:
        old_mtime = time.time() - (8 * 24 * 60 * 60)
        synergy_path = _snapshot(temp_dir, mtime=old_mtime)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )
            os.utime(synergy_path, (old_mtime, old_mtime))
            os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

            assert not heal_worker._synergy_data_fresh()
            assert not orchestrator.should_refresh_synergy(False)
            assert not heal_worker.detect_missing_artifacts()["synergy_data"]

    blocked_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    status = {"last_result": "blocked", "blocked_until": blocked_until}
    with TemporaryDirectory() as temp_dir:
        old_mtime = time.time() - (8 * 24 * 60 * 60)
        synergy_path = _snapshot(temp_dir, mtime=old_mtime)

        patches = _patch_synergy_dir(temp_dir, status=status)
        with patches[0], patches[1], patches[2], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "1"}):
            write_synergy_refresh_meta(
                target_path=synergy_path,
                base_url="https://apexlol.info/zh",
                resources=3,
                mapped=1,
                stats={"heroes": 1, "non_empty_heroes": 1, "synergy_entries": 1},
            )
            os.utime(synergy_path, (old_mtime, old_mtime))
            os.utime(Path(temp_dir) / "Champion_Synergy_latest.v1.json", (old_mtime, old_mtime))

            assert heal_worker._synergy_data_fresh()
            assert not orchestrator.should_refresh_synergy(False)

    with TemporaryDirectory() as temp_dir:
        _snapshot(temp_dir)

        patches = _patch_synergy_dir(temp_dir)
        with patches[0], patches[1], patches[2], patch.dict(os.environ, {"HEXTECH_AUTO_SYNERGY_REFRESH": "0"}):
            assert not orchestrator.should_refresh_synergy(True)
            assert not heal_worker.detect_missing_artifacts()["synergy_data"]


def check_synergy_snapshot_store() -> None:
    with TemporaryDirectory() as temp_dir:
        snapshot = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
        _write_json(
            Path(temp_dir) / "Champion_Synergy_latest.v1.json",
            {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
            1001,
        )

        with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(snapshot.resolve())
            assert runtime_store.build_synergy_data_path() == str(snapshot.resolve())

    with TemporaryDirectory() as temp_dir:
        older = _write_json(Path(temp_dir) / "Champion_Synergy_20260518_010101.json", {"1": {}}, 1000)
        newer = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"2": {}}, 2000)
        (Path(temp_dir) / "Champion_Synergy_latest.v1.json").write_text("{bad", encoding="utf-8")

        with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(newer)
            assert runtime_store.get_latest_synergy_snapshot_path() != str(older)

    with TemporaryDirectory() as temp_dir:
        legacy = _write_json(Path(temp_dir) / "Champion_Synergy.json", {"1": {}}, 1000)

        with patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)):
            assert runtime_store.get_latest_synergy_snapshot_path() is None
            assert runtime_store.build_synergy_data_path() == str(legacy)

    try:
        _validate_publish_size(
            {"heroes": 172, "non_empty_heroes": 100, "synergy_entries": 700},
            {"heroes": 172, "non_empty_heroes": 136, "synergy_entries": 876},
        )
    except ValueError as exc:
        assert "协同数据熔断" in str(exc)
    else:
        raise AssertionError("过小协同快照应触发发布熔断")


def _core_info() -> dict[str, ChampionInfo]:
    return {
        "55": ChampionInfo(
            id="55",
            name="卡特琳娜",
            title="不祥之刃",
            en_name="Katarina",
            aliases=["卡特"],
            slug=normalize_slug("Katarina"),
        )
    }


def _collision_core_info() -> dict[str, ChampionInfo]:
    return {
        "254": ChampionInfo(
            id="254",
            name="皮城执法官",
            title="蔚",
            en_name="Vi",
            aliases=["蔚", "w"],
            slug=normalize_slug("Vi"),
        ),
        "234": ChampionInfo(
            id="234",
            name="破败之王",
            title="佛耶戈",
            en_name="Viego",
            aliases=["vi", "vie", "佛耶戈"],
            slug=normalize_slug("Viego"),
        ),
        "112": ChampionInfo(
            id="112",
            name="奥术先驱",
            title="维克托",
            en_name="Viktor",
            aliases=["vi", "vik", "维克托"],
            slug=normalize_slug("Viktor"),
        ),
    }


def _augment_map() -> dict[str, str]:
    return {
        "bladewaltz": "利刃华尔兹",
        normalize_augment_name("利刃华尔兹"): "利刃华尔兹",
        "利刃华尔兹": "利刃华尔兹",
    }


def check_synergy_structured_payloads() -> None:
    payload = {
        "55": {
            "synergy_items": [
                {
                    "augment_names": ["利刃华尔兹"],
                    "tier": "黄金",
                    "rating": "A",
                    "tag": "强力联动",
                    "author": "ApexLoL",
                    "content": "卡特琳娜 R 可以触发这条联动。",
                }
            ]
        }
    }
    extractor = SynergyExtractor(
        champion_lookup=build_champion_lookup(_core_info()),
        augment_name_map=_augment_map(),
    )

    result = extractor.extract([
        FetchedResource(
            url="https://apexlol.info/zh/snapshot/data.json",
            text=json.dumps(payload, ensure_ascii=False),
            source="test",
        )
    ])

    assert "katarina" in result
    assert result["katarina"][0].augment_names == ["利刃华尔兹"]
    assert result["katarina"][0].tier == "黄金"

    entry = SynergyEntry(
        champion_slug="katarina",
        augment_names=["利刃华尔兹"],
        tier="黄金",
        rating="A",
        tag="强力联动",
        author="ApexLoL",
        is_original=True,
        content="卡特琳娜 R 可以触发这条联动。",
        upvotes=3,
        downvotes=1,
    )

    writer_payload = SynergyWriter(_core_info()).build_payload({"katarina": [entry]})
    assert writer_payload["55"]["synergy_items"][0]["augment_names"] == ["利刃华尔兹"]
    assert "利刃华尔兹 | 黄金 | 评分 A" in writer_payload["55"]["synergies"][0]

    legacy = "利刃华尔兹 | 黄金 | 评分 A | 强力联动 | A | B站晴转小雨Yy_ | 原创 | 卡特琳娜 R 可以触发这条联动。"
    items = _normalize_synergy_items([], [legacy])
    assert items[0]["augment_names"] == ["利刃华尔兹"]
    assert items[0]["rating"] == "A"
    assert items[0]["is_original"]
    assert _synergy_item_to_compat_string(items[0]).split(" | ")[4:6] == ["0", "0"]

    html_extractor = SynergyExtractor(
        champion_lookup=build_champion_lookup(_core_info()),
        augment_name_map=_augment_map(),
    )
    html = """
    <html><body>
    <div>利刃华尔兹</div>
    <div>黄金</div>
    <div>D 级</div>
    <div>陷阱</div>
    <div>0</div>
    <div>0</div>
    <div>作者</div>
    <div>ApexLoL</div>
    <p>卡特琳娜 R 在这个组合里会卡手。</p>
    </body></html>
    """

    parsed = html_extractor.extract([
        FetchedResource(
            url="https://apexlol.info/zh/champions/Katarina",
            text=html,
            source="test",
        )
    ])

    assert parsed["katarina"][0].rating == "D"
    assert parsed["katarina"][0].tag == "陷阱"


def check_synergy_alias_collision_guard() -> None:
    def entry(slug: str, content: str) -> SynergyEntry:
        return SynergyEntry(
            champion_slug=slug,
            augment_names=[content],
            tier="黄金",
            rating="A",
            tag="强力联动",
            author="ApexLoL",
            is_original=True,
            content=content,
            upvotes=0,
            downvotes=0,
        )

    writer = SynergyWriter(_collision_core_info())
    payload = writer.build_payload(
        {
            "vi": [entry("vi", "蔚专属联动")],
            "viego": [entry("viego", "佛耶戈专属联动")],
            "viktor": [entry("viktor", "维克托专属联动")],
        }
    )

    assert payload["254"]["synergy_items"][0]["content"] == "蔚专属联动"
    assert payload["234"]["synergy_items"][0]["content"] == "佛耶戈专属联动"
    assert payload["112"]["synergy_items"][0]["content"] == "维克托专属联动"

    payload_without_viktor = writer.build_payload({"vi": [entry("vi", "蔚专属联动")]})
    assert payload_without_viktor["112"]["synergy_items"] == []


def check_synergy_api_quarantines_duplicate_pollution() -> None:
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


def check_synergy_playwright_calibrator_contract() -> None:
    tool_path = RUN_DIR / "tools" / "calibrate_synergy_playwright.py"
    text = tool_path.read_text(encoding="utf-8")
    assert "sync_playwright" in text
    assert "只访问本地 Hextech Web/API" in text
    assert "apexlol.info" not in text.lower()
    assert "build_synergy_data_path" in text
    assert "api_quarantined" in text
    assert "if duplicate_with else []" in text


def _normalize_text(value: Any) -> str:
    return "".join(str(value or "").lower().split())


def _normalize_tier(value: Any) -> str:
    text = str(value or "").strip()
    if "棱彩" in text or "彩色" in text or text == "Prismatic":
        return "Prismatic"
    if "黄金" in text or "金" in text or text == "Gold":
        return "Gold"
    if "白银" in text or "银" in text or text == "Silver":
        return "Silver"
    return text


def _entry_to_expected(entry: SynergyEntry) -> dict:
    return {
        "name": ", ".join(entry.augment_names),
        "names": list(entry.augment_names),
        "tier": entry.tier,
        "rating": entry.rating,
        "tag": entry.tag,
        "author": entry.author,
        "content": entry.content,
    }


def _make_local_driver():
    errors = []
    headless = os.getenv("ACCEPT_HEADLESS", "1").strip() != "0"
    browser = os.getenv("ACCEPT_BROWSER", "auto").strip().lower() or "auto"
    if browser in {"auto", "edge"}:
        try:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options as EdgeOptions

            options = EdgeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1365,900")
            driver = webdriver.Edge(options=options)
            driver.set_page_load_timeout(20)
            return driver
        except Exception as exc:  # pragma: no cover - browser availability is machine local
            errors.append(f"edge={exc.__class__.__name__}:{str(exc)[:120]}")
    if browser in {"auto", "chrome"}:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options as ChromeOptions

            options = ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--window-size=1365,900")
            driver = webdriver.Chrome(options=options)
            driver.set_page_load_timeout(20)
            return driver
        except Exception as exc:  # pragma: no cover - browser availability is machine local
            errors.append(f"chrome={exc.__class__.__name__}:{str(exc)[:120]}")
    raise RuntimeError("无法启动验收浏览器：" + ", ".join(errors))


def _safe_get(driver, url: str) -> None:
    try:
        driver.get(url)
    except Exception:
        # 源站广告/长连接偶尔拖住 load；后续用 DOM 轮询判断页面是否可用。
        pass


def _wait_local_cards(driver, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const container = document.querySelector('#synergyArticleScroll');
            return {
              cards: document.querySelectorAll('#synergyArticleScroll .hextech-card').length,
              text: container ? container.textContent : ''
            };
            """
        )
        text = state.get("text", "")
        if state.get("cards", 0) > 0 or "暂无联动" in text or "该阶级无联动" in text:
            return
        time.sleep(0.5)
    raise TimeoutError("本地详情页联动区域等待超时")


def _extract_local_cards(driver) -> list[dict]:
    return driver.execute_script(
        """
        return Array.from(document.querySelectorAll('#synergyArticleScroll .hextech-card')).map(card => {
          const titleBlock = card.querySelector('.hextech-article-title-block');
          const row = titleBlock && titleBlock.children ? titleBlock.children[0] : null;
          const spans = Array.from(row ? row.querySelectorAll('span') : [])
            .map(s => (s.textContent || '').trim()).filter(Boolean);
          const title = spans[0] || '';
          const rating = spans[1] || '';
          const tier = titleBlock && titleBlock.children && titleBlock.children[1]
            ? (titleBlock.children[1].textContent || '').trim()
            : '';
          const content = (card.querySelector('.hextech-article-content')?.textContent || '').trim();
          const headTexts = Array.from(row ? row.children : [])
            .map(el => (el.textContent || '').trim()).filter(Boolean);
          const tags = headTexts.filter(text => (
            text && text !== title && text !== rating && text !== '?' && !text.includes(`${rating}\\n`)
          ));
          const img = card.querySelector('img[data-hextech-name]');
          return {
            title,
            rating,
            tier,
            tags,
            content,
            resolvedName: img ? (img.getAttribute('data-hextech-name') || '') : '',
            resolvedTier: img ? (img.getAttribute('data-hextech-tier') || '') : ''
          };
        });
        """
    )


def _click_tier(driver, tier: str) -> None:
    driver.execute_script(
        """
        const tier = arguments[0];
        const button = document.getElementById(`tab-${tier}`);
        if (!button) throw new Error(`missing tier button ${tier}`);
        button.click();
        """,
        tier,
    )
    _wait_local_cards(driver)


def _compare_expected_to_local(expected: dict, local: dict) -> dict:
    tag_ok = not expected["tag"] or any(
        _normalize_text(expected["tag"]) in _normalize_text(tag)
        for tag in local.get("tags", [])
    )
    possible_names = {_normalize_text(name) for name in expected["names"]}
    resolved_name = _normalize_text(local.get("resolvedName") or local.get("title"))
    return {
        "name": _normalize_text(local.get("title")) == _normalize_text(expected["name"]),
        "tier": _normalize_tier(local.get("tier")) == _normalize_tier(expected["tier"]),
        "rating": _normalize_text(local.get("rating")) == _normalize_text(expected["rating"]),
        "tag": tag_ok,
        "content": _normalize_text(local.get("content")).find(_normalize_text(expected["content"])[:24]) >= 0,
        "catalog": resolved_name in possible_names,
    }


def _source_visible_matches(driver, expected: list[dict]) -> list[dict]:
    text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    clean_text = _normalize_text(text)
    results = []
    for item in expected:
        results.append(
            {
                "name": all(_normalize_text(name) in clean_text for name in item["names"]),
                "tier": _normalize_text(item["tier"]) in clean_text,
                "rating": _normalize_text(item["rating"]) in clean_text,
                "tag": _normalize_text(item["tag"]) in clean_text,
                "content": _normalize_text(item["content"])[:24] in clean_text,
            }
        )
    return results


def _source_page_blocked(driver) -> bool:
    text = driver.execute_script("return document.body ? document.body.innerText : ''") or ""
    normalized = _normalize_text(text)
    return (
        "deploymentpaused" in normalized
        or "service_unavailable" in normalized
        or "503" in normalized
    )


def _all_match(rows: list[dict]) -> bool:
    return bool(rows) and all(all(row.values()) for row in rows)


def _wait_source_visible(driver, source_url: str, expected: list[dict]) -> list[dict]:
    """等待源站详情页真正把右侧联动文本渲染出来。"""

    last_matches: list[dict] = []
    for attempt in range(3):
        _safe_get(driver, source_url)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if _source_page_blocked(driver):
                break
            last_matches = _source_visible_matches(driver, expected)
            if _all_match(last_matches):
                return last_matches
            time.sleep(0.75)
        if attempt < 2:
            time.sleep(2)
    return last_matches


def _resolve_base_url(args) -> str:
    if args.base_url:
        return args.base_url.rstrip("/")
    if args.port_file:
        port = Path(args.port_file).read_text(encoding="utf-8").strip()
        return f"http://127.0.0.1:{port}"
    return "http://127.0.0.1:8000"


def _load_local_champions(base_url: str) -> list[dict]:
    response = requests.get(f"{base_url}/api/champions", timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"{base_url}/api/champions 未返回英雄列表")
    return payload


def _build_source_extractor() -> SynergyExtractor:
    core_info = build_core_info(_load_json_file("Champion_Core_Data.json", "core_data"))
    return SynergyExtractor(
        champion_lookup=build_champion_lookup(core_info),
        augment_name_map=build_augment_name_map_from_static(),
    )


def run_manual_web_synergy(args) -> dict:
    base_url = _resolve_base_url(args)
    source_base = args.source_base.rstrip("/")
    champions = _load_local_champions(base_url)
    rng = random.Random(args.seed)
    shuffled = list(champions)
    rng.shuffle(shuffled)

    source = ApexSource()
    extractor = _build_source_extractor()
    local_driver = _make_local_driver()
    screenshots = Path(args.screenshot_dir).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    selected = []
    attempts = []

    try:
        for champion in shuffled:
            champ_id = str(champion.get("英雄 ID") or champion.get("英雄ID") or "").strip()
            hero_name = str(champion.get("英雄名称") or "").strip()
            en_name = str(champion.get("英文名") or "").strip()
            if not champ_id or not hero_name or not en_name:
                continue

            source_url = f"{source_base}/champions/{en_name}"
            resource = source.fetch(source_url, allow_browser=True)
            entries = extractor._extract_from_resource(resource) if resource else []
            attempts.append({"id": champ_id, "name": hero_name, "en": en_name, "source_entries": len(entries)})
            if not entries:
                continue

            expected = [_entry_to_expected(entry) for entry in entries[: args.first_n]]
            local_url = (
                f"{base_url}/detail.html?hero={quote(hero_name)}&id={quote(champ_id)}&en={quote(en_name)}"
                f"&acceptance={args.seed}"
            )
            _safe_get(local_driver, local_url)
            _wait_local_cards(local_driver)
            _click_tier(local_driver, "all")
            local_cards = _extract_local_cards(local_driver)[: len(expected)]
            comparisons = [
                _compare_expected_to_local(item, local_cards[index] if index < len(local_cards) else {})
                for index, item in enumerate(expected)
            ]

            tier_checks = []
            for tier in TIER_IDS:
                _click_tier(local_driver, tier)
                cards = _extract_local_cards(local_driver)
                violations = [
                    {"title": card.get("title", ""), "tier": card.get("tier", ""), "resolvedTier": card.get("resolvedTier", "")}
                    for card in cards
                    if _normalize_tier(card.get("tier") or card.get("resolvedTier")) != tier
                ]
                tier_checks.append({"tier": tier, "count": len(cards), "violations": violations})

            source_matches = _wait_source_visible(local_driver, source_url, expected)

            result = {
                "id": champ_id,
                "name": hero_name,
                "en": en_name,
                "local_url": local_url,
                "source_url": source_url,
                "source_entries": len(entries),
                "local_count": len(local_cards),
                "field_ok": len(local_cards) == len(expected) and _all_match(comparisons),
                "tier_ok": all(not check["violations"] for check in tier_checks),
                "source_visible_ok": _all_match(source_matches),
                "comparisons": comparisons,
                "tier_checks": tier_checks,
                "source_matches": source_matches,
            }
            if not (result["field_ok"] and result["tier_ok"] and result["source_visible_ok"]):
                screenshot = screenshots / f"{args.label}-{champ_id}-{en_name}.png"
                try:
                    local_driver.save_screenshot(str(screenshot))
                    result["screenshot"] = str(screenshot)
                except Exception as exc:
                    result["screenshot_error"] = exc.__class__.__name__
            selected.append(result)
            if len(selected) >= args.sample_size:
                break
    finally:
        source.close()
        try:
            local_driver.quit()
        except Exception:
            pass

    passed = len(selected) == args.sample_size and all(
        item["field_ok"] and item["tier_ok"] and item["source_visible_ok"]
        for item in selected
    )
    return {
        "label": args.label,
        "base_url": base_url,
        "source_base": source_base,
        "seed": args.seed,
        "sample_size": args.sample_size,
        "first_n": args.first_n,
        "passed": passed,
        "selected": selected,
        "attempts": attempts,
    }


def run_default_checks() -> None:
    check_root_entrypoints()
    check_manual_alias_index()
    check_manifest_icon_url_safety()
    check_safe_detail_name_regex()
    check_apexlol_hextech_map_size_limit()
    check_runtime_alias_persistence()
    check_detail_hero_param_uses_text_content()
    check_heal_worker_contract()
    check_logging_contract()
    check_packaging_config()
    check_bundle_manifest()
    check_overlay_performance_probe_contract()
    check_game_overlay_documentation_contract()
    check_packaged_smoke_uses_explicit_feature_flags()
    check_packaged_smoke_extracts_representative_champion_id_variants()
    check_atomic_json_write_retries_transient_replace_conflict()
    check_precomputed_cache_freshness()
    check_apex_source_snapshot_policy()
    check_hextech_source_parser()
    check_synergy_refresh_freshness()
    check_synergy_snapshot_store()
    check_synergy_structured_payloads()
    check_detail_question_mark_augment_guard()
    check_static_css_single_mount_contract()
    check_web_bootstrap_avoids_load_event_gate()
    check_api_champions_uses_stable_catalog_before_network_snapshot()
    check_redirect_api_does_not_sync_preload_before_response()
    check_redirect_api_defers_browser_open_before_response()
    check_detail_api_defers_cold_local_processing()
    check_detail_renders_before_deferred_icon_catalog()
    check_ui_feature_flags_contract()
    check_overlay_hint_cache_contract()
    check_overlay_event_channel_contract()
    check_official_overlay_provider_contract()
    check_overlay_vision_sidecar_contract()
    check_service_manager_lifecycle_contract()
    check_game_overlay_host_contract()
    check_desktop_ui_feature_switch_contract()
    check_desktop_ui_toggle_rollback_contract()
    check_synergy_alias_collision_guard()
    check_synergy_api_quarantines_duplicate_pollution()
    check_synergy_playwright_calibrator_contract()
    check_no_legacy_imports()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech 开发自检与手动验收入口。")
    parser.add_argument("--bundle-manifest", action="store_true", help="只输出并校验 bundle manifest 明细。")
    parser.add_argument("--manual-web-synergy", action="store_true", help="执行 Web/UI 详情页联动人工验收辅助检查。")
    parser.add_argument("--base-url", default="", help="本地 Web 地址，例如 http://127.0.0.1:8000")
    parser.add_argument("--port-file", default="", help="UI/Web 写出的 web_server_port.txt；未传 base-url 时使用。")
    parser.add_argument("--source-base", default="https://apexlol.info/zh")
    parser.add_argument("--seed", type=int, default=20260518)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--first-n", type=int, default=5)
    parser.add_argument("--label", default="web", help="输出标签，例如 web 或 ui。")
    parser.add_argument("--screenshot-dir", default=os.path.join("data", "runtime", "acceptance", "synergy"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.manual_web_synergy:
        result = run_manual_web_synergy(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1

    if args.bundle_manifest:
        check_bundle_manifest(verbose=True)
        return 0

    run_default_checks()
    print("所有开发自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

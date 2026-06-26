from __future__ import annotations

"""开发自检与手动验收入口。

这个模块是 `run/` 的统一验证入口：
- 默认模式执行离线、无外网、无浏览器依赖的结构与回归自检。
- `--bundle-manifest` 输出 bundle manifest 明细并校验关键字段。
- `--overlay-only` 只执行游戏内 overlay 相关的离线契约自检。
- `--manual-web-synergy` 执行 Web/UI 详情页联动人工验收辅助检查。

它替代原先散落的 `run/tests/` 临时测试目录，以及独立的
`accept_web_synergy.py` / `verify_bundle_manifest.py` 工具入口。
"""

import argparse
import ast
from contextlib import ExitStack
import importlib
import inspect
import io
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from typing import Any, Mapping
from unittest.mock import patch
from urllib.parse import quote

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.support.python_runtime import ensure_python_311_for_source


if __name__ == "__main__":
    ensure_python_311_for_source()

import requests
import pandas as pd

WEB_STATIC_DIR = RUN_DIR / "hextech" / "display" / "web" / "static"
RESOURCE_DIR = RUN_DIR / "resources"
RESOURCE_IMAGE_DIR = RESOURCE_DIR / "图片资源"
RESOURCE_VERSION_DATA_DIR = RESOURCE_DIR / "版本数据"
RESOURCE_DIAGNOSTIC_DIR = RESOURCE_DIR / "诊断样例"

import hextech.core.refresh as orchestrator
import hextech.catalog.aliases as alias_search
import hextech.catalog.precomputed_cache as precomputed_cache
import hextech.catalog.runtime_store as runtime_store
import hextech.scraping.hextech.scraper as hextech_scraper
import hextech.scraping.synergy.scraper as synergy_scraper
import hextech.scraping.heal_worker as heal_worker
import hextech.scraping.icon_resolver as icon_resolver
import hextech.scraping.version_sync as version_sync
import hextech.scraping.transport.scrapling_client as scrapling_client
import hextech.display.web.runtime as web_runtime
from hextech.display.web.api import _build_synergy_api_payload, _normalize_synergy_items, _synergy_item_to_compat_string
from hextech.catalog.aliases import load_manual_alias_index
from hextech.catalog.version_catalog import (
    legacy_index_payload,
    legacy_static_payload,
    load_augment_manifest_entries,
    load_augment_name_to_icon_map,
    load_champion_alias_records,
    load_champion_core_data,
)
from hextech.catalog.view_adapter import process_hextechs_data
from hextech.scraping.hextech.scraper import extract_champion_stats
from hextech.scraping.synergy.scraper import (
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
from tools.package_rules import iter_package_data_entries
from tools.resource_manifest import validate_resource_manifest
from hextech.support.log_utils import install_summary_logging


TIER_IDS = ("Prismatic", "Gold", "Silver")
ORIGINAL_SYNC_HERO_DATA = version_sync.sync_hero_data
VERSION_SYNC_WRITE_GUARDED_CHECKS = frozenset(
    {
        "check_heal_worker_contract",
        "check_hextech_scraper_fallback_contract",
        "check_hextech_cooldown_and_heal_fallback",
        "check_hextech_failed_refresh_never_overwrites_csv",
        "check_hextech_success_clears_fallback_state",
    }
)


def _offline_sync_hero_data(*_args, **_kwargs) -> bool:
    """开发自检只验证本地缓存契约，不允许触发远端同步和资源写入。"""

    return Path(version_sync.CORE_DATA_FILE).exists()


def _run_read_only_offline_checks(check_names: tuple[str, ...]) -> None:
    """执行离线自检时隔离英雄版本同步，避免验证命令污染中文稳定资源目录。"""

    if not (set(check_names) & VERSION_SYNC_WRITE_GUARDED_CHECKS):
        _run_named_checks(check_names)
        return

    with ExitStack() as stack:
        stack.enter_context(patch.object(version_sync, "sync_hero_data", side_effect=_offline_sync_hero_data))
        stack.enter_context(patch.object(orchestrator, "sync_hero_data", side_effect=_offline_sync_hero_data))
        stack.enter_context(patch.object(heal_worker, "sync_hero_data", side_effect=_offline_sync_hero_data))
        _run_named_checks(check_names)


def check_root_entrypoints() -> None:
    root_scripts = {
        path.name
        for path in RUN_DIR.iterdir()
        if path.is_file() and path.suffix == ".py"
    }

    assert {"build.py", "hextech_ui.py", "web_server.py"}.issubset(root_scripts)
    for legacy_dir in ("crawler", "display", "game_overlay", "processing", "scraping"):
        assert not (RUN_DIR / legacy_dir).exists(), f"legacy root package still exists: {legacy_dir}"
    assert not (RUN_DIR / "game_overlay_host.py").exists()
    assert (RUN_DIR / "tools").exists()
    root_gitignore = (RUN_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert "run/frontend/node_modules/" in root_gitignore
    assert "run/display/node_modules/" not in root_gitignore


def check_python_runtime_guard_contract() -> None:
    """源码态入口必须先落到 run/.venv，再加载业务依赖。"""

    import hextech.support.python_runtime as python_runtime

    assert python_runtime.REQUIRED_PYTHON == (3, 11)
    assert python_runtime.DEFAULT_VENV_DIR == RUN_DIR / ".venv"
    assert python_runtime.default_venv_python_path().parent.name in {"Scripts", "bin"}
    assert "scrapling" in python_runtime.REQUIRED_RUNTIME_PACKAGES
    assert "curl_cffi" in python_runtime.REQUIRED_RUNTIME_PACKAGES
    assert "PyInstaller" in python_runtime.PACKAGING_RUNTIME_PACKAGES

    venv_command = [str(python_runtime.default_venv_python_path())]
    assert python_runtime.build_reexec_command(
        venv_command,
        module_name="hextech.overlay",
        argv=["__main__.py", "--self-check"],
    ) == [*venv_command, "-m", "hextech.overlay", "--self-check"]
    assert python_runtime.build_reexec_command(
        venv_command,
        argv=["hextech_ui.py", "--game-overlay", "--self-check"],
    ) == [*venv_command, "hextech_ui.py", "--game-overlay", "--self-check"]

    assert python_runtime.probe_python_version([sys.executable]) == sys.version_info[:2]
    if python_runtime.is_current_default_venv_python():
        assert python_runtime.find_python_311_command() is not None

    guarded_entries = {
        "hextech_ui.py": "ensure_python_311_for_source()",
        "web_server.py": "ensure_python_311_for_source()",
        "build.py": "ensure_python_311_for_source(require_packages=PACKAGING_RUNTIME_PACKAGES)",
        "hextech/overlay/__main__.py": 'ensure_python_311_for_source(module_name="hextech.overlay")',
        "hextech/overlay/host.py": 'ensure_python_311_for_source(module_name="hextech.overlay.host")',
        "hextech/overlay/vision/sidecar.py": 'ensure_python_311_for_source(module_name="hextech.overlay.vision.sidecar")',
        "hextech/scraping/transport/smoke_scrapling.py": (
            'ensure_python_311_for_source(module_name="hextech.scraping.transport.smoke_scrapling")'
        ),
        "tools/dev_checks.py": "ensure_python_311_for_source()",
    }
    for relative_path, expected_call in guarded_entries.items():
        text = (RUN_DIR / relative_path).read_text(encoding="utf-8")
        assert "hextech.support.python_runtime" in text, relative_path
        assert expected_call in text, relative_path

    setup_tool = RUN_DIR / "tools" / "setup_venv.py"
    setup_text = setup_tool.read_text(encoding="utf-8")
    assert "py -3.11" in setup_text
    assert "requirements.txt" in setup_text
    assert "scrapling_smoke" in setup_text
    assert ".venv" in (RUN_DIR / ".gitignore").read_text(encoding="utf-8")

    runtime_text = (RUN_DIR / "hextech" / "support" / "python_runtime.py").read_text(encoding="utf-8")
    assert '["py", "-3.11"]' not in runtime_text
    assert "DEFAULT_VENV_DIR" in runtime_text
    assert "PACKAGING_RUNTIME_PACKAGES" in runtime_text

    dev_checks_text = (RUN_DIR / "tools" / "dev_checks.py").read_text(encoding="utf-8")
    guard_index = dev_checks_text.index("ensure_python_311_for_source()")
    assert guard_index < dev_checks_text.index("import requests")
    assert guard_index < dev_checks_text.index("import pandas as pd")


def check_hextech_package_contract() -> None:
    """最终结构必须收口到 hextech 单包，不再保留旧根级 import 包。"""

    assert (RUN_DIR / "hextech").exists()
    assert (RUN_DIR / "frontend" / "package.json").exists()
    assert (RUN_DIR / "resources" / "README.md").exists()
    assert (RUN_DIR / "docs" / "README.md").exists()
    assert (RUN_DIR / "tools" / "checks" / "registry.py").exists()

    required_modules = (
        "hextech.display.web.app",
        "hextech.display.web.api",
        "hextech.display.web.runtime",
        "hextech.display.desktop.app",
        "hextech.display.desktop.runtime",
        "hextech.display.desktop.service_manager",
        "hextech.core.refresh",
        "hextech.core.settings",
        "hextech.overlay.events",
        "hextech.overlay.hints",
        "hextech.overlay.window",
        "hextech.overlay.window_titles",
        "hextech.overlay.context",
        "hextech.overlay.runtime_paths",
        "hextech.overlay.providers.official",
        "hextech.overlay.vision.layout",
        "hextech.overlay.vision.matcher",
        "hextech.overlay.vision.state",
        "hextech.overlay.data_source",
        "hextech.overlay.host",
        "hextech.overlay.lifecycle",
        "hextech.overlay.renderer",
        "hextech.overlay.vision.sidecar",
        "hextech.catalog.runtime_store",
        "hextech.catalog.precomputed_cache",
        "hextech.catalog.version_catalog",
        "hextech.catalog.view_adapter",
        "hextech.catalog.aliases",
        "hextech.catalog.alias_utils",
        "hextech.catalog.query_terminal",
        "hextech.scraping._paths",
        "hextech.scraping.version_sync",
        "hextech.scraping.augment_catalog",
        "hextech.scraping.icon_resolver",
        "hextech.scraping.heal_worker",
        "hextech.scraping.hextech.scraper",
        "hextech.scraping.synergy.scraper",
        "hextech.scraping.transport.scrapling_client",
        "hextech.scraping.transport.cloakbrowser_client",
        "hextech.support.atomic_io",
        "hextech.support.log_utils",
        "hextech.support.python_runtime",
    )
    for module_name in required_modules:
        importlib.import_module(module_name)
    for removed_module_path in (
        "hextech/core/lifecycle.py",
        "hextech/core/events.py",
        "hextech/core/runtime_paths.py",
        "hextech/catalog/augments.py",
        "hextech/catalog/champions.py",
        "hextech/display/web/routers",
    ):
        assert not (RUN_DIR / removed_module_path).exists(), f"dead preallocation still exists: {removed_module_path}"

    from tools.checks.registry import DEFAULT_CHECKS, DOMAIN_CHECKS, OVERLAY_ONLY_CHECKS

    grouped_checks = {name for names in DOMAIN_CHECKS.values() for name in names}
    assert set(DEFAULT_CHECKS) == grouped_checks
    assert "check_hextech_package_contract" in DEFAULT_CHECKS
    assert "check_overlay_vision_sidecar_contract" in OVERLAY_ONLY_CHECKS

    hint_cache_text = (RUN_DIR / "hextech" / "overlay" / "hints.py").read_text(encoding="utf-8")
    forbidden_imports = {
        ("processing", "runtime_store"),
        ("processing", "precomputed_cache"),
        ("processing.runtime_store", ""),
        ("processing.precomputed_cache", ""),
        ("tools", "atomic_io"),
        ("tools.atomic_io", ""),
    }
    for node in ast.walk(ast.parse(hint_cache_text)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert (alias.name, "") not in forbidden_imports
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            for alias in node.names:
                assert (module_name, alias.name) not in forbidden_imports
                assert (module_name, "") not in forbidden_imports
    assert "from hextech.catalog.runtime_store" in hint_cache_text
    assert "from hextech.catalog.precomputed_cache" in hint_cache_text
    assert "from hextech.support.atomic_io" in hint_cache_text

    assert WEB_STATIC_DIR.exists()
    assert (WEB_STATIC_DIR / "index.html").exists()
    assert not (RUN_DIR / "display" / "static").exists()

    web_app_text = (RUN_DIR / "hextech" / "display" / "web" / "app.py").read_text(encoding="utf-8")
    assert "display.web_api" not in web_app_text
    assert "display.web_runtime" not in web_app_text
    assert "from .api import register_routes" in web_app_text
    assert "from .runtime import" in web_app_text

    web_api_text = (RUN_DIR / "hextech" / "display" / "web" / "api.py").read_text(encoding="utf-8")
    web_runtime_text = (RUN_DIR / "hextech" / "display" / "web" / "runtime.py").read_text(encoding="utf-8")
    forbidden_web_imports = {
        ("processing", "alias_search"),
        ("processing", "precomputed_cache"),
        ("processing", "runtime_store"),
        ("processing", "view_adapter"),
        ("processing.alias_search", ""),
        ("processing.precomputed_cache", ""),
        ("processing.runtime_store", ""),
        ("processing.view_adapter", ""),
        ("processing.orchestrator", ""),
        ("tools.log_utils", ""),
    }
    for module_text in (web_api_text, web_runtime_text):
        for node in ast.walk(ast.parse(module_text)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert (alias.name, "") not in forbidden_web_imports
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                for alias in node.names:
                    assert (module_name, alias.name) not in forbidden_web_imports
                    assert (module_name, "") not in forbidden_web_imports

    hextech_scraper_text = (RUN_DIR / "hextech" / "scraping" / "hextech" / "scraper.py").read_text(encoding="utf-8")
    synergy_scraper_text = (RUN_DIR / "hextech" / "scraping" / "synergy" / "scraper.py").read_text(encoding="utf-8")
    assert "from processing.precomputed_cache" not in hextech_scraper_text
    assert "from processing.overlay_hint_cache" not in hextech_scraper_text
    assert "from hextech.catalog.precomputed_cache" in hextech_scraper_text
    assert "from hextech.overlay.hints" in hextech_scraper_text
    assert "RUNTIME_DATA_DIR" in synergy_scraper_text
    assert 'Path(BASE_DIR) / "data" / "runtime"' not in synergy_scraper_text
    assert 'os.path.join(BASE_DIR, "data", "runtime"' not in synergy_scraper_text
    assert "crawler.cloakbrowser_client" not in synergy_scraper_text
    assert "hextech.scraping.transport.cloakbrowser_client" in synergy_scraper_text
    smoke_scrapling_text = (RUN_DIR / "hextech" / "scraping" / "transport" / "smoke_scrapling.py").read_text(encoding="utf-8")
    assert "from crawler import fetch_page" not in smoke_scrapling_text
    assert "hextech.scraping.transport.scrapling_client" in smoke_scrapling_text
    assert ".venv\\Scripts\\python.exe -m hextech.scraping.transport.smoke_scrapling" in smoke_scrapling_text

    version_sync_module = importlib.import_module("hextech.scraping.version_sync")
    assert Path(version_sync_module.BASE_DIR).resolve() == RUN_DIR.resolve()
    assert Path(version_sync_module.RESOURCE_DIR).resolve() == RUN_DIR.resolve()
    paths_text = (RUN_DIR / "hextech" / "scraping" / "_paths.py").read_text(encoding="utf-8")
    assert 'os.path.join(RUNTIME_DATA_DIR, "raw")' in paths_text
    assert 'getattr(sys, "frozen", False)' in paths_text
    version_sync_text = (RUN_DIR / "hextech" / "scraping" / "version_sync.py").read_text(encoding="utf-8")
    icon_resolver_text = (RUN_DIR / "hextech" / "scraping" / "icon_resolver.py").read_text(encoding="utf-8")
    augment_catalog_text = (RUN_DIR / "hextech" / "scraping" / "augment_catalog.py").read_text(encoding="utf-8")
    heal_worker_text = (RUN_DIR / "hextech" / "scraping" / "heal_worker.py").read_text(encoding="utf-8")
    assert "from processing.alias_utils" not in version_sync_text
    assert "from scraping.icon_resolver" not in version_sync_text
    assert "from tools.log_utils" not in version_sync_text
    assert "from hextech.catalog.alias_utils" in version_sync_text
    assert "from hextech.scraping.icon_resolver" in version_sync_text
    assert "from hextech.scraping._paths" in version_sync_text
    assert "from hextech.support.log_utils" in version_sync_text
    assert "from scraping.version_sync" not in icon_resolver_text
    assert "from hextech.scraping.version_sync" not in icon_resolver_text
    assert "from hextech.scraping._paths" in icon_resolver_text
    assert "from processing.runtime_store" not in augment_catalog_text
    assert "from scraping." not in augment_catalog_text
    assert "from hextech.catalog.runtime_store" in augment_catalog_text
    assert "from hextech.scraping.version_sync" in augment_catalog_text
    assert "import scraping.version_sync" not in heal_worker_text
    assert "from scraping." not in heal_worker_text
    assert "from processing.runtime_store" not in heal_worker_text
    assert "from tools.atomic_io" not in heal_worker_text
    assert "from hextech.catalog.runtime_store" in heal_worker_text
    assert "from hextech.scraping.version_sync" in heal_worker_text
    assert "from hextech.support.atomic_io" in heal_worker_text

    core_refresh_text = (RUN_DIR / "hextech" / "core" / "refresh.py").read_text(encoding="utf-8")
    core_settings_text = (RUN_DIR / "hextech" / "core" / "settings.py").read_text(encoding="utf-8")
    assert "from processing.runtime_store" not in core_refresh_text
    assert "from processing.precomputed_cache" not in core_refresh_text
    assert "from scraping." not in core_refresh_text
    assert "from tools.atomic_io" not in core_refresh_text
    assert "from hextech.catalog.runtime_store" in core_refresh_text
    assert "from hextech.scraping.version_sync" in core_refresh_text
    assert "from hextech.support.atomic_io" in core_refresh_text
    assert "from processing.runtime_store" not in core_settings_text
    assert "from tools.atomic_io" not in core_settings_text
    assert "from hextech.catalog.runtime_store" in core_settings_text
    assert "from hextech.support.atomic_io" in core_settings_text

    desktop_app_text = (RUN_DIR / "hextech" / "display" / "desktop" / "app.py").read_text(encoding="utf-8")
    desktop_runtime_text = (RUN_DIR / "hextech" / "display" / "desktop" / "runtime.py").read_text(encoding="utf-8")
    desktop_service_text = (RUN_DIR / "hextech" / "display" / "desktop" / "service_manager.py").read_text(encoding="utf-8")
    assert "from processing." not in desktop_app_text
    assert "from scraping." not in desktop_app_text
    assert "from . import ui_runtime" not in desktop_app_text
    assert "from hextech.catalog.runtime_store" in desktop_app_text
    assert "from hextech.overlay.hints" in desktop_app_text
    assert "from hextech.scraping.version_sync" in desktop_app_text
    assert "from . import runtime as ui_runtime" in desktop_app_text
    assert "from processing." not in desktop_runtime_text
    assert "from scraping." not in desktop_runtime_text
    assert "from . import web_runtime" not in desktop_runtime_text
    assert "from hextech.display.web import runtime as web_runtime" in desktop_runtime_text
    assert "from hextech.catalog.query_terminal" in desktop_runtime_text
    assert "from hextech.overlay.window" in desktop_runtime_text
    assert "from hextech.scraping._paths" in desktop_runtime_text
    assert "from processing." not in desktop_service_text
    assert "from hextech.overlay.events" in desktop_service_text
    assert "from hextech.overlay.window" in desktop_service_text

    overlay_host_text = (RUN_DIR / "hextech" / "overlay" / "host.py").read_text(encoding="utf-8")
    overlay_lifecycle_text = (RUN_DIR / "hextech" / "overlay" / "lifecycle.py").read_text(encoding="utf-8")
    overlay_data_source_text = (RUN_DIR / "hextech" / "overlay" / "data_source.py").read_text(encoding="utf-8")
    overlay_renderer_text = (RUN_DIR / "hextech" / "overlay" / "renderer.py").read_text(encoding="utf-8")
    assert "from processing." not in overlay_host_text
    assert "from tools.atomic_io" not in overlay_host_text
    assert "from hextech.overlay.window" in overlay_host_text
    assert "from hextech.support.atomic_io" in overlay_host_text
    assert "from processing." not in overlay_lifecycle_text
    assert "from tools.atomic_io" not in overlay_lifecycle_text
    assert "from hextech.catalog.runtime_store" in overlay_lifecycle_text
    assert "from hextech.overlay.events" in overlay_lifecycle_text
    assert '"hextech.overlay.vision.sidecar"' in overlay_lifecycle_text
    assert "from processing." not in overlay_data_source_text
    assert "from hextech.overlay.hints" in overlay_data_source_text
    assert "from hextech.overlay.events" in overlay_data_source_text
    assert "from hextech.overlay.context" in overlay_data_source_text
    assert "from processing." not in overlay_renderer_text
    assert "from hextech.overlay.vision.layout" in overlay_renderer_text

    assert not (RUN_DIR / "game_overlay").exists()


def check_resource_classification_manifest() -> None:
    """验证中文资源分类清单只描述现有稳定资源，不误纳入运行态。"""

    expected_categories = {"图片资源", "版本数据", "首启快照", "诊断样例", "来源证据"}
    resolved = validate_resource_manifest(RUN_DIR)
    assert expected_categories.issubset(resolved.keys())
    for category in expected_categories:
        assert (RUN_DIR / "resources" / category / "README.md").exists()
        assert resolved[category], f"资源分类没有匹配现有文件：{category}"


def check_version_data_catalog_consolidation() -> None:
    """验证版本数据已收口到两个中文权威目录，同时保留旧文件名投影。"""

    assert (RESOURCE_VERSION_DATA_DIR / "英雄目录.v1.json").exists()
    assert (RESOURCE_VERSION_DATA_DIR / "海克斯资源目录.v1.json").exists()
    for legacy_name in (
        "Champion_Alias_Index.json",
        "champion.alias-to-id.v1.json",
        "champion.id-to-detail.v1.json",
        "champion.id-to-name.v1.json",
        "Augment_Apexlol_Map.json",
        "Augment_Icon_Manifest.json",
        "augment.name-to-icon.v1.json",
    ):
        assert not (RESOURCE_VERSION_DATA_DIR / legacy_name).exists(), f"旧拆分 JSON 不应继续作为事实源：{legacy_name}"

    assert len(load_champion_alias_records()) >= 100
    assert len(load_augment_manifest_entries()) >= 600
    assert len(load_augment_name_to_icon_map()) >= 500
    assert isinstance(legacy_index_payload("Champion_Alias_Index.json", RESOURCE_VERSION_DATA_DIR), list)
    assert isinstance(legacy_index_payload("augment.name-to-icon.v1.json", RESOURCE_VERSION_DATA_DIR), dict)
    assert isinstance(legacy_static_payload("Augment_Icon_Manifest.json", RESOURCE_VERSION_DATA_DIR), list)

    id_to_name = legacy_index_payload("champion.id-to-name.v1.json", RESOURCE_VERSION_DATA_DIR)
    id_to_detail = legacy_index_payload("champion.id-to-detail.v1.json", RESOURCE_VERSION_DATA_DIR)
    assert isinstance(id_to_name, dict) and isinstance(id_to_name.get("266"), str)
    assert isinstance(id_to_detail, dict) and isinstance(id_to_detail.get("266"), dict)
    assert id_to_name["266"] == id_to_detail["266"]["heroName"]


def check_stable_data_compat_routes_are_whitelisted() -> None:
    """验证旧数据 URL 是受控兼容入口，不暴露整个中文版本数据目录。"""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

    app = FastAPI()
    web_api.register_routes(app)
    client = TestClient(app)

    assert client.get("/data/static/海克斯资源目录.v1.json").status_code == 200
    assert client.get("/data/static/Champion_Synergy_Cleaned.json").status_code == 200
    assert client.get("/data/static/Champion_Core_Data.json").status_code == 200
    assert client.get("/data/static/Augment_Icon_Manifest.json").status_code == 200
    assert client.get("/data/static/Champion_Alias_Index.json").status_code == 200
    assert client.get("/data/static/README.md").status_code == 404
    assert client.get("/data/static/overlay_vision_fixtures/name_0.png").status_code == 403

    assert client.get("/data/indexes/Champion_Alias_Index.json").status_code == 200
    assert client.get("/data/indexes/augment.name-to-icon.v1.json").status_code == 200
    id_to_name = client.get("/data/indexes/champion.id-to-name.v1.json").json()
    id_to_detail = client.get("/data/indexes/champion.id-to-detail.v1.json").json()
    assert isinstance(id_to_name.get("266"), str)
    assert isinstance(id_to_detail.get("266"), dict)
    assert client.get("/data/indexes/英雄目录.v1.json").status_code == 404
    assert client.get("/data/indexes/Champion_Synergy_Cleaned.json").status_code == 404
    assert client.get("/data/indexes/README.md").status_code == 404
    assert client.get("/data/indexes/overlay_vision_fixtures/name_0.png").status_code == 403


def check_champion_core_projection_replaces_legacy_file() -> None:
    """验证后台旧 core 读取点走英雄目录投影，不依赖实体 Champion_Core_Data.json。"""

    assert Path(version_sync.CORE_DATA_FILE).name == "英雄目录.v1.json"
    projected = load_champion_core_data()
    assert len(projected) >= 100
    assert {"name", "title", "en_name", "aliases"}.issubset(projected["266"].keys())

    synergy_core = synergy_scraper._load_json_file("Champion_Core_Data.json", "core_data")
    assert len(synergy_core) == len(projected)
    assert synergy_core["266"]["name"] == projected["266"]["name"]
    assert synergy_core["266"]["id"] == "266"
    assert synergy_core["266"]["hero_id"] == "266"
    assert version_sync.load_champion_core_data()["266"]["en_name"] == projected["266"]["en_name"]

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_json(
            root / "英雄目录.v1.json",
            {
                "schema_version": 1,
                "aliases": [{"heroName": "英雄目录名", "title": "目录称号", "enName": "CatalogHero", "heroId": "1", "aliases": ["目录别名"]}],
                "alias_to_id": {"目录别名": "1"},
                "id_to_name": {"1": {"heroName": "英雄目录名", "enName": "CatalogHero", "title": "目录称号"}},
                "id_to_detail": {"1": "英雄目录名"},
            },
        )
        _write_json(
            root / "Champion_Core_Data.json",
            {"1": {"name": "旧文件名", "title": "旧称号", "en_name": "LegacyHero", "aliases": []}},
        )
        assert load_champion_core_data(root)["1"]["name"] == "英雄目录名"


def check_clean_mayhem_combos_uses_core_projection() -> None:
    """验证 Mayhem 清洗默认读取英雄目录投影，不要求旧 core 文件存在。"""

    import tools.clean_mayhem_combos as clean_mayhem_combos

    summary = clean_mayhem_combos.merge_mayhem_combos(
        apex_path=RESOURCE_DIR / "首启快照" / "Champion_Synergy_latest.v1.json",
        write_output=False,
    )
    assert summary["written"] is False
    assert summary["mayhem_raw_items"] >= 100
    assert summary["added_items"] >= 0


def check_manual_alias_index() -> None:
    payload = load_champion_alias_records()
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


def check_icon_resolver_defaults_to_resource_image_dir() -> None:
    """验证图标解析默认从中文图片资源目录读取，不回落到旧根级 assets。"""

    assert Path(icon_resolver._resolve_assets_dir(None)) == RESOURCE_IMAGE_DIR
    assert Path(icon_resolver._resolve_assets_dir_for_config(str(RESOURCE_VERSION_DATA_DIR))) == RESOURCE_IMAGE_DIR
    assert icon_resolver.find_existing_augment_asset_filename(None, "augment404_small.png") == "augment404_small.png"
    with TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        parent_config = temp_root / "config"
        parent_assets = temp_root / "assets"
        parent_config.mkdir()
        parent_assets.mkdir()
        assert Path(icon_resolver._resolve_assets_dir_for_config(str(parent_config))) == parent_assets

    with TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        child_config = temp_root / "config"
        child_assets = child_config / "assets"
        child_assets.mkdir(parents=True)
        assert Path(icon_resolver._resolve_assets_dir_for_config(str(child_config))) == child_assets


def check_cdragon_force_refresh_semantics() -> None:
    """CDragon minimal manifest 不应被旧完整描述字段规则强制重建。"""
    import hextech.scraping.augment_catalog as augment_catalog

    def cdragon_entry(index: int) -> dict[str, Any]:
        return {
            "schema_version": augment_catalog.MANIFEST_SCHEMA_VERSION,
            "name": f"Augment{index}",
            "tier": "Silver",
            "filename": f"augment{index}.png",
            "icon_url": f"/assets/augment{index}.png",
            "source_icon_url": f"{augment_catalog._CDRAGON_SOURCE_PREFIX}game/assets/augment{index}.png",
        }

    minimal_manifest = [cdragon_entry(index) for index in range(augment_catalog._MIN_VALID_MANIFEST_ENTRIES)]
    assert augment_catalog._is_cdragon_minimal_manifest(minimal_manifest)
    assert augment_catalog._is_cdragon_minimal_manifest([cdragon_entry(0)]) is False
    non_cdragon = [
        {**cdragon_entry(index), "source_icon_url": "https://apexlol.info/x.png"}
        for index in range(augment_catalog._MIN_VALID_MANIFEST_ENTRIES)
    ]
    assert augment_catalog._is_cdragon_minimal_manifest(non_cdragon) is False

    calls: dict[str, int] = {"icon_map": 0}

    def fake_load_icon_map(config_dir=None, force_refresh=False):
        calls["icon_map"] += 1
        return {}

    with (
        patch.object(augment_catalog, "_read_manifest_file", return_value=minimal_manifest),
        patch.object(augment_catalog, "load_augment_icon_map", side_effect=fake_load_icon_map),
        patch.object(augment_catalog, "_load_full_map", return_value={}),
        patch.object(augment_catalog, "_fetch_remote_augment_metadata", return_value={}),
        patch.object(augment_catalog, "_write_augment_icon_manifest"),
    ):
        result = augment_catalog.build_augment_icon_manifest(force_refresh=False)
        assert result is minimal_manifest
        assert calls["icon_map"] == 0
        augment_catalog.build_augment_icon_manifest(force_refresh=True)
        assert calls["icon_map"] == 1


def check_cdragon_source_schema_marker() -> None:
    """CDragon 条目优先使用显式 source_schema 标记，旧数据仍按前缀兼容。"""
    import hextech.scraping.augment_catalog as augment_catalog

    raw_entry = {
        "schema_version": augment_catalog.MANIFEST_SCHEMA_VERSION,
        "name": "缩小引擎",
        "tier": "棱彩",
        "filename": "shrinkengine.png",
        "icon_url": "/assets/shrinkengine.png",
        "source_icon_url": f"{augment_catalog._CDRAGON_SOURCE_PREFIX}game/assets/shrinkengine.png",
        "source_schema": augment_catalog._CDRAGON_SOURCE_SCHEMA,
    }
    normalized = augment_catalog._normalize_cdragon_manifest_entry(raw_entry)
    assert normalized["source_schema"] == augment_catalog._CDRAGON_SOURCE_SCHEMA

    explicit_only = {**raw_entry, "source_icon_url": "https://apexlol.info/x.png"}
    assert augment_catalog._is_cdragon_source_item(explicit_only) is True

    legacy = {key: value for key, value in raw_entry.items() if key != "source_schema"}
    assert augment_catalog._is_cdragon_source_item(legacy) is True

    foreign = {**raw_entry, "source_schema": "", "source_icon_url": "https://apexlol.info/x.png"}
    assert augment_catalog._is_cdragon_source_item(foreign) is False


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
            with patch("hextech.scraping.icon_resolver.requests.get", return_value=OversizeResponse()):
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


def check_detail_question_mark_augment_guard() -> None:
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


def check_detail_hextech_card_layout_contract() -> None:
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


def check_static_css_single_mount_contract() -> None:
    index_text = (WEB_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    detail_text = (WEB_STATIC_DIR / "detail.html").read_text(encoding="utf-8")
    web_server_text = (RUN_DIR / "hextech" / "display" / "web" / "app.py").read_text(encoding="utf-8")
    frontend_package = (RUN_DIR / "frontend" / "package.json").read_text(encoding="utf-8")
    tailwind_config = (RUN_DIR / "frontend" / "tailwind.config.js").read_text(encoding="utf-8")

    assert 'href="/static/css/hextech-theme.css"' in index_text
    assert 'href="/static/css/hextech-theme.css"' in detail_text
    assert 'app.mount("/css"' not in web_server_text
    assert "../display/static" not in frontend_package
    assert "../display/static" not in tailwind_config
    assert "../hextech/display/web/static/css/tailwind-compiled.css" in frontend_package
    assert "../hextech/display/web/static/**/*.html" in tailwind_config
    assert "../hextech/display/web/static/**/*.js" in tailwind_config


def check_web_bootstrap_avoids_load_event_gate() -> None:
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


def check_api_champions_uses_stable_catalog_before_network_snapshot() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

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


def check_redirect_api_defers_browser_open_before_response() -> None:
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


def check_detail_api_defers_cold_local_processing() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import hextech.display.web.api as web_api

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
    detail_script = (WEB_STATIC_DIR / "js" / "detail.js").read_text(encoding="utf-8")
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


def _write_runtime_csv(path: Path, row_count: int = 300) -> None:
    """生成满足运行态 schema 的最小离线样本。"""

    row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "英雄评级": "S",
        "英雄胜率": 0.5,
        "英雄出场率": 0.1,
        "海克斯阶级": "Gold",
        "海克斯名称": "测试海克斯",
        "海克斯胜率": 0.51,
        "海克斯出场率": 0.02,
        "胜率差": 0.01,
        "综合得分": 1.0,
    }
    pd.DataFrame([row] * row_count).to_csv(path, index=False, encoding=runtime_store.CSV_ENCODING)


def check_latest_valid_runtime_csv_fallback() -> None:
    """最新快照保留兼容读取；健康判断必须回退到上一份有效版本。"""

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        valid = root / "Hextech_Data_2026-06-19.csv"
        too_small = root / "Hextech_Data_2026-06-20.csv"
        broken = root / "Hextech_Data_2026-06-21.csv"
        _write_runtime_csv(valid, 300)
        _write_runtime_csv(too_small, 299)
        broken.write_text("unexpected\nvalue\n", encoding="utf-8")
        os.utime(valid, (1000, 1000))
        os.utime(too_small, (2000, 2000))
        os.utime(broken, (3000, 3000))

        with patch.object(
            runtime_store,
            "iter_runtime_csv_files",
            return_value=[str(valid), str(too_small), str(broken)],
        ):
            assert runtime_store.get_latest_valid_csv() == str(valid)
            assert runtime_store.get_latest_csv() == str(broken)

        with (
            patch.object(orchestrator, "get_latest_csv", side_effect=AssertionError("refresh must use valid csv")),
            patch.object(orchestrator, "get_latest_valid_csv", return_value=str(valid)),
            patch.object(orchestrator, "hextech_refresh_blocked", return_value=False),
            patch.object(orchestrator, "_file_is_fresh", return_value=True),
        ):
            assert orchestrator.should_refresh_hextech(False) is False

        with (
            patch.object(heal_worker, "get_latest_csv", side_effect=AssertionError("freshness must use valid csv")),
            patch.object(heal_worker, "get_latest_valid_csv", return_value=str(valid)),
            patch.object(heal_worker, "_file_is_fresh", return_value=True),
        ):
            assert heal_worker._latest_csv_ready() is True
            assert heal_worker._latest_csv_fresh() is True


def check_hextech_scraper_fallback_contract() -> None:
    """403 必须在英雄并发前熔断；有本地数据则降级可用。"""

    def fake_result(status_code: int | None, payload: Any = None, text: str = "", error: str = ""):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return hextech_scraper.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    class SequenceFetcher:
        def __init__(self) -> None:
            self.responses = [
                fake_result(200, {"100": {"displayName": "测试海克斯"}}),
                fake_result(200, [{"championId": "1"}]),
                fake_result(403, {}),
            ]
            self.calls = 0

        def __call__(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fallback_csv = root / "Hextech_Data_2026-06-19.csv"
        status_file = root / "scraper_status.json"
        _write_runtime_csv(fallback_csv, 300)
        fetcher = SequenceFetcher()
        started_at = time.time()

        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "fetch_text", side_effect=fetcher),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=str(fallback_csv)),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "ThreadPoolExecutor", side_effect=AssertionError("403 后不得创建英雄线程池")),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is True

        status = json.loads(status_file.read_text(encoding="utf-8"))
        assert fetcher.calls == 3
        assert status["last_result"] == "fallback"
        assert status["reason"] == "http_403"
        assert status["active_csv"] == str(fallback_csv)
        blocked_until = datetime.fromisoformat(status["blocked_until"])
        assert 5.9 * 60 * 60 <= blocked_until.timestamp() - started_at <= 6.1 * 60 * 60

        status_file.unlink()
        failed_fetcher = SequenceFetcher()
        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "fetch_text", side_effect=failed_fetcher),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=None),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "ThreadPoolExecutor", side_effect=AssertionError("403 后不得创建英雄线程池")),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is False

        failed_status = json.loads(status_file.read_text(encoding="utf-8"))
        assert failed_status["last_result"] == "failed"
        assert failed_status["active_csv"] == ""


def check_hextech_cooldown_and_heal_fallback() -> None:
    fallback_status = {
        "last_result": "fallback",
        "reason": "http_403",
        "blocked_until": datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(),
    }
    with (
        patch.object(hextech_scraper, "load_scraper_status", return_value=fallback_status),
        patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
        patch.object(hextech_scraper, "get_latest_valid_csv", return_value="valid.csv"),
        patch.object(hextech_scraper, "fetch_text", side_effect=AssertionError("冷却期不得发起网络请求")),
    ):
        assert hextech_scraper.main_scraper() is True
        assert hextech_scraper.check_execution_permission(force=True)[0] is True

    missing = {
        "hextech_rankings": True,
        "synergy_data": False,
        "augment_catalog": False,
        "champion_core": False,
        "images": False,
        "latest_csv": "valid.csv",
        "augment_icons_prefetched": True,
    }
    with TemporaryDirectory() as temp_dir:
        with (
            patch.object(heal_worker, "LOCK_FILE", Path(temp_dir) / "heal.lock"),
            patch.object(heal_worker, "detect_missing_artifacts", return_value=missing),
            patch.object(heal_worker, "_write_startup_status"),
            patch.object(heal_worker, "_heal_hero_rankings", return_value=True),
            patch.object(heal_worker, "load_scraper_status", return_value=fallback_status),
        ):
            report = heal_worker.heal_missing_artifacts()
        assert report["fallback"] == ["hextech_rankings"]
        assert report["failed"] == []


def check_hextech_failed_refresh_never_overwrites_csv() -> None:
    """低行数与 force 超时都只能回退，不能覆盖已有快照。"""

    def fake_result(status_code: int | None, payload: Any = None, text: str = "ok", error: str = ""):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return hextech_scraper.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    class SequenceFetcher:
        def __init__(self, responses: list[Any]) -> None:
            self.responses = list(responses)
            self.calls = 0

        def __call__(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    one_row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "英雄评级": "S",
        "英雄胜率": 0.5,
        "英雄出场率": 0.1,
        "海克斯阶级": "Gold",
        "海克斯名称": "测试海克斯",
        "海克斯胜率": 0.51,
        "海克斯出场率": 0.02,
        "源站排名": 1,
    }
    metadata = fake_result(200, {"100": {"displayName": "测试海克斯"}})
    stats = fake_result(200, [{"championId": "1"}])

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        fallback_csv = root / "Hextech_Data_2026-06-19.csv"
        output_csv = root / "Hextech_Data_2026-06-21.csv"
        status_file = root / "scraper_status.json"
        _write_runtime_csv(fallback_csv, 300)
        output_csv.write_text("do-not-overwrite", encoding="utf-8")
        low_row_fetcher = SequenceFetcher([metadata, stats, fake_result(200, {}, text="detail")])

        common_patches = (
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=str(fallback_csv)),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_daily_csv_path", return_value=str(output_csv)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "extract_champion_stats", return_value=[one_row]),
            patch.object(hextech_scraper.time, "sleep"),
        )
        with ExitStack() as stack:
            for context_manager in common_patches:
                stack.enter_context(context_manager)
            stack.enter_context(patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")))
            stack.enter_context(patch.object(hextech_scraper, "fetch_text", side_effect=low_row_fetcher))
            stack.enter_context(
                patch.object(hextech_scraper, "atomic_write_csv", side_effect=AssertionError("低行数不得覆盖 CSV"))
            )
            assert hextech_scraper.main_scraper() is True
        assert output_csv.read_text(encoding="utf-8") == "do-not-overwrite"
        assert json.loads(status_file.read_text(encoding="utf-8"))["reason"] == "insufficient_rows_1"

        status_file.unlink()
        future_block = {
            "last_result": "fallback",
            "reason": "http_403",
            "blocked_until": datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(),
        }
        timeout_fetcher = SequenceFetcher(
            [
                fake_result(200, {"100": {"displayName": "测试海克斯"}}),
                fake_result(200, [{"championId": "1"}]),
                fake_result(None, text="", error="simulated timeout"),
                fake_result(None, text="", error="simulated timeout"),
            ]
        )
        with (
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "get_latest_valid_csv", return_value=str(fallback_csv)),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "load_scraper_status", return_value=future_block),
            patch.object(hextech_scraper, "fetch_text", side_effect=timeout_fetcher),
            patch.object(hextech_scraper, "ThreadPoolExecutor", side_effect=AssertionError("预检超时不得创建线程池")),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper(force=True) is True
        assert timeout_fetcher.calls == 4
        assert json.loads(status_file.read_text(encoding="utf-8"))["reason"] == "timeout"


def check_hextech_detail_timeout_tail_retry() -> None:
    """握手瞬断和单英雄详情 timeout 都应重试，不能直接熔断整轮。"""

    def fake_result(status_code: int | None, payload: Any = None, text: str = "ok", error: str = ""):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return hextech_scraper.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    class SequenceFetcher:
        def __init__(self) -> None:
            self.responses = [
                fake_result(None, text="", error="simulated tls error"),
                fake_result(200, {"100": {"displayName": "测试海克斯"}}),
                fake_result(200, [{"championId": "1"}, {"championId": "2"}]),
                fake_result(200, text="detail-1"),
                fake_result(None, text="", error="simulated timeout"),
                fake_result(200, text="detail-2"),
            ]
            self.calls = 0

        def __call__(self, *_args, **_kwargs):
            self.calls += 1
            return self.responses.pop(0)

    row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "英雄评级": "S",
        "英雄胜率": 0.5,
        "英雄出场率": 0.1,
        "海克斯阶级": "Gold",
        "海克斯名称": "测试海克斯",
        "海克斯胜率": 0.51,
        "海克斯出场率": 0.02,
        "源站排名": 1,
    }
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        output_csv = root / "Hextech_Data_2026-06-21.csv"
        status_file = root / "scraper_status.json"
        fetcher = SequenceFetcher()
        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(
                hextech_scraper,
                "load_champion_core_data",
                return_value={"1": {"name": "测试英雄 1"}, "2": {"name": "测试英雄 2"}},
            ),
            patch.object(hextech_scraper, "fetch_text", side_effect=fetcher),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_daily_csv_path", return_value=str(output_csv)),
            patch.object(
                hextech_scraper,
                "build_hextech_detail_urls",
                side_effect=lambda champ_id: [f"https://example.test/detail/{champ_id}"],
            ),
            patch.object(hextech_scraper, "extract_champion_stats", return_value=[row] * 150),
            patch.object(hextech_scraper, "cleanup_old_csvs"),
            patch.object(hextech_scraper, "rebuild_runtime_caches"),
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is True

        assert fetcher.calls == 6
        assert json.loads(status_file.read_text(encoding="utf-8"))["last_result"] == "success"
        assert len(pd.read_csv(output_csv, encoding=runtime_store.CSV_ENCODING)) == 300


def check_scrapling_tls_error_contract() -> None:
    """curl TLS 错误必须被分类为 tls_error，并带上下文向上抛出。"""

    error_text = "curl: (35) TLS connect error: error:00000000:OPENSSL_internal:invalid library (0)"
    assert scrapling_client.classify_fetch_error(error_text) == "tls_error"

    class BadFetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            raise RuntimeError(error_text)

    fetchers_module = type(sys)("scrapling.fetchers")
    fetchers_module.Fetcher = BadFetcher
    fetchers_module.DynamicFetcher = BadFetcher
    fetchers_module.StealthyFetcher = BadFetcher
    scrapling_module = type(sys)("scrapling")
    scrapling_module.fetchers = fetchers_module
    with (
        patch.object(scrapling_client, "_require_scrapling"),
        patch.dict(sys.modules, {"scrapling": scrapling_module, "scrapling.fetchers": fetchers_module}),
    ):
        page_result = scrapling_client.fetch_page("https://example.test")
    assert page_result.error_kind == "tls_error"

    result = hextech_scraper.ScraplingFetchResult(
        url="https://example.test/detail/1",
        text="",
        status_code=None,
        fetched_at="2026-06-25T00:00:00+00:00",
        error=error_text,
        error_kind="tls_error",
        attempts=2,
    )
    assert hextech_scraper._scrapling_failure_reason(result) == ("tls_error", None)

    with patch.object(hextech_scraper, "fetch_text", return_value=result):
        try:
            hextech_scraper.fetch_with_retry(
                "https://example.test/detail/1",
                quiet=True,
                raise_on_failure=True,
                caller="hextech_detail",
                context="championId=1;champion=测试英雄",
            )
        except hextech_scraper.RemoteFetchError as exc:
            assert exc.reason == "tls_error"
            assert exc.url == "https://example.test/detail/1"
            assert exc.context == "championId=1;champion=测试英雄"
        else:
            raise AssertionError("tls_error 必须向上抛出 RemoteFetchError")


def check_version_sync_startup_resource_guard() -> None:
    """普通启动已有稳定资源时不得无条件访问远端或写 resources。"""

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        core_file = root / "英雄目录.v1.json"
        manifest_file = root / "海克斯资源目录.v1.json"
        version_file = root / "hero_version.txt"
        core_file.write_text("{}", encoding="utf-8")
        manifest_file.write_text("{}", encoding="utf-8")
        version_file.write_text("15.13.1", encoding="utf-8")
        with (
            patch.object(version_sync, "CORE_DATA_FILE", str(core_file)),
            patch.object(version_sync, "AUGMENT_MAP_FILE", str(root / "missing-augment-map.json")),
            patch.object(version_sync, "AUGMENT_ICON_FILE", str(root / "missing-augment-icon.json")),
            patch.object(version_sync, "AUGMENT_MANIFEST_FILE", str(manifest_file)),
            patch.object(version_sync, "VERSION_FILE", str(version_file)),
            patch.object(version_sync, "get_advanced_session", side_effect=AssertionError("普通启动不得查远端版本")),
        ):
            version_sync._last_sync_time = 0
            assert ORIGINAL_SYNC_HERO_DATA() is True

    class ResetSession:
        def get(self, *_args, **_kwargs):
            raise requests.ConnectionError(ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"))

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        core_file = root / "英雄目录.v1.json"
        manifest_file = root / "海克斯资源目录.v1.json"
        version_file = root / "hero_version.txt"
        core_file.write_text("{}", encoding="utf-8")
        manifest_file.write_text("{}", encoding="utf-8")
        version_file.write_text("15.13.1", encoding="utf-8")
        with (
            patch.object(version_sync, "CORE_DATA_FILE", str(core_file)),
            patch.object(version_sync, "AUGMENT_MAP_FILE", str(root / "missing-augment-map.json")),
            patch.object(version_sync, "AUGMENT_ICON_FILE", str(root / "missing-augment-icon.json")),
            patch.object(version_sync, "AUGMENT_MANIFEST_FILE", str(manifest_file)),
            patch.object(version_sync, "VERSION_FILE", str(version_file)),
            patch.object(version_sync, "get_advanced_session", return_value=ResetSession()),
            patch.object(version_sync.logger, "warning") as warning_log,
        ):
            version_sync._last_sync_time = 0
            assert ORIGINAL_SYNC_HERO_DATA(allow_remote_check=True) is True
        assert warning_log.call_args is not None
        assert warning_log.call_args.args[1] == "connection_reset"


def check_hextech_success_clears_fallback_state() -> None:
    def fake_result(payload: Any = None, text: str = "ok"):
        if payload is not None:
            text = json.dumps(payload, ensure_ascii=False)
        return hextech_scraper.ScraplingFetchResult(
            url="https://example.test",
            text=text,
            status_code=200,
            fetched_at="2026-06-23T00:00:00+00:00",
            error="",
        )

    class SequenceFetcher:
        def __init__(self) -> None:
            self.responses = [
                fake_result({"100": {"displayName": "测试海克斯"}}),
                fake_result([{"championId": "1"}]),
                fake_result({}, text="detail"),
            ]

        def __call__(self, *_args, **_kwargs):
            return self.responses.pop(0)

    row = {
        "英雄ID": "1",
        "英雄名称": "测试英雄",
        "英雄评级": "S",
        "英雄胜率": 0.5,
        "英雄出场率": 0.1,
        "海克斯阶级": "Gold",
        "海克斯名称": "测试海克斯",
        "海克斯胜率": 0.51,
        "海克斯出场率": 0.02,
        "源站排名": 1,
    }
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        output_csv = root / "Hextech_Data_2026-06-21.csv"
        status_file = root / "scraper_status.json"
        stale_status = {
            "last_result": "fallback",
            "reason": "http_403",
            "blocked_until": datetime.fromtimestamp(time.time() + 3600, tz=timezone.utc).isoformat(),
            "last_success_time": 1,
        }
        with (
            patch.object(hextech_scraper, "check_execution_permission", return_value=(True, "test")),
            patch.object(hextech_scraper, "load_scraper_status", return_value=stale_status),
            patch.object(hextech_scraper, "load_augment_map", return_value={"测试海克斯": "Gold"}),
            patch.object(hextech_scraper, "load_champion_core_data", return_value={"1": {"name": "测试英雄"}}),
            patch.object(hextech_scraper, "fetch_text", side_effect=SequenceFetcher()),
            patch.object(hextech_scraper, "build_runtime_state_path", return_value=str(status_file)),
            patch.object(hextech_scraper, "build_daily_csv_path", return_value=str(output_csv)),
            patch.object(hextech_scraper, "build_hextech_detail_urls", return_value=["https://example.test/detail/1"]),
            patch.object(hextech_scraper, "extract_champion_stats", return_value=[row] * 300),
            patch.object(hextech_scraper, "cleanup_old_csvs") as cleanup,
            patch.object(hextech_scraper, "rebuild_runtime_caches") as rebuild,
            patch.object(hextech_scraper.time, "sleep"),
        ):
            assert hextech_scraper.main_scraper() is True

        status = json.loads(status_file.read_text(encoding="utf-8"))
        assert status["last_result"] == "success"
        assert status["reason"] == ""
        assert status["blocked_until"] == ""
        assert status["active_csv"] == str(output_csv)
        assert status["last_success_time"] > 1
        assert len(pd.read_csv(output_csv, encoding=runtime_store.CSV_ENCODING)) == 300
        cleanup.assert_called_once()
        rebuild.assert_called_once()


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

    requirements = (RUN_DIR / "requirements.txt").read_text(encoding="utf-8")
    for dependency in (
        "requests>=2.32.3,<3",
        "scrapling[fetchers]>=0.4.8,<0.5",
        "cloakbrowser>=0.3,<0.4",
        "urllib3>=2.2,<3",
        "charset-normalizer>=3.3,<4",
        "chardet>=5.2,<6",
    ):
        assert dependency in requirements
    vision_text = (RUN_DIR / "hextech" / "overlay" / "vision" / "sidecar.py").read_text(encoding="utf-8")
    assert 'mode="L"' not in vision_text


def check_packaging_config() -> None:
    build_script = (RUN_DIR / "tools" / "build_package.py").read_text(encoding="utf-8")
    rules_text = (RUN_DIR / "tools" / "package_rules.py").read_text(encoding="utf-8")
    build_entry_text = (RUN_DIR / "build.py").read_text(encoding="utf-8")

    assert "PYINSTALLER_HIDDEN_IMPORTS = [" in build_script
    assert "PYINSTALLER_COLLECT_SUBMODULES = [" in build_script
    assert 'cmd.extend(["--hidden-import", module_name])' in build_script
    assert 'cmd.extend(["--collect-submodules", module_name])' in build_script
    assert "--workpath" in build_script
    assert "--distpath" in build_script
    assert "--specpath" in build_script
    assert "TemporaryDirectory" in build_script
    assert ".artifacts" in build_script
    for dependency in (
        "filelock",
        "tkinter",
        "_tkinter",
        "win32gui",
        "win32con",
        "scrapling.fetchers",
        "cloakbrowser",
        "hextech.overlay.lifecycle",
    ):
        assert f'"{dependency}"' in build_script
    for dependency in ("scrapling", "cloakbrowser"):
        assert f'"{dependency}"' in build_script
    assert "resolve_tcl_runtime_dirs" in build_script
    assert "resolve_tkinter_package_dir" in build_script
    assert '"_tcl_data"' in build_script
    assert '"_tk_data"' in build_script
    assert '"tkinter"' in build_script
    assert "from tools.build_package import main" in build_entry_text
    assert not (RUN_DIR / "tools" / "build_bundle.py").exists()
    assert not (RUN_DIR / "Hextech伴生终端.spec").exists()
    manifest_script_text = (RUN_DIR / "tools" / "bundle_manifest.py").read_text(encoding="utf-8")
    assert "hextech" in manifest_script_text
    assert "prepare_bundle_runtime" not in manifest_script_text
    assert "shutil.copy" not in manifest_script_text
    assert "PackageData" in rules_text
    assert "iter_package_data_entries" in rules_text
    assert '"display/' not in manifest_script_text
    assert '"processing/' not in manifest_script_text
    assert '"crawler/' not in manifest_script_text
    assert '"scraping/' not in manifest_script_text
    assert '"game_overlay/' not in manifest_script_text
    for module_name in ("hextech",):
        assert f'"{module_name}"' in build_script
    for legacy_module in ("display", "processing", "scraping", "crawler", "game_overlay"):
        assert f'"{legacy_module}"' not in build_script


def check_ui_feature_flags_contract() -> None:
    """验证双开关运行态配置的默认值、持久化和未知字段收口。"""
    import hextech.core.settings as ui_feature_flags

    with TemporaryDirectory() as tmp_dir:
        flags_path = Path(tmp_dir) / "ui_feature_flags.json"
        defaults = ui_feature_flags.load_ui_feature_flags(flags_path)

        assert defaults["web_frontend_enabled"] is False
        assert defaults["game_overlay_enabled"] is True
        assert defaults["auto_open_browser"] is True
        assert defaults["private_policy_stats_enabled"] is True
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

        parsed = ui_feature_flags.normalize_ui_feature_flags(
            {"game_overlay_enabled": "false", "web_frontend_enabled": "true", "auto_open_browser": "invalid"}
        )
        assert parsed["game_overlay_enabled"] is False
        assert parsed["web_frontend_enabled"] is True
        assert parsed["auto_open_browser"] is True


def check_overlay_hint_cache_contract() -> None:
    """验证 overlay hint cache 可直接查询，且默认不暴露私用统计字段。"""
    import hextech.overlay.hints as overlay_hint_cache
    import hextech.core.settings as core_settings
    import hextech.catalog.precomputed_cache as precomputed_cache
    import hextech.scraping.augment_catalog as augment_catalog

    sample_payload = {
        "德玛西亚之力": {
            "comprehensive": [
                {
                    "英雄 ID": "86",
                    "英雄名称": "德玛西亚之力",
                    "海克斯ID": "augment_001",
                    "海克斯名称": "珠光护手",
                    "海克斯阶级": "Gold",
                    "tooltip_plain": "技能可以暴击。",
                    "源站排名": 2,
                    "综合得分": 1.25,
                    "海克斯胜率": 0.551,
                    "海克斯出场率": 0.082,
                }
            ]
        },
        "时间刺客": {
            "comprehensive": [
                {
                    "英雄 ID": "245",
                    "英雄名称": "时间刺客",
                    "海克斯ID": "augment_001",
                    "海克斯名称": "珠光护手",
                    "海克斯阶级": "Gold",
                    "tooltip_plain": "技能可以暴击。",
                    "源站排名": 8,
                    "综合得分": 0.84,
                    "海克斯胜率": 0.612,
                    "海克斯出场率": 0.044,
                }
            ]
        }
    }
    sample_synergy = {
        "珠光护手": [
            {
                "hero_id": "266",
                "hero_name": "暗裔剑魔",
                "rating": "S",
                "tag": "强力联动",
                "tier": "棱彩",
                "content": "伤害爆炸",
                "augment_names": ["珠光护手"],
            }
        ]
    }

    public_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=False,
        source_tag="dev-check",
        synergy_by_name=sample_synergy,
    )
    public_hint = overlay_hint_cache.query_overlay_hint(public_cache, "augment_001")

    assert public_cache["schema_version"] == 1
    assert public_cache["source"]["tag"] == "dev-check"
    assert public_hint["ok"] is True
    assert public_hint["hint"]["name"] == "珠光护手"
    assert public_hint["hint"]["summary"] == "技能可以暴击。"
    # public 缓存严禁泄露私用统计字段
    for blocked_field in ("winrate", "pickrate", "rank", "score", "stats_by_champion_id", "stats_by_champion_name"):
        assert blocked_field not in public_hint["hint"], blocked_field
    # synergy 与私用统计无关，公共缓存也按 augment 名命中
    assert public_hint["hint"].get("synergies"), "公共缓存应保留按名命中的 synergy"
    assert public_hint["hint"]["synergies"][0]["hero_name"] == "暗裔剑魔"

    private_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=True,
        source_tag="dev-check",
        synergy_by_name=sample_synergy,
    )
    private_hint = overlay_hint_cache.query_overlay_hint(private_cache, "augment_001")
    assert private_hint["hint"]["winrate"] == 0.551
    assert private_hint["hint"]["pickrate"] == 0.082
    assert private_hint["hint"]["rank"] == 2
    assert private_hint["hint"]["score"] == 1.25
    assert private_hint["hint"]["source_heroes"] == ["德玛西亚之力", "时间刺客"]
    assert private_hint["hint"]["stats_by_champion_id"]["86"]["winrate"] == 0.551
    assert private_hint["hint"]["stats_by_champion_id"]["245"]["winrate"] == 0.612
    assert private_hint["hint"]["stats_by_champion_name"]["德玛西亚之力"]["pickrate"] == 0.082
    assert private_hint["hint"]["stats_by_champion_name"]["时间刺客"]["pickrate"] == 0.044
    assert private_hint["hint"]["synergies"][0]["augment_names"] == ["珠光护手"]

    # 没有 synergy 命中的 augment 不应出现 synergies 字段，避免 overlay 误判
    no_synergy_cache = overlay_hint_cache.build_overlay_hint_cache(
        sample_payload,
        include_private_stats=False,
        source_tag="dev-check",
        synergy_by_name={},
    )
    no_synergy_hint = overlay_hint_cache.query_overlay_hint(no_synergy_cache, "augment_001")
    assert "synergies" not in no_synergy_hint["hint"]

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

    # synergy 加载器也只读本地快照，缺失/损坏时静默给空 dict，不抛异常
    with TemporaryDirectory() as tmp_dir:
        good_path = Path(tmp_dir) / "syn.json"
        good_path.write_text(
            json.dumps(
                {
                    "266": {
                        "name": "暗裔剑魔",
                        "synergy_items": [
                            {
                                "augment_names": ["珠光护手"],
                                "tier": "棱彩",
                                "rating": "S",
                                "tag": "强力联动",
                                "content": "伤害爆炸",
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        index = overlay_hint_cache._load_synergy_by_augment_name(good_path)
        assert "珠光护手" in index and index["珠光护手"][0]["hero_name"] == "暗裔剑魔"
        with (
            patch.object(overlay_hint_cache, "build_synergy_data_path", return_value=str(good_path)),
            patch.object(
                overlay_hint_cache,
                "get_latest_synergy_snapshot_path",
                side_effect=AssertionError("cleaned 默认路径可用时不应回退 raw latest"),
            ),
        ):
            default_index = overlay_hint_cache._load_synergy_by_augment_name()
        assert "珠光护手" in default_index

        damaged_syn = Path(tmp_dir) / "bad.json"
        damaged_syn.write_text("not-json", encoding="utf-8")
        assert overlay_hint_cache._load_synergy_by_augment_name(damaged_syn) == {}
        assert overlay_hint_cache._load_synergy_by_augment_name(Path(tmp_dir) / "missing.json") == {}

    captured_refresh_kwargs: list[dict[str, Any]] = []
    with (
        patch.object(core_settings, "load_ui_feature_flags", return_value={"private_policy_stats_enabled": True}),
        patch.object(precomputed_cache, "rebuild_precomputed_api_cache_from_latest_csv", return_value=None),
        patch.object(
            overlay_hint_cache,
            "build_overlay_hint_cache_from_precomputed",
            side_effect=lambda **kwargs: captured_refresh_kwargs.append(dict(kwargs)) or {"hints": {}},
        ),
        patch.object(overlay_hint_cache, "write_overlay_hint_cache", return_value=Path("cache.json")),
    ):
        hextech_scraper.rebuild_runtime_caches()
    assert captured_refresh_kwargs[-1]["include_private_stats"] is True
    assert captured_refresh_kwargs[-1]["source_tag"] == "runtime-refresh"

    captured_refresh_kwargs.clear()
    with (
        patch.object(core_settings, "load_ui_feature_flags", return_value={"private_policy_stats_enabled": False}),
        patch.object(precomputed_cache, "rebuild_precomputed_api_cache_from_latest_csv", return_value=None),
        patch.object(
            overlay_hint_cache,
            "build_overlay_hint_cache_from_precomputed",
            side_effect=lambda **kwargs: captured_refresh_kwargs.append(dict(kwargs)) or {"hints": {}},
        ),
        patch.object(overlay_hint_cache, "write_overlay_hint_cache", return_value=Path("cache.json")),
    ):
        hextech_scraper.rebuild_runtime_caches()
    assert captured_refresh_kwargs[-1]["include_private_stats"] is False

    module_text = (RUN_DIR / "hextech" / "overlay" / "hints.py").read_text(encoding="utf-8")
    assert "requests" not in module_text
    assert "full_hextech_scraper" not in module_text

    # Overlay 启动只允许读取稳定清单；不得因 freshness 检查改写稳定版本数据。
    with TemporaryDirectory() as tmp_dir:
        manifest_path = Path(tmp_dir) / "Augment_Icon_Manifest.json"
        manifest_path.write_text(
            json.dumps(
                [
                    {
                        "schema_version": 2,
                        "name": "测试海克斯",
                        "tier": "黄金",
                        "filename": "test_small.png",
                        "local_path": "assets/test_small.png",
                        "icon_url": "/assets/test_small.png",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        before = manifest_path.read_bytes()
        lookup = augment_catalog.load_augment_catalog_lookup_read_only(tmp_dir)
        assert lookup["测试海克斯"]["icon_url"] == "/assets/test_small.png"
        assert manifest_path.read_bytes() == before

        debug_manifest_path = Path(tmp_dir) / "runtime" / "debug" / "augment_catalog" / "Augment_Icon_Manifest.debug.json"
        with (
            patch.object(augment_catalog, "AUGMENT_ICON_MANIFEST_FILE", str(manifest_path)),
            patch.object(
                augment_catalog,
                "AUGMENT_ICON_DEBUG_MANIFEST_FILE",
                str(debug_manifest_path),
                create=True,
            ),
        ):
            augment_catalog._write_augment_icon_manifest(
                [{"schema_version": 2, "name": "调试海克斯", "filename": "debug_small.png"}]
            )
        assert manifest_path.read_bytes() == before
        assert json.loads(debug_manifest_path.read_text(encoding="utf-8"))[0]["name"] == "调试海克斯"

        def valid_manifest(prefix: str) -> list[dict[str, Any]]:
            return [
                {
                    "schema_version": 2,
                    "name": f"{prefix}海克斯{i}",
                    "tier": "黄金",
                    "filename": f"{prefix.lower()}_{i}.png",
                    "local_path": f"assets/{prefix.lower()}_{i}.png",
                    "icon_url": f"/assets/{prefix.lower()}_{i}.png",
                    "description": "说明",
                    "tooltip": "说明",
                    "tooltip_plain": "说明",
                    "spell_values": {},
                    "status": "ready",
                    "updated_at": "2026-01-01T00:00:00+0000",
                }
                for i in range(55)
            ]

        manifest_path.write_text(json.dumps(valid_manifest("Static"), ensure_ascii=False), encoding="utf-8")
        runtime_manifest_path = Path(tmp_dir) / "Augment_Icon_Manifest.debug.json"
        runtime_manifest_path.write_text(
            json.dumps(valid_manifest("Runtime"), ensure_ascii=False),
            encoding="utf-8",
        )
        with (
            patch.object(augment_catalog, "_manifest_is_stale", return_value=False),
            patch.object(augment_catalog, "_AUGMENT_ICON_MANIFEST_CACHE", ("", 0.0, [])),
            patch.object(augment_catalog, "_AUGMENT_LOOKUP_CACHE", ("", 0.0, {})),
        ):
            manifest = augment_catalog.load_augment_icon_manifest(config_dir=tmp_dir)
            lookup = augment_catalog.build_augment_catalog_lookup(config_dir=tmp_dir)
        assert manifest[0]["name"] == "Runtime海克斯0"
        assert "Runtime海克斯0" in lookup and "Static海克斯0" not in lookup

        with patch.object(runtime_store, "get_runtime_root_dir", return_value=Path(tmp_dir) / "runtime-root"):
            resolved_debug_path = Path(
                runtime_store.build_runtime_debug_path("augment_catalog/manifest.json")
            )
            assert resolved_debug_path == Path(tmp_dir) / "runtime-root" / "debug" / "augment_catalog" / "manifest.json"
            try:
                runtime_store.build_runtime_debug_path("../escaped.json")
            except ValueError:
                pass
            else:
                raise AssertionError("runtime debug 路径不得逃逸 debug 根目录")

    stable_manifest = load_augment_manifest_entries()
    assert stable_manifest
    assert all(
        not Path(str(item.get("local_path") or "")).is_absolute()
        for item in stable_manifest
        if isinstance(item, dict)
    )

    latest_df = pd.DataFrame(
        [
            {
                "英雄 ID": "432",
                "英雄名称": "星界游神",
                "英雄评级": 1,
                "英雄胜率": 0.51,
                "英雄出场率": 0.02,
                "海克斯ID": "1314",
                "源站排名": 1,
                "源站层级": "T1",
                "海克斯阶级": "Gold",
                "海克斯名称": "自然即是治愈",
                "海克斯胜率": 0.613,
                "海克斯出场率": 0.041,
                "胜率差": 0.08,
                "综合得分": 2.1,
            },
            {
                "英雄 ID": float("nan"),
                "英雄名称": "缺失ID英雄",
                "英雄评级": 1,
                "英雄胜率": 0.49,
                "英雄出场率": 0.01,
                "海克斯ID": "nan-id",
                "源站排名": 2,
                "源站层级": "T2",
                "海克斯阶级": "Gold",
                "海克斯名称": "缺失ID海克斯",
                "海克斯胜率": 0.502,
                "海克斯出场率": 0.012,
                "胜率差": 0.01,
                "综合得分": 1.1,
            },
            {
                "英雄 ID": "999",
                "英雄名称": float("nan"),
                "英雄评级": 1,
                "英雄胜率": 0.5,
                "英雄出场率": 0.02,
                "海克斯ID": "nan-hero",
                "源站排名": 3,
                "源站层级": "T3",
                "海克斯阶级": "Gold",
                "海克斯名称": "污染英雄名",
                "海克斯胜率": 0.5,
                "海克斯出场率": 0.02,
                "胜率差": 0.0,
                "综合得分": 0.5,
            },
            {
                "英雄 ID": "998",
                "英雄名称": "污染海克斯名",
                "英雄评级": 1,
                "英雄胜率": 0.5,
                "英雄出场率": 0.02,
                "海克斯ID": "nan-augment",
                "源站排名": 4,
                "源站层级": "T3",
                "海克斯阶级": "Gold",
                "海克斯名称": float("nan"),
                "海克斯胜率": 0.5,
                "海克斯出场率": 0.02,
                "胜率差": 0.0,
                "综合得分": 0.5,
            },
        ]
    )
    with (
        patch.object(runtime_store, "get_latest_csv", return_value=str(RUN_DIR / "data" / "raw" / "hextech" / "Hextech_Data_2099-01-01.csv")),
        patch.object(runtime_store, "load_runtime_csv", return_value=latest_df),
        patch("hextech.scraping.augment_catalog.load_augment_catalog_lookup_read_only", return_value={}),
    ):
        latest_cache = overlay_hint_cache.build_overlay_hint_cache_from_precomputed(
            include_private_stats=True,
            source_tag="dev-check",
        )
    latest_hint = overlay_hint_cache.query_overlay_hint(latest_cache, "自然即是治愈")
    assert latest_hint["ok"] is True
    assert latest_hint["hint"]["winrate"] == 0.613
    assert latest_hint["hint"]["stats_by_champion_name"]["星界游神"]["pickrate"] == 0.041
    assert latest_cache["source"]["data_source"] == "runtime-csv"
    assert latest_cache["source"]["runtime_csv"] == "Hextech_Data_2099-01-01.csv"
    nan_id_hint = overlay_hint_cache.query_overlay_hint(latest_cache, "缺失ID海克斯")
    assert nan_id_hint["ok"] is True
    assert "nan" not in nan_id_hint["hint"].get("stats_by_champion_id", {})
    assert latest_cache["source"]["hero_count"] == 2
    assert overlay_hint_cache.query_overlay_hint(latest_cache, "污染英雄名")["ok"] is False
    assert overlay_hint_cache.query_overlay_hint(latest_cache, "nan-augment")["ok"] is False

    with TemporaryDirectory() as tmp_dir:
        champion_cache = Path(tmp_dir) / "Champion_List_Cache.json"
        hextech_cache = Path(tmp_dir) / "Champion_Hextech_Cache.json"
        champion_cache.write_text(
            json.dumps({"meta": {"source": "stale.csv"}, "data": [{"英雄名称": "德玛西亚之力"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        hextech_cache.write_text(
            json.dumps({"meta": {"source": "stale.csv"}, "data": sample_payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        with (
            patch.object(runtime_store, "get_latest_csv", return_value=None),
            patch.object(precomputed_cache, "warm_precomputed_hextech_cache", return_value=False),
            patch.object(precomputed_cache, "CHAMPION_LIST_CACHE_FILE", str(champion_cache)),
            patch.object(precomputed_cache, "HEXTECH_DETAIL_CACHE_FILE", str(hextech_cache)),
        ):
            stale_cache = overlay_hint_cache.build_overlay_hint_cache_from_precomputed(
                include_private_stats=True,
                source_tag="dev-check",
            )
    stale_hint = overlay_hint_cache.query_overlay_hint(stale_cache, "augment_001")
    assert stale_hint["ok"] is True
    assert stale_hint["hint"]["stats_by_champion_id"]["86"]["pickrate"] == 0.082

    synergy_index = overlay_hint_cache._load_synergy_by_augment_name()
    assert synergy_index, "cleaned/raw 协同源不得被 overlay 读成空索引"
    stable_augment_names = {
        str(item.get("name") or "")
        for item in load_augment_manifest_entries()
        if isinstance(item, dict) and str(item.get("name") or "")
    }
    if stable_augment_names:
        normalized_stable_names = {
            overlay_hint_cache.normalize_augment_id(name): name for name in stable_augment_names
        }
        normalized_missed = [
            name
            for name in synergy_index
            if name not in stable_augment_names
            and overlay_hint_cache.normalize_augment_id(name) in normalized_stable_names
        ]
        assert not normalized_missed[:5], f"联动名未命中不应是 overlay 归一化退化：{normalized_missed[:5]}"

    real_cache = overlay_hint_cache.build_overlay_hint_cache_from_precomputed(
        include_private_stats=True,
        source_tag="dev-check-real",
    )
    real_hints = real_cache.get("hints")
    if runtime_store.get_latest_csv() or (isinstance(real_hints, Mapping) and real_hints):
        assert isinstance(real_hints, Mapping) and real_hints, "有运行态数据时 overlay hint cache 不得为空"
        real_stats_hint_count = sum(
            1
            for hint in real_hints.values()
            if isinstance(hint, Mapping) and isinstance(hint.get("stats_by_champion_id"), Mapping)
        )
        real_synergy_hint_count = sum(
            1
            for hint in real_hints.values()
            if isinstance(hint, Mapping) and isinstance(hint.get("synergies"), list) and hint.get("synergies")
        )
        assert real_stats_hint_count > 0, "启用私用统计时 overlay cache 不得退回 0 条统计 hint"
        assert real_synergy_hint_count > 0, "overlay cache 不得退回 0 条联动 hint"


def check_overlay_runtime_paths_contract() -> None:
    """验证 overlay event/context 共享的轻量运行态路径规则。"""
    import hextech.overlay.runtime_paths as overlay_runtime_paths

    with (
        patch.object(overlay_runtime_paths.sys, "frozen", False, create=True),
        patch.dict(os.environ, {"HEXTECH_BASE_DIR": ""}),
    ):
        assert overlay_runtime_paths.overlay_runtime_root_dir() == RUN_DIR / "data" / "runtime"

    with TemporaryDirectory() as tmp_dir:
        source_base = Path(tmp_dir) / "source"
        with (
            patch.object(overlay_runtime_paths.sys, "frozen", False, create=True),
            patch.dict(os.environ, {"HEXTECH_BASE_DIR": str(source_base)}),
        ):
            resolved = Path(overlay_runtime_paths.overlay_runtime_state_path("probe.json"))
            assert resolved == (source_base / "data" / "runtime" / "state" / "probe.json").resolve()
            try:
                overlay_runtime_paths.overlay_runtime_state_path("../escaped.json")
            except ValueError as exc:
                assert "escaped state dir" in str(exc)
            else:
                raise AssertionError("overlay runtime state 路径不得逃逸 state 根目录")

        local_app_data = Path(tmp_dir) / "local-app-data"
        with (
            patch.object(overlay_runtime_paths.sys, "frozen", True, create=True),
            patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data), "APPDATA": ""}),
        ):
            resolved = Path(overlay_runtime_paths.overlay_runtime_state_path("probe.json"))
            assert resolved == (
                local_app_data / "HextechNexus" / "data" / "runtime" / "state" / "probe.json"
            ).resolve()


def check_overlay_event_channel_contract() -> None:
    """验证 overlay 本地事件通道可写、可读、可诊断，且固定为三槽位。"""
    import hextech.overlay.events as overlay_event_channel

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

        zero_ready_event = overlay_event_channel.build_overlay_event(
            [
                {"slot": 0, "state": "detecting"},
                {"slot": 1, "state": "detecting"},
                {"slot": 2, "state": "detecting"},
            ],
            source_tag="dev-check",
            active=False,
        )
        zero_ready_event["source"].update(
            {
                "selection_window_active": True,
                "ready_slots": 0,
                "content_ready": False,
            }
        )
        overlay_event_channel.write_overlay_event(zero_ready_event, event_path)
        zero_ready_snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert zero_ready_snapshot["ok"] is True
        assert zero_ready_snapshot["visible"] is False
        assert zero_ready_snapshot["active"] is False
        assert zero_ready_snapshot["source"]["selection_window_active"] is True
        assert zero_ready_snapshot["source"]["ready_slots"] == 0
        assert overlay_event_channel.EVENT_HEARTBEAT_SECONDS == 1.0
        assert overlay_event_channel.EVENT_STALE_HEARTBEAT_BUDGET == 2.5
        assert overlay_event_channel.EVENT_MAX_AGE_SECONDS == (
            overlay_event_channel.EVENT_HEARTBEAT_SECONDS
            * overlay_event_channel.EVENT_STALE_HEARTBEAT_BUDGET
        )

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

        unknown_event = dict(event)
        unknown_event["selection_type"] = "legacy-unknown"
        unknown_path = Path(tmp_dir) / "unknown-selection.json"
        overlay_event_channel.write_overlay_event(unknown_event, unknown_path)
        unknown_snapshot = overlay_event_channel.read_overlay_event(unknown_path)
        assert unknown_snapshot["ok"] is False
        assert unknown_snapshot["visible"] is False
        assert unknown_snapshot["error"] == "selection_type_unknown"

    module_text = (RUN_DIR / "hextech" / "overlay" / "events.py").read_text(encoding="utf-8")
    assert "requests" not in module_text
    assert "cv2" not in module_text
    assert "from processing.runtime_store" not in module_text
    assert "import processing.runtime_store" not in module_text
    assert "from processing.overlay_hint_cache" not in module_text
    assert "from hextech.overlay.runtime_paths import overlay_runtime_state_path" in module_text
    assert "def _overlay_runtime_root_dir" not in module_text
    assert "def _overlay_runtime_state_path" not in module_text


def check_overlay_context_contract() -> None:
    """验证游戏内 overlay 英雄上下文只通过本地 state 文件传递。"""
    import hextech.overlay.context as overlay_context
    import hextech.display.desktop.runtime as ui_runtime
    import hextech.display.web.runtime as web_runtime

    with TemporaryDirectory() as tmp_dir:
        context_path = Path(tmp_dir) / "game_overlay_context.v1.json"
        missing = overlay_context.read_overlay_context(context_path)
        assert missing["ok"] is False
        assert missing["error"] == "context_missing"
        assert missing["champion_id"] == ""

        payload = overlay_context.build_overlay_context_payload(
            champion_id=266,
            champion_name="暗裔剑魔",
            source="dev-check",
        )
        assert payload["schema_version"] == overlay_context.SCHEMA_VERSION
        assert payload["champion_id"] == "266"
        overlay_context.write_overlay_context(payload, context_path)
        loaded = overlay_context.read_overlay_context(context_path)
        assert loaded["ok"] is True
        assert loaded["champion_name"] == "暗裔剑魔"
        assert loaded["source"] == "dev-check"

        expired_payload = dict(payload)
        expired_payload["generated_at"] = time.time() - overlay_context.CONTEXT_MAX_AGE_SECONDS - 1
        context_path.write_text(json.dumps(expired_payload, ensure_ascii=False), encoding="utf-8")
        assert overlay_context.read_overlay_context(context_path)["error"] == "context_expired"

        context_path.write_text("not-json", encoding="utf-8")
        assert overlay_context.read_overlay_context(context_path)["error"] == "context_damaged"

        class DummyUI:
            core_data = {"266": {"name": "暗裔剑魔"}}

        live_state = {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"}
        assert ui_runtime._write_overlay_context_from_live_state(
            DummyUI(),
            live_state,
            source="web",
            context_path=context_path,
        ) is True
        live_loaded = overlay_context.read_overlay_context(context_path)
        assert live_loaded["ok"] is True
        assert live_loaded["champion_id"] == "266"
        assert live_loaded["champion_name"] == "暗裔剑魔"
        assert live_loaded["source"] == "web"

        assert ui_runtime._write_overlay_context_from_live_state(
            DummyUI(),
            {"local_champion_id": 0},
            source="web",
            context_path=context_path,
        ) is False
        cleared_live_loaded = overlay_context.read_overlay_context(context_path)
        assert cleared_live_loaded["ok"] is False
        assert cleared_live_loaded["error"] == "context_missing"
        assert cleared_live_loaded["champion_id"] == ""
        assert cleared_live_loaded["source"] == "web"
        assert "266" not in context_path.read_text(encoding="utf-8")

        class FakeLcuResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "localPlayerCellId": 7,
                    "myTeam": [
                        {"cellId": 3, "championId": 245},
                        {"cellId": 7, "championId": 266},
                    ],
                }

        lcu_payload = FakeLcuResponse().json()
        expected_groups = {
            "selected_champion_ids": ["245", "266"],
            "bench_champion_ids": ["86"],
        }
        lcu_payload["benchChampions"] = [
            {"championId": 245},
            {"championId": 86},
        ]
        assert ui_runtime.build_lcu_candidate_groups(lcu_payload) == expected_groups
        assert web_runtime.build_lcu_candidate_groups(lcu_payload) == expected_groups
        assert ui_runtime.normalize_candidate_groups(expected_groups) == expected_groups

        with web_runtime._lcu_state_lock:
            saved_lcu_state = {
                "current_ids": set(web_runtime._lcu_state.current_ids),
                "selected_ids": list(web_runtime._lcu_state.selected_ids),
                "bench_ids": list(web_runtime._lcu_state.bench_ids),
                "local_champ_id": web_runtime._lcu_state.local_champ_id,
                "local_champ_name": web_runtime._lcu_state.local_champ_name,
                "state_version": web_runtime._lcu_state.state_version,
                "updated_at": web_runtime._lcu_state.updated_at,
            }
            assert web_runtime._extract_lcu_local_champion_id(
                {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 0}]}
            ) is None
            assert web_runtime._extract_lcu_local_champion_id(
                {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": "266"}]}
            ) == 266
            web_runtime._lcu_state.current_ids = {"1", "2"}
            web_runtime._lcu_state.selected_ids = ["1"]
            web_runtime._lcu_state.bench_ids = ["2"]
            web_runtime._lcu_state.local_champ_id = 266
            web_runtime._lcu_state.local_champ_name = "暗裔剑魔"
            web_runtime._lcu_state.state_version = 20
            web_runtime._lcu_state.updated_at = 1.0
            assert web_runtime._clear_lcu_local_champion_state() is True
            assert web_runtime._lcu_state.current_ids == {"1", "2"}
            assert web_runtime._lcu_state.selected_ids == ["1"]
            assert web_runtime._lcu_state.bench_ids == ["2"]
            assert web_runtime._lcu_state.local_champ_id is None
            assert web_runtime._lcu_state.local_champ_name is None
            assert web_runtime._lcu_state.state_version == 21
            assert web_runtime._lcu_state.updated_at > 1.0
            assert web_runtime._clear_lcu_local_champion_state() is False

            web_runtime._lcu_state.current_ids = {"1", "2"}
            web_runtime._lcu_state.selected_ids = ["1"]
            web_runtime._lcu_state.bench_ids = ["2"]
            web_runtime._lcu_state.local_champ_id = 1
            web_runtime._lcu_state.local_champ_name = "英雄1"
            web_runtime._lcu_state.state_version = 10
            web_runtime._lcu_state.updated_at = 1.0
            assert web_runtime._clear_lcu_candidate_state(clear_local=True) is True
            assert web_runtime._lcu_state.current_ids == set()
            assert web_runtime._lcu_state.selected_ids == []
            assert web_runtime._lcu_state.bench_ids == []
            assert web_runtime._lcu_state.local_champ_id is None
            assert web_runtime._lcu_state.local_champ_name is None
            assert web_runtime._lcu_state.state_version == 11
            assert web_runtime._lcu_state.updated_at > 1.0
            assert web_runtime._clear_lcu_candidate_state(clear_local=True) is False
            web_runtime._lcu_state.current_ids = saved_lcu_state["current_ids"]
            web_runtime._lcu_state.selected_ids = saved_lcu_state["selected_ids"]
            web_runtime._lcu_state.bench_ids = saved_lcu_state["bench_ids"]
            web_runtime._lcu_state.local_champ_id = saved_lcu_state["local_champ_id"]
            web_runtime._lcu_state.local_champ_name = saved_lcu_state["local_champ_name"]
            web_runtime._lcu_state.state_version = saved_lcu_state["state_version"]
            web_runtime._lcu_state.updated_at = saved_lcu_state["updated_at"]

        web_runtime_text = (RUN_DIR / "hextech" / "display" / "web" / "runtime.py").read_text(encoding="utf-8")
        assert "cleared_local_champion = _clear_lcu_local_champion_state()" in web_runtime_text
        assert "_clear_lcu_candidate_state(clear_local=True)" in web_runtime_text.rsplit("except Exception as exc:", 1)[1]

        fetch_calls: list[tuple[str, dict[str, str]]] = []

        def fake_fetch(url: str, headers: dict[str, str]) -> FakeLcuResponse:
            fetch_calls.append((url, dict(headers)))
            return FakeLcuResponse()

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=fake_fetch,
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is True
        lcu_loaded = overlay_context.read_overlay_context(context_path)
        assert lcu_loaded["ok"] is True
        assert lcu_loaded["champion_id"] == "266"
        assert lcu_loaded["champion_name"] == "暗裔剑魔"
        assert lcu_loaded["source"] == "lcu"
        assert fetch_calls and fetch_calls[0][0].endswith("/lol-champ-select/v1/session")
        assert "secret-token" not in context_path.read_text(encoding="utf-8")

        with (
            patch.object(overlay_context, "write_overlay_context") as stopped_write_context,
            patch.object(overlay_context, "write_missing_overlay_context") as stopped_write_missing,
        ):
            assert overlay_context.write_current_lcu_overlay_context_once(
                credential_provider=lambda: ("12345", "secret-token"),
                fetch_response=lambda _url, _headers: FakeLcuResponse(),
                core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
                context_path=context_path,
                should_write=lambda: False,
            ) is False
            stopped_write_context.assert_not_called()
            stopped_write_missing.assert_not_called()

        class ZeroChampionResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"localPlayerCellId": 7, "myTeam": [{"cellId": 7, "championId": 0}]}

        assert overlay_context.write_current_lcu_overlay_context_once(
            credential_provider=lambda: ("12345", "secret-token"),
            fetch_response=lambda _url, _headers: ZeroChampionResponse(),
            core_data_loader=lambda: {"266": {"name": "暗裔剑魔"}},
            context_path=context_path,
        ) is False
        zero_loaded = overlay_context.read_overlay_context(context_path)
        assert zero_loaded["ok"] is False
        assert zero_loaded["error"] == "context_missing"
        assert zero_loaded["champion_id"] == ""
        assert zero_loaded["source"] == "lcu"
        assert "266" not in context_path.read_text(encoding="utf-8")
        assert overlay_context.OverlayContextPoller.stop.__defaults__ == (4.0,)

        context_module_text = (RUN_DIR / "hextech" / "overlay" / "context.py").read_text(encoding="utf-8")
        assert "should_write=lambda: not stop_event.is_set()" in context_module_text
        assert "if should_write is not None and not should_write():" in context_module_text
        assert "@lru_cache(maxsize=256)" in context_module_text

        class DummyServiceManager:
            def __init__(self, running: bool) -> None:
                self._running = running

            def is_game_overlay_running(self) -> bool:
                return self._running

        stopped_ui = DummyUI()
        stopped_ui.service_manager = DummyServiceManager(False)
        running_ui = DummyUI()
        running_ui.service_manager = DummyServiceManager(True)
        with (
            patch.object(overlay_context, "write_overlay_context") as mocked_write_context,
            patch.object(overlay_context, "write_missing_overlay_context") as mocked_write_missing,
        ):
            assert ui_runtime._write_overlay_context_from_live_state(
                stopped_ui,
                {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"},
                source="web",
            ) is False
            assert ui_runtime._write_overlay_context_from_live_state(
                stopped_ui,
                {"local_champion_id": 0},
                source="web",
            ) is False
            mocked_write_context.assert_not_called()
            mocked_write_missing.assert_not_called()

            assert ui_runtime._write_overlay_context_from_live_state(
                running_ui,
                {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"},
                source="web",
            ) is False
            mocked_write_context.assert_not_called()
            assert ui_runtime._write_overlay_context_from_live_state(
                running_ui,
                {"local_champion_id": 266, "local_champion_name": "暗裔剑魔"},
                source="web",
                context_path=context_path,
            ) is True
            mocked_write_context.assert_called_once()

    module_text = (RUN_DIR / "hextech" / "overlay" / "context.py").read_text(encoding="utf-8").lower()
    forbidden_terms = ["fastapi", "web_api", "web_runtime", "full_hextech_scraper", "auth.json"]
    assert not any(term in module_text for term in forbidden_terms)
    assert "remoting-auth-token" in module_text
    assert "write_current_lcu_overlay_context_once" in module_text
    assert "from hextech.overlay.runtime_paths import overlay_runtime_state_path" in module_text
    assert "def _overlay_runtime_root_dir" not in module_text
    assert "def _overlay_runtime_state_path" not in module_text


def check_official_overlay_provider_contract() -> None:
    """验证官方接口 provider 只做本地接口归一化，并通过现有 overlay 事件协议输出。"""
    import hextech.overlay.providers.official as official_overlay_provider
    import hextech.overlay.events as overlay_event_channel
    import tools.acceptance.probe_official_overlay_provider as probe_official_overlay_provider

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

    import hextech.overlay.vision.sidecar as overlay_vision_sidecar
    from hextech.overlay.vision.layout import CARD_PANELS_16_10, apply_transform, detect_selection_scene, pick_card_panels
    from hextech.overlay.vision.matcher import candidate_from_slot
    from hextech.overlay.vision.state import SelectionTracker
    from hextech.overlay.window import cursor_in_client_boxes

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
            int(image.size[0] * 0.445),
            int(image.size[1] * 0.775),
            int(image.size[0] * 0.555),
            int(image.size[1] * 0.813),
        )
        draw.rounded_rectangle(box, radius=14, fill="#168fcf", outline="#54d5ff", width=4)
        return box

    def _paint_card_borders(image: Image.Image) -> None:
        draw = ImageDraw.Draw(image)
        for left, top, right, bottom in pick_card_panels(image.size):
            draw.rectangle(
                (int(left * image.width), int(top * image.height), int(right * image.width), int(bottom * image.height)),
                outline="#d8b36f",
                width=8,
            )

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

    def _paint_slot_name(image: Image.Image, box: tuple[int, int, int, int], name: str) -> None:
        left, top, right, bottom = box
        draw = ImageDraw.Draw(image)
        draw.rectangle(box, fill="#001010")
        name_mask = overlay_vision_sidecar._render_name_mask(name)
        assert name_mask is not None
        text_image = Image.merge("RGB", (name_mask, name_mask, name_mask))
        x = left + max(0, (right - left - text_image.width) // 2)
        y = top + max(0, (bottom - top - text_image.height) // 2)
        image.paste(text_image, (x, y))

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
            "augment_a": {"name": "尤里卡", "tier": "Gold", "summary": "合成测试 A", "image": templates["augment_a"]},
            "augment_b": {"name": "精怪魔法", "tier": "Prismatic", "summary": "合成测试 B", "image": templates["augment_b"]},
            "augment_c": {"name": "重量级打击手", "tier": "Silver", "summary": "合成测试 C", "image": templates["augment_c"]},
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
    _paint_card_borders(frame)
    preset = overlay_vision_sidecar.resolve_roi_preset(2560, 1600, preset="auto")
    for slot_index, box in enumerate(preset.slot_boxes(frame.size)):
        left, top, right, bottom = box
        # ROI 即图标框：模板按整框贴入，与真实几何（crop ≈ 图标整图）一致。
        frame.paste(templates[f"augment_{chr(ord('a') + slot_index)}"].resize((right - left, bottom - top)), (left, top))
    for slot_index, name_box in enumerate(preset.name_boxes(frame.size)):
        _paint_slot_name(frame, name_box, ["尤里卡", "精怪魔法", "重量级打击手"][slot_index])

    with TemporaryDirectory() as tmp_dir:
        calibration_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        detection = overlay_vision_sidecar.detect_overlay_choices(
            frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
        assert not calibration_path.exists()
        cached_detection = overlay_vision_sidecar.detect_overlay_choices(
            frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=calibration_path,
        )
        assert cached_detection["source"]["calibration"] == "layout_v2"
    assert detection["active"] is True
    assert detection["selection_type"] == "hextech"
    assert detection["source"]["tag"] == "vision-sidecar"
    assert detection["source"]["preset"] == "2560x1600"
    assert detection["source"]["capture_size"] == [2560, 1600]
    assert detection["source"]["calibration"] == "layout_v2"
    assert detection["source"]["scene_present"] is True
    assert detection["source"]["scene_state"] == "candidate"
    assert detection["source"]["layout_id"] == "2560x1600"
    assert detection["source"]["gate_state"] == "visible_ready"
    assert detection["source"]["ready_slots"] == 3
    assert detection["source"].get("selection_button_present") is True
    assert detection["source"].get("selection_window_active") is True
    assert float(detection["source"].get("button_blue_ratio") or 0.0) > 0.0
    assert len(detection["source"].get("button_box") or []) == 4
    assert [slot["augment_id"] for slot in detection["slots"]] == ["augment_a", "augment_b", "augment_c"]
    first_channels = detection["_raw_slots"][0]["channels"]
    assert first_channels["icon_shortlist"]["top_candidates"]
    assert first_channels["text_narrowed"]["top_candidates"]
    assert "margin" in first_channels["text_narrowed"]
    assert "top_candidates" in first_channels["text_alt_narrowed"]

    partial_frame = frame.copy()
    partial_draw = ImageDraw.Draw(partial_frame)
    partial_draw.rectangle(preset.slot_boxes(partial_frame.size)[2], fill="#070b12")
    partial_draw.rectangle(preset.name_boxes(partial_frame.size)[2], fill="#070b12")
    with TemporaryDirectory() as tmp_dir:
        partial_detection = overlay_vision_sidecar.detect_overlay_choices(
            partial_frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    assert partial_detection["active"] is False
    assert partial_detection["source"]["ready_slots"] == 2
    assert partial_detection["source"]["content_ready"] is False
    assert partial_detection["source"]["reason"] == "partial_ready"
    assert partial_detection["source"].get("selection_button_present") is True
    assert partial_detection["source"].get("selection_window_active") is True

    conflict_frame = frame.copy()
    first_slot_box = preset.slot_boxes(conflict_frame.size)[0]
    left, top, right, bottom = first_slot_box
    conflict_frame.paste(templates["augment_b"].resize((right - left, bottom - top)), (left, top))
    with TemporaryDirectory() as tmp_dir:
        conflict_detection = overlay_vision_sidecar.detect_overlay_choices(
            conflict_frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    assert conflict_detection["active"] is True
    assert conflict_detection["source"]["ready_slots"] == 3
    assert conflict_detection["source"]["content_ready"] is True
    assert conflict_detection["slots"][0]["state"] == "ready"
    assert conflict_detection["slots"][0]["name"] == "尤里卡"
    assert conflict_detection["slots"][0]["diagnostic"] == "text_icon_disagree"

    modal_frame = frame.copy()
    modal_draw = ImageDraw.Draw(modal_frame)
    modal_draw.rectangle((870, 410, 1690, 780), fill="#111820", outline="#8d6b2e", width=4)
    modal_draw.rectangle((1110, 640, 1450, 715), fill="#6b491f", outline="#d8b36f", width=5)
    assert overlay_vision_sidecar._blocking_modal_present(modal_frame) is True
    assert overlay_vision_sidecar._blocking_modal_present(frame) is False
    with TemporaryDirectory() as tmp_dir:
        modal_detection = overlay_vision_sidecar.detect_overlay_choices(
            modal_frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    assert modal_detection["active"] is False
    assert modal_detection["source"]["reason"] == "blocking_modal_present"
    assert modal_detection["source"]["ready_slots"] >= 1

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
    assert blank_detection["source"]["reason"] == "selection_scene_not_detected"
    assert blank_detection["source"]["calibration"] == "layout_v2"
    assert blank_detection["source"].get("selection_window_active") is False
    assert blank_detection["source"]["content_ready"] is False
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
    assert no_button_detection["source"]["reason"] == "selection_scene_not_detected"
    assert no_button_detection["source"].get("selection_button_present") is False
    assert no_button_detection["source"].get("selection_window_active") is False

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
    assert scattered_detection["source"]["reason"] == "selection_scene_not_detected"

    # V2 不再读取或改写旧 anchor；旧文件保留在磁盘也不能影响当前版式。
    with TemporaryDirectory() as tmp_dir:
        old_anchor_path = Path(tmp_dir) / "overlay_anchor_calibration.v1.json"
        old_anchor_text = '{"schema_version":1,"button_box":[0.44,0.89,0.56,0.92]}'
        old_anchor_path.write_text(old_anchor_text, encoding="utf-8")
        ignored_anchor_detection = overlay_vision_sidecar.detect_overlay_choices(
            frame,
            template_index,
            preset_name="auto",
            min_confidence=0.80,
            calibration_path=old_anchor_path,
        )
        assert old_anchor_path.read_text(encoding="utf-8") == old_anchor_text
    assert ignored_anchor_detection["active"] is True
    assert ignored_anchor_detection["source"]["calibration"] == "layout_v2"

    missing_button = frame.copy()
    ImageDraw.Draw(missing_button).rectangle(
        (int(2560 * 0.43), int(1600 * 0.77), int(2560 * 0.57), int(1600 * 0.86)),
        fill="#070b12",
    )
    missing_button_detection = overlay_vision_sidecar.detect_overlay_choices(
        missing_button,
        template_index,
        preset_name="auto",
        min_confidence=0.80,
    )
    assert missing_button_detection["active"] is False
    assert missing_button_detection["source"]["reason"] == "selection_scene_not_detected"
    assert missing_button_detection["source"].get("selection_button_present") is False
    assert missing_button_detection["source"]["card_residue"] is True
    assert max(missing_button_detection["source"]["panel_scores"]) >= 0.35

    client_rect = (100, 200, 1100, 800)
    client_card_boxes = [(200, 100, 400, 500), (450, 100, 650, 500)]
    assert cursor_in_client_boxes(client_rect, client_card_boxes, cursor_position=(350, 350)) is True
    assert cursor_in_client_boxes(client_rect, client_card_boxes, cursor_position=(900, 750)) is False
    with patch.object(overlay_vision_sidecar, "cursor_in_client_boxes", return_value=True) as cursor_gate:
        assert overlay_vision_sidecar._cursor_over_card_panels(
            client_rect,
            missing_button.size,
            missing_button_detection["source"],
        ) is True
    assert len(cursor_gate.call_args.args[1]) == 3

    # 载入画面杂乱内容也能蹭到 low_confidence；空占位框不得触发 active 显示。
    assert (
        overlay_vision_sidecar._scene_active_from_slots(
            [{"state": "low_confidence", "confidence": 0.7} for _ in range(3)]
        )
        is False
    )
    assert (
        overlay_vision_sidecar._scene_active_from_slots(
            [{"state": "ready", "augment_id": "a"}, {"state": "low_confidence", "confidence": 0.6}, {"state": "empty"}]
        )
        is False
    )
    assert (
        overlay_vision_sidecar._scene_active_from_slots(
            [{"state": "ready", "augment_id": "a"}, {"state": "ready", "augment_id": "b"}, {"state": "ready", "augment_id": "c"}]
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
    assert detect_selection_scene(mid_band, layout_id="2560x1600").present is False

    # 载入进度条中心约 0.90H，颜色和宽度都像按钮，但垂直位置必须拒绝。
    loading_bar = Image.new("RGB", (2560, 1600), "#070b12")
    ImageDraw.Draw(loading_bar).rounded_rectangle(
        (int(2560 * 0.44), int(1600 * 0.885), int(2560 * 0.56), int(1600 * 0.915)),
        radius=14,
        fill="#168fcf",
    )
    assert overlay_vision_sidecar.detect_selection_button_box(loading_bar) is None
    assert detect_selection_scene(loading_bar, layout_id="2560x1600").present is False

    # 真实按钮在动画/抗锯齿帧里有时只剩内层蓝色区域被 mask 命中。
    # 这个框可以证明“按钮存在”，但不能把整套 ROI 往下拖，否则名称框会截到按钮边缘。
    inner_button_frame = Image.new("RGB", (2560, 1600), "#070b12")
    _paint_card_borders(inner_button_frame)
    ImageDraw.Draw(inner_button_frame).rectangle((1146, 1248, 1418, 1304), fill="#168fcf")
    inner_scene = detect_selection_scene(inner_button_frame, layout_id="2560x1600")
    assert inner_scene.present is True
    assert abs(inner_scene.transform.dy_ratio) <= (2.0 / 1600.0)
    stable_name_box = apply_transform(preset.name_slots[0], inner_button_frame.size, inner_scene.transform)
    assert stable_name_box[3] <= int(round(0.431 * inner_button_frame.height))

    # 1920×1200 是 16:10；sidecar 和 renderer 必须使用同一个面板选择规则。
    wide_16_10 = Image.new("RGB", (1920, 1200), "#070b12")
    _paint_selection_button(wide_16_10)
    _paint_card_borders(wide_16_10)
    wide_scene = detect_selection_scene(wide_16_10, layout_id="1920x1080")
    assert wide_scene.present is True
    assert pick_card_panels(wide_16_10.size) == CARD_PANELS_16_10

    body_shard_frame = Image.new("RGB", (2560, 1600), "#070b12")
    _paint_selection_button(body_shard_frame)
    _paint_card_borders(body_shard_frame)
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
    assert body_shard_detection["source"]["ready_slots"] == 0
    assert body_shard_detection["source"]["reason"] == "selection_scene_not_detected"

    shard_fixture_dir = RESOURCE_DIAGNOSTIC_DIR / "overlay_vision_fixtures" / "body_shard_20260621"
    shard_name_crops = [Image.open(shard_fixture_dir / f"name_{index}.png").convert("RGB") for index in range(3)]
    shard_scores = overlay_vision_sidecar._body_shard_name_scores(shard_name_crops)
    assert len(shard_scores) == 3
    assert sum(score >= 0.80 for score in shard_scores) >= 2
    assert overlay_vision_sidecar._body_shard_scene_present(shard_scores) is True

    real_index = overlay_vision_sidecar.load_default_template_index()
    glass_cannon_template = next((entry for entry in real_index if entry.name == "玻璃大炮"), None)
    assert glass_cannon_template is not None
    assert glass_cannon_template.augment_id == "glasscannon"
    assert glass_cannon_template.tier == "棱彩"

    regression_fixture_dir = RESOURCE_DIAGNOSTIC_DIR / "overlay_vision_fixtures" / "hextech_20260622"
    regression_names = ("更万用的瞄准镜", "闪现向前", "大法师")
    regression_crops = [
        Image.open(regression_fixture_dir / f"name_{index}.png").convert("RGB")
        for index in range(3)
    ]
    assert overlay_vision_sidecar._body_shard_scene_present(
        overlay_vision_sidecar._body_shard_name_scores(regression_crops)
    ) is False
    for crop, expected_name in zip(regression_crops, regression_names):
        _crop_std, ranked_names = overlay_vision_sidecar._rank_name_templates(crop, real_index)
        assert ranked_names and ranked_names[0][0].name == expected_name

    normal_name_crops = [frame.crop(box) for box in preset.name_boxes(frame.size)]
    normal_scores = overlay_vision_sidecar._body_shard_name_scores(normal_name_crops)
    assert overlay_vision_sidecar._body_shard_scene_present(normal_scores) is False

    real_shard_frame = body_shard_frame.copy()
    for name_box, name_crop in zip(preset.name_boxes(real_shard_frame.size), shard_name_crops):
        left, top, right, bottom = name_box
        real_shard_frame.paste(name_crop.resize((right - left, bottom - top)), (left, top))
    real_shard_detection = overlay_vision_sidecar.detect_overlay_choices(
        real_shard_frame,
        template_index,
        preset_name="auto",
        min_confidence=0.80,
    )
    assert real_shard_detection["active"] is False
    assert real_shard_detection["selection_type"] == "body_shard"
    assert real_shard_detection["source"]["reason"] == "body_shard_only"
    assert real_shard_detection["source"]["scene_kind"] == "body_shard"
    assert len(real_shard_detection["source"]["body_shard_scores"]) == 3

    shared_icon_frame = frame.copy()
    for box in preset.slot_boxes(shared_icon_frame.size):
        left, top, right, bottom = box
        shared_icon_frame.paste(templates["augment_a"].resize((right - left, bottom - top)), (left, top))
    shared_icon_detection = overlay_vision_sidecar.detect_overlay_choices(
        shared_icon_frame,
        template_index,
        preset_name="auto",
        min_confidence=0.80,
    )
    assert shared_icon_detection["active"] is True
    assert shared_icon_detection["source"]["ready_slots"] == 3
    assert [slot["name"] for slot in shared_icon_detection["slots"]] == ["尤里卡", "精怪魔法", "重量级打击手"]

    # 接近平坦的深色面板不得误识别卡片；按钮存在时仍只显示 detecting 骨架。
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
    assert dark_detection["source"].get("selection_window_active") is False
    assert dark_detection["source"].get("scene_reject_reason") == "selection_layout_missing"
    assert all(slot["state"] != "ready" for slot in dark_detection["slots"])

    # 共用图标按规范化 mask digest 分组；icon-only 仍不得直接授权显示。
    twin_index = overlay_vision_sidecar.build_template_index(
        {
            "twin_a": {"name": "孪生 A", "image": _make_glyph_template("ellipse")},
            "twin_b": {"name": "孪生 B", "image": _make_glyph_template("ellipse")},
        }
    )
    twin_slot = overlay_vision_sidecar._detect_slot(frame, preset.slot_boxes(frame.size)[0], 0, twin_index, min_confidence=0.80)
    assert twin_index[0].icon_digest == twin_index[1].icon_digest
    assert len(twin_index[0].icon_fingerprints) >= 2
    assert twin_slot["state"] != "ready"
    assert twin_slot["diagnostic"] == "icon_only_low_confidence"
    twin_shortlist = overlay_vision_sidecar._build_icon_shortlist(
        [(twin_index[0], 0.80), (twin_index[1], 0.80)],
        max_groups=1,
    )
    assert [template.name for template, _confidence in twin_shortlist] == ["孪生 A", "孪生 B"]

    # 槽位判定真值表：平坦拒绝、低置信度拒绝、margin 不足只接受极高置信度。
    assert overlay_vision_sidecar._slot_match_decision(5.0, 0.99, 0.5, min_confidence=0.80) is False
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.70, 0.5, min_confidence=0.80) is False
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.85, 0.001, min_confidence=0.80) is False
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.85, 0.05, min_confidence=0.80) is True
    assert overlay_vision_sidecar._slot_match_decision(40.0, 0.95, 0.0, min_confidence=0.80) is True

    # 中文名必须能生成稳定 ID；ASCII-only 归一化曾把 206/208 个模板滤成空导致识别不可用。
    assert overlay_vision_sidecar.normalize_augment_id("魄罗爆破手") == "魄罗爆破手"
    assert len(real_index) >= 100, f"真实模板索引过小: {len(real_index)}"

    # 退出防抖：按钮消失吸收 1 帧避免闪烁；blocking/modal 仍即时隐藏。
    assert "selection_window_active" in inspect.signature(
        overlay_vision_sidecar.should_defer_unstable_event
    ).parameters
    assert (
        overlay_vision_sidecar.should_defer_unstable_event(
            ("active", "a", "b", "c"),
            1,
            selection_window_active=False,
        )
        is True
    )
    assert (
        overlay_vision_sidecar.should_defer_unstable_event(
            ("active", "a", "b", "c"),
            2,
            selection_window_active=False,
        )
        is False
    )
    assert (
        overlay_vision_sidecar.should_defer_unstable_event(
            ("active", "a", "b", "c"),
            1,
            selection_window_active=False,
            reason="blocking_modal_present",
        )
        is False
    )
    assert (
        overlay_vision_sidecar.should_defer_unstable_event(
            ("active", "a", "b", "c"),
            1,
            selection_window_active=True,
        )
        is True
    )
    assert (
        overlay_vision_sidecar.should_defer_unstable_event(
            ("active", "a", "b", "c"),
            2,
            selection_window_active=True,
        )
        is False
    )
    assert overlay_vision_sidecar.should_defer_unstable_event(("inactive", "unstable"), 1) is False
    assert overlay_vision_sidecar.should_defer_unstable_event(None, 1) is False

    # 高速循环的 interval 是帧起点目标周期，识别本身已超时时不得再固定追加休眠。
    assert overlay_vision_sidecar.remaining_frame_sleep_seconds(160, elapsed_seconds=0.180) == 0.0
    assert abs(overlay_vision_sidecar.remaining_frame_sleep_seconds(160, elapsed_seconds=0.100) - 0.060) < 1e-9

    tracker = SelectionTracker()
    first_scene_frame = tracker.update(detection)
    stable = tracker.update(detection)
    assert first_scene_frame["active"] is False
    assert stable["active"] is True
    assert stable["slots"][1]["augment_id"] == "augment_b"
    assert stable["source"]["ready_slots"] == 3
    assert stable["source"]["content_ready"] is True

    hover_detection = json.loads(json.dumps(missing_button_detection, ensure_ascii=False))
    hover_detection["source"]["cursor_over_cards"] = True
    hover_detection["source"]["card_residue"] = True
    hover_tracker = SelectionTracker()
    hover_tracker.update(detection)
    hover_stable = hover_tracker.update(detection)
    stable_signature_before_hover = [slot["augment_id"] for slot in hover_stable["slots"]]
    for _index in range(20):
        hover_result = hover_tracker.update(hover_detection)
        assert hover_result["active"] is True
        assert hover_result["source"]["hover_occluded"] is True
        assert [slot["augment_id"] for slot in hover_result["slots"]] == stable_signature_before_hover

    hover_detection["source"]["cursor_over_cards"] = False
    residue_hold = hover_tracker.update(hover_detection)
    assert residue_hold["active"] is True
    assert residue_hold["source"]["reason"] == "scene_residue_hold"
    assert residue_hold["source"]["hover_occluded"] is False

    cleared_detection = json.loads(json.dumps(hover_detection, ensure_ascii=False))
    cleared_detection["source"]["card_residue"] = False
    cleared_detection["source"]["name_residue"] = [False, False, False]
    first_hover_exit = hover_tracker.update(cleared_detection)
    second_hover_exit = hover_tracker.update(cleared_detection)
    assert first_hover_exit["active"] is True
    assert second_hover_exit["active"] is False
    assert second_hover_exit["source"]["scene_state"] == "absent"

    # 三槽独立稳定：部分结果即可显示，未知槽保持固定占位。
    partial_tracker = SelectionTracker()
    partial_tracker.update(partial_detection)
    stable_partial = partial_tracker.update(partial_detection)
    assert stable_partial["active"] is True
    assert stable_partial["source"]["ready_slots"] == 2
    assert stable_partial["source"]["content_ready"] is False
    assert stable_partial["slots"][2]["state"] == "detecting"

    progressive_tracker = SelectionTracker()
    progressive_results: list[int] = []
    for ready_count in range(4):
        progressive_event = dict(detection)
        progressive_event["_raw_slots"] = [
            dict(slot) if index < ready_count else {}
            for index, slot in enumerate(detection["_raw_slots"])
        ]
        progressive_tracker.update(progressive_event)
        progressive_result = progressive_tracker.update(progressive_event)
        progressive_results.append(int(progressive_result["source"]["ready_slots"]))
        assert len(progressive_result["slots"]) == 3
    assert progressive_results == [0, 1, 2, 3]

    # 弱文字候选必须跨三帧一致，不能靠单帧降阈值授权。
    weak_event = dict(detection)
    weak_slot = json.loads(json.dumps(detection["_raw_slots"][0], ensure_ascii=False))
    weak_slot["channels"]["text"]["top_candidates"][0]["confidence"] = 0.69
    weak_slot["channels"]["text"]["margin"] = 0.015
    weak_slot["channels"]["text_alt"]["top_candidates"][0]["confidence"] = 0.50
    weak_slot["channels"]["icon"]["top_candidates"][0]["confidence"] = 0.50
    weak_slot["channels"]["icon_shortlist"] = {"top_candidates": []}
    weak_slot["channels"]["text_narrowed"] = {"margin": 0.0, "top_candidates": []}
    weak_slot["channels"]["text_alt_narrowed"] = {"margin": 0.0, "top_candidates": []}
    weak_event["_raw_slots"] = [weak_slot, {}, {}]
    weak_tracker = SelectionTracker(scene_enter_frames=1)
    assert weak_tracker.update(weak_event)["source"]["ready_slots"] == 0
    assert weak_tracker.update(weak_event)["source"]["ready_slots"] == 0
    assert weak_tracker.update(weak_event)["source"]["ready_slots"] == 1

    def _candidate(name: str, confidence: float, *, augment_id: str | None = None) -> dict[str, Any]:
        return {
            "augment_id": augment_id or name,
            "name": name,
            "tier": "Gold",
            "summary": "shortlist test",
            "confidence": confidence,
            "icon_digest": f"digest-{augment_id or name}",
        }

    alt_strong_slot = {
        "slot": 0,
        "top_candidates": [_candidate("主字体近似项", 0.75)],
        "channels": {
            "text": {"margin": 0.01, "top_candidates": [_candidate("主字体近似项", 0.75)]},
            "text_alt": {"margin": 0.05, "top_candidates": [_candidate("正确海克斯", 0.82)]},
            "icon": {"margin": 0.01, "top_candidates": [_candidate("弱图标候选", 0.72)]},
            "icon_shortlist": {"top_candidates": []},
            "text_narrowed": {"margin": 0.0, "top_candidates": []},
            "text_alt_narrowed": {"margin": 0.0, "top_candidates": []},
        },
    }
    alt_strong_candidate = candidate_from_slot(alt_strong_slot)
    assert alt_strong_candidate is not None
    assert alt_strong_candidate.name == "正确海克斯"
    assert alt_strong_candidate.rule == "strong_text_alt"
    assert alt_strong_candidate.required_frames == 2

    shortlist_temporal_slot = {
        "slot": 0,
        "top_candidates": [_candidate("正确海克斯", 0.72), _candidate("全局近似项", 0.715)],
        "channels": {
            "text": {"margin": 0.005, "top_candidates": [_candidate("正确海克斯", 0.72), _candidate("全局近似项", 0.715)]},
            "text_alt": {"margin": 0.001, "top_candidates": [_candidate("其它字体候选", 0.60)]},
            "icon": {"margin": 0.01, "top_candidates": [_candidate("正确海克斯", 0.74)]},
            "icon_shortlist": {"top_candidates": [_candidate("正确海克斯", 0.74)]},
            "text_narrowed": {"margin": 0.04, "top_candidates": [_candidate("正确海克斯", 0.72), _candidate("短名单次选", 0.68)]},
            "text_alt_narrowed": {"margin": 0.01, "top_candidates": [_candidate("其它字体候选", 0.60)]},
        },
    }
    temporal_candidate = candidate_from_slot(shortlist_temporal_slot)
    assert temporal_candidate is not None
    assert temporal_candidate.rule == "icon_shortlist_temporal"
    assert temporal_candidate.required_frames == 3

    shortlist_dual_slot = json.loads(json.dumps(shortlist_temporal_slot, ensure_ascii=False))
    shortlist_dual_slot["channels"]["text_narrowed"] = {
        "margin": 0.01,
        "top_candidates": [_candidate("正确海克斯", 0.67)],
    }
    shortlist_dual_slot["channels"]["text_alt_narrowed"] = {
        "margin": 0.01,
        "top_candidates": [_candidate("正确海克斯", 0.66)],
    }
    dual_candidate = candidate_from_slot(shortlist_dual_slot)
    assert dual_candidate is not None
    assert dual_candidate.rule == "icon_shortlist_dual_font"
    assert dual_candidate.required_frames == 2

    stale_hold_tracker = SelectionTracker(scene_enter_frames=1)
    stale_hold_tracker.update(detection)
    stable_before_weak_reroll = stale_hold_tracker.update(detection)
    assert stable_before_weak_reroll["slots"][0]["augment_id"] == "augment_a"
    weak_reroll_event = dict(detection)
    weak_reroll_slot = json.loads(json.dumps(shortlist_temporal_slot, ensure_ascii=False))
    weak_reroll_slot["slot"] = 0
    weak_reroll_event["_raw_slots"] = [weak_reroll_slot, {}, {}]
    stale_hold_event = stale_hold_tracker.update(weak_reroll_event)
    assert stale_hold_event["slots"][0]["augment_id"] == "augment_a"
    assert "stale_hold:" in stale_hold_event["_acceptance_rules"][0]

    conflict_shortlist_slot = json.loads(json.dumps(shortlist_temporal_slot, ensure_ascii=False))
    conflict_shortlist_slot["channels"]["icon"] = {
        "margin": 0.05,
        "top_candidates": [_candidate("冲突海克斯", 0.95)],
    }
    assert candidate_from_slot(conflict_shortlist_slot) is None

    icon_only_shortlist_slot = json.loads(json.dumps(shortlist_temporal_slot, ensure_ascii=False))
    icon_only_shortlist_slot["channels"]["text"] = {"margin": 0.0, "top_candidates": []}
    icon_only_shortlist_slot["channels"]["text_narrowed"] = {"margin": 0.0, "top_candidates": []}
    assert candidate_from_slot(icon_only_shortlist_slot) is None

    # 单槽出现一个新的强候选时只撤下该槽，不得替换成未经稳定的新结果。
    reroll = dict(detection)
    reroll_raw_slots = [dict(slot) for slot in detection["_raw_slots"]]
    reroll_raw_slots[0] = dict(reroll_raw_slots[1])
    reroll_raw_slots[0]["slot"] = 0
    reroll["_raw_slots"] = reroll_raw_slots
    reroll_first = tracker.update(reroll)
    assert reroll_first["active"] is True
    assert reroll_first["source"]["ready_slots"] == 2
    assert reroll_first["slots"][0]["state"] == "detecting"
    assert [slot["augment_id"] for slot in reroll_first["slots"][1:]] == ["augment_b", "augment_c"]
    reroll_ready = tracker.update(reroll)
    assert reroll_ready["slots"][0]["augment_id"] == "augment_b"

    # 场景/按钮消失两帧后结束 epoch；Tab 与阻塞弹窗必须立即清空。
    tracker.update(blank_detection)
    unstable = tracker.update(blank_detection)
    assert unstable["active"] is False
    assert unstable["source"]["scene_state"] == "absent"
    blocked_transition = tracker.update(modal_detection)
    assert blocked_transition["active"] is False
    assert blocked_transition["source"].get("reason") == "blocking_modal_present"
    tracker.update(body_shard_detection)
    shard_transition = tracker.update(body_shard_detection)
    assert shard_transition["active"] is False
    assert shard_transition["source"].get("reason") == "slots_detecting"

    shard_tracker = SelectionTracker(scene_enter_frames=1)
    shard_blocked = shard_tracker.update(real_shard_detection)
    assert shard_blocked["active"] is False
    assert shard_blocked["source"]["reason"] == "body_shard_only"
    assert shard_blocked["source"]["body_shard_latched"] is True
    false_regular_frame = shard_tracker.update(detection)
    assert false_regular_frame["active"] is False
    assert false_regular_frame["source"]["reason"] == "body_shard_only"
    assert false_regular_frame["source"]["body_shard_latched"] is True
    first_absent_after_shard = shard_tracker.update(blank_detection)
    assert first_absent_after_shard["active"] is False
    assert first_absent_after_shard["source"]["body_shard_latched"] is True
    second_absent_after_shard = shard_tracker.update(blank_detection)
    assert second_absent_after_shard["active"] is False
    assert second_absent_after_shard["source"].get("body_shard_latched") is not True
    assert shard_tracker.update(detection)["active"] is False
    assert shard_tracker.update(detection)["active"] is True

    tab_transition = tracker.block("scoreboard_key_down", scoreboard_key_down=True)
    assert tab_transition["active"] is False
    assert tab_transition["source"]["scoreboard_key_down"] is True
    active_signature = overlay_vision_sidecar._loop_event_signature(stable)
    unstable_signature = overlay_vision_sidecar._loop_event_signature(unstable)
    assert active_signature == ("active", "ready:augment_a", "ready:augment_b", "ready:augment_c")
    assert unstable_signature != active_signature
    assert unstable_signature[0] == "inactive"
    assert overlay_vision_sidecar.DEFAULT_LOOP_HEARTBEAT_SECONDS == 1.0
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
        last_signature=unstable_signature,
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

        # Tab 是硬门：按下立即 inactive，松开后必须重新积累两帧才能恢复。
        tab_event_path = Path(tmp_dir) / "tab-gated.json"
        tab_states = iter((True, False, False))
        tab_sleep_calls = 0

        def stop_after_tab_recovery(_seconds: float) -> None:
            nonlocal tab_sleep_calls
            tab_sleep_calls += 1
            if tab_sleep_calls >= 3:
                raise StopLoop()

        with (
            patch.object(overlay_vision_sidecar, "load_default_template_index", return_value=template_index),
            patch.object(overlay_vision_sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 100, 100))),
            patch.object(overlay_vision_sidecar, "_is_lol_game_foreground", return_value=True),
            patch.object(overlay_vision_sidecar, "is_scoreboard_key_down", side_effect=lambda: next(tab_states)),
            patch.object(overlay_vision_sidecar, "_capture_lol_game_rect", return_value=frame),
            patch.object(overlay_vision_sidecar, "detect_overlay_choices", return_value=detection),
            patch.object(overlay_vision_sidecar.time, "sleep", side_effect=stop_after_tab_recovery),
        ):
            try:
                overlay_vision_sidecar.run_loop(
                    write_event=True,
                    event_path=tab_event_path,
                    required_frames=2,
                )
            except StopLoop:
                pass
        tab_payload = json.loads(tab_event_path.read_text(encoding="utf-8"))
        assert tab_payload["active"] is True
        tab_history = json.loads(
            tab_event_path.with_name(overlay_vision_sidecar.OVERLAY_VISION_TRACE_HISTORY_FILE.name).read_text(encoding="utf-8")
        )
        assert any(entry.get("scoreboard_key_down") is True for entry in tab_history["entries"])

        # Alt+Tab 会清空复用事件；回到前台后必须重新获得两帧新检测。
        recovered_event_path = Path(tmp_dir) / "focus-recovered.json"
        focus_states = iter((True, False, True, True))
        sleep_calls = 0

        def stop_after_focus_recovery(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 4:
                raise StopLoop()

        with (
            patch.object(overlay_vision_sidecar, "load_default_template_index", return_value=template_index),
            patch.object(overlay_vision_sidecar, "_find_lol_game_window", return_value=(123, (0, 0, 100, 100))),
            patch.object(overlay_vision_sidecar, "_is_lol_game_foreground", side_effect=lambda _hwnd: next(focus_states)),
            patch.object(overlay_vision_sidecar, "is_scoreboard_key_down", return_value=False),
            patch.object(overlay_vision_sidecar, "_capture_lol_game_rect", return_value=frame),
            patch.object(overlay_vision_sidecar, "detect_overlay_choices", return_value=detection),
            patch.object(overlay_vision_sidecar.time, "sleep", side_effect=stop_after_focus_recovery),
        ):
            try:
                overlay_vision_sidecar.run_loop(
                    write_event=True,
                    event_path=recovered_event_path,
                    required_frames=2,
                )
            except StopLoop:
                pass
        recovered_payload = json.loads(recovered_event_path.read_text(encoding="utf-8"))
        assert recovered_payload["active"] is True
        trace_path = Path(tmp_dir) / overlay_vision_sidecar.OVERLAY_VISION_TRACE_FILE.name
        history_path = Path(tmp_dir) / overlay_vision_sidecar.OVERLAY_VISION_TRACE_HISTORY_FILE.name
        assert trace_path.is_file()
        assert history_path.is_file()
        trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
        history_payload = json.loads(history_path.read_text(encoding="utf-8"))
        assert trace_payload["active"] is True
        assert trace_payload["schema_version"] == 2
        assert trace_payload["source"]["scene_state"] == "active"
        assert trace_payload["source"]["layout_id"] == "2560x1600"
        assert set(trace_payload["source"]) >= {
            "scene_kind",
            "body_shard_scores",
            "body_shard_latched",
            "cursor_over_cards",
            "card_residue",
            "name_residue",
            "hover_occluded",
        }
        assert len(trace_payload["slots"]) == 3
        for traced_slot in trace_payload["slots"]:
            assert traced_slot["acceptance_rule"] in {
                "strong_text",
                "strong_text_alt",
                "dual_font",
                "temporal_text",
                "temporal_text_alt",
            }
            assert set(traced_slot["channels"]) >= {"icon", "text", "text_alt"}
            for channel_name in ("icon", "text", "text_alt"):
                channel = traced_slot["channels"][channel_name]
                assert "margin" in channel
                assert len(channel.get("top_candidates") or []) <= 3
        assert 1 <= len(history_payload["entries"]) <= overlay_vision_sidecar.VISION_TRACE_HISTORY_LIMIT
        assert all("slot_signature" in entry for entry in history_payload["entries"])
        assert all("button_center_y_ratio" in entry for entry in history_payload["entries"])
        assert all("scene_kind" in entry for entry in history_payload["entries"])
        assert all("body_shard_scores" in entry for entry in history_payload["entries"])
        assert all("cursor_over_cards" in entry for entry in history_payload["entries"])
        assert all("hover_occluded" in entry for entry in history_payload["entries"])

        capped_history_path = Path(tmp_dir) / "capped-history.json"
        capped_history_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": [{"generated_at": index} for index in range(256)],
                }
            ),
            encoding="utf-8",
        )
        overlay_vision_sidecar._append_vision_trace_history(stable, capped_history_path)
        capped_history = json.loads(capped_history_path.read_text(encoding="utf-8"))
        assert len(capped_history["entries"]) == overlay_vision_sidecar.VISION_TRACE_HISTORY_LIMIT
        assert capped_history["entries"][0]["generated_at"] == 1

        rate_trace_path = Path(tmp_dir) / "rate-limited-trace.json"
        rate_history_path = Path(tmp_dir) / "rate-limited-history.json"
        score_only_change = json.loads(json.dumps(stable, ensure_ascii=False))
        score_only_change["_raw_slots"][0]["channels"]["text"]["top_candidates"][0]["confidence"] = 0.75123
        with patch.object(overlay_vision_sidecar.time, "monotonic", side_effect=[100.0, 100.1, 101.2]):
            assert overlay_vision_sidecar.write_vision_trace_if_changed(
                stable, rate_trace_path, history_path=rate_history_path
            ) == rate_trace_path
            assert overlay_vision_sidecar.write_vision_trace_if_changed(
                score_only_change, rate_trace_path, history_path=rate_history_path
            ) is None
            assert overlay_vision_sidecar.write_vision_trace_if_changed(
                score_only_change, rate_trace_path, history_path=rate_history_path
            ) == rate_trace_path
        assert len(json.loads(rate_history_path.read_text(encoding="utf-8"))["entries"]) == 1

        roi_root = Path(tmp_dir) / "roi-debug"
        roi_dir = overlay_vision_sidecar._write_roi_diagnostic_dump(roi_root, frame, stable)
        roi_names = {path.name for path in roi_dir.iterdir()}
        assert "frame.png" not in roi_names
        assert roi_names == {
            "button.png",
            "icon_0.png", "icon_1.png", "icon_2.png",
            "name_0.png", "name_1.png", "name_2.png",
            "report.json",
        }
        assert overlay_vision_sidecar.ROI_DIAGNOSTIC_LIMIT == 32

    module_text = (RUN_DIR / "hextech" / "overlay" / "vision" / "sidecar.py").read_text(encoding="utf-8").lower()
    assert "requests" not in module_text
    assert "opencv" not in module_text
    assert "cv2" not in module_text
    assert "pyautogui" not in module_text
    assert "from processing." not in module_text
    assert "from tools.atomic_io" not in module_text
    assert "parents[3]" in module_text

    assert not (RUN_DIR / "processing").exists()


def check_lol_window_contract() -> None:
    """验证游戏窗口按进程发现，并排除最小化或 DWM cloak 的窗口。"""

    import hextech.overlay.vision.sidecar as overlay_vision_sidecar
    import hextech.overlay.window as lol_window

    class FakeWin32Gui:
        @staticmethod
        def EnumWindows(callback, extra) -> None:
            for hwnd in (101, 202):
                callback(hwnd, extra)

        @staticmethod
        def IsWindowVisible(_hwnd: int) -> bool:
            return True

        @staticmethod
        def IsIconic(hwnd: int) -> bool:
            return hwnd == 202

        @staticmethod
        def GetWindowRect(hwnd: int) -> tuple[int, int, int, int]:
            return (10, 20, 1930, 1100) if hwnd == 101 else (0, 0, 1920, 1080)

        @staticmethod
        def GetClientRect(hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 1920, 1080) if hwnd == 101 else (0, 0, 1920, 1080)

        @staticmethod
        def ClientToScreen(hwnd: int, _point: tuple[int, int]) -> tuple[int, int]:
            return (10, 20) if hwnd == 101 else (0, 0)

        @staticmethod
        def GetWindowText(hwnd: int) -> str:
            return "本地化游戏窗口" if hwnd == 101 else "League of Legends (TM) Client"

    with (
        patch.object(lol_window, "win32gui", FakeWin32Gui),
        patch.object(lol_window, "_window_process_name", side_effect=lambda hwnd: "league of legends.exe" if hwnd == 101 else ""),
        patch.object(lol_window, "is_window_cloaked", return_value=False),
    ):
        assert lol_window.find_lol_game_window() == (101, (10, 20, 1930, 1100))
        assert lol_window.is_window_renderable(101) is True
        assert lol_window.is_window_renderable(202) is False

    with (
        patch.object(lol_window, "win32gui", FakeWin32Gui),
        patch.object(lol_window, "is_window_cloaked", return_value=True),
    ):
        assert lol_window.is_window_renderable(101) is False

    class FakeRootUser32:
        @staticmethod
        def GetAncestor(hwnd: int, _kind: int) -> int:
            return 101 if hwnd in {101, 303} else hwnd

    class FakeForegroundWin32:
        @staticmethod
        def GetForegroundWindow() -> int:
            return 303

    with (
        patch.object(lol_window.ctypes.windll, "user32", FakeRootUser32()),
        patch.object(overlay_vision_sidecar, "win32gui", FakeForegroundWin32),
    ):
        assert lol_window.root_window_hwnd(303) == 101
        assert overlay_vision_sidecar._is_lol_game_foreground(101) is True

    class FakeKeyUser32:
        def __init__(self, state: int) -> None:
            self.state = state

        def GetAsyncKeyState(self, key: int) -> int:
            assert key == lol_window.VK_TAB
            return self.state

    with patch.object(lol_window.ctypes.windll, "user32", FakeKeyUser32(0x8000)):
        assert lol_window.is_scoreboard_key_down() is True
    with patch.object(lol_window.ctypes.windll, "user32", FakeKeyUser32(0)):
        assert lol_window.is_scoreboard_key_down() is False


def check_service_manager_lifecycle_contract() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()
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
        [sys.executable, "-B", "-c", code],
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
        [sys.executable, "-B", "-c", code],
        cwd=str(RUN_DIR),
        text=True,
        encoding="utf-8",
    )
    return set(json.loads(output))


def check_desktop_ui_toggle_rollback_contract() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_game_overlay_host_contract() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()
    # 旧事件（selection_window_active=None）回退到 event_visible+content_ready
def check_desktop_ui_feature_switch_contract() -> None:
    """验证桌面 UI 不再初始化时无条件启动 Web 服务。"""
    import hextech.display.desktop.runtime as ui_runtime
    import hextech.display.desktop.app as desktop_app

    ui_text = (RUN_DIR / "hextech" / "display" / "desktop" / "app.py").read_text(encoding="utf-8")
    runtime_text = (RUN_DIR / "hextech" / "display" / "desktop" / "runtime.py").read_text(encoding="utf-8")
    init_start = ui_text.index("    def __init__(self):")
    init_end = ui_text.index("    def _start_web_server", init_start)
    init_body = ui_text[init_start:init_end]

    assert "ServiceManager" in ui_text
    assert "Web 前端" in ui_text
    assert "游戏内显示" in ui_text
    assert "低频监听" not in ui_text
    assert "tk.Checkbutton" not in ui_text
    assert "feature_status_label" not in ui_text
    assert "Web:" not in ui_text
    assert " / sidecar" not in ui_text
    assert 'root.attributes("-alpha", 1.0' in ui_text
    assert "_build_feature_toggle" in ui_text
    assert "WINDOW_EXPANDED_GEOMETRY = \"320x740\"" in ui_text
    assert "GameOverlayController" in ui_text
    assert "overlay_controller=GameOverlayController(" in ui_text
    assert "start_vision_sidecar_process" not in ui_text
    assert "self._start_web_server()" not in init_body

    root_entry_imports = _top_level_import_names(RUN_DIR / "hextech_ui.py")
    assert not any(name.startswith("display") for name in root_entry_imports)

    assert _probe_clean_import("hextech_ui.py") == set()
    assert _probe_module_import("hextech.overlay.host") == set()

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

    candidate_groups = {
        "selected_champion_ids": ["1", "2", "3", "4", "5"],
        "bench_champion_ids": ["6", "7", "8", "9", "10"],
    }
    candidate_df = pd.DataFrame(
        [
            {"英雄ID": str(index), "英雄名称": f"英雄{index}", "英雄评级": f"T{index}", "英雄胜率": win, "英雄出场率": 0.01}
            for index, win in (
                (1, 0.41),
                (2, 0.55),
                (3, 0.49),
                (4, 0.52),
                (5, 0.47),
                (6, 0.60),
                (7, 0.46),
                (8, 0.58),
                (9, 0.50),
                (10, 0.44),
            )
        ]
    )
    dummy_ui = type("DummyDesktopUI", (), {})()
    dummy_ui._candidate_groups_from_input = desktop_app.HextechUI._candidate_groups_from_input.__get__(dummy_ui)
    display_list = desktop_app.HextechUI._build_candidate_display_list(dummy_ui, candidate_groups, candidate_df)
    assert [item["id"] for item in display_list] == ["2", "4", "3", "5", "1", "6", "8", "9", "7", "10"]


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
    assert all(str(item).replace("\\", "/").startswith("resources/snapshots/hextech/") for item in hextech_files)

    assert "synergy_data_file" in manifest
    assert manifest["synergy_data_file"]
    assert str(manifest["synergy_data_file"]).replace("\\", "/").startswith("resources/snapshots/synergy/")

    assert "synergy_data_files" in manifest
    synergy_files = manifest["synergy_data_files"]
    assert isinstance(synergy_files, list)
    assert all(str(item).replace("\\", "/").startswith("resources/snapshots/synergy/") for item in synergy_files)

    has_latest_pointer = any(Path(item).name == "Champion_Synergy_latest.v1.json" for item in synergy_files)
    has_timestamp_snapshot = any(
        Path(item).name.startswith("Champion_Synergy_")
        and Path(item).name != "Champion_Synergy_latest.v1.json"
        and Path(item).name.endswith(".json")
        for item in synergy_files
    )
    assert has_latest_pointer
    assert has_timestamp_snapshot
    assert "英雄目录.v1.json" in manifest.get("static_files", [])
    assert "海克斯资源目录.v1.json" in manifest.get("static_files", [])
    assert "augment.name-to-icon.v1.json" not in manifest.get("index_files", [])
    source_files = manifest.get("source_files", [])
    for required_source in (
        "hextech/__init__.py",
        "hextech/catalog/runtime_store.py",
        "hextech/core/refresh.py",
        "hextech/core/settings.py",
        "hextech/display/desktop/app.py",
        "hextech/display/web/app.py",
        "hextech/overlay/host.py",
        "hextech/overlay/lifecycle.py",
        "hextech/overlay/vision/sidecar.py",
        "hextech/scraping/hextech/scraper.py",
        "hextech/scraping/synergy/scraper.py",
        "hextech/scraping/transport/scrapling_client.py",
        "hextech/support/atomic_io.py",
        "hextech/support/log_utils.py",
        "hextech_ui.py",
        "web_server.py",
        "tools/acceptance/overlay_performance_probe.py",
        "tools/acceptance/smoke_packaged_startup.py",
        "tools/acceptance/probe_official_overlay_provider.py",
    ):
        assert required_source in source_files
    legacy_source_prefixes = ("crawler/", "display/", "game_overlay/", "processing/", "scraping/")
    assert not any(str(item).startswith(legacy_source_prefixes) for item in source_files)
    serialized_manifest = json.dumps(manifest, ensure_ascii=False)
    assert not any("data/runtime" in str(item) for item in source_files)
    assert not any("data/raw" in str(item) for item in source_files)
    assert "data/raw" not in serialized_manifest.replace("\\", "/")
    assert "data/runtime" not in serialized_manifest.replace("\\", "/")
    assert "overlay_anchor_calibration.v1.json" not in serialized_manifest

    with TemporaryDirectory() as tmp_dir:
        fixture_root = Path(tmp_dir) / "fixture"
        fixture_index = fixture_root / "resources" / "版本数据"
        fixture_static = fixture_root / "hextech" / "display" / "web" / "static"
        fixture_assets = fixture_root / "resources" / "图片资源"
        fixture_index.mkdir(parents=True)
        fixture_static.mkdir(parents=True)
        fixture_assets.mkdir(parents=True)
        (fixture_index / "海克斯资源目录.v1.json").write_text(
            '{"schema_version":1,"entries":[],"name_to_icon":{"尤里卡":"assets/1.png"},"apexlol_slug_map":{}}',
            encoding="utf-8",
        )
        (fixture_index / "英雄目录.v1.json").write_text('{"schema_version":1,"aliases":[]}', encoding="utf-8")
        (fixture_static / "index.html").write_text("<html></html>", encoding="utf-8")
        (fixture_assets / "1.png").write_bytes(b"png")
        manifest_path = Path(tmp_dir) / "bundle_manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        entries = iter_package_data_entries(fixture_root, manifest_path)
        entry_targets = {(entry.source.name, entry.target) for entry in entries}
        assert ("海克斯资源目录.v1.json", "resources/版本数据") in entry_targets
        assert ("英雄目录.v1.json", "resources/版本数据") in entry_targets
        assert ("static", "static") in entry_targets
        assert ("图片资源", "assets") in entry_targets
        assert ("bundle_manifest.json", ".") in entry_targets
        assert not (Path(tmp_dir) / "build" / "_bundle_runtime").exists()
        (fixture_assets / "debug.tmp").write_text("debug", encoding="utf-8")
        try:
            iter_package_data_entries(fixture_root, manifest_path)
        except ValueError as exc:
            assert "debug.tmp" in str(exc)
        else:
            raise AssertionError("assets 目录含非白名单文件时必须阻断打包规则生成")

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
    import tools.acceptance.overlay_performance_probe as overlay_performance_probe

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

    module_text = (RUN_DIR / "tools" / "acceptance" / "overlay_performance_probe.py").read_text(encoding="utf-8").lower()
    assert "requests" not in module_text
    assert "data/runtime" not in module_text


def check_game_overlay_documentation_contract() -> None:
    """验证阶段 3R-5 文档口径与当前实现一致。"""

    readme_text = (RUN_DIR / "README.md").read_text(encoding="utf-8")
    project_text = (RUN_DIR / "PROJECT.md").read_text(encoding="utf-8")
    design_text = (RUN_DIR / "docs" / "hextech_game_overlay_design.md").read_text(encoding="utf-8")
    assert "阶段 3R" in readme_text
    assert "2560x1600" in readme_text
    assert ".venv\\Scripts\\python.exe -m hextech.overlay.vision.sidecar --once --preset auto --write-event" in readme_text
    assert ".venv\\Scripts\\python.exe -m hextech.overlay.vision.sidecar --loop --preset auto --write-event" in readme_text
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
    assert "hextech/overlay/vision/sidecar.py" in project_text
    assert "hextech/overlay/providers/official.py" in project_text
    assert "tools/acceptance/overlay_performance_probe.py" in project_text
    assert "tools/acceptance/probe_official_overlay_provider.py" in project_text
    assert "默认不显示占位框" in project_text
    assert "蓝色按钮场景门控" in project_text
    assert "overlay_anchor_calibration.v1.json" in project_text
    assert "body_shard` 只作为诊断类型不显示" in project_text
    assert "蓝色选择按钮是游戏内显示的主场景门控" in design_text
    assert "官方接口优先验证顺序" in design_text
    assert "python tools/acceptance/probe_official_overlay_provider.py --duration-seconds 120 --interval-ms 500 --dump-runtime-json" in design_text
    assert "data/runtime/state/overlay_anchor_calibration.v1.json" in design_text
    assert "打包后首次启动必须重新校准" in design_text
    assert "不验证真实 Vision 识别" not in project_text


def check_packaged_smoke_uses_explicit_feature_flags() -> None:
    """验证空仓烟测不依赖桌面 UI 默认开关状态。"""

    smoke_text = (RUN_DIR / "tools" / "acceptance" / "smoke_packaged_startup.py").read_text(encoding="utf-8")
    assert '"web_frontend_enabled": True' in smoke_text
    assert '"game_overlay_enabled": False' in smoke_text
    assert '"auto_open_browser": False' in smoke_text
    assert "_write_smoke_feature_flags(runtime_root)" in smoke_text
    assert "OVERLAY_ANCHOR_CALIBRATION_FILENAME" in smoke_text
    assert "package:resources/snapshots/synergy/Champion_Synergy_latest.v1.json" in smoke_text
    assert "FORBIDDEN_PACKAGE_PATHS" in smoke_text
    assert 'child_env["LOCALAPPDATA"]' in smoke_text
    assert "runtime_base:data/{rel} absent" in smoke_text
    for forbidden_rel in (
        "data/raw",
        "data/runtime",
        "data/processed",
        "runtime/cache",
        "runtime/profile",
        "runtime/log",
        "runtime/logs",
        "runtime/debug",
    ):
        assert forbidden_rel in smoke_text
    assert "_internal" in smoke_text
    assert "overlay_anchor_calibration.v1.json" in smoke_text


def check_packaged_smoke_extracts_representative_champion_id_variants() -> None:
    """验证打包烟测代表英雄提取兼容 Web API 的真实字段名。"""

    import tools.acceptance.smoke_packaged_startup as smoke_packaged_startup

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

    import hextech.support.atomic_io as atomic_io

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
    detail_url = source.build_allowed_url("/zh/champions/Vi")
    origin_html = "<html><body>强力联动 作者 评分</body></html>"
    cf_html = "<html><script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>Just a moment</html>"

    def scrapling_result(status_code: int | None, text: str, error: str = "") -> synergy_scraper.ScraplingFetchResult:
        return synergy_scraper.ScraplingFetchResult(
            url=detail_url or source.base_url,
            text=text,
            status_code=status_code,
            fetched_at="2026-06-23T00:00:00+00:00",
            error=error,
        )

    try:
        assert detail_url
        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(200, origin_html)) as fetch_get,
            patch.object(source, "fetch_stealthy", side_effect=AssertionError("origin 页面不应启动 Stealthy")),
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("origin 页面不应启动 CloakBrowser")),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "scrapling-get"
            fetch_get.assert_called_once()

        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(
                source,
                "fetch_stealthy",
                return_value=FetchedResource(url=detail_url, text=origin_html, source="scrapling-stealthy", status_code=200),
            ) as fetch_stealthy,
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("Stealthy 成功后不应启动 CloakBrowser")),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "scrapling-stealthy"
            fetch_stealthy.assert_called_once_with(detail_url)

        blocked_stealthy = FetchedResource(
            url=detail_url,
            text=cf_html,
            source="scrapling-stealthy",
            status_code=403,
            error="cloudflare_block",
        )
        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(source, "fetch_stealthy", return_value=blocked_stealthy),
            patch.object(
                source,
                "fetch_cloakbrowser",
                return_value=FetchedResource(url=detail_url, text=origin_html, source="cloakbrowser", status_code=200),
            ) as fetch_cloakbrowser,
            patch.dict(os.environ, {"APEX_ALLOW_CLOAKBROWSER": "1"}),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "cloakbrowser"
            fetch_cloakbrowser.assert_called_once()

        with (
            patch.object(synergy_scraper, "fetch_text", return_value=scrapling_result(403, cf_html)),
            patch.object(source, "fetch_stealthy", return_value=blocked_stealthy),
            patch.object(source, "fetch_cloakbrowser", side_effect=AssertionError("CloakBrowser 已禁用")),
            patch.dict(os.environ, {"APEX_ALLOW_CLOAKBROWSER": "0"}),
        ):
            fetched = source.fetch(detail_url)
            assert fetched is not None
            assert fetched.source == "scrapling-stealthy"
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
            assert not orchestrator.auto_synergy_refresh_enabled()
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

    with TemporaryDirectory() as temp_dir:
        status_path = Path(temp_dir) / "synergy_refresh_status.json"
        started_at = time.time()
        with patch.object(orchestrator, "build_synergy_refresh_status_path", return_value=str(status_path)):
            orchestrator._write_synergy_refresh_status("blocked", "cloudflare_block")
        status = json.loads(status_path.read_text(encoding="utf-8"))
        blocked_until = datetime.fromisoformat(status["blocked_until"])
        assert 5.9 * 60 * 60 <= blocked_until.timestamp() - started_at <= 6.1 * 60 * 60


def check_synergy_snapshot_store() -> None:
    with TemporaryDirectory() as temp_dir:
        snapshot = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
        _write_json(
            Path(temp_dir) / "Champion_Synergy_latest.v1.json",
            {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
            1001,
        )
        cleaned_missing = Path(temp_dir) / "Champion_Synergy_Cleaned.json"

        with (
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned_missing)),
        ):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(snapshot.resolve())
            assert runtime_store.build_synergy_data_path() == str(snapshot.resolve())

    with TemporaryDirectory() as temp_dir:
        snapshot = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"1": {}}, 1000)
        cleaned = _write_json(Path(temp_dir) / "Champion_Synergy_Cleaned.json", {"cleaned": {}}, 1002)
        _write_json(
            Path(temp_dir) / "Champion_Synergy_latest.v1.json",
            {"version": 1, "filename": snapshot.name, "non_empty_heroes": 1},
            1001,
        )

        with (
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned)),
        ):
            assert runtime_store.build_synergy_data_path() == str(cleaned)

    with TemporaryDirectory() as temp_dir:
        older = _write_json(Path(temp_dir) / "Champion_Synergy_20260518_010101.json", {"1": {}}, 1000)
        newer = _write_json(Path(temp_dir) / "Champion_Synergy_20260519_223505.json", {"2": {}}, 2000)
        (Path(temp_dir) / "Champion_Synergy_latest.v1.json").write_text("{bad", encoding="utf-8")
        cleaned_missing = Path(temp_dir) / "Champion_Synergy_Cleaned.json"

        with (
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned_missing)),
        ):
            assert runtime_store.get_latest_synergy_snapshot_path() == str(newer)
            assert runtime_store.get_latest_synergy_snapshot_path() != str(older)

    with TemporaryDirectory() as temp_dir:
        legacy = _write_json(Path(temp_dir) / "Champion_Synergy.json", {"1": {}}, 1000)
        cleaned_missing = Path(temp_dir) / "Champion_Synergy_Cleaned.json"

        with (
            patch.object(runtime_store, "get_runtime_synergy_data_dir", return_value=Path(temp_dir)),
            patch.object(runtime_store, "build_synergy_cleaned_data_path", return_value=str(cleaned_missing)),
        ):
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


def check_mayhem_combo_pipeline_contract() -> None:
    from hextech.scraping.synergy.mayhem_combo_scraper import parse_combo_manifest
    from tools.clean_mayhem_combos import merge_mayhem_combos

    manifest_items, manifest_rejects, manifest_meta = parse_combo_manifest(
        {
            "pageSize": 1,
            "totalCombos": 2,
            "cards": [
                {
                    "id": 1,
                    "championId": "Vayne",
                    "champName": "暗夜猎手",
                    "augmentId": "fan_the_hammer",
                    "augmentName": "连拨击锤",
                    "tier": "S+",
                    "typeBadges": [{"label": "神级"}],
                    "comboDescription": "每一发弩箭都可以触发 W 和攻击特效。",
                    "comboHref": "/zh-cn/combo/vayne-fan-the-hammer/",
                },
                {
                    "id": 2,
                    "championId": "Brand",
                    "champName": "复仇焰魂",
                    "augmentId": "infernal_conduit",
                    "augmentName": "炼狱导管",
                    "tier": "S",
                    "comboDescription": "技能灼烧不断缩减冷却。",
                    "comboHref": "/zh-cn/combo/brand-infernal-conduit/",
                },
            ],
        },
        "https://arammayhem.com/zh-cn/combo/",
        max_pages=1,
    )
    assert len(manifest_items) == 1
    assert not manifest_rejects
    assert manifest_meta["selected"] == 1
    assert manifest_items[0]["source_url"].endswith("/zh-cn/combo/vayne-fan-the-hammer/")

    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        apex_path = root / "Champion_Synergy_20260519_223505.json"
        raw_path = root / "mayhem_combos.raw.json"
        augment_manifest_path = root / "Augment_Icon_Manifest.json"
        core_path = root / "Champion_Core_Data.json"
        output_path = root / "Champion_Synergy_Cleaned.json"

        _write_json(
            core_path,
            {
                "67": {"name": "暗夜猎手", "title": "薇恩", "en_name": "Vayne", "aliases": []},
                "63": {"name": "复仇焰魂", "title": "布兰德", "en_name": "Brand", "aliases": []},
            },
        )
        _write_json(
            augment_manifest_path,
            [
                {
                    "name": "连拨击锤",
                    "tier": "棱彩",
                    "filename": "fanthehammer_small.png",
                    "augment_name_id": "FanTheHammer",
                    "source_icon_path": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/FanTheHammer_small.png",
                },
                {
                    "name": "炼狱导管",
                    "tier": "棱彩",
                    "filename": "infernalconduit_small.png",
                    "augment_name_id": "InfernalConduit",
                    "source_icon_path": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/InfernalConduit_small.png",
                },
            ],
        )
        _write_json(
            apex_path,
            {
                "67": {
                    "id": "67",
                    "name": "暗夜猎手",
                    "title": "薇恩",
                    "en_name": "Vayne",
                    "aliases": [],
                    "synergies": [],
                    "synergy_items": [
                        {
                            "augment_names": ["连拨击锤"],
                            "tier": "棱彩",
                            "rating": "S",
                            "tag": "强力联动",
                            "author": "ApexLoL",
                            "content": "Apex 已有同组合。",
                        }
                    ],
                },
                "63": {
                    "id": "63",
                    "name": "复仇焰魂",
                    "title": "布兰德",
                    "en_name": "Brand",
                    "aliases": [],
                    "synergies": [],
                    "synergy_items": [],
                },
            },
        )
        _write_json(
            raw_path,
            {
                "schema_version": 1,
                "items": [
                    {
                        "champion": "暗夜猎手",
                        "champion_id": "Vayne",
                        "augment_names": ["Fan The Hammer"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S+",
                        "body": "重复组合不应加入。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/vayne-fan-the-hammer/",
                    },
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S+",
                        "body": "技能灼烧不断缩减冷却。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-infernal-conduit/",
                    },
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "S",
                        "body": "Retired in live Mayhem。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-retired/",
                    },
                    {
                        "champion": "复仇焰魂",
                        "champion_id": "Brand",
                        "augment_names": ["Infernal Conduit"],
                        "mayhem_tier": "神级",
                        "mayhem_rating": "A",
                        "body": "依赖旧 Trait / Augment Sets 的组合。",
                        "source_url": "https://arammayhem.com/zh-cn/combo/brand-trait/",
                    },
                ],
                "rejects": [],
            },
        )

        summary = merge_mayhem_combos(
            apex_path=apex_path,
            mayhem_raw_path=raw_path,
            augment_manifest_path=augment_manifest_path,
            core_data_path=core_path,
            output_path=output_path,
        )

        assert summary["mayhem_raw_items"] == 4
        assert summary["mayhem_valid_items"] == 2
        assert summary["added_items"] == 1
        assert summary["skipped_duplicate_items"] == 1
        assert summary["clean_reject_items"] == 2
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        brand_items = payload["63"]["synergy_items"]
        assert brand_items[0]["augment_names"] == ["炼狱导管"]
        assert brand_items[0]["source"] == "arammayhem"
        assert brand_items[0]["source_rating"] == "S+"

        sentinel = {"sentinel": True}
        output_path.write_text(json.dumps(sentinel, ensure_ascii=False), encoding="utf-8")
        _write_json(raw_path, {"schema_version": 1, "items": [], "rejects": [{"reason": "empty"}]})
        empty_summary = merge_mayhem_combos(
            apex_path=apex_path,
            mayhem_raw_path=raw_path,
            augment_manifest_path=augment_manifest_path,
            core_data_path=core_path,
            output_path=output_path,
        )
        assert empty_summary["written"] is False
        assert json.loads(output_path.read_text(encoding="utf-8")) == sentinel


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


def check_overlay_diagnostic_translation_table() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_format_private_stats_three_states() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_extract_event_status_legacy() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_render_rows_low_confidence_and_top_candidates() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_render_rows_excludes_source_heroes_from_tags() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_visibility_decision_table() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_stable_snapshot_separates_live_status() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_layout_three_viewports() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_overlay_draw_perf_smoke() -> None:
    """委托统一 overlay 模块契约检查。"""
    check_game_overlay_module_contract()

def check_game_overlay_module_contract() -> None:
    """验证独立模块边界、Controller 原子性、原生布局和隐藏态最小轮询。"""

    import queue

    import hextech.overlay.host as overlay_host
    import hextech.overlay.lifecycle as overlay_lifecycle
    import hextech.overlay.renderer as overlay_renderer
    from hextech.display.desktop.service_manager import ServiceManager
    from hextech.overlay.lifecycle import GameOverlayController
    from hextech.overlay.vision.layout import pick_card_panels
    from tools import overlay_render_snapshot

    # 导入 overlay 主实现不得隐式加载 Web 产品模块。
    probe = """
import importlib, json, sys
importlib.import_module('hextech.overlay')
importlib.import_module('hextech.overlay.host')
blocked = [name for name in sys.modules if name == 'display' or name.startswith('display.') or name == 'fastapi' or name.startswith('fastapi.') or name == 'uvicorn' or name.startswith('uvicorn.') or name == 'webbrowser']
print(json.dumps(blocked))
"""
    output = subprocess.check_output([sys.executable, "-B", "-c", probe], cwd=str(RUN_DIR), text=True, encoding="utf-8")
    assert json.loads(output) == []
    implementation_dir = RUN_DIR / "hextech" / "overlay"
    overlay_filenames = {"__init__.py", "__main__.py", "lifecycle.py", "host.py", "data_source.py", "renderer.py"}
    assert overlay_filenames <= {path.name for path in implementation_dir.iterdir() if path.is_file()}
    for path in implementation_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import display" not in text and "from display" not in text, path
        assert "fastapi" not in text.lower() and "uvicorn" not in text.lower(), path
    host_text = (implementation_dir / "host.py").read_text(encoding="utf-8")
    main_text = (implementation_dir / "__main__.py").read_text(encoding="utf-8")
    lifecycle_text = (implementation_dir / "lifecycle.py").read_text(encoding="utf-8")
    assert "prepare_shared_overlay_data" in host_text
    assert "_prepare_host_hint_cache()" in host_text.split("def run_overlay_host", 1)[1]
    assert 'row["status_code"] == "READY"' in host_text
    assert 'row["status_code"] == "READY"' in main_text
    assert "start_overlay_context_poller" in lifecycle_text

    with (
        patch.dict(overlay_lifecycle.os.environ, {overlay_lifecycle.OVERLAY_SIDECAR_DEBUG_DUMP_ENV: ""}),
        patch.object(overlay_lifecycle.sys, "frozen", True, create=True),
        patch.object(overlay_lifecycle, "_hidden_startupinfo", return_value=None),
        patch.object(overlay_lifecycle.subprocess, "Popen", return_value=object()) as frozen_popen,
    ):
        overlay_lifecycle.start_sidecar_process()
    frozen_command = frozen_popen.call_args.args[0]
    assert frozen_command[:2] == [sys.executable, "--overlay-sidecar"]
    assert "processing.overlay_vision_sidecar" not in frozen_command
    assert "--debug-dump" not in frozen_command

    with (
        patch.dict(overlay_lifecycle.os.environ, {overlay_lifecycle.OVERLAY_SIDECAR_DEBUG_DUMP_ENV: ""}),
        patch.object(overlay_lifecycle.sys, "frozen", False, create=True),
        patch.object(overlay_lifecycle, "_hidden_startupinfo", return_value=None),
        patch.object(overlay_lifecycle.subprocess, "Popen", return_value=object()) as source_popen,
    ):
        overlay_lifecycle.start_sidecar_process()
    source_command = source_popen.call_args.args[0]
    assert source_command[:3] == [sys.executable, "-m", "hextech.overlay.vision.sidecar"]
    assert "processing.overlay_vision_sidecar" not in source_command
    assert "--debug-dump" not in source_command

    with (
        patch.dict(overlay_lifecycle.os.environ, {overlay_lifecycle.OVERLAY_SIDECAR_DEBUG_DUMP_ENV: "1"}),
        patch.object(overlay_lifecycle.sys, "frozen", False, create=True),
        patch.object(overlay_lifecycle, "_hidden_startupinfo", return_value=None),
        patch.object(overlay_lifecycle.subprocess, "Popen", return_value=object()) as debug_popen,
    ):
        overlay_lifecycle.start_sidecar_process()
    debug_command = debug_popen.call_args.args[0]
    assert "--debug-dump" in debug_command
    assert Path(debug_command[debug_command.index("--debug-dump") + 1]).name == "overlay_vision"

    class DummyProcess:
        def __init__(self, pid: int, *, running: bool = True, calls: list[str] | None = None, label: str = ""):
            self.pid = pid
            self.running = running
            self.calls = calls if calls is not None else []
            self.label = label
            self.killed = False

        def poll(self):
            return None if self.running else 1

        def terminate(self) -> None:
            self.calls.append(f"stop:{self.label}")
            self.running = False

        def wait(self, timeout=None):
            if self.running:
                raise TimeoutError("still running")
            return 0

        def kill(self) -> None:
            self.killed = True
            self.running = False

    class StubbornProcess(DummyProcess):
        def terminate(self) -> None:
            self.calls.append(f"stop:{self.label}")

        def kill(self) -> None:
            self.calls.append(f"kill:{self.label}")
            self.killed = True

    # 空 Controller 没有事件所有权；重复停止不得覆盖其它实例的 active 事件。
    empty_stop_calls: list[str] = []
    empty_controller = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: empty_stop_calls.append("inactive"),
        start_sidecar_func=lambda: DummyProcess(90),
        start_host_func=lambda: DummyProcess(91),
    )
    empty_controller.stop()
    empty_controller.stop()
    assert empty_stop_calls == []
    assert empty_controller.snapshot()["status"] == "stopped"

    shared_event_calls: list[str] = []
    primary_controller = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: shared_event_calls.append("inactive"),
        start_sidecar_func=lambda: DummyProcess(92),
        start_host_func=lambda: DummyProcess(93),
    )
    secondary_controller = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: shared_event_calls.append("secondary-inactive"),
        start_sidecar_func=lambda: DummyProcess(94),
        start_host_func=lambda: DummyProcess(95),
    )
    primary_controller.start()
    event_count_before_secondary_stop = len(shared_event_calls)
    secondary_controller.stop()
    assert len(shared_event_calls) == event_count_before_secondary_stop
    assert primary_controller.is_running() is True
    primary_controller.stop()

    stale_process_calls: list[str] = []
    stale_controller = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: stale_process_calls.append("inactive"),
    )
    stale_controller.sidecar_process = DummyProcess(96, running=False)
    stale_controller.stop()
    assert stale_process_calls == ["inactive", "inactive"]
    assert stale_controller.sidecar_process is None

    with TemporaryDirectory() as tmp_dir:
        exit_signal = Path(tmp_dir) / "host.exit.json"

        class VoluntaryHostProcess(DummyProcess):
            def __init__(self) -> None:
                super().__init__(301, calls=[], label="host")
                self._hextech_overlay_exit_file = str(exit_signal)

            def wait(self, timeout=None):
                if exit_signal.exists():
                    self.running = False
                    return 0
                return super().wait(timeout=timeout)

        voluntary_host = VoluntaryHostProcess()
        assert overlay_lifecycle.stop_process(voluntary_host) is True
        assert voluntary_host.calls == []
        assert not exit_signal.exists()

    # 成功启停：停止前先隐藏，sidecar 退出后再写 inactive 作为最终 fence。
    calls: list[str] = []
    host_process = DummyProcess(101, calls=calls, label="host")
    sidecar_process = DummyProcess(102, calls=calls, label="sidecar")
    controller = GameOverlayController(
        prepare_data_func=lambda: calls.append("prepare"),
        write_inactive_func=lambda: calls.append("inactive"),
        start_sidecar_func=lambda: calls.append("start:sidecar") or sidecar_process,
        start_host_func=lambda: calls.append("start:host") or host_process,
    )
    controller.start()
    assert calls == ["prepare", "inactive", "start:sidecar", "start:host"]
    assert controller.snapshot()["status"] == "running"
    assert controller.snapshot()["host_pid"] == 101
    assert controller.snapshot()["sidecar_pid"] == 102
    assert "pid" not in controller.snapshot()
    controller.stop()
    assert calls[-4:] == ["inactive", "stop:sidecar", "inactive", "stop:host"]
    assert controller.snapshot()["status"] == "stopped"
    assert controller.host_process is None and controller.sidecar_process is None

    poller_calls: list[str] = []

    class FakeContextPoller:
        def stop(self) -> None:
            poller_calls.append("stop:context")

    polling_controller = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: None,
        start_sidecar_func=lambda: DummyProcess(103),
        start_host_func=lambda: DummyProcess(104),
        start_context_poller_func=lambda: poller_calls.append("start:context") or FakeContextPoller(),
    )
    polling_controller.start()
    assert polling_controller.snapshot()["context_poller_status"] == "running"
    polling_controller.stop()
    assert poller_calls == ["start:context", "stop:context"]
    assert polling_controller.snapshot()["context_poller_status"] == "stopped"

    unexpected_exit_calls: list[str] = []

    class UnexpectedExitPoller:
        def stop(self) -> None:
            unexpected_exit_calls.append("stop:context")

    unexpected_exit = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: None,
        start_sidecar_func=lambda: DummyProcess(105),
        start_host_func=lambda: DummyProcess(106),
        start_context_poller_func=lambda: unexpected_exit_calls.append("start:context") or UnexpectedExitPoller(),
    )
    unexpected_exit.start()
    assert unexpected_exit.host_process is not None
    unexpected_exit.host_process.running = False
    unexpected_snapshot = unexpected_exit.snapshot()
    assert unexpected_snapshot["status"] == "error"
    assert unexpected_snapshot["context_poller_status"] == "stopped"
    assert unexpected_exit_calls == ["start:context", "stop:context"]

    partial_restart_calls: list[str] = []
    healthy_host = DummyProcess(121, calls=partial_restart_calls, label="host")
    dead_sidecar = DummyProcess(122, running=False, calls=partial_restart_calls, label="dead-sidecar")
    new_sidecar = DummyProcess(123, calls=partial_restart_calls, label="sidecar")
    partial_restart = GameOverlayController(
        prepare_data_func=lambda: partial_restart_calls.append("prepare"),
        write_inactive_func=lambda: partial_restart_calls.append("inactive"),
        start_sidecar_func=lambda: partial_restart_calls.append("start:sidecar") or new_sidecar,
        start_host_func=lambda: partial_restart_calls.append("start:host") or DummyProcess(124),
    )
    partial_restart.host_process = healthy_host
    partial_restart.sidecar_process = dead_sidecar
    assert partial_restart.is_running() is False
    partial_restart.start()
    assert partial_restart.host_process is healthy_host
    assert partial_restart.sidecar_process is new_sidecar
    assert partial_restart_calls == ["prepare", "start:sidecar"]
    assert healthy_host.running is True
    assert partial_restart.snapshot()["status"] == "running"

    stale_cleanup_calls: list[str] = []
    stale_cleanup = GameOverlayController(
        prepare_data_func=lambda: stale_cleanup_calls.append("prepare"),
        write_inactive_func=lambda: stale_cleanup_calls.append("inactive"),
        start_sidecar_func=lambda: stale_cleanup_calls.append("start:sidecar") or DummyProcess(111),
        start_host_func=lambda: stale_cleanup_calls.append("start:host") or DummyProcess(112),
    )
    stale_cleanup.host_process = StubbornProcess(113, calls=stale_cleanup_calls, label="host")
    stale_cleanup.start()
    assert stale_cleanup.snapshot()["status"] == "running"
    assert stale_cleanup.host_process.pid == 113
    assert stale_cleanup.sidecar_process.pid == 111
    assert stale_cleanup_calls == ["prepare", "start:sidecar"]

    residual_stop_calls: list[str] = []
    residual_stop = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: residual_stop_calls.append("inactive"),
    )
    residual_stop.host_process = StubbornProcess(114, calls=residual_stop_calls, label="host")
    try:
        residual_stop.stop()
    except RuntimeError as exc:
        assert "host 停止失败(pid=114)" in str(exc)
    else:
        raise AssertionError("stop_process 失败必须向调用方报错")
    assert residual_stop.host_process is not None and residual_stop.host_process.pid == 114
    assert residual_stop.sidecar_process is None
    assert residual_stop.snapshot()["residual_pids"] == {"host": 114}
    assert residual_stop_calls[:2] == ["inactive", "inactive"]

    # host 失败必须回滚已启动 sidecar；sidecar 失败不得继续启动 host。
    rollback_calls: list[str] = []
    rollback_event_calls: list[str] = []
    orphan_sidecar = DummyProcess(201, calls=rollback_calls, label="sidecar")
    rollback = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: rollback_event_calls.append("inactive"),
        start_sidecar_func=lambda: orphan_sidecar,
        start_host_func=lambda: (_ for _ in ()).throw(RuntimeError("host failed")),
    )
    try:
        rollback.start()
    except RuntimeError as exc:
        assert "host failed" in str(exc)
    else:
        raise AssertionError("host 启动失败必须向调用方报错")
    assert not orphan_sidecar.running
    assert rollback_event_calls == ["inactive", "inactive"]
    assert rollback.snapshot()["status"] == "error"
    assert rollback.host_process is None and rollback.sidecar_process is None

    stubborn_rollback_calls: list[str] = []
    stubborn_orphan_sidecar = StubbornProcess(204, calls=stubborn_rollback_calls, label="sidecar")
    stubborn_rollback = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: None,
        start_sidecar_func=lambda: stubborn_orphan_sidecar,
        start_host_func=lambda: (_ for _ in ()).throw(RuntimeError("host failed with stubborn sidecar")),
    )
    try:
        stubborn_rollback.start()
    except RuntimeError as exc:
        assert "回滚后仍有残留" in str(exc)
        assert "sidecar(pid=204)" in str(exc)
    else:
        raise AssertionError("回滚残留必须向调用方报错")
    assert stubborn_rollback.sidecar_process is stubborn_orphan_sidecar
    assert stubborn_rollback.snapshot()["residual_pids"] == {"sidecar": 204}

    sidecar_failed_host_calls: list[str] = []
    sidecar_failure = GameOverlayController(
        prepare_data_func=lambda: None,
        write_inactive_func=lambda: None,
        start_sidecar_func=lambda: DummyProcess(202, running=False),
        start_host_func=lambda: sidecar_failed_host_calls.append("host") or DummyProcess(203),
    )
    try:
        sidecar_failure.start()
    except RuntimeError as exc:
        assert "sidecar" in str(exc)
    else:
        raise AssertionError("已退出 sidecar 不得被标记为 running")
    assert sidecar_failed_host_calls == []

    # Web / game overlay 四状态矩阵，两个入口互不启动对方。
    matrix_calls: list[str] = []
    matrix_controller = GameOverlayController(
        prepare_data_func=lambda: matrix_calls.append("prepare"),
        write_inactive_func=lambda: matrix_calls.append("inactive"),
        start_sidecar_func=lambda: matrix_calls.append("sidecar") or DummyProcess(302),
        start_host_func=lambda: matrix_calls.append("host") or DummyProcess(303),
    )
    manager = ServiceManager(
        start_web_func=lambda: matrix_calls.append("web") or DummyProcess(301),
        overlay_controller=matrix_controller,
    )
    assert not manager.is_web_running() and not manager.is_game_overlay_running()
    manager.start_web()
    assert manager.is_web_running() and not manager.is_game_overlay_running()
    assert matrix_calls == ["web"]
    manager.start_game_overlay()
    assert manager.is_web_running() and manager.is_game_overlay_running()
    manager.stop_web()
    assert not manager.is_web_running() and manager.is_game_overlay_running()
    manager.stop_game_overlay()
    assert not manager.is_web_running() and not manager.is_game_overlay_running()

    class ReleaseAfterWaitProcess(DummyProcess):
        def __init__(self, pid: int):
            super().__init__(pid)
            self.wait_entered = threading.Event()
            self.release = threading.Event()

        def terminate(self) -> None:
            self.calls.append("stop:web")

        def wait(self, timeout=None):
            self.wait_entered.set()
            if self.release.wait(timeout=1):
                self.running = False
                return 0
            raise TimeoutError("still running")

        def poll(self):
            return None if self.running else 0

    joined_process = ReleaseAfterWaitProcess(304)
    joined_manager = ServiceManager(
        start_web_func=lambda: joined_process,
        overlay_controller=GameOverlayController(prepare_data_func=lambda: None, write_inactive_func=lambda: None),
    )
    joined_manager.start_web()

    def release_shutdown_wait() -> None:
        assert joined_process.wait_entered.wait(timeout=1)
        joined_process.release.set()

    release_thread = threading.Thread(target=release_shutdown_wait, daemon=True)
    release_thread.start()
    try:
        joined_manager.shutdown(timeout_seconds=0.01)
        assert joined_process.release.is_set(), "默认退出路径应等待后台收尾线程完成"
        assert joined_manager._shutdown_thread is not None
        assert not joined_manager._shutdown_thread.is_alive()
        joined_snapshot = joined_manager.get_status_snapshot()["web"]
        assert joined_snapshot["status"] == "stopped"
    finally:
        joined_process.release.set()
        release_thread.join(timeout=1)

    def hint_cache(*, private: bool = True, stats: bool = True, synergy_count: int = 3) -> dict[str, Any]:
        hints: dict[str, Any] = {}
        for index in range(3):
            hint: dict[str, Any] = {
                "augment_id": f"a{index}",
                "name": f"强化 {index + 1}",
                "tier": ("Prismatic", "Gold", "Silver")[index],
                # 顶层 stats 只保留兼容；正式 overlay 必须优先读取当前英雄维度。
                "winrate": 0.11 + index * 0.01,
                "pickrate": 0.01 + index * 0.01,
                "synergies": [],
            }
            if stats:
                hint.update(
                    stats_by_champion_id={
                        "266": {"winrate": 0.55 + index * 0.01, "pickrate": 0.03 + index * 0.01},
                        "245": {"winrate": 0.61 + index * 0.01, "pickrate": 0.04 + index * 0.01},
                    },
                    stats_by_champion_name={
                        "暗裔剑魔": {"winrate": 0.55 + index * 0.01, "pickrate": 0.03 + index * 0.01},
                        "时间刺客": {"winrate": 0.61 + index * 0.01, "pickrate": 0.04 + index * 0.01},
                    },
                )
            if index < synergy_count:
                hint["synergies"] = [{
                    "hero_id": "266",
                    "hero_name": "暗裔剑魔",
                    "rating": "S",
                    "tag": "联动",
                    "content": f"联动 {index + 1}",
                }]
            hints[f"a{index}"] = hint
        return {
            "schema_version": 1,
            "generated_at": time.time(),
            "source": {"private_policy_stats_enabled": private},
            "hints": hints,
            "name_index": {},
        }

    slots = [
        {"slot": index, "state": "ready", "augment_id": f"a{index}", "name": f"强化 {index + 1}", "tier": tier}
        for index, tier in enumerate(("Prismatic", "Gold", "Silver"))
    ]
    snapshot = {
        "ok": True,
        "visible": True,
        "source": {"selection_window_active": True},
        "slots": slots,
    }
    context = {"ok": True, "champion_id": "266", "champion_name": "暗裔剑魔"}

    ready_model = overlay_renderer.build_render_model(snapshot, hint_cache=hint_cache(), context=context)
    assert len(ready_model["stats"]) == 3
    assert ready_model["stats"][0]["stats_text"] == "胜率 55.0% · 出场 3.0%"
    assert ready_model["stats"][0]["status_code"] == "READY"
    assert ready_model["stats"][0]["winrate_text"] == "55.0%"
    assert ready_model["stats"][0]["pickrate_text"] == "3.0%"
    assert ready_model["stats"][0]["status_text"] == ""
    ekko_model = overlay_renderer.build_render_model(
        snapshot,
        hint_cache=hint_cache(),
        context={"ok": True, "champion_id": "245", "champion_name": "时间刺客"},
    )
    assert ekko_model["stats"][0]["stats_text"] == "胜率 61.0% · 出场 4.0%"
    missing_champion_model = overlay_renderer.build_render_model(
        snapshot,
        hint_cache=hint_cache(),
        context={"ok": True, "champion_id": "999", "champion_name": "不存在"},
    )
    assert {row["stats_text"] for row in missing_champion_model["stats"]} == {"暂无该英雄统计"}
    assert {row["status_code"] for row in missing_champion_model["stats"]} == {"NO_STATS"}
    assert {row["status_text"] for row in missing_champion_model["stats"]} == {"暂无统计"}
    assert [row["slot"] for row in ready_model["synergies"]] == [0, 1, 2]
    ranked_cache = hint_cache()
    ranked_cache["hints"]["a0"]["synergies"] = [
        {"hero_id": "266", "hero_name": "暗裔剑魔", "rating": "B", "tag": "弱联动", "content": "低优先级"},
        {"hero_id": "266", "hero_name": "暗裔剑魔", "rating": "S", "tag": "强联动", "content": "高优先级"},
        {"hero_id": "245", "hero_name": "时间刺客", "rating": "SS", "tag": "其它英雄", "content": "不应命中"},
    ]
    ranked_model = overlay_renderer.build_render_model(snapshot, hint_cache=ranked_cache, context=context)
    assert ranked_model["synergies"][0]["rating"] == "S"
    assert ranked_model["synergies"][0]["content"] == "高优先级"
    stable_cache = hint_cache()
    stable_cache["hints"]["a0"]["synergies"] = [
        {"hero_id": "266", "hero_name": "暗裔剑魔", "rating": "A", "tag": "先出现", "content": "第一条"},
        {"hero_id": "266", "hero_name": "暗裔剑魔", "rating": "A", "tag": "后出现", "content": "第二条"},
    ]
    stable_model = overlay_renderer.build_render_model(snapshot, hint_cache=stable_cache, context=context)
    assert stable_model["synergies"][0]["content"] == "第一条"
    privacy_model = overlay_renderer.build_render_model(snapshot, hint_cache=hint_cache(private=False), context=context)
    assert {row["stats_text"] for row in privacy_model["stats"]} == {"已开启隐私模式"}
    assert {row["status_code"] for row in privacy_model["stats"]} == {"PRIVACY_OFF"}
    assert {row["status_text"] for row in privacy_model["stats"]} == {"统计关闭"}
    context_missing_model = overlay_renderer.build_render_model(
        snapshot,
        hint_cache=hint_cache(),
        context={"ok": False, "error": "context_missing"},
    )
    assert {row["status_code"] for row in context_missing_model["stats"]} == {"CONTEXT_MISSING"}
    assert {row["status_text"] for row in context_missing_model["stats"]} == {"等待英雄"}
    context_expired_model = overlay_renderer.build_render_model(
        snapshot,
        hint_cache=hint_cache(),
        context={"ok": False, "error": "context_expired"},
    )
    assert {row["status_code"] for row in context_expired_model["stats"]} == {"CONTEXT_EXPIRED"}
    assert {row["status_text"] for row in context_expired_model["stats"]} == {"等待英雄"}
    missing_model = overlay_renderer.build_render_model(snapshot, hint_cache=hint_cache(stats=False), context=context)
    assert {row["stats_text"] for row in missing_model["stats"]} == {"暂无该英雄统计"}
    assert {row["status_code"] for row in missing_model["stats"]} == {"NO_STATS"}
    partial_stats_cache = hint_cache()
    partial_stats_cache["hints"]["a0"]["stats_by_champion_id"]["266"].pop("pickrate")
    partial_stats_model = overlay_renderer.build_render_model(
        snapshot,
        hint_cache=partial_stats_cache,
        context=context,
    )
    assert partial_stats_model["stats"][0]["stats_text"] == "胜率 55.0%"
    assert partial_stats_model["stats"][0]["status_code"] == "NO_STATS"
    assert partial_stats_model["stats"][0]["status_text"] == "暂无统计"
    partial_snapshot = dict(snapshot)
    partial_snapshot["slots"] = [slots[0], {"slot": 1, "state": "detecting"}]
    partial_model = overlay_renderer.build_render_model(partial_snapshot, hint_cache=hint_cache(), context=context)
    assert [row["stats_text"] for row in partial_model["stats"]] == [
        "胜率 55.0% · 出场 3.0%", "识别中…", "识别中…"
    ]
    assert [row["status_code"] for row in partial_model["stats"]] == ["READY", "DETECTING", "DETECTING"]
    assert [row["status_text"] for row in partial_model["stats"]] == ["", "识别中…", "识别中…"]
    for count in range(4):
        model = overlay_renderer.build_render_model(snapshot, hint_cache=hint_cache(synergy_count=count), context=context)
        assert len(model["synergies"]) == count
        assert [row["slot"] for row in model["synergies"]] == list(range(count))

    # 三档常见 viewport + 1920×1200：统计条完全内嵌原生卡片且互不重叠；联动组整体居中且按槽位排序。
    for viewport in ((1366, 768), (1920, 1080), (1920, 1200), (2560, 1600)):
        width, height = viewport
        assert overlay_renderer._card_panel_ratios(viewport) == pick_card_panels(viewport)
        for count in range(4):
            layout = overlay_renderer.resolve_overlay_layout(viewport, synergy_count=count)
            assert len(layout["stat_boxes"]) == 3
            for stat_box, card_box in zip(layout["stat_boxes"], layout["card_boxes"]):
                assert card_box[0] < stat_box[0] < stat_box[2] < card_box[2]
                assert card_box[1] < stat_box[1] < stat_box[3] < card_box[3]
                assert 46 <= stat_box[3] - stat_box[1] <= 72
                assert 10 <= stat_box[0] - card_box[0] <= 20
                assert 10 <= card_box[2] - stat_box[2] <= 20
                assert 0.81 * (card_box[3] - card_box[1]) <= stat_box[1] - card_box[1] <= 0.85 * (card_box[3] - card_box[1])
                assert 0 <= stat_box[0] < stat_box[2] <= width
                assert 0 <= stat_box[1] < stat_box[3] <= height
            for left, right in zip(layout["stat_boxes"], layout["stat_boxes"][1:]):
                assert left[2] < right[0]
            boxes = layout["synergy_boxes"]
            assert len(boxes) == count
            assert all(first[3] < second[1] for first, second in zip(boxes, boxes[1:]))
            if boxes:
                rail = layout["synergy_rail"]
                group_center = (boxes[0][1] + boxes[-1][3]) / 2
                rail_center = (rail[1] + rail[3]) / 2
                assert abs(group_center - rail_center) <= 1.0

    class RecordingCanvas:
        def __init__(self) -> None:
            self.text_calls: list[dict[str, Any]] = []

        def delete(self, *_args, **_kwargs):
            return None

        def create_polygon(self, *_args, **_kwargs):
            return None

        def create_rectangle(self, *_args, **_kwargs):
            return None

        def create_line(self, *_args, **_kwargs):
            return None

        def create_text(self, *args, **kwargs):
            self.text_calls.append({"args": args, **kwargs})
            return None

    ready_canvas = RecordingCanvas()
    overlay_renderer._draw_stat_panel(ready_canvas, (100, 100, 340, 160), ready_model["stats"][0])
    assert any(
        call.get("text") == "胜率 55.0% · 出场 3.0%" and call.get("anchor") == "center"
        for call in ready_canvas.text_calls
    )

    # 极值百分位也必须作为整体贴近统计框中轴线；固定左右列会被长短数字拖偏。
    stat_box = (100, 100, 546, 172)
    stat_center_x = (stat_box[0] + stat_box[2]) / 2
    for winrate_text, pickrate_text in (
        ("57.7%", "3.9%"),
        ("100.0%", "0.1%"),
        ("49.9%", "12.3%"),
        ("9.8%", "18.8%"),
    ):
        row = dict(
            ready_model["stats"][0],
            stats_text=f"胜率 {winrate_text} · 出场 {pickrate_text}",
            winrate_text=winrate_text,
            pickrate_text=pickrate_text,
        )
        stat_canvas = overlay_render_snapshot.PillowCanvas(700, 260)
        overlay_renderer._draw_stat_panel(stat_canvas, stat_box, row)
        bbox = stat_canvas.image.getchannel("A").crop(stat_box).getbbox()
        assert bbox is not None
        text_center_x = stat_box[0] + (bbox[0] + bbox[2]) / 2
        assert abs(text_center_x - stat_center_x) <= 1.5

    for row, expected_text in (
        (partial_model["stats"][1], "识别中…"),
        (privacy_model["stats"][0], "统计关闭"),
        (context_missing_model["stats"][0], "等待英雄"),
        (missing_model["stats"][0], "暂无统计"),
    ):
        status_canvas = RecordingCanvas()
        overlay_renderer._draw_stat_panel(status_canvas, (100, 100, 340, 160), row)
        assert {call.get("text") for call in status_canvas.text_calls} == {expected_text}
        assert {call.get("anchor") for call in status_canvas.text_calls} == {"center"}

    long_cache = hint_cache()
    long_content = (
        "技能循环更顺畅，适合持续作战；命中后继续追击并利用回复窗口拉开第二轮技能差。"
        "提高正面承伤与回复效率，团战中优先保持阵型，再根据关键技能决定进场时机。"
    ) * 3
    for hint in long_cache["hints"].values():
        hint["synergies"][0]["content"] = long_content
    long_model = overlay_renderer.build_render_model(snapshot, hint_cache=long_cache, context=context)
    for viewport in ((1366, 768), (1920, 1080), (2560, 1600)):
        width, height = viewport
        canvas = RecordingCanvas()
        long_layout = overlay_renderer.draw_overlay_frame(canvas, long_model, viewport_size=viewport)
        long_boxes = long_layout["synergy_boxes"]
        assert len(long_boxes) == 3
        assert all(first[3] < second[1] for first, second in zip(long_boxes, long_boxes[1:]))
        assert all(0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height for box in long_boxes)

        rail_width = long_layout["synergy_rail"][2] - long_layout["synergy_rail"][0]
        minimum_height = overlay_renderer._clamp(96, height * 0.11, 176)
        for box, row in zip(long_boxes, long_model["synergies"]):
            panel_height = box[3] - box[1]
            text_layout = overlay_renderer._resolve_synergy_text_layout(
                row,
                rail_width,
                minimum_height=minimum_height,
                panel_height=panel_height,
            )
            assert text_layout["body_offset"] + len(text_layout["body_lines"]) * text_layout["line_height"] <= panel_height
            text_width = rail_width - 2 * overlay_renderer._clamp(12, rail_width * 0.05, 20)
            assert all(
                overlay_renderer._visual_text_width(line, text_layout["body_size"]) <= text_width + 1
                for line in text_layout["body_lines"]
            )

        extreme = overlay_renderer.resolve_overlay_layout(
            viewport,
            synergy_heights=[1000, 1000, 1000],
        )["synergy_boxes"]
        assert len(extreme) == 3
        assert all(box[3] <= height for box in extreme)
        assert all(box[3] - box[1] >= 80 for box in extreme)
        assert all(first[3] < second[1] for first, second in zip(extreme, extreme[1:]))

    real_frame = RUN_DIR / "data" / "runtime" / "debug" / "auto_selection" / "selection-20260612-205839" / "frame.png"
    if real_frame.exists():
        from PIL import Image
        import numpy as np

        frame = Image.open(real_frame).convert("RGB")
        frame_viewport = frame.size
        frame_canvas = overlay_render_snapshot.PillowCanvas(*frame_viewport)
        frame_layout = overlay_renderer.draw_overlay_frame(frame_canvas, ready_model, viewport_size=frame_viewport)
        alpha = frame_canvas.image.getchannel("A")

        def detect_real_card_box(card_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
            left, top, right, bottom = card_box
            crop = np.asarray(frame.crop((left, top, right, bottom)), dtype=np.uint8)
            red = crop[:, :, 0]
            green = crop[:, :, 1]
            blue = crop[:, :, 2]
            # 真机卡框是暖白/金色高亮；只在预期卡片框内取 bbox，避免下方重随按钮污染。
            mask = (
                (red > 165)
                & (green > 140)
                & (blue > 105)
                & ((red.astype(np.int16) - blue.astype(np.int16)) > 12)
            )
            ys, xs = np.where(mask)
            if len(xs) < 500:
                return card_box
            return (
                int(xs.min() + left),
                int(ys.min() + top),
                int(xs.max() + left + 1),
                int(ys.max() + top + 1),
            )

        for stat_box, card_box in zip(frame_layout["stat_boxes"], frame_layout["card_boxes"]):
            real_card = detect_real_card_box(card_box)
            bbox = alpha.crop(stat_box).getbbox()
            assert bbox is not None
            text_left = stat_box[0] + bbox[0]
            text_top = stat_box[1] + bbox[1]
            text_right = stat_box[0] + bbox[2]
            text_bottom = stat_box[1] + bbox[3]
            text_center_x = (text_left + text_right) / 2
            real_center_x = (real_card[0] + real_card[2]) / 2
            real_height = max(1, real_card[3] - real_card[1])
            assert abs(text_center_x - real_center_x) <= 4.0
            assert (text_top - real_card[1]) / real_height >= 0.86
            assert text_bottom <= real_card[3] - max(20, int(real_height * 0.045))

    def decide(**overrides):
        args = {
            "user_enabled": True,
            "event_visible": True,
            "game_foreground": True,
            "content_ready": True,
            "selection_window_active": True,
            "event_error": "",
            "blocking_modal": False,
            "scoreboard_key_down": False,
            "event_fresh_after_tab": True,
        }
        args.update(overrides)
        return overlay_host.decide_visibility(**args)

    # 显隐矩阵：V2 至少一个稳定槽即可显示，Tab 和旧事件是硬门。
    assert decide(event_visible=False, content_ready=False) == (False, "event_inactive")
    assert decide() == (True, "visible_ready")
    assert decide(selection_window_active=False) == (False, "selection_window_inactive")
    assert decide(selection_window_active=None) == (True, "visible_ready")
    assert decide(content_ready=False, ready_slots=1) == (True, "visible_partial")
    for overrides in (
        {"event_error": "event_expired", "event_visible": False},
        {"blocking_modal": True},
        {"game_foreground": False},
        {"user_enabled": False},
        {"scoreboard_key_down": True},
        {"event_fresh_after_tab": False},
    ):
        assert decide(**overrides)[0] is False
    assert decide(event_error="event_expired", event_visible=False, stale_event_hold=True) == (
        True,
        "event_expired_hold",
    )
    assert decide(game_foreground=False, diagnostic_mode=True) == (True, "diagnostic:game_not_foreground")
    assert overlay_host.build_overlay_window_config()["event_poll_ms"] == 120
    hotkey_visibility = {"user_enabled": True}
    hotkeys: queue.Queue[str] = queue.Queue()
    hotkeys.put("toggle")
    overlay_host._drain_hotkey_requests(hotkeys, hotkey_visibility)
    assert hotkey_visibility["user_enabled"] is False

    class HiddenSource:
        def __init__(self) -> None:
            self.event_reads = 0
            self.hint_reads = 0
            self.context_reads = 0

        def read_event(self):
            self.event_reads += 1
            return {"ok": True, "visible": False, "source": {"selection_window_active": False}, "slots": []}

        def read_hint_cache(self):
            self.hint_reads += 1
            return {}

        def read_context(self):
            self.context_reads += 1
            return {}

    class FakeRoot:
        def withdraw(self):
            raise AssertionError("已隐藏窗口不应重复操作")

    class FakeCanvas:
        def __init__(self) -> None:
            self.after_calls = 0
            self.delete_calls = 0

        def after(self, _delay, _callback):
            self.after_calls += 1

        def delete(self, *_args):
            self.delete_calls += 1

    hidden_source = HiddenSource()
    hidden_canvas = FakeCanvas()
    config = overlay_host.build_overlay_window_config()
    config["no_activate"] = False
    visibility = {"user_enabled": True, "target_hwnd": 123, "window_visible": False}
    with (
        patch.object(overlay_host, "_find_target_game_window", return_value=None),
        patch.object(overlay_host, "_is_game_window_foreground", return_value=True),
    ):
        overlay_host._schedule_event_render(
            FakeRoot(), hidden_canvas, config, visibility, queue.Queue(), data_source=hidden_source
        )
    assert hidden_source.event_reads == 1
    assert hidden_source.hint_reads == 0 and hidden_source.context_reads == 0
    assert hidden_canvas.delete_calls == 0 and hidden_canvas.after_calls == 1

    class DiagnosticSource:
        def __init__(self) -> None:
            self.hint_reads = 0
            self.context_reads = 0

        def read_event(self):
            return {
                "ok": False,
                "visible": False,
                "error": "event_missing",
                "generated_at": 0.0,
                "source": {"reason": "event_missing"},
                "slots": [],
            }

        def read_hint_cache(self):
            self.hint_reads += 1
            return {}

        def read_context(self):
            self.context_reads += 1
            return {}

    class DiagnosticRoot:
        def __init__(self) -> None:
            self.deiconify_calls = 0
            self.attributes_calls: list[tuple[Any, ...]] = []

        def geometry(self, _value):
            return None

        def deiconify(self):
            self.deiconify_calls += 1

        def attributes(self, *args):
            self.attributes_calls.append(args)

    class DiagnosticCanvas(FakeCanvas):
        def __init__(self) -> None:
            super().__init__()
            self.text_calls: list[dict[str, Any]] = []

        def create_rectangle(self, *_args, **_kwargs):
            return None

        def create_text(self, *args, **kwargs):
            self.text_calls.append({"args": args, **kwargs})
            return None

    diagnostic_source = DiagnosticSource()
    diagnostic_canvas = DiagnosticCanvas()
    diagnostic_config = overlay_host.build_overlay_window_config()
    diagnostic_config["diagnostic_mode"] = True
    diagnostic_config["no_activate"] = False
    diagnostic_visibility = {"user_enabled": True, "target_hwnd": None, "window_visible": False}
    with (
        patch.object(overlay_host, "_find_target_game_window", return_value=None),
        patch.object(overlay_host, "_ensure_overlay_window_styles", return_value=True),
    ):
        overlay_host._schedule_event_render(
            DiagnosticRoot(),
            diagnostic_canvas,
            diagnostic_config,
            diagnostic_visibility,
            queue.Queue(),
            data_source=diagnostic_source,
    )
    assert diagnostic_visibility["window_visible"] is True
    assert diagnostic_visibility["visibility_reason"] == "diagnostic:game_not_foreground"
    assert any("Hextech overlay diagnostic" in str(call.get("text")) for call in diagnostic_canvas.text_calls)
    assert diagnostic_source.hint_reads == 0 and diagnostic_source.context_reads == 0

    stale_visibility = {"user_enabled": True, "target_hwnd": 123, "window_visible": True}
    stale_snapshot = {
        "visible": False,
        "active": True,
        "error": "event_expired",
        "generated_at": time.time() - 3.0,
        "source": {"selection_window_active": True},
        "slots": [{"slot": 0, "state": "ready", "augment_id": "a0"}],
    }
    stale_config = dict(diagnostic_config)
    stale_config.update({"diagnostic_mode": False, "no_activate": False})
    with patch.object(overlay_host, "_is_game_window_foreground", return_value=True):
        assert overlay_host._sync_event_visibility(
            DiagnosticRoot(),
            stale_config,
            stale_visibility,
            stale_snapshot,
            apply_window=False,
        ) is True
    assert stale_visibility["visibility_reason"] == "event_expired_hold"
    assert stale_visibility["event_stale_hold_active"] is True
    assert stale_visibility["render_full_overlay"] is True

    with (
        TemporaryDirectory() as tmp_dir,
        patch.dict(os.environ, {overlay_host.OVERLAY_READY_FILE_ENV: str(Path(tmp_dir) / "ready.json")}),
        patch.object(overlay_host, "atomic_write_json", side_effect=OSError("disk busy")),
    ):
        overlay_host._signal_overlay_ready()

    class ExitWatchRoot:
        def __init__(self) -> None:
            self.after_callbacks: list[Any] = []
            self.quit_calls = 0

        def after(self, _delay, callback):
            self.after_callbacks.append(callback)

        def quit(self):
            self.quit_calls += 1

    with TemporaryDirectory() as tmp_dir:
        exit_path = Path(tmp_dir) / "host.exit.json"
        exit_root = ExitWatchRoot()
        overlay_host._schedule_exit_file_watch(exit_root, exit_path)
        assert len(exit_root.after_callbacks) == 1
        exit_root.after_callbacks[-1]()
        assert exit_root.quit_calls == 0
        exit_path.write_text("{}", encoding="utf-8")
        exit_root.after_callbacks[-1]()
        assert exit_root.quit_calls == 1

    class ErrorSource:
        def read_event(self):
            raise RuntimeError("broken event")

        def read_hint_cache(self):
            raise AssertionError("渲染失败时不应继续读取 hint cache")

        def read_context(self):
            raise AssertionError("渲染失败时不应继续读取 context")

    class BackoffCanvas:
        def __init__(self) -> None:
            self.after_calls: list[tuple[int, Any]] = []

        def after(self, delay, callback):
            self.after_calls.append((int(delay), callback))

    error_canvas = BackoffCanvas()
    with (
        patch.object(overlay_host, "_find_target_game_window", return_value=None),
        patch.object(overlay_host, "is_scoreboard_key_down", return_value=False),
    ):
        overlay_host._schedule_event_render(
            object(),
            error_canvas,
            config,
            {"user_enabled": True, "target_hwnd": 123, "window_visible": False},
            queue.Queue(),
            data_source=ErrorSource(),
        )
        for _ in range(4):
            error_canvas.after_calls[-1][1]()
    assert [delay for delay, _callback in error_canvas.after_calls[:4]] == [120, 120, 120, 240]

    host_text = (implementation_dir / "host.py").read_text(encoding="utf-8")
    assert "_schedule_window_follow" not in host_text
    assert "follow_poll_ms" not in host_text

    class GeometryMustStayHidden:
        def geometry(self, _value):
            raise AssertionError("隐藏状态只允许记录 pending geometry")

    hidden_geometry_visibility = {"window_visible": False}
    with patch.object(
        overlay_host,
        "_find_target_game_window",
        return_value=(321, (10, 20, 1930, 1100)),
    ):
        overlay_host._refresh_target_window(
            GeometryMustStayHidden(),
            config,
            hidden_geometry_visibility,
        )
    assert hidden_geometry_visibility["target_hwnd"] == 321
    assert hidden_geometry_visibility["pending_geometry"] == "1920x1080+10+20"
    assert overlay_host._target_overlay_geometry((-1920, -100, 0, 980), config) == "1920x1080-1920-100"

    class VisibleGeometryRoot:
        def __init__(self) -> None:
            self.geometries: list[str] = []

        def geometry(self, value):
            self.geometries.append(value)

    visible_geometry_root = VisibleGeometryRoot()
    visible_geometry_visibility = {
        "window_visible": True,
        "applied_geometry": "1x1+0+0",
    }
    with (
        patch.object(
            overlay_host,
            "_find_target_game_window",
            return_value=(321, (10, 20, 1930, 1100)),
        ),
        patch.object(overlay_host, "_apply_overlay_rect") as apply_rect,
        patch.object(overlay_host, "_ensure_overlay_window_styles", return_value=True) as ensure_styles,
    ):
        overlay_host._refresh_target_window(
            visible_geometry_root,
            config,
            visible_geometry_visibility,
        )
    assert visible_geometry_root.geometries == ["1920x1080+10+20"]
    apply_rect.assert_called_once_with(visible_geometry_root, (10, 20, 1930, 1100))
    ensure_styles.assert_called_once_with(visible_geometry_root, config)

    class FakeUser32:
        def __init__(self, foreground: int = 0) -> None:
            self.foreground = foreground
            self.set_window_pos_flags: list[int] = []
            self.set_window_pos_calls: list[tuple[int, int, int, int, int]] = []

        def SetWindowPos(self, _hwnd, _after, _x, _y, _cx, _cy, flags):
            self.set_window_pos_flags.append(int(flags))
            self.set_window_pos_calls.append((int(_x), int(_y), int(_cx), int(_cy), int(flags)))
            return 1

        def GetForegroundWindow(self):
            return self.foreground

        @staticmethod
        def GetAncestor(hwnd, _kind):
            return hwnd

    position_user32 = FakeUser32()
    with (
        patch.object(overlay_host.ctypes.windll, "user32", position_user32),
        patch.object(overlay_host, "_root_hwnd", return_value=99),
    ):
        overlay_host._apply_overlay_rect(object(), (-1920, -100, 0, 980))
    assert position_user32.set_window_pos_calls == [(-1920, -100, 1920, 1080, overlay_host.SWP_NOACTIVATE)]

    desired_style = (
        overlay_host.WS_EX_LAYERED
        | overlay_host.WS_EX_TOPMOST
        | overlay_host.WS_EX_TOOLWINDOW
        | overlay_host.WS_EX_TRANSPARENT
    )
    unchanged_user32 = FakeUser32()
    with (
        patch.object(overlay_host.ctypes.windll, "user32", unchanged_user32),
        patch.object(overlay_host, "_root_hwnd", return_value=99),
        patch.object(overlay_host, "_get_window_exstyle", return_value=desired_style),
        patch.object(overlay_host, "_set_window_exstyle") as set_style,
    ):
        assert overlay_host._apply_overlay_window_styles(
            object(),
            click_through=True,
            no_activate=False,
        ) is False
    set_style.assert_not_called()
    assert unchanged_user32.set_window_pos_flags == []

    changed_user32 = FakeUser32()
    with (
        patch.object(overlay_host.ctypes.windll, "user32", changed_user32),
        patch.object(overlay_host, "_root_hwnd", return_value=99),
        patch.object(overlay_host, "_get_window_exstyle", side_effect=[0, desired_style]),
        patch.object(overlay_host, "_set_window_exstyle"),
    ):
        assert overlay_host._apply_overlay_window_styles(
            object(),
            click_through=True,
            no_activate=False,
        ) is True
    assert len(changed_user32.set_window_pos_flags) == 1
    assert changed_user32.set_window_pos_flags[0] & overlay_host.SWP_FRAMECHANGED

    class FakeShowRoot:
        def __init__(self) -> None:
            self.geometries: list[str] = []
            self.shown = False

        def geometry(self, value):
            self.geometries.append(value)

        def deiconify(self):
            self.shown = True

        def attributes(self, *_args):
            return None

    show_root = FakeShowRoot()
    show_user32 = FakeUser32()
    show_visibility = {
        "pending_geometry": "1920x1080+10+20",
        "target_rect": (10, 20, 1930, 1100),
    }
    with (
        patch.object(overlay_host.ctypes.windll, "user32", show_user32),
        patch.object(overlay_host, "_root_hwnd", return_value=99),
        patch.object(overlay_host, "_ensure_overlay_window_styles", return_value=True) as ensure_styles,
    ):
        overlay_host._show_overlay_window(show_root, config, show_visibility)
    assert show_root.shown is True
    assert show_root.geometries == ["1920x1080+10+20"]
    assert len(show_user32.set_window_pos_flags) == 1
    assert not (show_user32.set_window_pos_flags[0] & overlay_host.SWP_FRAMECHANGED)
    assert show_user32.set_window_pos_calls == [(10, 20, 1920, 1080, overlay_host.SWP_NOACTIVATE)]
    ensure_styles.assert_called_once_with(show_root, config)

    overlay_foreground_user32 = FakeUser32(foreground=456)
    with patch.object(overlay_host.ctypes.windll, "user32", overlay_foreground_user32):
        assert overlay_host._is_game_window_foreground(123, overlay_hwnd=456) is False

    class RootNormalizingUser32(FakeUser32):
        @staticmethod
        def GetAncestor(hwnd, _kind):
            return 123 if hwnd in {123, 456} else hwnd

    child_foreground_user32 = RootNormalizingUser32(foreground=456)
    with patch.object(overlay_host.ctypes.windll, "user32", child_foreground_user32):
        assert overlay_host._is_game_window_foreground(123) is True

    class HotkeyFallbackUser32:
        post_calls = 0

        @staticmethod
        def PeekMessageW(*_args):
            return 1

        @staticmethod
        def RegisterHotKey(*_args):
            return 0

        @staticmethod
        def GetAsyncKeyState(_key):
            return 0x8000

        def PostThreadMessageW(self, *_args):
            self.post_calls += 1
            return 1

    fallback_user32 = HotkeyFallbackUser32()
    fallback_queue: queue.Queue[str] = queue.Queue()
    with patch.object(overlay_host.ctypes.windll, "user32", fallback_user32):
        fallback_controller = overlay_host._start_hotkey_thread(fallback_queue)
        try:
            assert fallback_controller.mode == "poll"
            assert fallback_queue.get(timeout=1.0) == "toggle"
        finally:
            overlay_host._stop_hotkey_thread(fallback_controller)
    assert fallback_controller.thread is not None
    assert fallback_controller.thread.is_alive() is False
    assert fallback_user32.post_calls == 1

    class DebounceStop:
        def __init__(self) -> None:
            self.waits = 0

        def is_set(self):
            return False

        def wait(self, _timeout):
            self.waits += 1
            return self.waits >= 5

    class DebounceUser32:
        def __init__(self) -> None:
            self.loop_index = 0
            self.states = [True, False, True, False, True]

        def GetAsyncKeyState(self, key):
            pressed = self.states[min(self.loop_index, len(self.states) - 1)]
            if key == ord("H"):
                self.loop_index += 1
            return 0x8000 if pressed else 0

    debounce_queue: queue.Queue[str] = queue.Queue()
    debounce_controller = overlay_host.HotkeyController(debounce_queue)
    debounce_controller.stop_requested = DebounceStop()  # type: ignore[assignment]
    with patch.object(overlay_host.time, "monotonic", side_effect=[1.00, 1.10, 1.20, 1.25, 1.35]):
        overlay_host._poll_alt_h_hotkey(debounce_controller, DebounceUser32())
    drained_toggles: list[str] = []
    while not debounce_queue.empty():
        drained_toggles.append(debounce_queue.get_nowait())
    assert drained_toggles == ["toggle", "toggle"]

    # 正式与诊断共用同一纯 renderer；快照必须直接输出 PNG，源码不允许 PS fallback。
    renderer_text = (implementation_dir / "renderer.py").read_text(encoding="utf-8").lower()
    assert not any(token in renderer_text for token in ("banner", "top_candidates", "cache miss", "context pending"))
    snapshot_tool_text = (RUN_DIR / "tools" / "overlay_render_snapshot.py").read_text(encoding="utf-8").lower()
    assert "postscript" not in snapshot_tool_text and "ghostscript" not in snapshot_tool_text
    pillow_canvas = overlay_render_snapshot.PillowCanvas(200, 100)
    pillow_canvas.create_rectangle(10, 10, 60, 40, fill="#123456", outline="")
    assert pillow_canvas.image.getpixel((20, 20))[:3] == (18, 52, 86)

    left_anchor_canvas = overlay_render_snapshot.PillowCanvas(200, 100)
    left_anchor_canvas.create_text(50, 50, text="胜率:", fill="#FFFFFF", anchor="e")
    left_bbox = left_anchor_canvas.image.getchannel("A").getbbox()
    assert left_bbox is not None and left_bbox[0] < 50 and left_bbox[2] <= 51

    right_anchor_canvas = overlay_render_snapshot.PillowCanvas(200, 100)
    right_anchor_canvas.create_text(150, 50, text="3.0%", fill="#FFFFFF", anchor="w")
    right_bbox = right_anchor_canvas.image.getchannel("A").getbbox()
    assert right_bbox is not None and right_bbox[0] >= 149 and right_bbox[2] > 150

    with TemporaryDirectory() as tmp_dir:
        png_path = overlay_render_snapshot.render_case(
            "ready_three_tiers", Path(tmp_dir), (1366, 768)
        )
        assert png_path.suffix == ".png"
        assert png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def _run_named_checks(check_names: tuple[str, ...]) -> None:
    """按 registry 保存的顺序执行检查函数，避免 CLI 层继续维护长清单。"""

    namespace = globals()
    for check_name in check_names:
        check = namespace.get(check_name)
        if not callable(check):
            raise RuntimeError(f"自检清单引用了不存在的检查函数：{check_name}")
        check()


def run_default_checks() -> None:
    from tools.checks.registry import DEFAULT_CHECKS

    _run_read_only_offline_checks(DEFAULT_CHECKS)


def run_overlay_only_checks() -> None:
    from tools.checks.registry import OVERLAY_ONLY_CHECKS

    _run_read_only_offline_checks(OVERLAY_ONLY_CHECKS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hextech 开发自检与手动验收入口。")
    parser.add_argument("--bundle-manifest", action="store_true", help="只输出并校验 bundle manifest 明细。")
    parser.add_argument("--overlay-only", action="store_true", help="只执行游戏内 overlay 相关离线自检。")
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

    if args.overlay_only:
        run_overlay_only_checks()
        print("overlay 自检通过。")
        return 0

    run_default_checks()
    print("所有开发自检通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

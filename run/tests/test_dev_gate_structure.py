"""项目结构开发门禁。

这些测试由 pytest 直接收集；本文件不依赖旧的字符串 registry。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


import importlib
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Sequence
from unittest.mock import patch

import hextech.display.web.runtime as web_runtime
import hextech.scraping.icon_resolver as icon_resolver
import hextech.scraping.synergy.scraper as synergy_scraper
import hextech.scraping.version_sync as version_sync
from hextech.catalog.aliases import load_manual_alias_index
from hextech.catalog.version_catalog import (
    legacy_index_payload,
    legacy_static_payload,
    load_augment_manifest_entries,
    load_augment_name_to_icon_map,
    load_champion_alias_records,
    load_champion_core_data,
)
from hextech.support import image_validation
from tools.resource_manifest import validate_resource_manifest


RUN_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = RUN_DIR / "data"
DATA_STATIC_ASSET_DIR = DATA_DIR / "static" / "assets"
DATA_STATIC_VERSION_DIR = DATA_DIR / "static" / "version"
DATA_STARTUP_SEED_DIR = DATA_DIR / "seed" / "startup"
WEB_STATIC_DIR = RUN_DIR / "hextech" / "display" / "web" / "static"


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


EXPECTED_STRUCTURE_TESTS = {
    "test_root_entrypoints",
    "test_python_runtime_guard_contract",
    "test_hextech_package_contract",
    "test_resource_classification_manifest",
    "test_version_data_catalog_consolidation",
    "test_stable_data_compat_routes_are_whitelisted",
    "test_champion_core_projection_replaces_legacy_file",
    "test_clean_mayhem_combos_uses_core_projection",
    "test_manual_alias_index",
    "test_manifest_icon_url_safety",
    "test_icon_resolver_defaults_to_resource_image_dir",
    "test_icon_downloads_reject_non_png_bytes",
    "test_no_legacy_imports",
}


@pytest.mark.dev_gate
def test_structure_checks_are_owned_by_pytest() -> None:
    source_path = Path(__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    marked_tests = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Attribute)
            and isinstance(decorator.value, ast.Attribute)
            and isinstance(decorator.value.value, ast.Name)
            and decorator.value.value.id == "pytest"
            and decorator.value.attr == "mark"
            and decorator.attr == "dev_gate"
            for decorator in node.decorator_list
        )
    }
    assert EXPECTED_STRUCTURE_TESTS <= marked_tests
    source = source_path.read_text(encoding="utf-8")
    assert "tools" + ".checks" not in source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get" + "attr"
        for node in ast.walk(tree)
    )


@pytest.mark.dev_gate
def test_legacy_runner_does_not_reference_migrated_structure_checks() -> None:
    registry_path = RUN_DIR / "tools" / "checks" / "registry.py"
    registry_source = registry_path.read_text(encoding="utf-8") if registry_path.exists() else ""
    migrated_check_names = {
        "check_" + test_name.removeprefix("test_")
        for test_name in EXPECTED_STRUCTURE_TESTS
    }

    assert not (RUN_DIR / "tools" / "checks" / "structure.py").exists()
    assert all(f'"{name}"' not in registry_source for name in migrated_check_names)
    assert '"structure"' not in registry_source


@pytest.mark.dev_gate
def test_all_automated_dev_gate_domains_are_owned_by_pytest() -> None:
    domain_test_paths = {
        RUN_DIR / "tests" / f"test_dev_gate_{domain}.py"
        for domain in ("web", "overlay", "bundle", "scraping", "synergy", "runtime")
    }
    assert all(path.exists() for path in domain_test_paths)

    legacy_source = (RUN_DIR / "tools" / "dev_checks.py").read_text(encoding="utf-8")
    legacy_tree = ast.parse(legacy_source)
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("check_")
        for node in legacy_tree.body
    )
    checks_dir = RUN_DIR / "tools" / "checks"
    assert not checks_dir.exists() or not any(checks_dir.glob("*.py"))


@pytest.mark.dev_gate
def test_root_entrypoints() -> None:
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

@pytest.mark.dev_gate
def test_python_runtime_guard_contract() -> None:
    """源码态入口必须先落到 run/.venv，再加载业务依赖。"""

    import hextech.support.python_runtime as python_runtime

    assert python_runtime.REQUIRED_PYTHON == (3, 11)
    assert python_runtime.DEFAULT_VENV_DIR == RUN_DIR / ".venv"
    assert python_runtime.default_venv_python_path().parent.name in {"Scripts", "bin"}
    assert "scrapling" in python_runtime.REQUIRED_RUNTIME_PACKAGES
    assert "cloakbrowser" in python_runtime.REQUIRED_RUNTIME_PACKAGES
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
    assert '"tools" / "requirements" / "compat.txt"' in setup_text
    assert "scrapling_smoke" in setup_text
    assert ".venv" in (RUN_DIR / ".gitignore").read_text(encoding="utf-8")

    runtime_text = (RUN_DIR / "hextech" / "support" / "python_runtime.py").read_text(encoding="utf-8")
    assert 'return [["py", "-3.11"]]' in runtime_text
    assert "bootstrap_default_venv" in runtime_text
    assert "创建 Python 3.11 虚拟环境" in runtime_text
    assert 'install", "-r", str(requirements)' in runtime_text
    assert "DEFAULT_VENV_DIR" in runtime_text
    assert "PACKAGING_RUNTIME_PACKAGES" in runtime_text

    with TemporaryDirectory() as temp_dir:
        fake_venv_dir = Path(temp_dir) / ".venv"
        fake_venv_python = fake_venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        bootstrap_commands: list[list[str]] = []
        missing_sequence = iter((["scrapling"], []))

        def fake_probe(command: Sequence[str], **_: object) -> tuple[int, int] | None:
            command_list = list(command)
            if command_list == python_runtime._creator_candidates()[0]:
                return python_runtime.REQUIRED_PYTHON
            if command_list == [str(fake_venv_python)]:
                return python_runtime.REQUIRED_PYTHON
            return None

        def fake_missing(command: Sequence[str], packages: Sequence[str]) -> list[str]:
            assert list(command) == [str(fake_venv_python)]
            assert tuple(packages) == ("scrapling",)
            return next(missing_sequence)

        def fake_run(command: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
            bootstrap_commands.append([str(part) for part in command])
            return subprocess.CompletedProcess(list(command), 0)

        with (
            patch.object(python_runtime, "DEFAULT_VENV_DIR", fake_venv_dir),
            patch.object(python_runtime, "default_venv_python_path", return_value=fake_venv_python),
            patch.object(python_runtime, "probe_python_version", side_effect=fake_probe),
            patch.object(python_runtime, "missing_required_imports", side_effect=fake_missing),
            patch.object(python_runtime.subprocess, "run", side_effect=fake_run),
        ):
            assert python_runtime.bootstrap_default_venv(require_packages=("scrapling",)) == [str(fake_venv_python)]

        assert bootstrap_commands == [
            [*python_runtime._creator_candidates()[0], "-m", "venv", str(fake_venv_dir)],
            [str(fake_venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            [
                str(fake_venv_python),
                "-m",
                "pip",
                "install",
                "-r",
                str(RUN_DIR / "tools" / "requirements" / "compat.txt"),
            ],
        ]

    dev_checks_text = (RUN_DIR / "tools" / "dev_checks.py").read_text(encoding="utf-8")
    guard_index = dev_checks_text.index("ensure_python_311_for_source()")
    assert guard_index < dev_checks_text.index("from tools import dev_check_manual")

@pytest.mark.dev_gate
def test_hextech_package_contract() -> None:
    """最终结构必须收口到 hextech 单包，不再保留旧根级 import 包。"""

    assert (RUN_DIR / "hextech").exists()
    assert (RUN_DIR / "frontend" / "package.json").exists()
    assert (DATA_DIR / "README.md").exists()
    assert (RUN_DIR / "docs" / "README.md").exists()
    assert (RUN_DIR / "tests" / "test_dev_gate_structure.py").exists()

    required_modules = (
        "hextech.display.web.app",
        "hextech.display.web.api",
        "hextech.display.web.runtime",
        "hextech.display.desktop.app",
        "hextech.display.desktop.runtime",
        "hextech.display.desktop.service_manager",
        "hextech.core.refresh",
        "hextech.core.settings",
        "hextech.data_service",
        "hextech.data_snapshot",
        "hextech.client_context",
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
        "hextech.support.user_diagnostics",
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
    assert "from hextech.catalog.precomputed_cache" not in hextech_scraper_text
    assert "from hextech.overlay.hints" not in hextech_scraper_text
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
    assert Path(version_sync_module.BUNDLE_ROOT_DIR).resolve() == RUN_DIR.resolve()
    paths_text = (RUN_DIR / "hextech" / "scraping" / "_paths.py").read_text(encoding="utf-8")
    assert 'os.path.join(RUNTIME_DATA_DIR, "raw")' in paths_text
    assert 'else os.path.join(DATA_DIR, "raw")' not in paths_text
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
    assert "from hextech.catalog.runtime_store" not in core_settings_text
    assert "from hextech.scraping._paths import RUNTIME_DATA_DIR" in core_settings_text
    assert "from hextech.support.atomic_io" in core_settings_text

    desktop_app_text = (RUN_DIR / "hextech" / "display" / "desktop" / "app.py").read_text(encoding="utf-8")
    desktop_runtime_text = (RUN_DIR / "hextech" / "display" / "desktop" / "runtime.py").read_text(encoding="utf-8")
    desktop_service_text = (RUN_DIR / "hextech" / "display" / "desktop" / "service_manager.py").read_text(encoding="utf-8")
    assert "from processing." not in desktop_app_text
    assert "from scraping." not in desktop_app_text
    assert "from . import ui_runtime" not in desktop_app_text
    assert "from hextech.catalog.runtime_store" not in desktop_app_text.split("class HextechUI", 1)[0]
    assert "from hextech.overlay.hints" not in desktop_app_text.split("class HextechUI", 1)[0]
    assert "from hextech.scraping.version_sync" not in desktop_app_text.split("class HextechUI", 1)[0]
    assert "from hextech.data_snapshot import DataSnapshotClient" in desktop_app_text
    assert "CachedDataFrameLoader" not in desktop_app_text
    assert "get_latest_csv" not in desktop_app_text
    assert "from hextech.scraping.version_sync import ASSET_DIR, get_advanced_session, load_champion_core_data" in desktop_app_text
    assert "from . import runtime as ui_runtime" in desktop_app_text
    assert "from processing." not in desktop_runtime_text
    assert "from scraping." not in desktop_runtime_text
    assert "from . import web_runtime" not in desktop_runtime_text
    assert "from hextech.display.web import runtime as web_runtime" not in desktop_runtime_text.split("def _web_runtime()", 1)[0]
    assert "def _web_runtime()" in desktop_runtime_text
    assert "def _query_terminal()" in desktop_runtime_text
    assert "from hextech.catalog import query_terminal" in desktop_runtime_text
    assert "from hextech.catalog.query_terminal" not in desktop_runtime_text
    assert "from hextech.overlay.window" in desktop_runtime_text
    assert "from hextech.scraping._paths" in desktop_runtime_text
    assert "from processing." not in desktop_service_text
    assert "from hextech.overlay.events" in desktop_service_text
    assert "from hextech.overlay.window" in desktop_service_text
    assert "self.data_service = ManagedService" in desktop_service_text

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
    assert "from hextech.data_snapshot import DataSnapshotClient" in overlay_data_source_text
    assert "from hextech.overlay.hints" not in overlay_data_source_text
    assert "from hextech.overlay.events" in overlay_data_source_text
    assert "from hextech.overlay.context" in overlay_data_source_text
    assert "from processing." not in overlay_renderer_text
    assert "from hextech.overlay.vision.layout" in overlay_renderer_text

    assert not (RUN_DIR / "game_overlay").exists()

@pytest.mark.dev_gate
def test_resource_classification_manifest() -> None:
    """验证单一 data 根分类清单只描述现有稳定资源，不误纳入运行态。"""

    expected_categories = {
        "static-version",
        "static-assets",
        "startup-seed",
        "source-evidence",
        "diagnostic-fixtures",
        "runtime",
    }
    resolved = validate_resource_manifest(RUN_DIR)
    assert expected_categories.issubset(resolved.keys())
    for category in expected_categories:
        if category == "runtime":
            continue
        assert resolved[category], f"资源分类没有匹配现有文件：{category}"

@pytest.mark.dev_gate
def test_version_data_catalog_consolidation() -> None:
    """验证版本数据已收口到 data/static/version，同时保留旧文件名投影。"""

    assert (DATA_STATIC_VERSION_DIR / "英雄目录.v1.json").exists()
    assert (DATA_STATIC_VERSION_DIR / "海克斯资源目录.v1.json").exists()
    for legacy_name in (
        "Champion_Alias_Index.json",
        "champion.alias-to-id.v1.json",
        "champion.id-to-detail.v1.json",
        "champion.id-to-name.v1.json",
        "Augment_Apexlol_Map.json",
        "Augment_Icon_Manifest.json",
        "augment.name-to-icon.v1.json",
    ):
        assert not (DATA_STATIC_VERSION_DIR / legacy_name).exists(), f"旧拆分 JSON 不应继续作为事实源：{legacy_name}"

    assert len(load_champion_alias_records()) >= 100
    assert len(load_augment_manifest_entries()) >= 600
    assert len(load_augment_name_to_icon_map()) >= 500
    assert isinstance(legacy_index_payload("Champion_Alias_Index.json", DATA_STATIC_VERSION_DIR), list)
    assert isinstance(legacy_index_payload("augment.name-to-icon.v1.json", DATA_STATIC_VERSION_DIR), dict)
    assert isinstance(legacy_static_payload("Augment_Icon_Manifest.json", DATA_STATIC_VERSION_DIR), list)

    id_to_name = legacy_index_payload("champion.id-to-name.v1.json", DATA_STATIC_VERSION_DIR)
    id_to_detail = legacy_index_payload("champion.id-to-detail.v1.json", DATA_STATIC_VERSION_DIR)
    assert isinstance(id_to_name, dict) and isinstance(id_to_name.get("266"), str)
    assert isinstance(id_to_detail, dict) and isinstance(id_to_detail.get("266"), dict)
    assert id_to_name["266"] == id_to_detail["266"]["heroName"]

@pytest.mark.dev_gate
def test_stable_data_compat_routes_are_whitelisted() -> None:
    """验证旧数据 URL 是受控兼容入口，不暴露整个稳定版本数据目录。"""

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

@pytest.mark.dev_gate
def test_champion_core_projection_replaces_legacy_file() -> None:
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

@pytest.mark.dev_gate
def test_clean_mayhem_combos_uses_core_projection() -> None:
    """验证 Mayhem 清洗默认读取英雄目录投影，不要求旧 core 文件存在。"""

    import tools.clean_mayhem_combos as clean_mayhem_combos

    summary = clean_mayhem_combos.merge_mayhem_combos(
        apex_path=DATA_STARTUP_SEED_DIR / "synergy" / "Champion_Synergy_latest.v1.json",
        write_output=False,
    )
    assert summary["written"] is False
    assert summary["mayhem_raw_items"] >= 100
    assert summary["added_items"] >= 0

@pytest.mark.dev_gate
def test_manual_alias_index() -> None:
    payload = load_champion_alias_records()
    assert payload, "Champion_Alias_Index.json 应至少包含一条手工索引"
    first = payload[0]
    assert isinstance(first, dict)
    assert "heroName" in first
    assert load_manual_alias_index()

@pytest.mark.dev_gate
def test_manifest_icon_url_safety() -> None:
    assert icon_resolver.sanitize_augment_icon_url("/assets/safe-name_01.png") == "/assets/safe-name_01.png"
    assert icon_resolver.sanitize_augment_icon_url("https://raw.communitydragon.org/latest/game/assets/x.webp")
    assert icon_resolver.sanitize_augment_icon_url("https://cdn.communitydragon.org/latest/game/assets/x.webp")
    assert icon_resolver.sanitize_augment_icon_url("https://ddragon.leagueoflegends.com/cdn/1/img/champion/Aatrox.png")
    assert icon_resolver.sanitize_augment_icon_url("https://apexlol.info/images/hextech/example.webp")
    assert not icon_resolver.sanitize_augment_icon_url("http://raw.communitydragon.org/latest/game/assets/x.png")
    assert not icon_resolver.sanitize_augment_icon_url("https://evil.com/assets/x.png")
    assert not icon_resolver.sanitize_augment_icon_url("/assets/not-png.webp")
    assert not icon_resolver.sanitize_augment_icon_url("/assets/../secret.png")

@pytest.mark.dev_gate
def test_icon_resolver_defaults_to_resource_image_dir() -> None:
    """验证图标解析默认从 data/static/assets 读取，不回落到旧根级 assets。"""

    assert Path(icon_resolver._resolve_assets_dir(None)) == DATA_STATIC_ASSET_DIR
    assert Path(icon_resolver._resolve_assets_dir_for_config(str(DATA_STATIC_VERSION_DIR))) == DATA_STATIC_ASSET_DIR
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

@pytest.mark.dev_gate
def test_icon_downloads_reject_non_png_bytes() -> None:
    """远端图标下载必须验证 PNG 内容，不能把 200 HTML 写成本地 png。"""

    import tools.sync_cdragon_augments as sync_cdragon_augments

    class HtmlStreamResponse:
        status_code = 200

        def iter_content(self, chunk_size=8192):
            del chunk_size
            yield b"<html><body>not an image</body></html>"

    class OversizeStreamResponse:
        status_code = 200

        def iter_content(self, chunk_size=8192):
            del chunk_size
            yield b"x" * (image_validation.MAX_PNG_RESPONSE_BYTES + 1)

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return HtmlStreamResponse()

    class HtmlBytesResponse:
        status_code = 200
        content = b"<html><body>not an image</body></html>"

        def raise_for_status(self) -> None:
            return None

    with TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "bad_icon.png"
        with (
            patch.object(icon_resolver, "_get_download_session", return_value=FakeSession()),
            patch.object(icon_resolver, "_iter_augment_icon_urls", return_value=iter(("https://example.test/bad_icon.png",))),
        ):
            assert icon_resolver.ensure_augment_icon_cached("bad_icon.png", asset_dir=tmp_dir, force_refresh=True) is None
        assert not target.exists()

    with TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "too_large.png"
        fake_session = type("OversizeSession", (), {"get": lambda self, *_args, **_kwargs: OversizeStreamResponse()})()
        with (
            patch.object(icon_resolver, "_get_download_session", return_value=fake_session),
            patch.object(icon_resolver, "_iter_augment_icon_urls", return_value=iter(("https://example.test/too_large.png",))),
        ):
            assert icon_resolver.ensure_augment_icon_cached("too_large.png", asset_dir=tmp_dir, force_refresh=True) is None
        assert not target.exists()

    with TemporaryDirectory() as tmp_dir:
        asset_dir = Path(tmp_dir)
        target = asset_dir / "bad_champion.png"
        fake_session = type("FakeChampionSession", (), {"get": lambda self, *_args, **_kwargs: HtmlBytesResponse()})()
        assert version_sync._download_champion_image(fake_session, "15.13.1", "Aatrox", str(target)) is False
        assert not target.exists()

    with TemporaryDirectory() as tmp_dir:
        asset_dir = Path(tmp_dir)
        target = asset_dir / "bad_web.png"
        with (
            patch.object(web_runtime, "get_assets_dir", return_value=str(asset_dir)),
            patch.object(web_runtime, "resolve_remote_augment_icon_url", return_value="https://example.test/bad_web.png"),
            patch("hextech.display.web.runtime.requests.get", return_value=HtmlStreamResponse()),
        ):
            assert web_runtime.download_augment_icon_from_remote("测试海克斯", "bad_web.png") is None
        assert not target.exists()

    with TemporaryDirectory() as tmp_dir:
        asset_dir = Path(tmp_dir)
        target = asset_dir / "too_large_web.png"
        with (
            patch.object(web_runtime, "get_assets_dir", return_value=str(asset_dir)),
            patch.object(web_runtime, "resolve_remote_augment_icon_url", return_value="https://example.test/too_large_web.png"),
            patch("hextech.display.web.runtime.requests.get", return_value=OversizeStreamResponse()),
        ):
            assert web_runtime.download_augment_icon_from_remote("测试海克斯", "too_large_web.png") is None
        assert not target.exists()

    with TemporaryDirectory() as tmp_dir:
        asset_dir = Path(tmp_dir)
        target = asset_dir / "bad_sync.png"
        with patch("tools.sync_cdragon_augments.requests.get", return_value=HtmlBytesResponse()):
            try:
                sync_cdragon_augments._download_one(
                    {"name": "测试海克斯", "filename": "bad_sync.png", "source_icon_url": "https://example.test/bad_sync.png"},
                    force=True,
                    timeout=1,
                    asset_dir=asset_dir,
                )
            except ValueError as exc:
                assert "invalid png" in str(exc)
            else:
                raise AssertionError("CDragon 同步不得接受非 PNG 字节")
        assert not target.exists()

@pytest.mark.dev_gate
def test_no_legacy_imports() -> None:
    legacy_hits = []
    for path in RUN_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == "dev_checks.py":
            continue
        text = path.read_text(encoding="utf-8")
        if (
            "from " + "app." in text
            or "from " + "services." in text
            or "import " + "app." in text
            or "import " + "services." in text
        ):
            legacy_hits.append(path)
    assert not legacy_hits, f"仍存在旧导入: {legacy_hits}"

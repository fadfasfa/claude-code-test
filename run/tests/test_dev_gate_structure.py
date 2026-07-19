"""新 ``src/resources/var`` 布局的结构门禁。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


RUN_DIR = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.dev_gate


def test_root_contains_only_supported_layout() -> None:
    for required in ("src", "resources", "var", "tests", "tooling", "docs", "pyproject.toml", "README.md"):
        assert (RUN_DIR / required).exists(), required
    for removed in ("hextech", "frontend", "data", "to" + "ols", "scripts", "PROJECT.md"):
        assert not (RUN_DIR / removed).exists(), removed
    assert not list(RUN_DIR.glob("*.py")), "根目录不得恢复 Python 转发入口"


def test_pyproject_exposes_only_bootstrap_commands() -> None:
    payload = tomllib.loads((RUN_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = payload["project"]["scripts"]
    assert scripts == {
        "hextech-desktop": "hextech.bootstrap.desktop:main",
        "hextech-web": "hextech.bootstrap.web:main",
        "hextech-overlay": "hextech.bootstrap.overlay:main",
        "hextech-data-service": "hextech.bootstrap.data_service:main",
        "hextech-supervisor": "hextech.bootstrap.supervisor:main",
    }


def test_resources_and_runtime_are_separated() -> None:
    assert (RUN_DIR / "resources" / "catalog" / "英雄目录.v1.json").is_file()
    assert (RUN_DIR / "resources" / "catalog" / "海克斯资源目录.v1.json").is_file()
    assert (RUN_DIR / "resources" / "seeds" / "current.v2.json").is_file()
    assert not (RUN_DIR / "resources" / "seeds" / "current.v1.json").exists()
    assert (RUN_DIR / "resources" / "manifest.v2.json").is_file()
    assert not (RUN_DIR / "resources" / "manifest.v1.json").exists()
    assert not (RUN_DIR / "resources" / "var").exists()
    assert not (RUN_DIR / "var" / "data").exists()
    for retired in ("ipc", "persisted", "profile", "sources/synergy"):
        assert not (RUN_DIR / "var" / retired).exists(), retired


def test_documented_layout_matches_active_paths() -> None:
    data_layout = (RUN_DIR / "docs" / "data-layout.md").read_text(encoding="utf-8")
    system_design = (RUN_DIR / "docs" / "system-design.md").read_text(encoding="utf-8")
    assert "evidence/mayhem_combos.raw.json" in data_layout
    assert "cache/{overlay_vision,assets}" in data_layout
    assert "user-data/preferences" in data_layout
    assert 'Runtime["runtime"]' not in system_design


def test_source_packages_follow_modular_monolith_boundaries() -> None:
    root = RUN_DIR / "src" / "hextech"
    for package in ("contracts", "modules", "interfaces", "infrastructure", "bootstrap"):
        assert (root / package).is_dir(), package
    assert not (root / "runtime").exists(), "禁止恢复无消费者的 runtime 预留空包"
    assert not (root / "bootstrap" / "data_refresh.py").exists(), "禁止恢复旧刷新编排"
    assert (root / "modules" / "acquisition" / "hextech" / "contracts.py").is_file()
    assert (root / "modules" / "acquisition" / "hextech" / "validation.py").is_file()
    assert (root / "modules" / "acquisition" / "apex" / "parser.py").is_file()
    assert (root / "modules" / "acquisition" / "mayhem" / "merge.py").is_file()
    assert (root / "infrastructure" / "sources" / "hextech" / "service.py").is_file()
    assert (root / "infrastructure" / "sources" / "apex" / "service.py").is_file()
    assert (root / "infrastructure" / "sources" / "mayhem" / "service.py").is_file()
    assert (root / "infrastructure" / "transport" / "scrapling_client.py").is_file()
    assert not list((root / "infrastructure" / "scraping").rglob("*.py"))


def test_production_python_files_stay_within_maintainable_size() -> None:
    root = RUN_DIR / "src" / "hextech"
    oversized = {
        str(path.relative_to(root)): len(path.read_text(encoding="utf-8").splitlines())
        for path in root.rglob("*.py")
        if len(path.read_text(encoding="utf-8").splitlines()) > 800
    }
    assert not oversized


def test_browser_evasion_packages_are_not_runtime_dependencies() -> None:
    runtime = (RUN_DIR / "tooling" / "requirements" / "runtime.txt").read_text(encoding="utf-8").lower()
    python_guard = (RUN_DIR / "src" / "hextech" / "modules" / "session" / "python_environment.py").read_text(encoding="utf-8").lower()
    assert "cloak" + "browser" not in runtime + python_guard
    assert "steal" + "th" not in runtime + python_guard

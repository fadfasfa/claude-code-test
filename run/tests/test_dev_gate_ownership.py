"""开发门禁测试所有权的结构回归测试。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


RUN_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = RUN_DIR / "tests"
DOMAINS = ("structure", "web", "overlay", "bundle", "scraping", "synergy", "runtime")
pytestmark = pytest.mark.dev_gate


def _top_level_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_each_dev_gate_domain_owns_test_entities() -> None:
    for domain in DOMAINS:
        path = TESTS_DIR / f"test_dev_gate_{domain}.py"
        assert _top_level_tests(path), f"{path.name} 仍未持有测试实体"


def test_dev_gate_support_contains_no_tests_or_manual_web_copy() -> None:
    support_path = TESTS_DIR / "_dev_gate_support.py"
    assert support_path.exists()
    assert not _top_level_tests(support_path)
    support_source = support_path.read_text(encoding="utf-8")
    for manual_helper in (
        "_make_local_driver",
        "_extract_local_cards",
        "_source_visible_matches",
        "run_manual_web_synergy",
    ):
        assert f"def {manual_helper}" not in support_source


def test_bundle_gate_uses_manual_module_validation_api() -> None:
    bundle_source = (TESTS_DIR / "test_dev_gate_bundle.py").read_text(encoding="utf-8")
    manual_source = (RUN_DIR / "tooling" / "checks" / "manual.py").read_text(encoding="utf-8")
    assert "validate_bundle_manifest_contract" in manual_source
    assert "dev_check_manual.validate_bundle_manifest_contract" in bundle_source
    assert "build_bundle_manifest(" not in bundle_source


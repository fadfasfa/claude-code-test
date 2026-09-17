"""Frozen historical refresh tests cannot become another production engine."""

import ast
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
RETIRED = {"refresh_coordinator", "refresh_cycle", "refresh_adoption", "refresh_promotion",
           "source_freshness", "source_worker_failure"}


def _forbidden(module: str) -> bool:
    return (module.split(".")[0] in {"tests", "support", "legacy_refresh"}
            or module in {"hextech.bootstrap." + name for name in RETIRED})


def test_production_cannot_import_test_support_or_retired_refresh_engine():
    violations = []
    for root in (ROOT / "src", ROOT / "tooling"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    modules = [base, *(base + "." + item.name for item in node.names)]
                elif isinstance(node, ast.Call) and node.args:
                    function = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    if function in {"import_module", "__import__"} and isinstance(node.args[0], ast.Constant):
                        modules = [node.args[0].value] if isinstance(node.args[0].value, str) else []
                violations.extend(f"{path.relative_to(ROOT)}:{node.lineno}: {module}"
                                  for module in modules if _forbidden(module))
    assert not violations, "\n".join(violations)


def test_retired_cycle_only_exists_under_frozen_test_baseline():
    assert all(not (ROOT / "src/hextech/bootstrap" / (name + ".py")).exists() for name in RETIRED)
    assert all((ROOT / "tests/support/legacy_refresh" / (name + ".py")).is_file() for name in RETIRED)
    for path in (ROOT / "src").rglob("*.py"):
        assert not re.search(r"\bRefreshCycleMixin\b", path.read_text(encoding="utf-8-sig")), path

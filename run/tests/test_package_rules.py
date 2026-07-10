"""测试构建入口和依赖分层规则。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


RUN_DIR = Path(__file__).resolve().parents[1]


def test_build_defaults_to_offline_and_refresh_flag_is_opt_in():
    from tools.build_package import parse_build_args

    assert parse_build_args([]).refresh_data is False
    assert parse_build_args(["--refresh-data"]).refresh_data is True


@pytest.mark.parametrize(
    ("cwd", "script"),
    [
        (RUN_DIR, Path("tools/build_package.py")),
        (RUN_DIR.parent, Path("run/tools/build_package.py")),
    ],
)
def test_build_package_direct_help_is_cwd_independent(cwd: Path, script: Path):
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--refresh-data" in completed.stdout


def test_offline_build_validation_does_not_call_remote_refresh(monkeypatch):
    from tools import build_package
    from hextech.core import refresh

    monkeypatch.setattr(
        refresh,
        "refresh_backend_data",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("offline build must not refresh")),
    )
    monkeypatch.setattr(
        build_package,
        "validate_hextech_seed_health",
        lambda _base: {
            "valid": True,
            "filename": "Hextech_Data_2026-07-05.csv",
            "mtime": "2026-07-05T00:00:00",
            "rows": 1000,
            "unique_heroes": 20,
        },
    )

    build_package.prepare_runtime_data_for_package(refresh_data=False)


@pytest.mark.parametrize("state", ["degraded", "failed"])
def test_explicit_refresh_rejects_non_ready_result(monkeypatch, state):
    from tools import build_package
    from hextech.core import refresh

    monkeypatch.setattr(
        refresh,
        "refresh_backend_data",
        lambda **_kwargs: SimpleNamespace(state=state, reason_code=f"{state}_reason"),
    )
    monkeypatch.setattr(build_package, "validate_hextech_seed_health", lambda _base: {"valid": True})

    with pytest.raises(RuntimeError, match=state):
        build_package.prepare_runtime_data_for_package(refresh_data=True)


def test_dependency_files_split_runtime_build_and_dev_tools():
    compatibility = (RUN_DIR / "requirements.txt").read_text(encoding="utf-8")
    runtime = (RUN_DIR / "requirements-runtime.txt").read_text(encoding="utf-8")
    build = (RUN_DIR / "requirements-build.txt").read_text(encoding="utf-8")
    dev = (RUN_DIR / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "-r requirements-build.txt" in compatibility
    assert "pyinstaller" not in runtime.lower()
    assert "-r requirements-runtime.txt" in build
    assert "pyinstaller" in build.lower()
    assert "-r requirements-build.txt" in dev
    for package in ("ruff", "pyright", "coverage", "pytest-cov"):
        assert package not in runtime.lower()
        assert package not in build.lower()
        assert f"{package}==" in dev.lower()


def test_pyproject_defines_python_quality_tool_boundaries():
    pyproject = (RUN_DIR / "pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.ruff]' in pyproject
    assert 'target-version = "py311"' in pyproject
    assert '[tool.pyright]' in pyproject
    assert 'pythonVersion = "3.11"' in pyproject
    assert '[tool.coverage.run]' in pyproject

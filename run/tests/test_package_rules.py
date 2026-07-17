"""测试构建入口和依赖分层规则。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


RUN_DIR = Path(__file__).resolve().parents[1]


def test_build_defaults_to_offline_and_refresh_flag_is_opt_in():
    from tooling.build.package import parse_build_args

    assert parse_build_args([]).refresh_data is False
    assert parse_build_args(["--refresh-data"]).refresh_data is True


def test_build_accepts_existing_verified_snapshot_root(tmp_path):
    from tooling.build.package import parse_build_args

    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()

    args = parse_build_args(["--verified-snapshot-root", str(snapshot_root)])

    assert args.refresh_data is False
    assert args.verified_snapshot_root == snapshot_root.resolve()


@pytest.mark.parametrize(
    ("cwd", "script"),
    [
        (RUN_DIR, Path("tooling/build/package.py")),
        (RUN_DIR.parent, Path("run/tooling/build/package.py")),
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
    from tooling.build import package as build_package
    from hextech.bootstrap import data_refresh as refresh

    monkeypatch.setattr(
        refresh,
        "refresh_backend_data",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("offline build must not refresh")),
    )
    monkeypatch.setattr(
        build_package,
        "validate_snapshot_seed",
        lambda _base: {
            "valid": True,
            "generation_id": "g1",
            "champion_count": 173,
            "augment_count": 204,
            "stat_record_count": 24910,
        },
    )

    build_package.prepare_runtime_data_for_package(refresh_data=False)


@pytest.mark.parametrize("state", ["degraded", "failed"])
def test_explicit_refresh_rejects_non_ready_result(monkeypatch, state):
    from tooling.build import package as build_package
    from hextech.bootstrap import data_refresh as refresh

    monkeypatch.setattr(
        refresh,
        "refresh_backend_data",
        lambda **_kwargs: SimpleNamespace(state=state, reason_code=f"{state}_reason"),
    )
    monkeypatch.setattr(build_package, "validate_snapshot_seed", lambda _base: {"valid": True})

    with pytest.raises(RuntimeError, match=state):
        build_package.prepare_runtime_data_for_package(refresh_data=True)


def test_dependency_files_split_runtime_build_and_dev_tools():
    requirements_dir = RUN_DIR / "tooling" / "requirements"
    runtime = (requirements_dir / "runtime.txt").read_text(encoding="utf-8")
    build = (requirements_dir / "build.txt").read_text(encoding="utf-8")
    dev = (requirements_dir / "dev.txt").read_text(encoding="utf-8")

    assert "pyinstaller" not in runtime.lower()
    assert "-r runtime.txt" in build
    assert "pyinstaller" in build.lower()
    assert "-r build.txt" in dev
    for package in ("ruff", "pyright", "coverage", "pytest-cov"):
        assert package not in runtime.lower()
        assert package not in build.lower()
        assert f"{package}==" in dev.lower()


def test_portable_launcher_is_ascii_crlf_and_discovers_single_root_exe(tmp_path):
    from tooling.build.package import write_portable_launcher

    launcher = write_portable_launcher(tmp_path)
    content = launcher.read_bytes()
    assert b"\r\r\n" not in content
    assert content.splitlines()[0] == b"@echo off"
    assert b"for %%F in (*.exe)" in content
    assert b"Hextech" not in content
    content.decode("ascii")


def test_packaged_smoke_requires_unique_root_exe_and_bat(tmp_path):
    from tooling.acceptance.smoke_packaged_startup import SmokeFailure, _find_exe, _find_launcher

    (tmp_path / "Hextech.exe").write_bytes(b"exe")
    (tmp_path / "start.bat").write_bytes(b"@echo off\r\n")
    assert _find_exe(tmp_path).name == "Hextech.exe"
    assert _find_launcher(tmp_path).name == "start.bat"

    (tmp_path / "extra.exe").write_bytes(b"exe")
    with pytest.raises(SmokeFailure, match="一个根 exe"):
        _find_exe(tmp_path)
    (tmp_path / "start.bat").unlink()
    with pytest.raises(SmokeFailure, match="一个根 BAT"):
        _find_launcher(tmp_path)


def test_pyinstaller_collects_scraping_package_data():
    from tooling.build.package import PYINSTALLER_COLLECT_DATA, REQUIRED_PACKAGED_SCRAPING_DATA

    assert {"scrapling", "browserforge", "apify_fingerprint_datapoints"} <= set(PYINSTALLER_COLLECT_DATA)
    assert "input-network-definition.zip" in REQUIRED_PACKAGED_SCRAPING_DATA


def test_package_entries_include_verified_snapshot_files(tmp_path):
    from tooling.build.rules import iter_package_data_entries

    snapshot_root = tmp_path / "verified"
    generation_dir = snapshot_root / "generations" / "g1"
    generation_dir.mkdir(parents=True)
    (snapshot_root / "current.v1.json").write_text('{"current_generation_id":"g1"}', encoding="utf-8")
    (generation_dir / "manifest.json").write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "bundle_manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    entries = iter_package_data_entries(
        tmp_path,
        manifest_path,
        verified_snapshot_root=snapshot_root,
    )
    snapshot_entries = [entry for entry in entries if snapshot_root in entry.source.parents]

    assert {entry.source.name for entry in snapshot_entries} == {"current.v1.json", "manifest.json"}
    assert {entry.target for entry in snapshot_entries} == {
        "resources/seeds",
        "resources/seeds/generations/g1",
    }


def test_pyproject_defines_python_quality_tool_boundaries():
    pyproject = (RUN_DIR / "pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.ruff]' in pyproject
    assert 'target-version = "py311"' in pyproject
    assert '[tool.pyright]' in pyproject
    assert 'pythonVersion = "3.11"' in pyproject
    assert '[tool.coverage.run]' in pyproject

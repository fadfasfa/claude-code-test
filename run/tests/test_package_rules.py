"""测试构建入口和依赖分层规则。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
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


def test_stable_build_module_help_uses_current_python_environment_module() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "tooling.build", "--help"],
        cwd=RUN_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--refresh-data" in completed.stdout


def test_offline_build_validation_does_not_call_remote_refresh(monkeypatch):
    from tooling.build import package as build_package
    from hextech.bootstrap import refresh_once

    monkeypatch.setattr(
        refresh_once,
        "refresh_runtime_once",
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
    from hextech.bootstrap import refresh_once

    monkeypatch.setattr(
        refresh_once,
        "refresh_runtime_once",
        lambda **_kwargs: {"state": state, "reason_code": f"{state}_reason"},
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
    from tooling.build.resource_manifest import write_resource_manifest
    from tooling.build.rules import iter_package_data_entries

    snapshot_root = tmp_path / "verified"
    generation_dir = snapshot_root / "generations" / "g1"
    generation_dir.mkdir(parents=True)
    (snapshot_root / "current.v2.json").write_text('{"current_generation_id":"g1"}', encoding="utf-8")
    (generation_dir / "manifest.json").write_text("{}", encoding="utf-8")
    seed_root = tmp_path / "resources" / "seeds"
    seed_generation = seed_root / "generations" / "g1"
    seed_generation.mkdir(parents=True)
    (seed_root / "current.v2.json").write_bytes((snapshot_root / "current.v2.json").read_bytes())
    (seed_generation / "manifest.json").write_bytes((generation_dir / "manifest.json").read_bytes())
    write_resource_manifest(tmp_path)
    manifest_path = tmp_path / "bundle_manifest.json"
    verified_files = [snapshot_root / "current.v2.json", generation_dir / "manifest.json"]
    seed_sha256 = {
        f"resources/seeds/{path.relative_to(snapshot_root).as_posix()}": hashlib.sha256(path.read_bytes()).hexdigest()
        for path in verified_files
    }
    manifest_path.write_text(json.dumps({"seed_sha256": seed_sha256}), encoding="utf-8")

    entries = iter_package_data_entries(
        tmp_path,
        manifest_path,
        verified_snapshot_root=snapshot_root,
    )
    snapshot_entries = [entry for entry in entries if snapshot_root in entry.source.parents]

    assert {entry.source.name for entry in snapshot_entries} == {"current.v2.json", "manifest.json"}
    assert {entry.target for entry in snapshot_entries} == {
        "resources/seeds",
        "resources/seeds/generations/g1",
    }


def test_observed_name_exemplars_are_in_the_package_whitelist():
    from tooling.build.resource_manifest import validate_resource_manifest

    packaged = set(validate_resource_manifest(RUN_DIR)["packaged_files"])
    assert {
        "resources/assets/vision/name_exemplars/aram_dawnbringersresolve__20260721.png",
        "resources/assets/vision/name_exemplars/aram_yowchmycoins__20260721.png",
    } <= packaged


def test_package_entries_exclude_unlisted_png(tmp_path):
    from tooling.build.resource_manifest import write_resource_manifest
    from tooling.build.rules import iter_package_data_entries, stage_package_data_tree

    asset_dir = tmp_path / "resources" / "assets" / "champions"
    asset_dir.mkdir(parents=True)
    listed = asset_dir / "listed.png"
    listed.write_bytes(b"listed")
    write_resource_manifest(tmp_path)
    unlisted = asset_dir / "unlisted.png"
    unlisted.write_bytes(b"unlisted")
    bundle_manifest = tmp_path / "bundle_manifest.json"
    bundle_manifest.write_text("{}", encoding="utf-8")

    entries = iter_package_data_entries(tmp_path, bundle_manifest)
    sources = {entry.source.resolve() for entry in entries}

    assert listed.resolve() in sources
    assert unlisted.resolve() not in sources

    staged = stage_package_data_tree(entries, tmp_path / "clean-package-data")
    assert staged.target == "."
    assert (staged.source / "resources" / "assets" / "champions" / "listed.png").is_file()
    assert not (staged.source / "resources" / "assets" / "champions" / "unlisted.png").exists()


def test_pyproject_defines_python_quality_tool_boundaries():
    pyproject = (RUN_DIR / "pyproject.toml").read_text(encoding="utf-8")

    assert '[tool.ruff]' in pyproject
    assert 'target-version = "py311"' in pyproject
    assert '[tool.pyright]' in pyproject
    assert 'pythonVersion = "3.11"' in pyproject
    assert '[tool.coverage.run]' in pyproject

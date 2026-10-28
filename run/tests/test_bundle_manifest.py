"""测试 bundle manifest 生成器。

调用方: pytest; 关键依赖: tooling.build.manifest。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest


def test_package_builder_rejects_pyinstaller_from_another_python(monkeypatch):
    from tooling.build import package as build_package

    monkeypatch.setattr(
        build_package.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(RuntimeError, match="禁止回退到 PATH"):
        build_package.resolve_pyinstaller_command()


def test_manifest_forbidden_paths_are_matched_by_path_parts():
    from tooling.build.manifest import manifest_contains_forbidden_path

    manifest = {
        "source_files": [
            "hextech/metadata-cache/runtime-note.json",
            "hextech/cache/data-runtime-summary.py",
            "__pycache__",
            "hextech/__pycache__/module.cpython-311.pyc",
        ],
        "runtime_files": ["var/cache/example.json"],
    }

    assert manifest_contains_forbidden_path(manifest, "var/cache")
    assert manifest_contains_forbidden_path(manifest, "__pycache__")
    assert manifest_contains_forbidden_path(manifest, ".pyc")
    assert not manifest_contains_forbidden_path(manifest, "runtime/report")
    assert not manifest_contains_forbidden_path(manifest, "data" + "/raw")


def test_validate_bundle_manifest_rejects_empty_critical_fields():
    from tooling.build.manifest import validate_bundle_manifest

    manifest = {
        "catalog_files": ["resources/catalog/英雄目录.v1.json"],
        "asset_files": [],
        "seed_files": [],
        "seed_health": {},
        "source_files": ["src/hextech/bootstrap/desktop.py"],
    }

    try:
        validate_bundle_manifest(manifest)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("关键字段为空的 bundle manifest 必须失败")

    assert "seed_files" in message
    assert "seed_health" in message


def _package_fixture(tmp_path, monkeypatch):
    import shutil
    from tests.support.v4_release import make_release
    from tooling.build.resource_manifest import write_resource_manifest
    base = tmp_path / "project"
    runtime = base / "resources/seeds/runtime.v4"
    published = make_release(runtime, monkeypatch)
    catalog_id = published.source_files[0].catalog_generation_id
    shutil.copytree(runtime / "catalog/generations" / catalog_id, base / "resources/catalog")
    asset = base / "resources/assets/ui/one.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    for name in ("bootstrap/desktop.py", "interfaces/overlay/host.py", "infrastructure/sources/apex/service.py", "infrastructure/sources/mayhem/service.py"):
        source = base / "src/hextech" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# fixture\n", encoding="utf-8")
    write_resource_manifest(base)
    return base, runtime, published


@pytest.mark.parametrize("runtime_profile", ["installed", "isolated_candidate"])
def test_verified_snapshot_seed_is_validated_and_recorded(tmp_path, monkeypatch, runtime_profile):
    from tooling.build import manifest as bundle_manifest
    from tooling.build.rules import iter_package_data_entries
    base, runtime, published = _package_fixture(tmp_path, monkeypatch)
    manifest = bundle_manifest.build_bundle_manifest(base, verified_snapshot_root=runtime / "snapshots",
                                                     runtime_profile=runtime_profile)
    manifest_path = base / "bundle_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    entries = iter_package_data_entries(base, manifest_path, verified_snapshot_root=runtime / "snapshots")
    assert manifest["seed_health"]["generation_id"] == published.generation_id
    assert manifest["schema_version"] == 3
    assert manifest["runtime_profile"] == runtime_profile
    assert manifest["build_id"] and len(manifest["source_fingerprint"]) == 64
    assert manifest["runtime_contracts"] == {"overlay_event": 3, "sidecar_status": 2, "overlay_session_report": 2}
    assert "resources/seeds/runtime.v4/snapshots/current.v4.json" in manifest["seed_files"]
    assert all(len(digest) == 64 for digest in manifest["seed_sha256"].values())
    selected = [entry for entry in entries if entry.target.startswith("resources/seeds/runtime.v4")]
    assert selected
    assert all(runtime.resolve() in entry.source.resolve().parents for entry in selected)

    for invalid in (None, "", "unknown"):
        invalid_manifest = {**manifest, "runtime_profile": invalid}
        with pytest.raises(ValueError, match="runtime_profile"):
            bundle_manifest.validate_bundle_manifest(invalid_manifest)


def test_v4_seed_reports_missing_or_corrupt_manifest_without_editing_existing_state(tmp_path):
    from hextech.infrastructure.persistence.release_seed import initialize_snapshot
    from hextech.modules.data.generation import DataSnapshotPublisher
    state = tmp_path / "runtime/state/startup_status.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"last_error":"existing_failure"}', encoding="utf-8")
    before = state.read_bytes()
    seed = tmp_path / "seed"
    seed.mkdir()
    publisher = DataSnapshotPublisher(tmp_path / "runtime/snapshots")
    assert initialize_snapshot(publisher, seed)["reason_code"] == "v4_seed_unavailable"
    (seed / "snapshots").mkdir()
    (seed / "snapshots/current.v4.json").write_text("{broken", encoding="utf-8")
    assert initialize_snapshot(publisher, seed)["reason_code"] == "v4_seed_unavailable"
    assert state.read_bytes() == before


def test_finalize_output_runs_smoke_before_replacing_existing_release(tmp_path, monkeypatch):
    from tooling.build import package as build_package

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    old_release = releases / "HextechCompanion-20260707"
    old_release.mkdir(parents=True)
    (old_release / "old.txt").write_text("old", encoding="utf-8")
    exe_dir = tmp_path / "pyinstaller-dist" / "Hextech伴生终端"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Hextech伴生终端.exe").write_text("exe", encoding="utf-8")

    monkeypatch.setattr(build_package, "RELEASES_DIR", releases)
    monkeypatch.setattr(build_package, "STAGING_RELEASES_DIR", staging)
    monkeypatch.setattr(build_package, "_release_dir_name", lambda _build_time: "HextechCompanion-20260707")
    monkeypatch.setattr(build_package, "validate_packaged_scraping_data", lambda _package_dir: None)

    def fail_smoke(_package_dir: Path, timeout: int = 210) -> None:
        assert timeout == 210
        raise RuntimeError("smoke failed")

    monkeypatch.setattr(build_package, "run_packaged_smoke", fail_smoke)

    try:
        build_package.finalize_output(exe_dir)
    except RuntimeError as exc:
        assert "smoke failed" in str(exc)
    else:
        raise AssertionError("smoke 失败时 finalize_output 必须失败")

    assert old_release.is_dir()
    assert (old_release / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (releases / "HextechCompanion-20260707.zip").exists()


def test_finalize_output_explicit_release_name_refuses_existing_target(tmp_path, monkeypatch):
    from tooling.build import package as build_package

    releases = tmp_path / "releases"
    existing_release = releases / "HextechCompanion-20260707-stage-stats"
    existing_release.mkdir(parents=True)
    (existing_release / "old.txt").write_text("old", encoding="utf-8")
    exe_dir = tmp_path / "pyinstaller-dist" / "Hextech伴生终端"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Hextech伴生终端.exe").write_text("exe", encoding="utf-8")

    monkeypatch.setattr(build_package, "RELEASES_DIR", releases)

    with pytest.raises(RuntimeError, match="拒绝覆盖"):
        build_package.finalize_output(exe_dir, release_name=existing_release.name)

    assert (existing_release / "old.txt").read_text(encoding="utf-8") == "old"
    assert exe_dir.is_dir()


def test_finalize_output_restores_old_release_when_zip_creation_fails_after_smoke(tmp_path, monkeypatch):
    from tooling.build import package as build_package

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    old_release = releases / "HextechCompanion-20260707"
    old_release.mkdir(parents=True)
    (old_release / "old.txt").write_text("old", encoding="utf-8")
    old_zip = releases / "HextechCompanion-20260707.zip"
    old_zip.write_text("old zip", encoding="utf-8")
    exe_dir = tmp_path / "pyinstaller-dist" / "Hextech伴生终端"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Hextech伴生终端.exe").write_text("exe", encoding="utf-8")

    monkeypatch.setattr(build_package, "RELEASES_DIR", releases)
    monkeypatch.setattr(build_package, "STAGING_RELEASES_DIR", staging)
    monkeypatch.setattr(build_package, "_release_dir_name", lambda _build_time: "HextechCompanion-20260707")
    monkeypatch.setattr(build_package, "validate_packaged_scraping_data", lambda _package_dir: None)
    monkeypatch.setattr(build_package, "run_packaged_smoke", lambda _package_dir, timeout=210: None)
    monkeypatch.setattr(build_package, "create_portable_zip", lambda _final_dir: (_ for _ in ()).throw(RuntimeError("zip failed")))

    try:
        build_package.finalize_output(exe_dir)
    except RuntimeError as exc:
        assert "zip failed" in str(exc)
    else:
        raise AssertionError("zip 创建失败时 finalize_output 必须失败")

    assert old_release.is_dir()
    assert (old_release / "old.txt").read_text(encoding="utf-8") == "old"
    assert old_zip.read_text(encoding="utf-8") == "old zip"


def test_finalize_output_promotes_staging_after_smoke_success(tmp_path, monkeypatch):
    from tooling.build import package as build_package

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    exe_dir = tmp_path / "pyinstaller-dist" / "Hextech伴生终端"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Hextech伴生终端.exe").write_text("exe", encoding="utf-8")
    smoke_calls: list[tuple[Path, int]] = []

    monkeypatch.setattr(build_package, "RELEASES_DIR", releases)
    monkeypatch.setattr(build_package, "STAGING_RELEASES_DIR", staging)
    monkeypatch.setattr(build_package, "_release_dir_name", lambda _build_time: "HextechCompanion-20260707")
    monkeypatch.setattr(build_package, "validate_packaged_scraping_data", lambda _package_dir: None)
    monkeypatch.setattr(build_package, "run_packaged_smoke", lambda package_dir, timeout=210: smoke_calls.append((package_dir, timeout)))

    final_dir, zip_path = build_package.finalize_output(exe_dir)

    assert final_dir == releases / "HextechCompanion-20260707"
    assert (final_dir / "Hextech伴生终端.exe").is_file()
    assert (final_dir / build_package.LAUNCHER_NAME).is_file()
    assert zip_path == releases / "HextechCompanion-20260707.zip"
    assert zip_path.is_file()
    assert smoke_calls and smoke_calls[0][0].parent == staging
    assert smoke_calls[0][1] == 210




def test_packaged_smoke_cleanup_retries_transient_windows_locks(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    smoke_root = tmp_path / "smoke"
    smoke_root.mkdir()
    attempts: list[int] = []

    def delayed_remove(path: Path, *, ignore_errors: bool) -> None:
        del ignore_errors
        attempts.append(1)
        if len(attempts) >= 3:
            path.rmdir()

    monkeypatch.setattr(smoke.shutil, "rmtree", delayed_remove)
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    assert smoke._cleanup_smoke_root(smoke_root, attempts=3)
    assert len(attempts) == 3

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


def test_verified_snapshot_seed_is_validated_and_recorded(tmp_path, monkeypatch):
    from hextech.modules.data.generation import DataSnapshotPublisher
    from tooling.build import manifest as bundle_manifest
    from tooling.build.resource_manifest import write_resource_manifest
    from tooling.build.rules import CATALOG_FILES

    snapshot_root = tmp_path / "resources" / "seeds"
    published = DataSnapshotPublisher(snapshot_root).publish(
        {
            "champions": [{"id": "1", "name": "英雄一"}],
            "champion_hextech": {"英雄一": {"hero_id": "1", "augments": [{"id": "a1"}]}},
            "overlay_hints": {
                "hints": {"a1": {"augment_id": "a1", "name": "强化一"}},
                "name_index": {"a1": "a1", "强化一": "a1"},
            },
            "identities": {
                "schema_version": 2,
                "champions": {"1": "英雄一"},
                "augments": {"a1": "强化一"},
            },
        },
    )
    catalog_dir = tmp_path / "resources" / "catalog"
    catalog_dir.mkdir(parents=True)
    for name in CATALOG_FILES:
        (catalog_dir / name).write_text("{}", encoding="utf-8")
    assets = tmp_path / "resources" / "assets" / "champions"
    assets.mkdir(parents=True)
    (assets / "one.png").write_bytes(b"png")
    write_resource_manifest(tmp_path)
    monkeypatch.setattr(bundle_manifest, "iter_source_files", lambda _base: ["src/hextech/bootstrap/desktop.py"])

    manifest = bundle_manifest.build_bundle_manifest(
        tmp_path,
        verified_snapshot_root=snapshot_root,
    )

    assert manifest["seed_health"]["generation_id"] == published.generation_id
    assert "resources/seeds/current.v2.json" in manifest["seed_files"]
    for relative_name in manifest["seed_files"]:
        assert len(manifest["seed_sha256"][relative_name]) == 64


def test_runtime_bundle_reports_missing_or_corrupt_manifest(tmp_path, monkeypatch):
    from hextech.infrastructure.persistence import runtime_bundle

    state_dir = tmp_path / "runtime" / "state"
    warnings: list[str] = []

    monkeypatch.setattr(runtime_bundle.logger, "warning", lambda message, *args: warnings.append(message % args))
    monkeypatch.setattr(
        runtime_bundle,
        "build_runtime_state_path",
        lambda filename: str(state_dir / filename),
    )

    state_dir.mkdir(parents=True)
    (state_dir / "startup_status.json").write_text(
        json.dumps({"last_error": "remote_failed_local_fallback"}, ensure_ascii=False),
        encoding="utf-8",
    )

    missing_root = tmp_path / "missing_bundle"
    missing_root.mkdir()
    manifest = runtime_bundle._load_bundle_manifest(missing_root)

    status = json.loads((state_dir / "startup_status.json").read_text(encoding="utf-8"))
    assert manifest["seed_files"] == []
    assert status["bundle_manifest"]["status"] == "missing"
    assert status["bundle_manifest"]["warning"] == "bundle_manifest_missing"
    assert status["last_error"] == "remote_failed_local_fallback"
    assert warnings

    corrupt_root = tmp_path / "corrupt_bundle"
    corrupt_root.mkdir()
    (corrupt_root / "bundle_manifest.json").write_text("{broken", encoding="utf-8")
    runtime_bundle._load_bundle_manifest(corrupt_root)

    status = json.loads((state_dir / "startup_status.json").read_text(encoding="utf-8"))
    assert status["bundle_manifest"]["status"] == "error"
    assert status["bundle_manifest"]["warning"] == "bundle_manifest_invalid"
    assert status["last_error"] == "remote_failed_local_fallback"


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

    def fail_smoke(_package_dir: Path, timeout: int = 60) -> None:
        assert timeout == 60
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
    monkeypatch.setattr(build_package, "run_packaged_smoke", lambda _package_dir, timeout=60: None)
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
    monkeypatch.setattr(build_package, "run_packaged_smoke", lambda package_dir, timeout=60: smoke_calls.append((package_dir, timeout)))

    final_dir, zip_path = build_package.finalize_output(exe_dir)

    assert final_dir == releases / "HextechCompanion-20260707"
    assert (final_dir / "Hextech伴生终端.exe").is_file()
    assert (final_dir / build_package.LAUNCHER_NAME).is_file()
    assert zip_path == releases / "HextechCompanion-20260707.zip"
    assert zip_path.is_file()
    assert smoke_calls and smoke_calls[0][0].parent == staging
    assert smoke_calls[0][1] == 60


def test_packaged_smoke_startup_status_uses_runtime_auth_token(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    runtime_root = tmp_path / "runtime"
    state_dir = runtime_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "auth_token.txt").write_text("local-secret", encoding="utf-8")
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_fetch(url: str, timeout: float = 8.0, headers: dict[str, str] | None = None):
        calls.append((url, headers))
        if url.endswith("/api/startup_status"):
            return 200, b'{"hextech_ready":true,"data_snapshot":{"state":"ready","generation_id":"g1"}}'
        if url.endswith("/api/champions"):
            return 200, b'[{"heroName":"Garen","heroId":"86"}]'
        if "/api/champion/" in url:
            return 200, b'{"loading":true,"generation_id":"g1"}'
        if "/api/synergies/" in url:
            return 200, b'{"synergies":[]}'
        if url.endswith(".png"):
            return 200, b"png"
        return 200, b"<html></html>"

    monkeypatch.setattr(smoke, "_fetch", fake_fetch)

    smoke._web_ready("8211", runtime_root)

    startup_call = next(item for item in calls if item[0].endswith("/api/startup_status"))
    assert startup_call[1] == {
        "Origin": "http://127.0.0.1:8211",
        "X-Hextech-Token": "local-secret",
    }


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


def test_packaged_smoke_rejects_web_generation_mismatch():
    from tooling.acceptance.smoke_packaged_startup import _business_ready

    checks = _business_ready(
        {"data_snapshot": {"state": "ready", "generation_id": "g1"}},
        [{"heroId": "1"}],
        {"ready": True, "generation_id": "g2"},
        {"synergies": []},
        {"code": 200, "bytes": 10},
        require_snapshot_status=True,
    )

    assert checks["snapshot_generation_ready"] is True
    assert checks["web_generation_matches_snapshot"] is False


def test_packaged_smoke_allows_runtime_generation_without_verified_seed_status():
    from tooling.acceptance.smoke_packaged_startup import _business_ready

    checks = _business_ready(
        {"hextech_ready": True},
        [{"heroId": "1"}],
        {"ready": True, "generation_id": "runtime-g1"},
        {"synergies": []},
        {"code": 200, "bytes": 10},
    )

    assert checks["snapshot_generation_ready"] is True
    assert checks["web_generation_matches_snapshot"] is True

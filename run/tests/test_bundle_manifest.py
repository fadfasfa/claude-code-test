"""测试 bundle manifest 生成器。

调用方: pytest; 关键依赖: tools.bundle_manifest。
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path


def test_manifest_forbidden_paths_are_matched_by_path_parts():
    from tools.bundle_manifest import manifest_contains_forbidden_path

    manifest = {
        "source_files": [
            "hextech/metadata/runtime-note.json",
            "hextech/cache/data-runtime-summary.py",
            "__pycache__",
            "hextech/__pycache__/module.cpython-311.pyc",
        ],
        "runtime_files": ["data/runtime/cache/example.json"],
    }

    assert manifest_contains_forbidden_path(manifest, "data/runtime")
    assert manifest_contains_forbidden_path(manifest, "__pycache__")
    assert manifest_contains_forbidden_path(manifest, ".pyc")
    assert not manifest_contains_forbidden_path(manifest, "runtime/report")
    assert not manifest_contains_forbidden_path(manifest, "data/raw")


def test_validate_bundle_manifest_rejects_empty_critical_fields():
    from tools.bundle_manifest import validate_bundle_manifest

    manifest = {
        "static_files": ["英雄目录.v1.json"],
        "index_files": [],
        "asset_files": [],
        "hextech_snapshot_files": [],
        "synergy_data_files": ["data/seed/startup/synergy/Champion_Synergy_latest.v1.json"],
        "synergy_data_file": "",
        "source_files": ["hextech_ui.py"],
    }

    try:
        validate_bundle_manifest(manifest)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("关键字段为空的 bundle manifest 必须失败")

    assert "hextech_snapshot_files" in message
    assert "synergy_data_file" in message


def test_hextech_seed_health_requires_valid_snapshot_rows_and_heroes(tmp_path):
    from tools.bundle_manifest import validate_hextech_seed_health

    seed_dir = tmp_path / "data" / "seed" / "startup" / "hextech"
    seed_dir.mkdir(parents=True)
    (seed_dir / "Hextech_Data_2026-07-06.csv").write_text(
        "\n".join(
            [
                "英雄 ID,英雄名称,海克斯名称,英雄胜率,英雄出场率,海克斯胜率,海克斯出场率",
                "1,英雄一,海克斯A,50%,1%,55%,2%",
                "2,英雄二,海克斯B,51%,1%,56%,2%",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = validate_hextech_seed_health(tmp_path, min_rows=2, min_heroes=2)

    assert summary["valid"] is True
    assert summary["path"] == "data/seed/startup/hextech/Hextech_Data_2026-07-06.csv"
    assert summary["filename"] == "Hextech_Data_2026-07-06.csv"
    assert summary["rows"] == 2
    assert summary["unique_heroes"] == 2


def test_bundle_manifest_records_dataset_version_and_sha256_for_mutable_seeds(tmp_path, monkeypatch):
    from tools import bundle_manifest

    hextech = tmp_path / "data" / "seed" / "startup" / "hextech" / "Hextech_Data_2026-07-05.csv"
    synergy_dir = tmp_path / "data" / "seed" / "startup" / "synergy"
    synergy = synergy_dir / "Champion_Synergy_20260519_223505.json"
    latest = synergy_dir / "Champion_Synergy_latest.v1.json"
    hextech.parent.mkdir(parents=True)
    synergy_dir.mkdir(parents=True)
    hextech.write_bytes(b"hextech-seed")
    synergy.write_bytes(b'{"heroes":172}')
    latest.write_text(
        json.dumps({"version": 1, "filename": synergy.name}),
        encoding="utf-8",
    )

    monkeypatch.setattr(bundle_manifest, "iter_hextech_snapshot_files", lambda _base: [hextech])
    monkeypatch.setattr(bundle_manifest, "iter_synergy_data_files", lambda _base: [synergy, latest])
    monkeypatch.setattr(bundle_manifest, "iter_source_files", lambda _base: ["hextech_ui.py"])
    monkeypatch.setattr(bundle_manifest, "STABLE_STATIC_FILES", ("英雄目录.v1.json",))
    monkeypatch.setattr(
        bundle_manifest,
        "validate_hextech_seed_health",
        lambda _base: {"valid": True},
    )
    static_dir = tmp_path / "data" / "static" / "version"
    static_dir.mkdir(parents=True)
    (static_dir / "英雄目录.v1.json").write_text("{}", encoding="utf-8")

    manifest = bundle_manifest.build_bundle_manifest(tmp_path)

    hextech_name = "data/seed/startup/hextech/Hextech_Data_2026-07-05.csv"
    latest_name = "data/seed/startup/synergy/Champion_Synergy_latest.v1.json"
    assert manifest["seed_metadata"][hextech_name] == {
        "dataset_version": "2026-07-05",
        "sha256": hashlib.sha256(b"hextech-seed").hexdigest(),
    }
    assert manifest["seed_metadata"][latest_name] == {
        "dataset_version": "20260519_223505",
        "sha256": hashlib.sha256(latest.read_bytes()).hexdigest(),
    }


def test_validate_bundle_manifest_accepts_manifest_without_seed_metadata():
    from tools.bundle_manifest import validate_bundle_manifest

    validate_bundle_manifest(
        {
            "static_files": ["英雄目录.v1.json"],
            "index_files": ["index.json"],
            "asset_files": [],
            "hextech_snapshot_files": ["data/seed/startup/hextech/Hextech_Data_2026-07-05.csv"],
            "synergy_data_files": ["data/seed/startup/synergy/Champion_Synergy_20260519_223505.json"],
            "synergy_data_file": "data/seed/startup/synergy/Champion_Synergy_20260519_223505.json",
            "source_files": ["hextech_ui.py"],
        }
    )


def test_runtime_bundle_reports_missing_or_corrupt_manifest(tmp_path, monkeypatch):
    from tools import runtime_bundle

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
    assert manifest["hextech_snapshot_files"] == []
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
    from tools import build_package

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
    from tools import build_package

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
    from tools import build_package

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    exe_dir = tmp_path / "pyinstaller-dist" / "Hextech伴生终端"
    exe_dir.mkdir(parents=True)
    (exe_dir / "Hextech伴生终端.exe").write_text("exe", encoding="utf-8")
    smoke_calls: list[tuple[Path, int]] = []

    monkeypatch.setattr(build_package, "RELEASES_DIR", releases)
    monkeypatch.setattr(build_package, "STAGING_RELEASES_DIR", staging)
    monkeypatch.setattr(build_package, "_release_dir_name", lambda _build_time: "HextechCompanion-20260707")
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
    from tools.acceptance import smoke_packaged_startup as smoke

    runtime_root = tmp_path / "runtime"
    state_dir = runtime_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "auth_token.txt").write_text("local-secret", encoding="utf-8")
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_fetch(url: str, timeout: float = 8.0, headers: dict[str, str] | None = None):
        calls.append((url, headers))
        if url.endswith("/api/startup_status"):
            return 200, b'{"hextech_ready":true}'
        if url.endswith("/api/champions"):
            return 200, b'[{"heroName":"Garen","heroId":"86"}]'
        if "/api/champion/" in url:
            return 200, b'{"loading":true}'
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

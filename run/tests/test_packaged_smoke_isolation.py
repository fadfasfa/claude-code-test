from tooling.acceptance import smoke_packaged_startup as smoke

import json
import pytest


def test_smoke_accepts_explicit_v3_units_but_rejects_missing_closure(tmp_path):
    root = tmp_path / "_internal"
    root.mkdir()
    manifest = {"schema_version": smoke.BUNDLE_MANIFEST_SCHEMA_VERSION, "build_id": "test",
                "runtime_contracts": smoke.RUNTIME_CONTRACT_VERSIONS,
                "cohort_seed": {"schema_version": 2, "snapshot_schema_version": 3,
                                "units": {"aramkit/rank": {}}, "production_pool_count": 1},
                "cohort_seed_files": ["test"], "cohort_seed_sha256": {"test": "a" * 64}}
    path = root / "bundle_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert smoke._validate_bundle_contract(tmp_path)["cohort_seed"]["snapshot_schema_version"] == 3
    manifest["cohort_seed"]["units"] = {}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="units"):
        smoke._validate_bundle_contract(tmp_path)


def test_smoke_overrides_parent_runtime_and_disables_remote_auto_refresh(monkeypatch, tmp_path):
    package = tmp_path / "package"
    captured = {}
    monkeypatch.setattr(smoke, "_runtime_environment_conflict", lambda _: "")
    monkeypatch.setenv("HEXTECH_VAR_DIR", str(tmp_path / "unrelated-parent-runtime"))
    monkeypatch.setattr(smoke, "_validate_bundle_contract", lambda _: {"build_id": "test"})
    monkeypatch.setattr(smoke, "_find_exe", lambda _: package / "app.exe")
    monkeypatch.setattr(smoke, "_find_launcher", lambda _: package / "app.bat")
    monkeypatch.setattr(smoke, "_validate_windows_gui_subsystem", lambda _: None)
    monkeypatch.setattr(smoke, "_has_verified_snapshot_seed", lambda _: True)
    monkeypatch.setattr(smoke, "_write_smoke_feature_flags", lambda root: None)

    def reject_before_launch(exe, directory, env, runtime, manifest):
        captured.update(env=env, runtime=runtime)
        raise smoke.SmokeFailure("fixture stops before launch")

    monkeypatch.setattr(smoke, "_acquisition_worker_import_smoke", reject_before_launch)
    result = smoke.run_smoke(package, 1)
    assert not result["ok"]
    assert captured["env"]["HEXTECH_VAR_DIR"] == str(captured["runtime"])
    assert captured["runtime"] == tmp_path / "appdata-clean" / "Local" / "HextechNexus" / "var"
    assert captured["env"]["HEXTECH_DATA_SERVICE_SKIP_AUTO_REFRESH"] == "1"


def test_smoke_refuses_live_game_before_launch_or_runtime_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "_validate_bundle_contract", lambda _: {"build_id": "test"})
    monkeypatch.setattr(smoke, "_find_exe", lambda _: tmp_path / "app.exe")
    monkeypatch.setattr(smoke, "_runtime_environment_conflict", lambda _: "real_game_active_smoke_requires_idle_environment")
    monkeypatch.setattr(smoke, "_write_smoke_feature_flags", lambda _: pytest.fail("runtime write while blocked"))
    result = smoke.run_smoke(tmp_path, 1)
    assert not result["ok"] and result["blocked_reason"].startswith("real_game_active")


def test_existing_smoke_evidence_is_never_deleted(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir()
    previous = root / "failure.json"
    previous.write_text("preserve", encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="已有证据"):
        smoke._copy_clean_package(tmp_path / "package", root)
    assert previous.read_text(encoding="utf-8") == "preserve"


def test_v3_runtime_paths_require_declared_pointers_not_absent_optional(tmp_path):
    package = tmp_path / "package"
    data = package / "_internal"
    seed = data / "resources" / "cohort-seed"
    (seed / "catalog").mkdir(parents=True)
    (seed / "catalog" / "current.v2.json").write_text("{}", encoding="utf-8")
    (seed / "sources" / "apex").mkdir(parents=True)
    (seed / "sources" / "apex" / "current.v2.json").write_text("{}", encoding="utf-8")
    (data / "bundle_manifest.json").write_text(json.dumps({"cohort_seed": {
        "schema_version": 2, "snapshot_schema_version": 3, "units": {"aramkit/rank": {}}
    }}), encoding="utf-8")
    checks = smoke._required_paths_ready(package, tmp_path / "runtime", 0)
    assert "runtime:sources/blitz/current.v2.json" not in checks
    assert checks["runtime:sources/apex/current.v2.json"] is False
    assert checks["runtime:catalog/current.v2.json"] is False

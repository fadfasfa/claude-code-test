from tooling.acceptance import smoke_packaged_startup as smoke

import json
import pytest


def test_smoke_accepts_explicit_v3_units_but_rejects_missing_closure(tmp_path):
    root = tmp_path / "_internal"
    root.mkdir()
    manifest = {"schema_version": smoke.BUNDLE_MANIFEST_SCHEMA_VERSION, "build_id": "test",
                "runtime_profile": "installed",
                "runtime_contracts": smoke.RUNTIME_CONTRACT_VERSIONS,
                "cohort_seed": {"schema_version": 3, "snapshot_schema_version": 4,
                                "units": {"aramkit/rank": {}}, "production_pool_count": 1},
                "cohort_seed_files": ["test"], "cohort_seed_sha256": {"test": "a" * 64}}
    path = root / "bundle_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert smoke._validate_bundle_contract(tmp_path)["cohort_seed"]["snapshot_schema_version"] == 4
    manifest["cohort_seed"]["units"] = {}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="units"):
        smoke._validate_bundle_contract(tmp_path)


def test_smoke_accepts_dual_catalog_identity_and_rejects_empty_recognition_id(tmp_path):
    root = tmp_path / "_internal"
    root.mkdir()
    manifest = {
        "schema_version": smoke.BUNDLE_MANIFEST_SCHEMA_VERSION,
        "build_id": "test",
        "runtime_profile": "installed",
        "runtime_contracts": smoke.RUNTIME_CONTRACT_VERSIONS,
        "cohort_seed": {
            "schema_version": 3,
            "snapshot_schema_version": 4,
            "catalog_generation_id": "catalog-statistics",
            "recognition_catalog_generation_id": "catalog-recognition",
            "units": {"aramkit/rank": {}},
            "production_pool_count": 1,
        },
        "cohort_seed_files": ["test"],
        "cohort_seed_sha256": {"test": "a" * 64},
    }
    path = root / "bundle_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    cohort = smoke._validate_bundle_contract(tmp_path)["cohort_seed"]
    assert cohort["catalog_generation_id"] == "catalog-statistics"
    assert cohort["recognition_catalog_generation_id"] == "catalog-recognition"

    manifest["cohort_seed"]["recognition_catalog_generation_id"] = ""
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="recognition Catalog"):
        smoke._validate_bundle_contract(tmp_path)


def test_smoke_overrides_parent_runtime_and_disables_remote_auto_refresh(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "real-local"))
    package = tmp_path / "package"
    captured = {}
    monkeypatch.setattr(smoke, "_runtime_environment_conflict", lambda _: "")
    monkeypatch.setenv("HEXTECH_VAR_DIR", str(tmp_path / "unrelated-parent-runtime"))
    monkeypatch.setattr(smoke, "_validate_bundle_contract", lambda _: {"build_id": "test", "runtime_profile": "installed"})
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
    assert "HEXTECH_VAR_DIR" not in captured["env"]
    assert captured["runtime"] == tmp_path / "appdata-clean" / "Local" / "HextechNexus" / "var"
    assert captured["env"]["HEXTECH_APP_SKIP_AUTO_REFRESH"] == "1"


@pytest.mark.parametrize("profile", [None, "", "portable", "candidate"])
def test_bundle_rejects_missing_or_unknown_runtime_profile(tmp_path, profile):
    manifest = {"schema_version": smoke.BUNDLE_MANIFEST_SCHEMA_VERSION, "build_id": "test",
                "runtime_contracts": smoke.RUNTIME_CONTRACT_VERSIONS, "runtime_profile": profile}
    (tmp_path / "bundle_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(smoke.SmokeFailure, match="runtime_profile"):
        smoke._validate_bundle_contract(tmp_path)


def test_candidate_preserves_real_appdata_and_ignores_parent_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "real-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "real-roaming"))
    monkeypatch.setenv("HEXTECH_VAR_DIR", str(tmp_path / "unrelated-parent-runtime"))
    package = tmp_path / "candidate"
    env, runtime = smoke._smoke_runtime_environment(package, "clean", {"runtime_profile": "isolated_candidate"})
    assert env["LOCALAPPDATA"] == str(tmp_path / "real-local")
    assert env["APPDATA"] == str(tmp_path / "real-roaming")
    assert "HEXTECH_VAR_DIR" not in env
    assert runtime == package.resolve() / "var"


@pytest.mark.parametrize("actual", ["", "wrong"])
def test_candidate_rejects_worker_with_missing_or_wrong_runtime_root(monkeypatch, tmp_path, actual):
    runtime = tmp_path / "package" / "var"
    result_path = runtime / "state" / "acquisition_worker_self_check.v1.json"
    payload = {"state": "ready", "build_id": "test", "runtime_profile": "isolated_candidate",
               "runtime_root": str(tmp_path / "formal") if actual else "",
               "checks": {name: "ready" for name in (
                   "curl_cffi._wrapper", "hextech.infrastructure.sources.aramkit.service",
                   "hextech.infrastructure.sources.catalog_versioned")}}

    def fake_worker(command, **_kwargs):
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return smoke.subprocess.CompletedProcess(command, 0, stdout=b"")

    monkeypatch.setattr(smoke.subprocess, "run", fake_worker)
    with pytest.raises(smoke.SmokeFailure, match="runtime_root"):
        smoke._acquisition_worker_import_smoke(tmp_path / "app.exe", tmp_path, {}, runtime,
                                              {"build_id": "test", "runtime_profile": "isolated_candidate"})


def test_candidate_runtime_var_is_allowed_only_beside_exe(tmp_path):
    package = tmp_path / "package"
    runtime = package / "var"
    runtime.mkdir(parents=True)
    (package / "_internal").mkdir()
    checks = smoke._required_paths_ready(package, runtime, 0)
    assert "package:var absent" not in checks
    assert checks["_internal:var absent"] is True
    assert checks["runtime:data absent"] is True
    installed_checks = smoke._required_paths_ready(package, tmp_path / "sandbox", 0)
    assert installed_checks["package:var absent"] is False


def test_candidate_chain_requires_each_process_actual_runtime_root(monkeypatch, tmp_path):
    runtime = tmp_path / "candidate" / "var"
    state = runtime / "state"
    state.mkdir(parents=True)
    for name in ("startup_timing.v1.json", "game_overlay_visibility.v1.json", "game_overlay_sidecar_status.json"):
        (state / name).write_text(json.dumps({"runtime_root": str(runtime)}), encoding="utf-8")
    monkeypatch.setattr(smoke, "_process_alive", lambda _: False)
    checks, _ = smoke._overlay_chain_status(runtime, {"runtime_profile": "isolated_candidate"},
                                             desktop_pid=1, started_at_wall=0)
    assert all(checks[f"{role}_runtime_root"] for role in ("app", "host", "sidecar"))
    (state / "game_overlay_visibility.v1.json").write_text(json.dumps({"runtime_root": str(tmp_path / "formal")}), encoding="utf-8")
    checks, _ = smoke._overlay_chain_status(runtime, {"runtime_profile": "isolated_candidate"},
                                             desktop_pid=1, started_at_wall=0)
    assert checks["host_runtime_root"] is False


def test_candidate_refuses_existing_fixture_runtime_before_starting_processes(monkeypatch, tmp_path):
    package = tmp_path / "package"
    (package / "var").mkdir(parents=True)
    monkeypatch.setattr(smoke, "_find_exe", lambda _: package / "app.exe")
    monkeypatch.setattr(smoke, "_find_launcher", lambda _: package / "app.bat")
    monkeypatch.setattr(smoke, "_validate_windows_gui_subsystem", lambda _: None)
    monkeypatch.setattr(smoke, "_write_smoke_feature_flags", lambda _: pytest.fail("mutated existing evidence"))
    with pytest.raises(smoke.SmokeFailure, match="runtime 已存在"):
        smoke._run_smoke_fixture(package, 1, fixture="clean", bundle_manifest={"runtime_profile": "isolated_candidate"})


def test_formal_metadata_snapshot_never_reads_file_content(monkeypatch, tmp_path):
    root = tmp_path / "formal"
    root.mkdir()
    protected = root / "private.json"
    protected.write_text("must not read", encoding="utf-8")
    monkeypatch.setattr(smoke.Path, "open", lambda *_a, **_kw: pytest.fail("content read"))
    snapshot = smoke._runtime_metadata_snapshot(root)
    assert snapshot["private.json"][1] == len("must not read")


def test_formal_runtime_mutation_fails_even_when_fixture_reports_success(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "real-local"))
    monkeypatch.setattr(smoke, "_validate_bundle_contract", lambda _: {"runtime_profile": "isolated_candidate"})
    monkeypatch.setattr(smoke, "_find_exe", lambda _: tmp_path / "app.exe")
    monkeypatch.setattr(smoke, "_runtime_environment_conflict", lambda _: "")
    protected = smoke._get_packaged_runtime_root()

    def bad_fixture(*_a, **_kw):
        protected.mkdir(parents=True)
        (protected / "unexpected.log").write_text("bad", encoding="utf-8")
        return {"ok": True}

    monkeypatch.setattr(smoke, "_run_smoke_fixture", bad_fixture)
    result = smoke.run_smoke(tmp_path / "package", 1)
    assert result["ok"] is False
    assert result["formal_runtime_unchanged"] is False
    assert "unexpected.log" in result["formal_runtime_metadata"]["changed_paths"]


def test_formal_metadata_check_covers_early_smoke_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "real-local"))
    monkeypatch.setattr(smoke, "_validate_bundle_contract", lambda _: {"runtime_profile": "installed"})
    monkeypatch.setattr(smoke, "_find_exe", lambda _: tmp_path / "app.exe")
    monkeypatch.setattr(smoke, "_runtime_environment_conflict", lambda _: "")

    def failure(*_a, **_kw):
        raise smoke.SmokeFailure("before launch")

    monkeypatch.setattr(smoke, "_run_smoke_fixture", failure)
    result = smoke.run_smoke(tmp_path / "package", 1)
    assert not result["ok"] and result["formal_runtime_unchanged"] is True
    assert result["last_error"] == "before launch"


def test_main_uses_three_independent_package_copies(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.exe").write_bytes(b"fake")
    root = tmp_path / "smoke"
    monkeypatch.setattr(smoke.sys, "argv", ["smoke", "--package-dir", str(source), "--smoke-root", str(root), "--keep"])
    observed = []

    def fixture(package, _timeout, *, fixture):
        assert not (package / "var").exists()
        (package / "var").mkdir()
        observed.append(package)
        return {"ok": True}

    monkeypatch.setattr(smoke, "run_smoke", fixture)
    assert smoke.main() == 0
    assert len(set(observed)) == 3
    assert {path.parent.name for path in observed} == {"clean", "stale_sidecar", "populated_runtime"}


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




def test_v4_runtime_paths_require_only_the_single_release_head(tmp_path):
    package = tmp_path / "package"
    data = package / "_internal"
    seed = data / "resources/seeds/runtime.v4/snapshots"
    seed.mkdir(parents=True)
    (seed / "current.v4.json").write_text("{}", encoding="utf-8")
    checks = smoke._required_paths_ready(package, tmp_path / "runtime", 0)
    assert checks["package:v4_cohort_head"] is True
    assert checks["runtime:snapshots/current.v4.json"] is False
    assert not any("current.v2.json" in key for key in checks)

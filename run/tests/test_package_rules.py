"""测试构建入口和依赖分层规则。"""
from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import time
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


def test_build_accepts_safe_release_name_and_rejects_path_like_names():
    from tooling.build.package import parse_build_args

    release_name = "HextechCompanion-20260815T191500-stage-stats"
    assert parse_build_args(["--release-name", release_name]).release_name == release_name

    for invalid in ("HextechCompanion-stage-stats", "../HextechCompanion-20260815", "other-20260815"):
        with pytest.raises(SystemExit):
            parse_build_args(["--release-name", invalid])


def test_build_accepts_isolated_artifact_and_smoke_roots(tmp_path):
    from tooling.build.package import parse_build_args

    args = parse_build_args(
        [
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
            "--smoke-root",
            str(tmp_path / "smoke"),
        ]
    )

    assert args.artifacts_dir == (tmp_path / "artifacts").resolve()
    assert args.smoke_root == (tmp_path / "smoke").resolve()


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
    import os
    completed = subprocess.run(
        [sys.executable, "-m", "tooling.build", "--help"],
        cwd=RUN_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env={**os.environ, "HEXTECH_PYTHON311": sys.executable},
        timeout=30,
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

    assert build_package.prepare_runtime_data_for_package(refresh_data=False) == (
        build_package.BASE_DIR / "resources" / "seeds"
    ).resolve()


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


def test_explicit_refresh_packages_the_refreshed_runtime_snapshot(monkeypatch, tmp_path):
    from tooling.build import package as build_package
    from hextech.bootstrap import refresh_once
    from hextech.modules.data import generation

    snapshot_root = tmp_path / "var" / "snapshots"
    snapshot_root.mkdir(parents=True)
    monkeypatch.setattr(refresh_once, "refresh_runtime_once", lambda **_kwargs: {"state": "ready"})
    monkeypatch.setattr(generation, "default_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr(
        build_package,
        "validate_snapshot_seed",
        lambda root: {
            "valid": True,
            "generation_id": "g1",
            "champion_count": 173,
            "augment_count": 198,
            "stat_record_count": 9929,
        }
        if root == snapshot_root.resolve()
        else (_ for _ in ()).throw(AssertionError("refresh build validated stale resources/seeds")),
    )

    assert build_package.prepare_runtime_data_for_package(refresh_data=True) == snapshot_root.resolve()


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


def test_packaged_smoke_rejects_console_subsystem(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    exe = tmp_path / "Hextech.exe"
    payload = bytearray(512)
    payload[0:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    subsystem_offset = 0x80 + 4 + 20 + 68
    struct.pack_into("<H", payload, subsystem_offset, 3)
    exe.write_bytes(payload)
    monkeypatch.setattr(smoke.os, "name", "nt")

    with pytest.raises(smoke.SmokeFailure, match="GUI subsystem"):
        smoke._validate_windows_gui_subsystem(exe)

    struct.pack_into("<H", payload, subsystem_offset, 2)
    exe.write_bytes(payload)
    smoke._validate_windows_gui_subsystem(exe)


def test_packaged_overlay_self_check_reads_tokenized_file(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    expected = {
        "ok": True,
        "window_probe_ok": True,
        "context_contract_ok": True,
        "visibility_contract_ok": True,
        "lcu_scanner_configured": True,
        "overlay_event_contract_ok": True,
        "sidecar_status_contract_ok": True,
        "session_report_contract_ok": True,
        "runtime_contracts": smoke.RUNTIME_CONTRACT_VERSIONS,
        "build_id": "build-test",
    }

    class Completed:
        returncode = 0
        stdout = b""

    def fake_run(_command, *, env, **_kwargs):
        payload = dict(expected)
        payload["token"] = env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"]
        Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"]).write_text(json.dumps(payload), encoding="utf-8")
        return Completed()

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)
    result = smoke._overlay_self_check(
        tmp_path / "Hextech.exe",
        tmp_path,
        {"HEXTECH_EXPECTED_BUILD_ID": "build-test"},
    )

    assert result["build_id"] == "build-test"
    assert result["runtime_contracts"] == smoke.RUNTIME_CONTRACT_VERSIONS
    assert "token" not in result


def test_packaged_presentation_smoke_requires_host_surface_and_capture_exclusion(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    expected = {
        "ok": True,
        "build_id": "build-test",
        "checks": {
            "fullscreen_blocked": True,
            "fullscreen_probe_not_run": True,
            "fullscreen_presented_at_absent": True,
            "borderless_resumed": True,
            "three_ready_rows": True,
            "canvas_items": True,
            "hwnd_mapped": True,
            "rect_matches": True,
            "style_checks": True,
            "not_cloaked": True,
            "capture_exclusion_applied": True,
            "host_surface_content": True,
            "composition_excluded": True,
            "desktop_capture_excluded": True,
        },
        "presentation": {
            "state": "composed",
            "composition_probe": {"state": "excluded", "sample_count": 0},
        },
        "capture_exclusion_probe": {"state": "excluded", "sample_count": 3,
                                    "background_matched_count": 3,
                                    "probe_contract": "mss_capture_exclusion_v1"},
    }

    class Completed:
        returncode = 0
        stdout = b""

    def fake_run(_command, *, env, **_kwargs):
        payload = dict(expected)
        payload["token"] = env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"]
        Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"]).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return Completed()

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)
    result = smoke._overlay_presentation_smoke(
        tmp_path / "Hextech.exe",
        tmp_path,
        {"HEXTECH_EXPECTED_BUILD_ID": "build-test"},
    )

    assert result["presentation"]["state"] == "composed"
    assert result["presentation"]["composition_probe"]["state"] == "excluded"
    assert result["capture_exclusion_probe"]["state"] == "excluded"
    expected["capture_exclusion_probe"]["probe_contract"] = "pil_imagegrab_capture_exclusion_v1"
    with pytest.raises(smoke.SmokeFailure):
        smoke._overlay_presentation_smoke(tmp_path / "Hextech.exe", tmp_path,
                                         {"HEXTECH_EXPECTED_BUILD_ID": "build-test"})


def test_packaged_diagnostic_retention_smoke_requires_two_clean_cycles(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    expected = {
        "ok": True,
        "build_id": "build-test",
        "checks": {
            "legacy_v1_retained": True,
            "v2_retained": True,
            "terminal_marker_retained": True,
            "writer_append_confirmed": True,
            "writer_no_failure": True,
            "first_retention_completed": True,
            "second_retention_completed": True,
        },
    }

    class Completed:
        returncode = 0
        stdout = b""

    def fake_run(_command, *, env, **_kwargs):
        payload = {**expected, "token": env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"]}
        Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"]).write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return Completed()

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)
    result = smoke._diagnostic_retention_smoke(
        tmp_path / "Hextech.exe",
        tmp_path,
        {"HEXTECH_EXPECTED_BUILD_ID": "build-test"},
    )

    assert result["checks"]["legacy_v1_retained"] is True
    assert "token" not in result


def test_packaged_overlay_chain_rejects_stale_sidecar_then_accepts_current_build(tmp_path, monkeypatch):
    from tooling.acceptance import smoke_packaged_startup as smoke

    runtime_root = tmp_path / "var"
    state_dir = runtime_root / "state"
    report_dir = runtime_root / "reports" / "overlay_sessions"
    state_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)
    started_at = time.time() - 1.0
    manifest = {
        "build_id": "build-current",
        "cohort_seed": {"generation_id": "generation-current"},
    }
    (state_dir / "startup_status.json").write_text(
        json.dumps(
            {
                "data_snapshot": {
                    "state": "ready",
                    "generation_id": "generation-current",
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "data-service").mkdir(parents=True, exist_ok=True)
    (state_dir / "data-service" / "cohort_selection.v1.json").write_text(
        json.dumps({"schema_version": 2, "writer_role": "desktop_seed"}),
        encoding="utf-8",
    )
    (state_dir / "game_overlay_visibility.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build_id": "build-current",
                "pid": 101,
                "data_generation_id": "generation-current",
                "stats_generation_id": "generation-current",
                "vision_pool_generation_id": "generation-current",
                "generation_roles": {
                    "vision_pool_generation_id": "sidecar_template_runtime",
                    "stats_generation_id": "host_game_session",
                },
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )
    (report_dir / "latest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build_id": "build-current",
                "recorded_at": time.time(),
                "generation_id": "",
                "stats_generation_id": "generation-current",
                "vision_pool_generation_id": "generation-current",
                "generation_roles": {
                    "vision_pool_generation_id": "sidecar_template_runtime",
                    "stats_generation_id": "host_game_session",
                },
                "stats_scope": {
                    "status": "unavailable",
                    "stage_context": {"status": "unknown"},
                },
            }
        ),
        encoding="utf-8",
    )
    events = [
        {
            "event": "game_overlay.started",
            "build_id": "build-current",
            "correlation_id": "action-1",
        },
        {
            "event": "game_overlay.completed",
            "build_id": "build-current",
            "correlation_id": "action-1",
            "result_status": "running",
            "host_ready_state": "ready",
            "host_startup_seconds": 12.0,
            "host_pid": 101,
            "sidecar_pid": 202,
        },
    ]
    (state_dir / "supervisor_events.v1.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    sidecar_path = state_dir / "game_overlay_sidecar_status.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build_id": "build-stale",
                "status": "running",
                "pid": 202,
                "heartbeat_at": time.time(),
                "data_generation_id": "generation-stale",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(smoke, "_process_alive", lambda pid: int(pid or 0) in {100, 101, 202})

    stale_checks, _ = smoke._overlay_chain_status(
        runtime_root,
        manifest,
        desktop_pid=100,
        started_at_wall=started_at,
    )
    assert stale_checks["sidecar_build"] is False
    assert stale_checks["sidecar_vision_pool_generation"] is False

    sidecar_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "build_id": "build-current",
                "status": "running",
                "pid": 202,
                "heartbeat_at": time.time(),
                "data_generation_id": "generation-current",
                "vision_pool_generation_id": "generation-current",
                "stats_generation_id": "",
                "generation_roles": {
                    "vision_pool_generation_id": "sidecar_template_runtime",
                    "stats_generation_id": "host_game_session",
                },
                "matrix_rows": {"observed_name": 2},
            }
        ),
        encoding="utf-8",
    )
    current_checks, details = smoke._overlay_chain_status(
        runtime_root,
        manifest,
        desktop_pid=100,
        started_at_wall=started_at,
    )

    assert all(current_checks.values()), current_checks
    assert details["host_pid"] == 101
    assert details["sidecar_pid"] == 202


def test_populated_runtime_fixture_contains_eight_generations_and_four_legacy(tmp_path: Path) -> None:
    from tooling.acceptance import smoke_packaged_startup as smoke

    package = tmp_path / "package"
    internal = package / "_internal"
    seed_generation = internal / "resources" / "seeds" / "generations" / "bundle-generation"
    seed_generation.mkdir(parents=True)
    (seed_generation / "manifest.json").write_text(
        json.dumps({"generation_id": "bundle-generation"}),
        encoding="utf-8",
    )
    seed_pointer = internal / "resources" / "seeds" / "current.v2.json"
    seed_pointer.parent.mkdir(parents=True, exist_ok=True)
    seed_pointer.write_text(json.dumps({"current_generation_id": "bundle-generation"}), encoding="utf-8")
    (internal / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "seed_files": [
                    "resources/seeds/current.v2.json",
                    "resources/seeds/generations/bundle-generation/manifest.json",
                ],
                "cohort_seed_files": [],
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"

    smoke._write_populated_runtime_fixture(package, runtime)

    generations = list((runtime / "snapshots" / "generations").iterdir())
    assert len(generations) == 8
    legacy = [
        path
        for path in generations
        if '"source": "hextech"' in (path / "manifest.json").read_text(encoding="utf-8", errors="ignore")
    ]
    assert len(legacy) == 4


def test_pyinstaller_collects_scraping_package_data():
    from tooling.build.package import PYINSTALLER_COLLECT_DATA, REQUIRED_PACKAGED_SCRAPING_DATA

    assert {"scrapling", "browserforge", "apify_fingerprint_datapoints"} <= set(PYINSTALLER_COLLECT_DATA)
    assert "input-network-definition.zip" in REQUIRED_PACKAGED_SCRAPING_DATA


def test_native_binary_path_budget_accepts_boundary_and_rejects_overflow(tmp_path: Path) -> None:
    from tooling.build.package import validate_packaged_native_paths

    package_dir = tmp_path / "package"
    native = package_dir / "_internal" / "curl_cffi.libs" / "libcurl-test.dll"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"dll")
    exact_length = len(str(native.resolve()))

    status = validate_packaged_native_paths(package_dir, max_path_length=exact_length)

    assert status["native_file_count"] == 1
    assert status["max_path_length"] == exact_length
    with pytest.raises(RuntimeError, match="原生 DLL 路径过长"):
        validate_packaged_native_paths(package_dir, max_path_length=exact_length - 1)


def test_package_entries_include_verified_snapshot_files(tmp_path, monkeypatch):
    from tooling.build.resource_manifest import write_resource_manifest
    from tooling.build import rules as build_rules
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
    manifest_path.write_text(
        json.dumps({"seed_sha256": seed_sha256, "cohort_seed_sha256": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        build_rules,
        "collect_cohort_seed",
        lambda _root: type("SnapshotOnlySeed", (), {"files": (), "bundled_name": lambda self, _path: ""})(),
    )

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
        "resources/assets/vision/name_exemplars/aram_infernalconduit__20260821.png",
        "resources/assets/vision/name_exemplars/ominouspact__20260821.png",
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

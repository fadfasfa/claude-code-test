"""Admission must consume production-generated B/B status, never a made-up A/B DTO."""
from dataclasses import replace
import json

import pytest

from hextech.infrastructure.vision import data_source, template_runtime
from hextech.modules.data.catalog import versioned
from hextech.modules.data.ports.atomic import atomic_write_json
from test_catalog_identity_capabilities import pool
from test_cohort_v3 import _v3_runtime, _publish_independent_recognition_catalog
from tooling.build.cohort_seed import collect_cohort_seed
from tooling.build.recognition_contract import recognition_pool_contract
from tooling.acceptance import smoke_packaged_startup as smoke
from tooling.build import deploy


@pytest.fixture
def production_dual_catalog(tmp_path, monkeypatch):
    runtime, generation, _ = _v3_runtime(tmp_path, details=True)
    pointer = _publish_independent_recognition_catalog(runtime)
    catalog_dir = runtime / "catalog/generations" / pointer["catalog_generation_id"]
    # B changes both the actual pool and its identity count, not just its Catalog label.
    atomic_write_json(catalog_dir / "augment_identities.v2.json", pool())
    atomic_write_json(catalog_dir / "augment_assets.v1.json", {"schema_version": 1, "entries": []})
    manifest = replace(versioned.build_catalog_manifest(catalog_dir, created_at="2026-09-17T00:00:00+00:00"),
                       catalog_generation_id=pointer["catalog_generation_id"])
    atomic_write_json(catalog_dir / "manifest.json", manifest.to_dict())
    pointer.update(content_sha256=manifest.content_sha256,
                   manifest_sha256=versioned.sha256_file(catalog_dir / "manifest.json"))
    atomic_write_json(runtime / "catalog/current.v2.json", pointer)
    monkeypatch.setattr(versioned, "catalog_root", lambda: runtime / "catalog")
    monkeypatch.setattr(data_source, "catalog_root", lambda: runtime / "catalog")
    hints = data_source.CatalogVisionDataSource(generation_id=generation).read_hint_cache()
    built = template_runtime.load_or_build_default_template_runtime(
        hint_cache=hints, base_dir=catalog_dir, cache_file=tmp_path / "matrix-cache.npz",
        resource_signature={"test": "dual-production"}, require_production_pool=True,
    )
    metadata = collect_cohort_seed(runtime / "snapshots").metadata
    status = {**built.stats, "schema_version": 2, "build_id": "test-build", "status": "stopped",
              "phase": "once_complete", "pid": 505}
    assert status["catalog_generation_id"] == status["recognition_catalog_id"] == pointer["catalog_generation_id"]
    assert status["catalog_generation_id"] != metadata["catalog_generation_id"]
    assert status["production_pool_id"] != metadata["production_pool_id"]
    assert status["production_pool_count"] != metadata["production_pool_count"]
    return runtime, metadata, status


@pytest.mark.parametrize("mutation", [None, "catalog_generation_id", "production_pool_id", "production_pool_count", "matrix_rows"])
def test_smoke_checks_production_recognition_pool(production_dual_catalog, monkeypatch, mutation):
    root, cohort, status = production_dual_catalog
    if mutation:
        status[mutation] = {"icon": -1} if mutation == "matrix_rows" else cohort.get(mutation, "wrong")
    def run(command, **_kwargs):
        atomic_write_json(root / "state/game_overlay_sidecar_status.json", status)
        return smoke.subprocess.CompletedProcess(command, 0, stdout=b"")
    monkeypatch.setattr(smoke.subprocess, "run", run)
    if mutation:
        with pytest.raises(smoke.SmokeFailure, match="Sidecar pool smoke 失败"):
            smoke._sidecar_pool_smoke(root / "fake.exe", root, {}, root, {"build_id": "test-build", "cohort_seed": cohort})
    else:
        result = smoke._sidecar_pool_smoke(root / "fake.exe", root, {}, root, {"build_id": "test-build", "cohort_seed": cohort})
        assert result["state"] == "ready"


def test_deploy_checks_production_recognition_pool(production_dual_catalog, monkeypatch):
    root, cohort, status = production_dual_catalog
    monkeypatch.setattr(deploy, "_packaged_var_dir", lambda: root)
    # Skip only the launch-session receipt: no packaged process is launched in this fixture.
    monkeypatch.setattr(deploy, "validated_launch_generation", lambda *args, **kwargs: (cohort["generation_id"], []))
    atomic_write_json(root / "state/data-service/refresh_schedule.v1.json", {
        "schema_version": 1, "generation_id": cohort["generation_id"],
        "sources": {source: {"state": "ready", "failure_kind": "", "current_run_id": run_id}
                    for source, run_id in {"catalog": cohort["recognition_catalog_generation_id"],
                                           **cohort["source_run_ids"]}.items()},
    })
    for relative, schema in deploy.RUNTIME_BUILD_STATE_SPECS:
        payload = {"schema_version": schema, "build_id": "test-build"}
        if relative.name == "game_overlay_sidecar_status.json":
            payload.update(status, status="running")
        elif relative.name in {"game_overlay_visibility.v1.json", "latest.json"}:
            payload.update(generation_id=cohort["generation_id"], data_generation_id=cohort["generation_id"],
                           stats_generation_id=cohort["generation_id"], vision_pool_generation_id=cohort["generation_id"])
        atomic_write_json(root / relative, payload)
    assert deploy._runtime_build_errors(expected_build_id="test-build", launch_started_at=0,
                                       sidecar_pid=505, expected_cohort=cohort) == []
    path = root / "state/game_overlay_sidecar_status.json"
    status.update(status="running", production_pool_id=cohort["production_pool_id"])
    atomic_write_json(path, status)
    errors = deploy._runtime_build_errors(expected_build_id="test-build", launch_started_at=0,
                                          sidecar_pid=505, expected_cohort=cohort)
    assert any("production_pool_id" in error for error in errors)


@pytest.mark.parametrize("drift", ["pointer", "artifact"])
def test_recognition_contract_rejects_catalog_drift(production_dual_catalog, monkeypatch, drift):
    root, cohort, _status = production_dual_catalog
    pointer = json.loads((root / "catalog/current.v2.json").read_text(encoding="utf-8"))
    if drift == "pointer":
        pointer["manifest_sha256"] = "0" * 64
        atomic_write_json(root / "catalog/current.v2.json", pointer)
    else:
        path = root / "catalog/generations" / pointer["catalog_generation_id"] / "hero_version.txt"
        path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError):
        recognition_pool_contract(root, cohort)
    monkeypatch.setattr(smoke.subprocess, "run", lambda *args, **kwargs: pytest.fail("invalid Catalog launched Sidecar"))
    with pytest.raises(smoke.SmokeFailure, match="recognition Catalog"):
        smoke._sidecar_pool_smoke(root / "fake.exe", root, {}, root, {"build_id": "test-build", "cohort_seed": cohort})

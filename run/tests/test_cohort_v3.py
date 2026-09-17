from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from hextech.contracts import ArtifactDescriptor, CatalogManifestV2, CohortRecoveryPointV1, SourceProvenance, SourceRunManifestV2
from hextech.infrastructure.persistence.cohort_recovery import build_recovery_point, validate_generation_cohort
from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort
from hextech.infrastructure.persistence.cohort_validation_receipt import write_validation_receipt
from hextech.modules.data.catalog.versioned import sha256_file
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from tooling.build.cohort_seed import collect_cohort_seed
from test_cohort_seed import _bundle_from_runtime, _fixture_runtime


def _v3_runtime(tmp_path: Path, *, details: bool) -> tuple[Path, str, Path | None]:
    runtime, _ = _fixture_runtime(tmp_path)
    old = DataSnapshotClient(runtime / "snapshots").open_view()
    payload = {"champions": old.get_champions(), "champion_hextech": {"英雄一": old.get_champion_detail("1")},
               "identities": old.get_identity_indexes(), "overlay_hints": old.get_overlay_hints()}
    source = next(item for item in old.manifest.source_files if item.source == "aramkit")
    template = SourceRunManifestV2.from_mapping(json.loads((runtime / "sources/aramkit/runs" / source.run_id / "manifest.json").read_text()))
    provenance = [item for item in old.manifest.source_files if item.source == "catalog"]
    child_path = None
    for run_id, role in [("rank-v3", "hero_rankings"), *(([("hero-v3-a", "scoped_stats"), ("hero-v3-b", "scoped_stats")]) if details else [])]:
        run = runtime / "sources/aramkit/runs" / run_id
        run.mkdir(parents=True)
        artifact_path = run / "artifact.json"
        if role == "scoped_stats":
            child_path = run / "champion.json"
            atomic_write_json(child_path, {"hero_id": "1", "all": [{"id": "1001"}]})
            atomic_write_json(artifact_path, {"schema_version": 1, "champion_count": 1, "record_count": 1,
                                             "files": [{"champion_id": "1", "relative_path": child_path.name,
                                                        "sha256": sha256_file(child_path), "size": child_path.stat().st_size,
                                                        "record_count": 1}]})
        else:
            atomic_write_json(artifact_path, [{"hero_id": "1"}])
        artifact = ArtifactDescriptor(role=role, relative_path=artifact_path.name, sha256=sha256_file(artifact_path),
                                      record_count=1, content_schema_version=2, size=artifact_path.stat().st_size)
        manifest = replace(template, run_id=run_id, artifact=artifact)
        path = run / "manifest.json"
        atomic_write_json(path, manifest.to_dict())
        provenance.append(SourceProvenance(source="aramkit", run_id=run_id,
                                           catalog_generation_id=source.catalog_generation_id, artifact_role=role,
                                           artifact_sha256=artifact.sha256, record_count=1,
                                           manifest_sha256=sha256_file(path), content_schema_version=2))
    components = {"ranking": {"source_version": "rank-marker", "catalog_id": source.catalog_generation_id, "run_id": "rank-v3"},
                  "champions": {"1": {"source_version": "hero-marker", "catalog_id": source.catalog_generation_id,
                                        "complete": details, **({"run_id": "hero-v3-b"} if details else {})}}}
    if not details:
        payload["champion_hextech"]["英雄一"].update(augments=[], data_status="pending")
        payload["identities"]["augments"] = {}
    manifest = DataSnapshotPublisher(runtime / "snapshots").publish(payload, source_files=provenance,
                                                                   components=components, require_complete_provenance=True)
    candidate = validate_generation_cohort(runtime, manifest.generation_id)
    atomic_write_json(runtime / "sources/aramkit/current.v2.json", candidate.pointers["aramkit"])
    # Fixture-only inactive optional pointers/schedule do not belong to v3 seed.
    (runtime / "state/data-service/refresh_schedule.v1.json").unlink()
    return runtime, manifest.generation_id, child_path


def _publish_independent_recognition_catalog(runtime: Path) -> dict[str, object]:
    current_path = runtime / "catalog/current.v2.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    old_id = str(current["catalog_generation_id"])
    new_id = "catalog-recognition-new"
    source = runtime / "catalog/generations" / old_id
    target = runtime / "catalog/generations" / new_id
    shutil.copytree(source, target)
    manifest_path = target / "manifest.json"
    manifest = CatalogManifestV2.from_mapping(json.loads(manifest_path.read_text(encoding="utf-8")))
    updated = replace(
        manifest,
        catalog_generation_id=new_id,
        created_at="2026-09-15T00:00:00+00:00",
    )
    atomic_write_json(manifest_path, updated.to_dict(), ensure_ascii=False, indent=2)
    pointer: dict[str, object] = {
        "schema_version": 2,
        "catalog_generation_id": new_id,
        "content_sha256": updated.content_sha256,
        "manifest_sha256": sha256_file(manifest_path),
        "completed_at": updated.created_at,
        "last_success_at": updated.created_at,
    }
    atomic_write_json(current_path, pointer, ensure_ascii=False, indent=2)
    return pointer


@pytest.mark.parametrize("details", [False, True])
def test_v3_seed_installs_partial_and_all_referenced_units(tmp_path: Path, details: bool) -> None:
    runtime, generation_id, _ = _v3_runtime(tmp_path / "input", details=details)
    seed = collect_cohort_seed(runtime / "snapshots")
    assert seed.metadata["schema_version"] == 2
    assert len(seed.metadata["units"]) == (3 if details else 1)
    if details:
        assert {path.parent.name for path in seed.files if path.name == "champion.json"} == {"hero-v3-a", "hero-v3-b"}
    bundle = _bundle_from_runtime(tmp_path, runtime)
    installed = tmp_path / "installed"
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=installed) == "installed"
    candidate = validate_generation_cohort(installed, generation_id)
    assert set(candidate.pointers) == {"catalog", "aramkit"}
    assert len(candidate.units) == (3 if details else 1)
    point = build_recovery_point(candidate, previous_generation_id="")
    assert point.schema_version == 2
    assert CohortRecoveryPointV1.from_mapping(point.to_dict()).units == candidate.units
    assert write_validation_receipt(installed, candidate)
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=installed) == "already_current"


def test_v3_seed_packages_independent_recognition_catalog_and_old_statistics_closure(
    tmp_path: Path,
) -> None:
    runtime, generation_id, _ = _v3_runtime(tmp_path / "input", details=True)
    statistics = validate_generation_cohort(runtime, generation_id)
    statistics_catalog_id = str(statistics.pointers["catalog"]["catalog_generation_id"])
    recognition = _publish_independent_recognition_catalog(runtime)

    seed = collect_cohort_seed(runtime / "snapshots")

    assert seed.metadata["catalog_generation_id"] == statistics_catalog_id
    assert seed.metadata["recognition_catalog_generation_id"] == recognition["catalog_generation_id"]
    packaged_catalogs = {
        path.parent.name
        for path in seed.files
        if path.name == "manifest.json" and path.parent.parent.name == "generations"
    }
    assert {statistics_catalog_id, recognition["catalog_generation_id"]}.issubset(packaged_catalogs)

    bundle = _bundle_from_runtime(tmp_path / "package", runtime)
    installed = tmp_path / "installed"
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=installed) == "installed"
    assert json.loads((installed / "catalog/current.v2.json").read_text(encoding="utf-8")) == recognition
    installed_statistics = validate_generation_cohort(installed, generation_id)
    assert installed_statistics.pointers["catalog"]["catalog_generation_id"] == statistics_catalog_id
    assert all(
        pointer["catalog_generation_id"] == statistics_catalog_id
        for pointer in installed_statistics.units.values()
    )
    assert write_validation_receipt(installed, installed_statistics)
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=installed) == "already_current"


def test_v3_installer_accepts_legacy_single_catalog_metadata(tmp_path: Path) -> None:
    runtime, generation_id, _ = _v3_runtime(tmp_path / "input", details=True)
    bundle = _bundle_from_runtime(tmp_path / "package", runtime)
    manifest_path = bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    statistics_catalog_id = str(manifest["cohort_seed"]["catalog_generation_id"])
    manifest["cohort_seed"].pop("recognition_catalog_generation_id", None)
    atomic_write_json(manifest_path, manifest, ensure_ascii=False, indent=2)

    installed = tmp_path / "installed"
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=installed) == "installed"
    current = json.loads(
        (installed / "catalog/current.v2.json").read_text(encoding="utf-8")
    )
    assert current["catalog_generation_id"] == statistics_catalog_id
    assert validate_generation_cohort(installed, generation_id).generation_id == generation_id


def test_v3_seed_rejects_broken_independent_recognition_catalog(tmp_path: Path) -> None:
    runtime, _generation_id, _ = _v3_runtime(tmp_path, details=True)
    recognition = _publish_independent_recognition_catalog(runtime)
    manifest_path = (
        runtime
        / "catalog/generations"
        / str(recognition["catalog_generation_id"])
        / "manifest.json"
    )
    manifest_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Catalog"):
        collect_cohort_seed(runtime / "snapshots")


def test_v3_seed_rejects_unsafe_recognition_catalog_id(tmp_path: Path) -> None:
    runtime, _generation_id, _ = _v3_runtime(tmp_path, details=True)
    current_path = runtime / "catalog/current.v2.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["catalog_generation_id"] = "../outside"
    atomic_write_json(current_path, current, ensure_ascii=False, indent=2)

    with pytest.raises(ValueError, match="catalog_generation_id"):
        collect_cohort_seed(runtime / "snapshots")


def test_v3_installer_rejects_unsafe_recognition_catalog_id(tmp_path: Path) -> None:
    runtime, _generation_id, _ = _v3_runtime(tmp_path / "input", details=True)
    bundle = _bundle_from_runtime(tmp_path / "package", runtime)
    manifest_path = bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pointer_name = "resources/cohort-seed/catalog/current.v2.json"
    pointer_path = bundle / Path(pointer_name)
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["catalog_generation_id"] = "../outside"
    atomic_write_json(pointer_path, pointer, ensure_ascii=False, indent=2)
    manifest["cohort_seed"]["recognition_catalog_generation_id"] = "../outside"
    manifest["cohort_seed_sha256"][pointer_name] = hashlib.sha256(
        pointer_path.read_bytes()
    ).hexdigest()
    atomic_write_json(manifest_path, manifest, ensure_ascii=False, indent=2)

    with pytest.raises(ValueError, match="catalog_generation_id"):
        install_bundled_cohort(
            bundle_root=bundle,
            runtime_root=tmp_path / "installed",
        )


def test_every_scoped_child_is_validated_before_seed_copy(tmp_path: Path) -> None:
    runtime, generation_id, child = _v3_runtime(tmp_path, details=True)
    assert child is not None
    child.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="子文件"):
        validate_generation_cohort(runtime, generation_id)
    with pytest.raises(ValueError, match="子文件"):
        collect_cohort_seed(runtime / "snapshots")


@pytest.mark.parametrize("details", [False, True])
def test_package_seed_admits_verified_v3_units_with_optional_absence(tmp_path, details):
    from tooling.build.manifest import validate_snapshot_seed
    runtime, generation_id, _ = _v3_runtime(tmp_path, details=details)
    result = validate_snapshot_seed(runtime / "snapshots")
    assert result["valid"] and result["generation_id"] == generation_id

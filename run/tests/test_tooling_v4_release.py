"""Real synthetic v4 closure coverage for packaging and acceptance tools."""
from __future__ import annotations
import json
import shutil
import pytest
from tests.support.v4_release import make_release
from tooling.build.cohort_seed import collect_cohort_seed
from tooling.build.manifest import validate_snapshot_seed
from tooling.build.deploy import _runtime_cohort_errors, _resolve_runtime_cohort_expected
from tooling.build.recognition_contract import recognition_pool_contract
from tooling.acceptance.verify_data_pipeline import verify_strict_full_chain
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.ports.atomic import atomic_write_json


def test_verified_seed_relocates_without_source_heads_or_query_payload_copies(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    manifest = make_release(runtime, monkeypatch)
    seed = collect_cohort_seed(runtime / "snapshots")
    assert seed.metadata["snapshot_schema_version"] == 4
    assert seed.metadata["production_pool_count"] == 1
    assert all(name.startswith("resources/seeds/runtime.v4/") for name in map(seed.bundled_name, seed.files))
    assert not any(p.name == "current.v2.json" for p in seed.files)
    assert not any(p.name in {"champions.json", "champion_hextech.json", "overlay_hints.json", "identities.json"} for p in seed.files)
    # Unknown source-adjacent files must never enter the frozen bundle.
    unknown = runtime / "sources/aramkit/private-note.txt"
    unknown.write_text("outside closure", encoding="utf-8")
    assert unknown not in collect_cohort_seed(runtime / "snapshots").files
    relocated = tmp_path / "seed"
    for original in seed.files:
        target = relocated / original.relative_to(runtime)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    summary = verify_strict_full_chain(relocated / "snapshots")
    assert summary["generation"]["generation_id"] == manifest.generation_id
    assert summary["generation"]["complete_champion_count"] == 1
    assert validate_snapshot_seed(relocated / "snapshots")["valid"]
    assert recognition_pool_contract(relocated, seed.metadata)["production_pool_count"] == 1
    assert _runtime_cohort_errors(relocated, seed.metadata) == []
    from hextech.infrastructure.persistence.release_seed import install_v4_seed
    installed = tmp_path / "installed"
    assert install_v4_seed(relocated, installed)["generation_id"] == manifest.generation_id
    assert DataSnapshotClient(installed / "snapshots").open_view().get_champion_detail("1")


def test_seed_carries_new_recognition_catalog_without_rebinding_old_statistics(tmp_path, monkeypatch):
    from tests.support.v4_release import _publish_independent_recognition_catalog
    runtime = tmp_path / "source"
    manifest = make_release(runtime, monkeypatch)
    new_catalog = _publish_independent_recognition_catalog(runtime)
    seed = collect_cohort_seed(runtime / "snapshots")
    assert seed.metadata["recognition_catalog_generation_id"] == new_catalog["catalog_generation_id"]
    assert seed.metadata["catalog_generation_id"] != new_catalog["catalog_generation_id"]
    target = tmp_path / "copied"
    for file in seed.files:
        output = target / file.relative_to(runtime)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, output)
    assert DataSnapshotClient(target / "snapshots").open_view().manifest.generation_id == manifest.generation_id
    assert recognition_pool_contract(target, seed.metadata)["recognition_catalog_id"] == new_catalog["catalog_generation_id"]


@pytest.mark.parametrize("damage", ["manifest", "artifact", "missing_child", "wrong_catalog"])
def test_v4_seed_and_deployment_reject_corrupt_or_mixed_closure(tmp_path, monkeypatch, damage):
    runtime = tmp_path / "runtime"
    manifest = make_release(runtime, monkeypatch)
    expected = collect_cohort_seed(runtime / "snapshots").metadata
    if damage == "manifest":
        path = runtime / "snapshots/generations" / manifest.generation_id / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["content_fingerprint"] = "0" * 64
        atomic_write_json(path, payload)
    elif damage == "artifact":
        ref = next(r for r in manifest.source_files if r.artifact_role == "hero_rankings")
        run = runtime / "sources/aramkit/runs" / ref.run_id
        payload = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        (run / payload["artifact"]["relative_path"]).write_text("{}", encoding="utf-8")
    elif damage == "missing_child":
        ref = next(r for r in manifest.source_files if r.artifact_role == "scoped_stats")
        run = runtime / "sources/aramkit/runs" / ref.run_id
        payload = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        index = run / payload["artifact"]["relative_path"]
        child = json.loads(index.read_text(encoding="utf-8"))["files"][0]
        (index.parent / child["relative_path"]).unlink()
    else:
        expected["catalog_generation_id"] = "wrong-catalog"
        assert _runtime_cohort_errors(runtime, expected)
        assert _resolve_runtime_cohort_expected(runtime, expected)[1]
        return
    with pytest.raises((RuntimeError, ValueError, OSError)):
        collect_cohort_seed(runtime / "snapshots")
    assert _runtime_cohort_errors(runtime, expected)


def test_single_head_replaces_old_checkpoint_acceptance(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    make_release(runtime, monkeypatch)
    expected = collect_cohort_seed(runtime / "snapshots").metadata
    old = runtime / "state/data-service/refresh_checkpoint.v1.json"
    old.parent.mkdir(parents=True)
    old.write_text("{corrupted retired file", encoding="utf-8")
    assert _runtime_cohort_errors(runtime, expected) == []
    before = old.read_bytes()
    current, errors = _resolve_runtime_cohort_expected(runtime, expected)
    assert errors == [] and current["generation_id"] == expected["generation_id"]
    assert old.read_bytes() == before


def test_pipeline_packaging_does_not_accept_a_legacy_head(tmp_path):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    (snapshots / "current.v2.json").write_text('{"schema_version":2,"current_generation_id":"old"}', encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError, OSError)):
        validate_snapshot_seed(snapshots)


def test_frozen_bundle_uses_one_relocatable_v4_seed_and_rejects_unlisted_files(tmp_path, monkeypatch):
    from tooling.build.manifest import build_bundle_manifest
    from tooling.build.resource_manifest import write_resource_manifest
    from tooling.build.rules import iter_package_data_entries, stage_package_data_tree
    runtime = tmp_path / "runtime"
    make_release(runtime, monkeypatch)
    seed = collect_cohort_seed(runtime / "snapshots")
    base = tmp_path / "project"
    catalog_id = seed.metadata["catalog_generation_id"]
    shutil.copytree(runtime / "catalog/generations" / catalog_id, base / "resources/catalog")
    assets = base / "resources/assets/ui"
    assets.mkdir(parents=True)
    (assets / "marker.png").write_bytes(b"png")
    source = base / "src/hextech/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("", encoding="utf-8")
    write_resource_manifest(base)
    (assets / "unlisted.png").write_bytes(b"private")
    bundle = build_bundle_manifest(base, verified_snapshot_root=runtime / "snapshots")
    manifest_path = base / "bundle_manifest.json"
    atomic_write_json(manifest_path, bundle)
    entries = iter_package_data_entries(base, manifest_path, verified_snapshot_root=runtime / "snapshots")
    assert all(entry.source.name != "unlisted.png" for entry in entries)
    assert not any(entry.target == "static" for entry in entries)
    staged = stage_package_data_tree(entries, tmp_path / "staging")
    installed_seed = staged.source / "resources/seeds/runtime.v4"
    assert DataSnapshotClient(installed_seed / "snapshots").open_view().get_champion_detail("1")
    assert not (staged.source / "resources/cohort-seed").exists()
    assert not (staged.source / "resources/seeds/current.v4.json").exists()


def test_seed_head_copies_all_previous_role_references_without_private_neighbors(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.support.v4_release import _publish_independent_recognition_catalog
    from hextech.modules.data.generation.release_store import read_head, update_head, catalog_paths
    runtime = tmp_path / "source"
    original = make_release(runtime, monkeypatch)
    _publish_independent_recognition_catalog(runtime)
    newer = replace(original, generation_id="20990101T000000-release-current", created_at="2099-01-01T00:00:00+00:00")
    atomic_write_json(runtime / "snapshots/generations" / newer.generation_id / "manifest.json", newer.to_dict())
    update_head(runtime / "snapshots", current_generation_id=newer.generation_id)
    head = read_head(runtime / "snapshots")
    private = runtime / "catalog/generations" / head["recognition_catalog"]["catalog_generation_id"] / "private-note.txt"
    private.write_text("private", encoding="utf-8")
    seed = collect_cohort_seed(runtime / "snapshots")
    assert private not in seed.files
    target = tmp_path / "relocated"
    for path in seed.files:
        copied = target / path.relative_to(runtime)
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copied)
    assert read_head(target / "snapshots") == head
    assert DataSnapshotClient(target / "snapshots").open_generation(head["previous_generation_id"]).manifest.generation_id == original.generation_id
    for role in ("recognition_catalog", "previous_recognition_catalog"):
        assert catalog_paths(target, head[role])
    assert not (target / private.relative_to(runtime)).exists()


def test_existing_newer_statistics_and_recognition_are_preserved_and_verified(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.support.v4_release import _publish_independent_recognition_catalog
    from test_catalog_identity_capabilities import pool
    from hextech.modules.data.catalog.versioned import build_catalog_manifest, sha256_file
    from hextech.modules.data.generation.release_store import read_head, update_head
    from hextech.infrastructure.persistence.release_seed import install_v4_seed
    supplied = tmp_path / "bundle-seed"
    make_release(supplied, monkeypatch)
    b = _publish_independent_recognition_catalog(supplied)
    expected = {**collect_cohort_seed(supplied / "snapshots").metadata, "_seed_runtime": str(supplied)}
    target = tmp_path / "existing"
    current = make_release(target, monkeypatch)
    _publish_independent_recognition_catalog(target)
    c_root = target / "catalog/generations/catalog-recognition-c"
    shutil.copytree(target / "catalog/generations" / b["catalog_generation_id"], c_root)
    atomic_write_json(c_root / "augment_identities.v2.json", pool())
    atomic_write_json(c_root / "augment_assets.v1.json", {"schema_version": 1, "entries": []})
    c_manifest = replace(build_catalog_manifest(c_root, created_at="2099-01-02T00:00:00+00:00"), catalog_generation_id=c_root.name)
    atomic_write_json(c_root / "manifest.json", c_manifest.to_dict())
    c = {"schema_version": 2, "catalog_generation_id": c_root.name, "content_sha256": c_manifest.content_sha256,
         "manifest_sha256": sha256_file(c_root / "manifest.json")}
    update_head(target / "snapshots", recognition_catalog=c)
    new_stats = replace(current, generation_id="20990103T000000-current-stats", created_at="2099-01-03T00:00:00+00:00")
    atomic_write_json(target / "snapshots/generations" / new_stats.generation_id / "manifest.json", new_stats.to_dict())
    update_head(target / "snapshots", current_generation_id=new_stats.generation_id)
    before = (target / "snapshots/current.v4.json").read_bytes()
    assert expected["generation_id"] != current.generation_id
    assert not (target / "snapshots/generations" / expected["generation_id"]).exists()
    assert install_v4_seed(supplied, target)["source"] == "runtime_current"
    assert (target / "snapshots/current.v4.json").read_bytes() == before
    actual, errors = _resolve_runtime_cohort_expected(target, expected)
    assert errors == []
    assert actual["generation_id"] == new_stats.generation_id
    assert actual["recognition_catalog_generation_id"] == c_root.name
    assert actual["production_pool_id"] != expected["production_pool_id"]
    assert recognition_pool_contract(target, actual)["catalog_generation_id"] == c_root.name
    assert read_head(target / "snapshots")["recognition_catalog"] == c
    from tooling.build.deploy_lineage import validated_launch_generation
    assert validated_launch_generation(target, expected, build_id="test", launch_started_at=0,
                                       desktop_pid=None, current_generation_id=new_stats.generation_id) == ("", [])


def test_newer_runtime_never_bypasses_corrupt_seed_baseline_and_older_recognition_is_rejected(tmp_path, monkeypatch):
    from tests.support.v4_release import _publish_independent_recognition_catalog
    from hextech.infrastructure.persistence.release_seed import install_v4_seed
    supplied = tmp_path / "supplied"
    make_release(supplied, monkeypatch)
    pointer = _publish_independent_recognition_catalog(supplied)
    expected = {**collect_cohort_seed(supplied / "snapshots").metadata, "_seed_runtime": str(supplied)}
    target = tmp_path / "existing"
    make_release(target, monkeypatch)
    before = (target / "snapshots/current.v4.json").read_bytes()
    assert any("recognition Catalog predates" in error for error in _resolve_runtime_cohort_expected(target, expected)[1])
    damaged = supplied / "catalog/generations" / pointer["catalog_generation_id"] / "hero_version.txt"
    damaged.write_text("damaged", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError, OSError)):
        install_v4_seed(supplied, target)
    assert _resolve_runtime_cohort_expected(target, expected)[1]
    assert (target / "snapshots/current.v4.json").read_bytes() == before

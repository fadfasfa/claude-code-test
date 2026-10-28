from dataclasses import replace
import json
from pathlib import Path

import pytest

from tooling.migration.v3_contracts import LegacySnapshotManifestV3, SnapshotFileDescriptor
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher, SnapshotValidationError
from hextech.modules.data.generation.release_store import closure, closure_paths, fingerprint
from hextech.modules.data.catalog.versioned import sha256_file
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.infrastructure.persistence.release_seed import install_v4_seed, initialize_snapshot
from support.v4_release import make_release


def test_release_persists_only_reference_manifest_and_one_atomic_head(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    manifest = make_release(root, monkeypatch)
    assert manifest.schema_version == 4
    assert set(p.name for p in (root / "snapshots/generations" / manifest.generation_id).iterdir()) == {"manifest.json"}
    assert not (root / "snapshots/current.v2.json").exists()
    assert not (root / "snapshots/previous.v2.json").exists()
    assert set(json.loads((root / "snapshots/current.v4.json").read_text())) == {
        "schema_version", "current_generation_id", "previous_generation_id", "recognition_catalog", "previous_recognition_catalog"}


def test_ranking_and_identity_queries_never_read_hero_artifacts(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    make_release(root, monkeypatch)
    read = Path.read_bytes
    reads = []
    def observed(path, *args, **kwargs):
        if "scoped_stats" in path.parts:
            reads.append(path)
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", observed)
    view = DataSnapshotClient(root / "snapshots").open_view()
    assert view.get_champions()[0]["id"] == "1"
    assert view.resolve_augment("强化一")["canonical_id"] == "1001"
    assert not reads
    hints = view.get_overlay_hints()
    assert not reads
    assert all("private_stats" not in row for row in hints["hints"].values())
    detail = view.get_champion_detail("1")
    assert detail["augments"][0]["id"] == "1001"
    count = len(reads)
    assert count == 2  # only selected unit index and its one hero child
    assert view.get_champion_detail("1") == detail
    assert len(reads) == count
    assert len(view._payloads._hero_cache) <= 2


def test_corrupt_selected_child_is_rejected_without_mixing_other_data(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    manifest = make_release(root, monkeypatch)
    child = next(p for p in closure_paths(root, manifest) if p.name != "manifest.json" and "scoped_stats" in p.parts)
    (root / child).write_bytes(b"damaged")
    view = DataSnapshotClient(root / "snapshots").open_view()
    assert view.get_champions()[0]["id"] == "1"
    with pytest.raises(SnapshotValidationError):
        view.get_champion_detail("1")
    with pytest.raises(SnapshotValidationError):
        closure(root, manifest)


def test_seed_copies_exact_closure_without_private_siblings(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    manifest = make_release(source, monkeypatch)
    run = next(r for r in manifest.source_files if r.source == "aramkit")
    private = source / "sources" / run.source / "runs" / run.run_id / "private-note.txt"
    private.write_text("not part of the release", encoding="utf-8")
    assert install_v4_seed(source, target)["state"] in {"ready", "degraded"}
    assert not (target / private.relative_to(source)).exists()
    assert DataSnapshotClient(target / "snapshots").open_view().get_champion_detail("1")["hero_id"] == "1"


def test_legacy_runtime_is_never_silently_seeded_or_rewritten(tmp_path, monkeypatch):
    seed, old = tmp_path / "seed", tmp_path / "old"
    make_release(seed, monkeypatch)
    path = old / "snapshots/current.v2.json"
    atomic_write_json(path, {"schema_version": 2, "current_generation_id": "old"})
    before = path.read_bytes()
    result = initialize_snapshot(DataSnapshotPublisher(old / "snapshots"), seed)
    assert result["reason_code"] == "migration_required"
    assert path.read_bytes() == before
    assert not (old / "snapshots/current.v4.json").exists()


def test_manifest_cannot_bind_another_hero_or_unverified_core_unit(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    manifest = make_release(root, monkeypatch)
    components = json.loads(json.dumps(manifest.components))
    components["champions"]["2"] = components["champions"].pop("1")
    bad = replace(manifest, components=components,
                  content_fingerprint=fingerprint(manifest.source_files, components, manifest.source_status, manifest.health, manifest.degraded_sources))
    with pytest.raises(SnapshotValidationError, match="cohort mismatch"):
        closure(root, bad)


def _legacy_current(root, monkeypatch):
    from tooling.migration.v3_validation import count_records, content_fingerprint
    manifest = make_release(root, monkeypatch)
    view = DataSnapshotClient(root / "snapshots").open_view()
    payloads = {"champions": view.get_champions(), "champion_hextech": {"英雄一": view.get_champion_detail("1")},
                "overlay_hints": view.get_overlay_hints(), "identities": view.get_identity_indexes()}
    files = []
    directory = root / "snapshots/generations" / "legacy-v3-current"
    for role, value in payloads.items():
        path = directory / f"{role}.json"
        atomic_write_json(path, value, ensure_ascii=False)
        files.append(SnapshotFileDescriptor(role, path.name, path.stat().st_size, sha256_file(path)))
    counts = count_records(payloads, schema_version=3)
    legacy = LegacySnapshotManifestV3("legacy-v3-current", manifest.created_at,
        content_fingerprint(manifest.source_files), manifest.source_files, *counts, tuple(files),
        health=manifest.health, degraded_sources=manifest.degraded_sources, source_status=manifest.source_status,
        schema_version=3, components=manifest.components)
    atomic_write_json(directory / "manifest.json", legacy.to_dict())
    atomic_write_json(root / "snapshots/current.v2.json", {"schema_version": 2, "current_generation_id": legacy.generation_id})
    return legacy


def test_offline_conversion_is_current_v3_only_and_preserves_source(tmp_path, monkeypatch):
    from tooling.migration.v3_to_v4 import convert_current_v3
    source, target = tmp_path / "source", tmp_path / "converted"
    _legacy_current(source, monkeypatch)
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    result = convert_current_v3(source, target)
    assert result["schema_version"] == 4
    assert DataSnapshotClient(target / "snapshots").open_view().is_champion_complete("1")
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
    assert not (target / "snapshots/current.v2.json").exists()
    with pytest.raises(ValueError, match="new and independent"):
        convert_current_v3(source, target)


def test_offline_conversion_does_not_repair_corrupt_current_or_follow_previous(tmp_path, monkeypatch):
    from tooling.migration.v3_to_v4 import convert_current_v3
    root = tmp_path / "source"
    legacy = _legacy_current(root, monkeypatch)
    path = root / "snapshots/generations" / legacy.generation_id / "champions.json"
    path.write_bytes(b"[]")
    with pytest.raises(SnapshotValidationError):
        convert_current_v3(root, tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_migration_rejects_dotdot_target_inside_source_without_writing(tmp_path, monkeypatch):
    from tooling.migration.v3_to_v4 import convert_current_v3
    source = tmp_path / "source"
    _legacy_current(source, monkeypatch)
    (tmp_path / "alias").mkdir()
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="independent"):
        convert_current_v3(source, tmp_path / "alias" / ".." / "source" / "converted")
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == before
    assert not (source / "converted").exists()


def test_catalog_check_clock_never_rotates_verified_previous_identity(tmp_path, monkeypatch):
    from support.v4_release import _publish_independent_recognition_catalog
    from hextech.modules.data.generation.release_store import read_head, update_head
    from hextech.modules.data.catalog.versioned import load_runtime_catalog_from_pointer
    root = tmp_path / "source"
    make_release(root, monkeypatch)
    a = read_head(root / "snapshots")["recognition_catalog"]
    b = _publish_independent_recognition_catalog(root)
    update_head(root / "snapshots", recognition_catalog={**b, "last_success_at": "2099-01-01T00:00:00+00:00"})
    head = read_head(root / "snapshots")
    assert head["previous_recognition_catalog"] == a
    (root / "catalog/generations" / b["catalog_generation_id"] / "hero_version.txt").write_text("broken", encoding="utf-8")
    assert load_runtime_catalog_from_pointer(head["recognition_catalog"], runtime_root=root) is None
    assert load_runtime_catalog_from_pointer(head["previous_recognition_catalog"], runtime_root=root).generation_id == a["catalog_generation_id"]


def test_reordered_unit_references_do_not_publish_a_new_release(tmp_path, monkeypatch):
    manifest = make_release(tmp_path, monkeypatch)
    repeated = DataSnapshotPublisher(tmp_path / "snapshots").publish(None, source_files=tuple(reversed(manifest.source_files)),
        components=manifest.components, source_status=manifest.source_status, health=manifest.health,
        degraded_sources=manifest.degraded_sources)
    assert repeated.generation_id == manifest.generation_id
    assert list(repeated.source_files) == sorted(repeated.source_files, key=lambda p: (p.source, p.run_id, p.artifact_role))


def test_fingerprint_is_stable_across_process_hash_seeds():
    import os
    import subprocess
    import sys
    script = """from hextech.contracts import SourceProvenance
from hextech.modules.data.generation.release_store import fingerprint
refs=[SourceProvenance('aramkit',run,'catalog','scoped_stats','a'*64,1,'b'*64,1) for run in {'hero-1','hero-2','hero-3','hero-4'}]
print(fingerprint(refs,{}, {},'healthy',()))
"""
    values = [subprocess.check_output([sys.executable, "-B", "-c", script], env={**os.environ, "PYTHONHASHSEED": seed}).strip()
              for seed in ("1", "987654")]
    assert values[0] == values[1]


def test_public_query_results_cannot_mutate_frozen_cached_data(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    view = DataSnapshotClient(tmp_path / "snapshots").open_view()
    detail = view.get_champion_detail("1")
    detail["augments"][0]["winrate"] = -999
    hints = view.get_overlay_hints()
    hints["name_index"].clear()
    hints["source"]["production_augment_pool"]["canonical_ids"].clear()
    assert view.get_champion_detail("1")["augments"][0]["winrate"] == .55
    assert view.resolve_augment("强化一")["canonical_id"] == "1001"


@pytest.mark.parametrize("alias", ["ARAM_Test", "aram_test", "ARAM_TEST", "aramtest"])
def test_current_release_resolves_normalized_internal_aliases(tmp_path, monkeypatch, alias):
    from hextech.modules.recommendation.hints import query_overlay_hint
    make_release(tmp_path, monkeypatch)
    view = DataSnapshotClient(tmp_path / "snapshots").open_view()
    assert view.resolve_augment(alias)["canonical_id"] == "1001"
    assert query_overlay_hint(view.get_overlay_hints(), alias)["hint"]["augment_id"] == "1001"


def test_verified_catalog_cache_rechecks_changed_asset_bytes(tmp_path, monkeypatch):
    manifest = make_release(tmp_path, monkeypatch)
    closure(tmp_path, manifest)
    image = next(p for p in closure_paths(tmp_path, manifest) if p.suffix == ".png")
    path = tmp_path / image
    path.write_bytes(path.read_bytes() + b"bad")
    with pytest.raises((ValueError, RuntimeError)):
        closure(tmp_path, manifest)


def test_existing_target_never_skips_new_build_seed_integrity(tmp_path, monkeypatch):
    source, target = tmp_path / "seed", tmp_path / "already-running"
    seed = make_release(source, monkeypatch)
    make_release(target, monkeypatch)
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    image = next(p for p in closure_paths(source, seed) if p.suffix == ".png")
    (source / image).write_bytes(b"broken supplied Build baseline")
    with pytest.raises(ValueError):
        install_v4_seed(source, target)
    assert {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()} == before


def test_error_after_atomic_commit_does_not_remove_published_release(tmp_path, monkeypatch):
    import hextech.modules.data.generation as generation
    manifest = make_release(tmp_path, monkeypatch)
    update = generation.update_head
    def commit_then_fail(*args, **kwargs):
        update(*args, **kwargs)
        raise OSError("postcommit diagnostic failure")
    monkeypatch.setattr(generation, "update_head", commit_then_fail)
    published = DataSnapshotPublisher(tmp_path / "snapshots").publish(None,
        source_files=manifest.source_files, components=manifest.components, source_status=manifest.source_status,
        health="healthy")
    assert published.generation_id != manifest.generation_id
    assert DataSnapshotClient(tmp_path / "snapshots").open_view().manifest.generation_id == published.generation_id

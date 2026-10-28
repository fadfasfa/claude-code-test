"""Per-client release proof reuse, bounded lazy data, and independent role heads."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher, SnapshotValidationError
from hextech.modules.data.generation.release_store import update_head
from hextech.modules.data.ports.atomic import atomic_write_json
from support.v4_release import make_release, _publish_independent_recognition_catalog


def _clone(root, manifest, identity):
    cloned = replace(manifest, generation_id=identity)
    atomic_write_json(root / "snapshots/generations" / identity / "manifest.json", cloned.to_dict())
    update_head(root / "snapshots", current_generation_id=identity)
    return cloned


def _many_release(root, monkeypatch, count=3):
    """Construct a declared multi-hero fixture without touching deployed data."""
    from hextech.modules.data import source_runs
    from hextech.modules.data.catalog.versioned import build_catalog_manifest, sha256_file, CatalogView
    from hextech.infrastructure.sources.aramkit.catalog_binding import CatalogBinding
    from hextech.infrastructure.sources.aramkit.incremental_projection import IncrementalProjection
    from hextech.infrastructure.sources.aramkit.schema import resolve_version, normalize_rankings, normalize_detail
    from hextech.infrastructure.sources.aramkit.units import publish_unit
    from support.v4_release import _write_catalog, _ranking, _version, _detail
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: root.joinpath(*parts))
    directory, _, catalog_manifest = _write_catalog(root)
    hero_path = directory / "英雄目录.v1.json"
    heroes = json.loads(hero_path.read_text(encoding="utf-8"))
    for hero in map(str, range(2, count + 1)):
        name = f"英雄{hero}"
        heroes["aliases"].append({"heroId": hero, "heroName": name, "aliases": []})
        heroes["id_to_name"][hero] = {"heroName": name, "enName": f"Hero{hero}"}
        heroes["id_to_detail"][hero] = name
    atomic_write_json(hero_path, heroes, ensure_ascii=False)
    catalog_manifest = build_catalog_manifest(directory, created_at=catalog_manifest.created_at)
    final = directory.parent / catalog_manifest.catalog_generation_id
    directory.replace(final)
    atomic_write_json(final / "manifest.json", catalog_manifest.to_dict())
    catalog = CatalogView(final, catalog_manifest, sha256_file(final / "manifest.json"))
    binding = CatalogBinding(catalog.generation_id, catalog.content_sha256,
                             frozenset(map(str, range(1, count + 1))), frozenset({1001}))
    version = resolve_version(_version())
    rows = normalize_rankings({"rows": [_ranking(str(i)) for i in range(1, count + 1)]})
    ranking = publish_unit(version, rows, binding=binding)
    units = {row["id"]: publish_unit(version, normalize_detail(_detail(_ranking(row["id"]), 1001), row),
                                    binding=binding, champion_id=row["id"]) for row in rows}
    build = IncrementalProjection(catalog, runtime_root=root).build_references(ranking, units, {})
    return DataSnapshotPublisher(root / "snapshots").publish(None, source_files=build.source_files,
        components=build.components, source_status=build.source_status, health="degraded", degraded_sources=("apex", "mayhem"))


def test_same_manifest_reuses_query_without_rewalking_units_or_catalog(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    first = client.open_view()
    champions = first.get_champions()
    import hextech.modules.data.generation.query as query
    def forbidden(*args, **kwargs):
        pytest.fail("warm query reopened a validated unit or Catalog closure")
    monkeypatch.setattr(query, "closure", forbidden)
    monkeypatch.setattr(first._payloads, "unit", forbidden)
    assert client.open_view()._payloads is first._payloads
    assert client.get_champions() == champions


def test_cache_is_per_client_and_keeps_only_two_generation_queries(tmp_path, monkeypatch):
    manifest = make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    first = client.open_view()
    assert DataSnapshotClient(client.root).open_view()._payloads is not first._payloads
    second = _clone(tmp_path, manifest, "second")
    second_query = client.open_view()._payloads
    assert client.open_generation(manifest.generation_id)._payloads is first._payloads
    _clone(tmp_path, second, "third")
    client.open_view()
    assert len(client._query_cache) == 2
    assert client.open_generation("second")._payloads is not second_query
    assert first.get_champions()[0]["id"] == "1"  # held views survive LRU eviction


def test_changed_manifest_bytes_in_same_generation_require_new_proof(tmp_path, monkeypatch):
    manifest = make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    first = client.open_view()
    path = client.root / "generations" / manifest.generation_id / "manifest.json"
    changed = replace(manifest, created_at="2026-10-20T00:00:00+00:00")
    atomic_write_json(path, changed.to_dict())
    second = client.open_view()
    assert second._payloads is not first._payloads
    assert second.manifest.created_at == changed.created_at
    assert len(client._query_cache) == 1
    changed.components["ranking"]["source_version"] = "tampered"
    atomic_write_json(path, changed.to_dict())
    with pytest.raises(SnapshotValidationError, match="fingerprint mismatch"):
        client.open_view()


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_bad_current_manifest_falls_back_to_cached_previous(tmp_path, monkeypatch, damage):
    manifest = make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    previous = client.open_view()
    _clone(tmp_path, manifest, "current")
    client.open_view()
    path = client.root / "generations/current/manifest.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupt")
    view = client.open_view()
    assert view.degraded and view.failed_generation_id == "current"
    assert view._payloads is previous._payloads


def test_recognition_head_moves_without_rebuilding_statistics_query(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    first = client.open_view()
    _publish_independent_recognition_catalog(tmp_path)
    assert client.open_view()._payloads is first._payloads
    _clone(tmp_path, first.manifest, "statistics-next")
    assert client.open_view()._payloads is not first._payloads


def test_views_and_results_do_not_share_mutable_manifest_or_dtos(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    old = client.open_view()
    old.manifest.components["champions"].clear()
    old.manifest.source_status["aramkit"].coverage["poisoned"] = True
    old.manifest.source_status.clear()
    old.get_champions()[0]["name"] = "poisoned"
    detail = old.get_champion_detail("1")
    detail["augments"][0]["winrate"] = -999
    view = client.open_view()
    assert view.manifest.champion_count == 1 and view.manifest.source_status
    assert "poisoned" not in view.manifest.source_status["aramkit"].coverage
    assert view.get_champions()[0]["name"] == "英雄一"
    assert view.get_champion_detail("1")["augments"][0]["winrate"] == .55


def test_hero_bodies_are_lazy_and_two_entry_lru_revalidates_evictions(tmp_path, monkeypatch):
    _many_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    query = client.open_view()._payloads
    assert not query._hero_cache
    assert len(client.get_champions()) == 3 and not query._hero_cache
    original = Path.read_bytes
    reads = []
    def observed(path, *args, **kwargs):
        if "scoped_stats" in path.parts:
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", observed)
    client.get_champion_detail("1")
    assert len(reads) == 2
    client.get_champion_detail("2")
    client.get_champion_detail("1")
    client.get_champion_detail("3")
    assert list(query._hero_cache) == ["1", "3"]
    assert len(reads) == 6
    client.get_champion_detail("2")
    assert len(reads) == 8


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_cached_memory_is_safe_but_uncached_hero_bytes_must_still_verify(tmp_path, monkeypatch, damage):
    _many_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    first = client.get_champion_detail("1")
    query = client.open_view()._payloads
    for hero in ("1", "2"):
        pointer = query.heroes[hero]
        index_path, index = query.unit(pointer)
        child = index_path.parent / index["files"][0]["relative_path"]
        if damage == "missing":
            child.unlink()
        else:
            child.write_bytes(b"broken")
    assert client.get_champion_detail("1") == first
    with pytest.raises(SnapshotValidationError):
        client.get_champion_detail("2")


def test_manifest_path_reparse_is_not_hidden_by_cache(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    client.open_view()
    original = Path.lstat
    def reparsed(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == client.root:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", reparsed)
    with pytest.raises(SnapshotValidationError, match="reparse"):
        client.open_view()


def test_lazy_unit_reparse_is_rejected_even_when_generation_is_cached(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    query = client.open_view()._payloads
    original = Path.lstat
    base = tmp_path / "sources/aramkit/runs" / query.heroes["1"]["run_id"]
    def reparsed(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == base:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", reparsed)
    assert client.open_view()._payloads is query
    with pytest.raises(SnapshotValidationError, match="reparse"):
        client.get_champion_detail("1")


def test_lazy_json_never_parses_different_bytes_after_digest_check(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    query = client.open_view()._payloads
    index_path, index = query.unit(query.heroes["1"])
    child = index_path.parent / index["files"][0]["relative_path"]
    original = Path.read_bytes
    def substituted(path, *args, **kwargs):
        raw = original(path, *args, **kwargs)
        if path == child:
            # A valid JSON replacement of equal size must still fail its digest.
            return raw.replace(b"0.55", b"0.99")
        return raw
    monkeypatch.setattr(Path, "read_bytes", substituted)
    with pytest.raises(SnapshotValidationError, match="hash mismatch"):
        client.get_champion_detail("1")


def test_shared_client_serializes_concurrent_cache_and_query_mutations(tmp_path, monkeypatch):
    make_release(tmp_path, monkeypatch)
    client = DataSnapshotClient(tmp_path / "snapshots")
    def read(_):
        view = client.open_view()
        detail = view.get_champion_detail("1")
        detail["augments"][0]["winrate"] = -1
        return view._payloads, view.resolve_augment("强化一")["canonical_id"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(read, range(40)))
    assert len({id(query) for query, _ in rows}) == 1
    assert {identity for _, identity in rows} == {"1001"}
    assert len(client._query_cache) == 1
    assert client.get_champion_detail("1")["augments"][0]["winrate"] == .55

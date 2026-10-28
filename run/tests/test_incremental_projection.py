from copy import deepcopy
from dataclasses import asdict

import pytest

from hextech.contracts import ItemOutcome, SourceHealth, SourceRunManifestV2, SourceStatusV2
from hextech.infrastructure.sources.aramkit.catalog_binding import CatalogBinding
from hextech.infrastructure.sources.aramkit.incremental_projection import IncrementalProjection
from hextech.infrastructure.sources.aramkit.projection import build_aramkit_payloads
from hextech.infrastructure.sources.aramkit.schema import normalize_detail, normalize_rankings, resolve_version
from hextech.infrastructure.sources.aramkit.service import _write_artifact
from hextech.infrastructure.sources.aramkit.units import publish_unit
from hextech.modules.data import source_runs
from hextech.modules.data.catalog.versioned import CatalogView, build_catalog_manifest, sha256_file
from hextech.modules.data.ports.atomic import atomic_write_json
from test_aramkit_source import _detail, _ranking, _version


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: runtime.joinpath(*parts))
    root = runtime / "catalog/fixture"
    root.mkdir(parents=True)
    atomic_write_json(root / "英雄目录.v1.json", {
        "aliases": [{"heroId": str(i), "heroName": f"Hero{i}", "enName": f"Hero{i}", "aliases": []} for i in (1,2)],
        "id_to_name": {str(i): {"heroName": f"Hero{i}", "enName": f"Hero{i}"} for i in (1, 2)}})
    entries = [{"name": "强化一", "cdragon_id": 10, "tier": "Gold", "augment_name_id": "augment_one"}]
    atomic_write_json(root / "海克斯资源目录.v1.json", {"entries": entries})
    atomic_write_json(root / "augment_assets.v1.json", {"schema_version": 1, "entries": []})
    from hextech.modules.acquisition.hextech.production_pool import build_production_augment_pool
    pool = build_production_augment_pool({"10": {"enabled": True,"name":"augment_one","displayName":"强化一","rarity":"Gold"}},
        entries,schema_version=2,upstream_marker_sha256="a"*64)
    pool.update(mode="aram-mayhem",source_marker_sha256="a"*64)
    atomic_write_json(root / "augment_identities.v2.json", pool)
    (root / "hero_version.txt").write_text("16.15", encoding="utf-8")
    manifest = build_catalog_manifest(root, created_at="2026-01-01T00:00:00Z")
    atomic_write_json(root / "manifest.json", asdict(manifest))
    target = runtime / "catalog/generations" / manifest.catalog_generation_id
    target.parent.mkdir(parents=True)
    root.replace(target)
    catalog = CatalogView(target, manifest, sha256_file(target / "manifest.json"))
    binding = CatalogBinding(catalog.generation_id, catalog.content_sha256, frozenset({"1", "2"}), frozenset({10}))
    raw_rows = [_ranking("1", win_rate=0.53), _ranking("2", win_rate=0.49)]
    rows = normalize_rankings({"rows": raw_rows})
    version = resolve_version(_version())
    details = {row["id"]: normalize_detail(_detail(raw, 10), row) for raw, row in zip(raw_rows, rows, strict=True)}
    ranking = publish_unit(version, rows, binding=binding)
    heroes = {hero: publish_unit(version, detail, binding=binding, champion_id=hero) for hero, detail in details.items()}
    return catalog, binding, version, rows, details, ranking, heroes


def release_view(catalog, build):
    from hextech.modules.data.generation import DataSnapshotPublisher, DataSnapshotClient
    root = catalog.root.parents[2] / "snapshots"
    DataSnapshotPublisher(root).publish(None,source_files=build.source_files,components=build.components,
        source_status=build.source_status,health="degraded",degraded_sources=("apex","mayhem"))
    return DataSnapshotClient(root).open_view()


def test_rank_first_then_individual_complete_heroes(inputs):
    catalog, _, _, _, _, ranking, heroes = inputs
    projection = IncrementalProjection(catalog)
    first = projection.build_references(ranking, {}, {})
    initial = release_view(catalog, first)
    assert len(initial.get_champions()) == 2
    assert initial.get_champion_detail("1")["data_status"] == "pending"
    assert not any(item["complete"] for item in first.components["champions"].values())
    second = projection.build_references(ranking, {"1": heroes["1"]}, {})
    partial = release_view(catalog, second)
    assert partial.is_champion_complete("1") and not partial.is_champion_complete("2")
    final = projection.build_references(ranking, heroes, {})
    completed = release_view(catalog, final)
    assert all(item["complete"] for item in final.components["champions"].values())
    assert initial.get_champions() == completed.get_champions()
    assert initial.get_champion_detail("1")["data_status"] == "pending"
    assert not first.payloads and not final.payloads
    assert final.source_status["apex"]["data_status"] == "pending"
    assert final.source_status["aramkit"]["data_at"] == "1970-01-01T00:00:01.234000Z"
    assert len([p for p in final.source_files if p.source == "aramkit"]) == 3
    for status in final.source_status.values():
        SourceStatusV2.from_mapping(status)


def test_ranked_champion_ids_uses_the_same_validated_cohort_as_build(inputs):
    catalog, binding, version, rows, _, _, heroes = inputs
    reduced = publish_unit(version, rows[:1], binding=binding)
    projection = IncrementalProjection(catalog)

    assert projection.ranked_champion_ids(reduced) == frozenset({"1"})
    with pytest.raises(ValueError, match="absent from ranking cohort"):
        projection.build_references(reduced, heroes, {})


def full_pointer(catalog, version, details, run_id="full-fixture"):
    path, count = _write_artifact(run_id, version, details)
    artifact = source_runs.build_artifact_descriptor(path, role="scoped_stats", relative_path="scoped_stats/manifest.json",
                                                     record_count=count, content_schema_version=1)
    manifest = SourceRunManifestV2(source="aramkit", run_id=run_id, catalog_generation_id=catalog.generation_id,
        catalog_sha256=catalog.content_sha256, health=SourceHealth.HEALTHY, started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:00Z", expected_items=len(details), successful_items=len(details),
        confirmed_empty_items=0, failed_items=0, artifact=artifact,
        outcomes=tuple(ItemOutcome(item, "success", "detail", record_count=1) for item in details), metadata={"version":version})
    return source_runs.publish_source_run(manifest)


def test_formula_parity_and_old_full_pointer_is_shared(inputs, monkeypatch):
    catalog, _, version, _, details, ranking, _ = inputs
    pointer = full_pointer(catalog, version, details)
    expected, _ = build_aramkit_payloads(pointer, catalog=catalog)
    projection = IncrementalProjection(catalog)
    first = projection.build_references(ranking, {"1": pointer, "2": pointer}, {})
    assert release_view(catalog, first).get_champions() == expected
    assert len([p for p in first.source_files if p.artifact_role == "scoped_stats"]) == 1
    import hextech.infrastructure.sources.aramkit.incremental_projection as module
    monkeypatch.setattr(module, "validated_source_artifact", lambda *a, **k: pytest.fail("cached inputs reread"))
    assert release_view(catalog, projection.build_references(ranking, {"1": pointer, "2": pointer}, {})).get_champions() == expected


def test_old_hero_version_is_preserved_not_falsely_rebound(inputs):
    catalog, _, version, _, details, ranking, _ = inputs
    pointer = full_pointer(catalog, {**version, "dataPath": "data/older"}, details)
    result = IncrementalProjection(catalog).build_references(ranking, {"1": pointer}, {})
    assert result.components["ranking"]["source_version"] == version["dataPath"]
    assert result.components["champions"]["1"]["source_version"] == "data/older"
    assert release_view(catalog,result).get_champion_detail("1")["data_status"] == "data_stale"


@pytest.mark.parametrize("which", ["manifest", "body", "catalog"])
def test_tamper_after_cached_read_is_rejected(inputs, which):
    catalog, _, _, _, _, ranking, heroes = inputs
    projection = IncrementalProjection(catalog)
    projection.build_references(ranking, heroes, {})
    if which == "catalog":
        path = catalog.root / "英雄目录.v1.json"
    elif which == "manifest":
        path = source_runs.source_run_dir("aramkit", heroes["1"]["run_id"]) / "manifest.json"
    else:
        path = source_runs.source_run_dir("aramkit", heroes["1"]["run_id"]) / "scoped_stats/champions/1.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="changed"):
        projection.build_references(ranking, heroes, {})


def test_catalog_mismatch_and_hero_mismatch_rejected(inputs):
    catalog, _, _, _, _, ranking, heroes = inputs
    projection = IncrementalProjection(catalog)
    wrong = deepcopy(heroes["1"])
    wrong["catalog_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="Catalog"):
        projection.build_references(ranking, {"1": wrong}, {})
    with pytest.raises(ValueError, match="hero mismatch|incomplete"):
        projection.build_references(ranking, {"1": heroes["2"]}, {})
    result = projection.build_references(ranking, {}, {"apex": wrong})
    assert result.source_status["apex"]["data_reason"] == "optional_source_rejected"


def test_first_read_tamper_is_rejected(inputs):
    catalog, _, _, _, _, ranking, heroes = inputs
    path = source_runs.source_run_dir("aramkit", heroes["1"]["run_id"]) / "scoped_stats/champions/1.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        IncrementalProjection(catalog).build_references(ranking, heroes, {})


def test_incomplete_stage_does_not_claim_complete(inputs):
    catalog, binding, version, _, details, ranking, _ = inputs
    incomplete = deepcopy(details["1"])
    incomplete["augments"]["stages"]["4"] = []
    pointer = publish_unit(version, incomplete, binding=binding, champion_id="1")
    with pytest.raises(ValueError, match="incomplete"):
        IncrementalProjection(catalog).build_references(ranking, {"1": pointer}, {})


def optional_pointer(catalog, source, payload, *, metadata=None):
    role = {"apex": "synergy", "mayhem": "combos", "blitz": "augment_ranking"}[source]
    run_id = "optional-" + source
    path = source_runs.source_run_dir(source, run_id) / "payload.json"
    atomic_write_json(path, payload)
    artifact = source_runs.build_artifact_descriptor(path, role=role, relative_path="payload.json",
                                                     record_count=1, content_schema_version=1)
    manifest = SourceRunManifestV2(source=source, run_id=run_id, catalog_generation_id=catalog.generation_id,
        catalog_sha256=catalog.content_sha256, health=SourceHealth.HEALTHY, started_at="2026-01-01T00:00:00Z",
        completed_at="2026-01-01T00:00:00Z", expected_items=1, successful_items=1,
        confirmed_empty_items=0, failed_items=0, artifact=artifact,
        outcomes=(ItemOutcome("1", "success", "detail", record_count=1),), metadata=metadata or {})
    return source_runs.publish_source_run(manifest)


@pytest.mark.parametrize("available", [("apex",), ("mayhem",), ("apex", "mayhem")])
def test_optional_sources_merge_independently(inputs, available):
    catalog, _, _, _, _, ranking, heroes = inputs
    apex = {"1": {"name": "Hero1", "synergy_items": [{"augment_names": ["强化一"],
             "content": "Apex content", "author": "Apex", "tier": "Gold", "rating": "5"}]}}
    mayhem = {"items": [{"champion_id": "1", "augment_names": ["强化一"], "body": "Mayhem content"}]}
    candidates = {source: optional_pointer(catalog, source, apex if source == "apex" else mayhem) for source in available}
    result = IncrementalProjection(catalog).build_references(ranking, heroes, candidates)
    view = release_view(catalog,result)
    items = view.get_champion_detail("1")["synergy"]["synergy_items"]
    assert len(items) == 1
    assert items[0]["content"] == ("Apex content" if "apex" in available else "Mayhem content")
    assert all(result.source_status[source]["data_status"] == "fresh" for source in available)
    assert view.get_overlay_hints()["hints"]["10"]["synergies"]


def test_rank_only_publisher_accepts_complete_v4_provenance(inputs, tmp_path):
    from hextech.modules.data.generation import DataSnapshotPublisher, DataSnapshotClient
    catalog, _, _, _, _, ranking, _ = inputs
    result = IncrementalProjection(catalog).build_references(ranking, {}, {})
    root = catalog.root.parents[2] / "snapshots"
    publisher = DataSnapshotPublisher(root)
    generation = publisher.publish(result.payloads, source_files=result.source_files,
        components=result.components, source_status=result.source_status, require_complete_provenance=True)
    assert generation
    view = DataSnapshotClient(root).open_view()
    assert view.get_champion_detail("1")["data_status"] == "pending"


def test_v4_scoped_queries_resolve_distinct_hero_runs(inputs, tmp_path):
    from hextech.modules.data.generation import DataSnapshotPublisher, DataSnapshotClient
    from hextech.modules.data.scoped_stats import ScopedStatsCache
    catalog, _, _, _, _, ranking, heroes = inputs
    result = IncrementalProjection(catalog).build_references(ranking, heroes, {})
    root = catalog.root.parents[2] / "snapshots"
    DataSnapshotPublisher(root).publish(result.payloads, source_files=result.source_files,
        components=result.components, source_status=result.source_status, require_complete_provenance=True)
    view = DataSnapshotClient(root).open_view()
    cache = ScopedStatsCache()
    for hero_id in ("1", "2"):
        loaded = cache.load(view, hero_id)
        assert loaded.available, loaded.reason
        assert loaded.run_id == heroes[hero_id]["run_id"]
        assert loaded.view.champion_id == hero_id
        assert loaded.view.select("10", 3).record["win_rate"] == 0.55
        assert loaded.view.select("10", 3).stats_scope == "stage_3"
    first_detail = view.get_champion_detail("1")
    manifest = source_runs.source_run_dir("aramkit", heroes["1"]["run_id"]) / "manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    damaged = ScopedStatsCache().load(view, "1")
    assert not damaged.available and damaged.reason == "aramkit_run_manifest_hash_mismatch"
    assert view.get_champion_detail("1") == first_detail


def test_retired_blitz_is_not_projected_into_new_generation(inputs):
    catalog, _, _, _, _, ranking, _ = inputs
    with pytest.raises(ValueError, match="retired"):
        IncrementalProjection(catalog).build_references(ranking, {}, {"blitz": {}})


@pytest.mark.parametrize("data_at,expected", [
    ("2020-01-01T00:00:00Z", "2020-01-01T00:00:00+00:00"),
    ("bad", "2026-01-01T00:00:00Z"),
    ("2020-01-01T00:00:00", "2026-01-01T00:00:00Z"),
    ("2999-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
])
def test_projection_uses_validated_acquisition_time_metadata(inputs, data_at, expected):
    catalog, _, _, _, _, ranking, _ = inputs
    pointer = optional_pointer(catalog, "apex", {}, metadata={"data_at": data_at})
    projection = IncrementalProjection(catalog)
    result = projection.build_references(ranking, {}, {"apex": pointer})
    assert result.source_status["apex"]["data_at"] == expected
    loaded = projection._load("aramkit", ranking, "hero_rankings")
    loaded.manifest["metadata"]["data_at"] = "2025-01-01T00:00:00Z"
    assert projection._status(loaded)["data_at"] == "1970-01-01T00:00:01.234000Z"


def test_aramkit_missing_upstream_time_does_not_use_local_completion(inputs):
    catalog, _, _, _, _, ranking, _ = inputs
    projection = IncrementalProjection(catalog)
    loaded = projection._load("aramkit", ranking, "hero_rankings")
    loaded.manifest["metadata"]["version"].pop("buildTimeUnixMs", None)
    assert projection._status(loaded)["data_at"] == ""

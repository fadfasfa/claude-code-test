from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from hextech.contracts import DataContractError, DataSnapshotManifestV2, SourceProvenance
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher, SnapshotValidationError
from hextech.modules.data.generation.validation import count_records, validate_complete_provenance


def _payload(*, pending: bool = False) -> dict:
    detail = {"hero_id": 1, "augments": [{"id": 9, "win_rate": 0.51}]}
    if pending:
        detail.update(augments=[], data_status="pending")
    return {
        "champions": [{"id": 1, "name": "hero"}],
        "champion_hextech": {"hero": detail},
        "overlay_hints": {"hints": {"9": {"augment_id": "9", "name": "augment"}},
                          "name_index": {"9": "9", "augment": "9"}},
        "identities": {"schema_version": 2, "champions": {"1": "hero"},
                       "augments": {} if pending else {"9": "augment"}},
    }


def _components(*, complete: bool = True) -> dict:
    return {"ranking": {"source_version": "rank-v1", "catalog_id": "catalog-hex"},
            "champions": {"1": {"source_version": "hero-v1", "catalog_id": "catalog-hex", "complete": complete}}}


def test_default_v3_writer_and_legacy_v2_reader_and_constructor(tmp_path: Path) -> None:
    manifest = DataSnapshotPublisher(tmp_path).publish(_payload())
    assert manifest.schema_version == 3
    assert DataSnapshotClient(tmp_path).open_view().is_champion_complete(1)
    legacy = replace(manifest, schema_version=2, components={})
    args = legacy.to_dict()
    args.pop("schema_version")
    args.pop("components")
    args["files"] = legacy.files
    args["source_files"] = legacy.source_files
    assert DataSnapshotManifestV2(**args).schema_version == 2
    path = tmp_path / "generations" / manifest.generation_id / "manifest.json"
    payload = legacy.to_dict()
    payload.pop("components")
    path.write_text(json.dumps(payload), encoding="utf-8")
    view = DataSnapshotClient(tmp_path).open_view()
    assert view.manifest.schema_version == 2
    assert view.is_champion_complete("hero")


def test_rank_only_v3_accepts_explicit_pending_but_v2_stays_strict(tmp_path: Path) -> None:
    payload = _payload(pending=True)
    with pytest.raises(SnapshotValidationError, match="非空统计"):
        count_records(payload)
    manifest = DataSnapshotPublisher(tmp_path).publish(payload, components=_components(complete=False))
    assert manifest.schema_version == 3
    assert (manifest.augment_count, manifest.stat_record_count) == (0, 0)
    view = DataSnapshotClient(tmp_path).open_view()
    assert not view.is_champion_complete(1)
    assert not view.is_champion_complete(2)
    assert view.get_overlay_hints()["hints"]["9"]["name"] == "augment"


@pytest.mark.parametrize("mutation", ["pending", "missing_stage", "empty_stage"])
def test_complete_marker_cannot_override_missing_actual_details(tmp_path: Path, mutation: str) -> None:
    payload = _payload(pending=mutation == "pending")
    if mutation != "pending":
        payload["champion_hextech"]["hero"]["stages"] = {str(n): [{"id": 9}] for n in range(1, 5)}
        stages = payload["champion_hextech"]["hero"]["stages"]
        if mutation == "missing_stage":
            stages.pop("4")
        else:
            stages["4"] = []
    with pytest.raises(SnapshotValidationError, match="complete champion"):
        DataSnapshotPublisher(tmp_path).publish(payload, components=_components())
    DataSnapshotPublisher(tmp_path).publish(payload, components=_components(complete=False))
    assert not DataSnapshotClient(tmp_path).open_view().is_champion_complete(1)


def test_component_versions_participate_in_dedupe_and_status_is_a_copy(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    components = _components()
    first = publisher.publish(_payload(), components=components)
    same = publisher.publish(_payload(), components=components)
    assert same.generation_id == first.generation_id
    components["champions"]["1"]["source_version"] = "hero-v2"
    second = publisher.publish(_payload(), components=components)
    assert second.generation_id != first.generation_id
    view = DataSnapshotClient(tmp_path).open_view()
    projected = view.status()["components"]
    projected["champions"]["1"]["complete"] = False
    assert view.is_champion_complete(1)
    components["champions"]["1"]["complete"] = False
    assert second.components["champions"]["1"]["complete"] is True


@pytest.mark.parametrize("malformed", [
    None, [], {}, {"unknown": {}},
    {"ranking": {}, "champions": {}},
    {"ranking": {"source_version": 1, "catalog_id": "catalog"}, "champions": {"1": {}}},
])
def test_malformed_components_fail_closed(tmp_path: Path, malformed: object) -> None:
    manifest = DataSnapshotPublisher(tmp_path).publish(_payload())
    payload = manifest.to_dict()
    payload["components"] = malformed
    with pytest.raises(DataContractError):
        DataSnapshotManifestV2.from_mapping(payload)


@pytest.mark.parametrize("field,value", [("complete", "true"), ("catalog_id", "../catalog"), ("unknown", True)])
def test_manifest_component_tampering_is_unavailable(tmp_path: Path, field: str, value: object) -> None:
    manifest = DataSnapshotPublisher(tmp_path).publish(_payload(), components=_components())
    payload = manifest.to_dict()
    payload["components"]["champions"]["1"][field] = value
    path = tmp_path / "generations" / manifest.generation_id / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert DataSnapshotClient(tmp_path).status()["state"] == "unavailable"


def test_component_hero_projection_and_stats_union_remain_exact(tmp_path: Path) -> None:
    components = _components()
    components["champions"]["2"] = deepcopy(components["champions"]["1"])
    with pytest.raises(SnapshotValidationError, match="身份不一致"):
        DataSnapshotPublisher(tmp_path).publish(_payload(), components=components)
    payload = _payload(pending=True)
    payload["identities"]["augments"] = {"9": "augment"}
    with pytest.raises(SnapshotValidationError, match="完全一致"):
        DataSnapshotPublisher(tmp_path).publish(payload, components=_components(complete=False))


@pytest.mark.parametrize("state", ["pending", "confirmed_empty", "unavailable"])
def test_optional_source_states_survive_status_projection(tmp_path: Path, state: str) -> None:
    DataSnapshotPublisher(tmp_path).publish(_payload(), source_status={"apex": {"data_status": state,
                                                                               "data_at": "2000-01-01T00:00:00Z"}})
    assert DataSnapshotClient(tmp_path).status()["source_status"]["apex"]["data_status"] == state


def _source(source: str, role: str, run_id: str) -> SourceProvenance:
    return SourceProvenance(source=source, artifact_role=role, run_id=run_id,
                            catalog_generation_id="catalog-hex", artifact_sha256="a" * 64,
                            manifest_sha256="b" * 64, record_count=1, content_schema_version=2)


def test_v3_provenance_accepts_ranking_and_distinct_champion_runs_only() -> None:
    sources = [_source("catalog", role, "catalog-hex") for role in ("champions", "augments", "versions")]
    sources.append(_source("aramkit", "hero_rankings", "rank-v1"))
    validate_complete_provenance(sources, schema_version=3)
    with pytest.raises(SnapshotValidationError):
        validate_complete_provenance(sources)
    sources.extend([_source("aramkit", "scoped_stats", f"hero-{n}") for n in (1, 2)])
    validate_complete_provenance(sources, schema_version=3)
    with pytest.raises(SnapshotValidationError, match="角色重复"):
        validate_complete_provenance([*sources, sources[-1]], schema_version=3)


@pytest.mark.parametrize("run_id", ["missing", "rank-v1"])
def test_champion_reference_cannot_bind_missing_or_rank_only_run(tmp_path: Path, run_id: str) -> None:
    components = _components()
    components["ranking"]["run_id"] = "rank-v1"
    components["champions"]["1"]["run_id"] = run_id
    with pytest.raises(SnapshotValidationError, match="provenance 不一致"):
        DataSnapshotPublisher(tmp_path).publish(_payload(), components=components,
                                               source_files=[_source("aramkit", "hero_rankings", "rank-v1")])


def test_production_complete_units_require_run_binding(tmp_path: Path) -> None:
    sources = [_source("catalog", role, "catalog-hex") for role in ("champions", "augments", "versions")]
    sources.append(_source("aramkit", "scoped_stats", "hero-v1"))
    with pytest.raises(SnapshotValidationError, match="缺少 run_id"):
        DataSnapshotPublisher(tmp_path).publish(_payload(), components=_components(), source_files=sources,
                                               require_complete_provenance=True)


def test_source_version_is_logical_text_not_a_filesystem_identifier(tmp_path: Path) -> None:
    components = _components()
    components["ranking"]["source_version"] = "data/16.15-fixture"
    components["champions"]["1"]["source_version"] = "https://example.invalid/data/16.15"
    manifest = DataSnapshotPublisher(tmp_path).publish(_payload(), components=components)
    assert manifest.components == components


@pytest.mark.parametrize("version", ["\nversion", "version\x00", "x" * 257, " "])
def test_source_version_rejects_controls_empty_and_unbounded_text(version: str) -> None:
    components = _components()
    components["ranking"]["source_version"] = version
    with pytest.raises(DataContractError, match="source_version"):
        DataSnapshotManifestV2.validate_components(components)

"""Synthetic release fixtures shared by packaging, lifecycle and data tests."""
import json
import shutil
from dataclasses import replace
from hextech.contracts import CatalogManifestV2
from pathlib import Path
from typing import Any, Mapping
from PIL import Image
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.catalog.versioned import build_catalog_manifest, sha256_file
DATA_PATH = "data/16.15-fixture"
def _write_catalog(runtime: Path) -> tuple[Path, dict[str, object], object]:
    staging = runtime / "catalog" / "fixture"
    staging.mkdir(parents=True)
    champion = {
        "schema_version": 1,
        "aliases": [{"heroId": "1", "heroName": "英雄一", "title": "", "enName": "HeroOne", "aliases": []}],
        "alias_to_id": {"英雄一": "1", "HeroOne": "1"},
        "id_to_name": {"1": {"heroName": "英雄一", "title": "", "enName": "HeroOne"}},
        "id_to_detail": {"1": "英雄一"},
    }
    atomic_write_json(staging / "英雄目录.v1.json", champion, ensure_ascii=False)
    icon_temp = staging / "icon.png"
    Image.new("RGB", (8, 8), (20, 40, 60)).save(icon_temp)
    icon_sha = sha256_file(icon_temp)
    icon_relative = f"assets/augments/{icon_sha}.png"
    icon_path = staging / icon_relative
    icon_path.parent.mkdir(parents=True)
    icon_temp.replace(icon_path)
    augment = {
        "schema_version": 1,
        "entries": [
            {
                "source_id": "1001",
                "cdragon_id": 1001,
                "name": "强化一",
                "augment_name_id": "ARAM_Test",
                "filename": icon_path.name,
                "local_path": icon_relative,
                "icon_sha256": icon_sha,
            }
        ],
        "name_to_icon": {"强化一": icon_relative},
    }
    atomic_write_json(staging / "海克斯资源目录.v1.json", augment, ensure_ascii=False)
    (staging / "hero_version.txt").write_text("fixture", encoding="utf-8")
    atomic_write_json(
        staging / "augment_assets.v1.json",
        {
            "schema_version": 1,
            "entries": [
                {
                    "canonical_id": "1001",
                    "augment_name_id": "ARAM_Test",
                    "relative_path": icon_relative,
                    "sha256": icon_sha,
                    "size": icon_path.stat().st_size,
                }
            ],
        },
    )
    from hextech.modules.acquisition.hextech.production_pool import build_production_augment_pool
    pool = build_production_augment_pool({"1001": {"enabled": True, "displayName": "强化一", "name": "ARAM_Test", "rarity": "kGold"}},
        augment["entries"], schema_version=2, upstream_marker_sha256="a" * 64)
    pool.update(mode="aram-mayhem", source_marker_sha256="a" * 64)
    atomic_write_json(staging / "augment_identities.v2.json", pool, ensure_ascii=False)
    manifest = build_catalog_manifest(staging, created_at="2026-08-13T00:00:00+00:00")
    final = runtime / "catalog" / "generations" / manifest.catalog_generation_id
    final.parent.mkdir(parents=True)
    staging.replace(final)
    manifest_path = final / "manifest.json"
    atomic_write_json(manifest_path, manifest.to_dict(), ensure_ascii=False, indent=2)
    pointer = {
        "schema_version": 2,
        "catalog_generation_id": manifest.catalog_generation_id,
        "content_sha256": manifest.content_sha256,
        "manifest_sha256": sha256_file(manifest_path),
        "completed_at": manifest.created_at,
        "last_success_at": manifest.created_at,
    }
    from hextech.modules.data.generation.release_store import update_head
    update_head(runtime / "snapshots", recognition_catalog=pointer)
    return final, pointer, manifest

def _version(*, data_path: str = DATA_PATH, build_time: int = 1234) -> dict[str, Any]:
    return {
        "latest": "16.15",
        "versions": [
            {
                "version": "16.15",
                "dataPath": data_path,
                "buildTimeUnixMs": build_time,
                "allMatches": 999,
            }
        ],
    }

def _ranking(champion_id: str, *, win_rate: float = 0.51) -> dict[str, Any]:
    return {
        "id": champion_id,
        "rank": int(champion_id),
        "tier": "S",
        "sampleCount": 100,
        "winRate": win_rate,
        "pickRate": 0.1,
        "blueWinRate": 0.52,
        "redWinRate": 0.50,
    }

def _augment(augment_id: int, *, rank: int = 1) -> dict[str, Any]:
    return {
        "id": augment_id,
        "rank": rank,
        "sampleCount": 50,
        "winRate": 0.55,
        "pickRate": 0.2,
        "blueWinRate": 0.56,
        "redWinRate": 0.54,
    }

def _detail(ranking: Mapping[str, Any], augment_id: int) -> dict[str, Any]:
    all_row = {**_augment(augment_id), "stageAgnostic": True, "availableStages": ["1", "2", "3", "4"]}
    return {
        "champion": {
            "id": ranking["id"],
            "rank": ranking["rank"],
            "tier": ranking["tier"],
            "stats": {
                "sampleCount": ranking["sampleCount"],
                "winRate": ranking["winRate"],
                "pickRate": ranking["pickRate"],
                "blueWinRate": ranking["blueWinRate"],
                "redWinRate": ranking["redWinRate"],
            },
        },
        "augments": {
            "all": [all_row],
            "stages": {stage: [_augment(augment_id)] for stage in ("1", "2", "3", "4")},
        },
        "items": {"filtered": ["必须被丢弃"]},
        "builds": {"unfiltered": ["必须被丢弃"]},
        "summoners": ["必须被丢弃"],
    }

def make_release(root, monkeypatch, *, complete=True):
    from hextech.modules.data import source_runs
    from hextech.modules.data.generation import DataSnapshotPublisher
    from hextech.infrastructure.sources.aramkit.catalog_binding import CatalogBinding
    from hextech.infrastructure.sources.aramkit.schema import resolve_version, normalize_rankings, normalize_detail
    from hextech.infrastructure.sources.aramkit.units import publish_unit
    from hextech.infrastructure.sources.aramkit.incremental_projection import IncrementalProjection
    from hextech.modules.data.catalog.versioned import CatalogView
    root = Path(root)
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: root.joinpath(*parts))
    catalog_root, pointer, manifest = _write_catalog(root)
    catalog = CatalogView(catalog_root, manifest, pointer["manifest_sha256"])
    binding = CatalogBinding(manifest.catalog_generation_id, manifest.content_sha256, frozenset({"1"}), frozenset({1001}))
    version = resolve_version(_version())
    rows = normalize_rankings({"rows": [_ranking("1")]})
    ranking = publish_unit(version, rows, binding=binding)
    heroes = {}
    if complete:
        heroes["1"] = publish_unit(version, normalize_detail(_detail(_ranking("1"),1001), rows[0]), binding=binding, champion_id="1")
    build = IncrementalProjection(catalog, runtime_root=root).build_references(ranking, heroes, {})
    released = DataSnapshotPublisher(root / "snapshots").publish(None, source_files=build.source_files,
        components=build.components, source_status=build.source_status, health="degraded", degraded_sources=("apex","mayhem"))
    return released



def _publish_independent_recognition_catalog(runtime: Path) -> dict[str, object]:
    from hextech.modules.data.generation.release_store import read_head, update_head
    current = read_head(runtime / "snapshots")["recognition_catalog"]
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
    update_head(runtime / "snapshots", recognition_catalog=pointer)
    return pointer

def release_runtime(tmp_path, *, details=True):
    import pytest
    root = Path(tmp_path) / "runtime"
    with pytest.MonkeyPatch.context() as patch:
        manifest = make_release(root, patch, complete=details)
    child = None
    if details:
        source = next(r for r in manifest.source_files if r.source == "aramkit" and r.artifact_role == "scoped_stats")
        run = root / "sources/aramkit/runs" / source.run_id
        raw = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        index_path = run / raw["artifact"]["relative_path"]
        index = json.loads(index_path.read_text(encoding="utf-8"))
        child = index_path.parent / index["files"][0]["relative_path"]
    return root, manifest.generation_id, child

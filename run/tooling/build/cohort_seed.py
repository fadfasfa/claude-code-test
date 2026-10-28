"""Collect only the verified v4 release and its immutable reference closure."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.release_store import checked_file, closure, closure_paths, read_head, catalog_paths

COHORT_SEED_DIR = Path("resources") / "seeds" / "runtime.v4"
SOURCE_ROLES = ("aramkit", "apex", "mayhem")

@dataclass(frozen=True)
class CohortSeed:
    runtime_root: Path
    files: tuple[Path, ...]
    metadata: dict[str, Any]

    def bundled_name(self, path: Path) -> str:
        return (COHORT_SEED_DIR / path.relative_to(self.runtime_root)).as_posix()


def collect_cohort_seed(snapshot_root: Path) -> CohortSeed:
    snapshots = snapshot_root.resolve()
    if snapshots.name.casefold() != "snapshots":
        raise ValueError("verified snapshot root must be a snapshots directory")
    runtime = snapshots.parent
    view = DataSnapshotClient(snapshots).open_view()
    manifest = view.manifest
    if manifest.schema_version != 4 or view.degraded:
        raise ValueError("packaging requires an intact v4 release; migrate v3 offline")
    catalog, ranking, heroes, optional = closure(runtime, manifest)
    head = checked_file(snapshots, "current.v4.json")
    head_state = read_head(snapshots)
    if head_state.get("current_generation_id") != manifest.generation_id:
        raise ValueError("release head changed during seed collection")
    files = {head, checked_file(snapshots, f"generations/{manifest.generation_id}/manifest.json")}
    files.update(runtime / relative for relative in closure_paths(runtime, manifest))
    if previous := head_state["previous_generation_id"]:
        previous_view = DataSnapshotClient(snapshots).open_generation(previous)
        files.add(checked_file(snapshots, f"generations/{previous}/manifest.json"))
        files.update(runtime / relative for relative in closure_paths(runtime, previous_view.manifest))
    for role in ("recognition_catalog", "previous_recognition_catalog"):
        if head_state[role]:
            files.update(runtime / relative for relative in catalog_paths(runtime, head_state[role]))
    units = {f"{ref.source}:{ref.run_id}:{ref.artifact_role}": ref.to_dict()
             for ref in manifest.source_files if ref.source != "catalog"}
    from tooling.build.recognition_contract import recognition_catalog_view
    from hextech.infrastructure.persistence.production_pool_binding import _catalog_production_pool
    recognition_catalog = recognition_catalog_view(runtime, head_state)
    pool = _catalog_production_pool(recognition_catalog)
    from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool
    validate_production_augment_pool(pool)
    ordered = tuple(sorted((p.resolve() for p in files), key=lambda p: p.as_posix()))
    if read_head(snapshots) != head_state:
        raise ValueError("release head changed during seed collection")
    from hextech.modules.data.catalog.versioned import sha256_file
    return CohortSeed(runtime, ordered, {
        "schema_version": 3, "snapshot_schema_version": 4,
        "generation_id": manifest.generation_id,
        "statistics_manifest_sha256": sha256_file(snapshots / "generations" / manifest.generation_id / "manifest.json"),
        "recognition_catalog": dict(head_state["recognition_catalog"]),
        "catalog_generation_id": catalog.manifest.catalog_generation_id,
        "recognition_catalog_generation_id": recognition_catalog.generation_id,
        "source_run_ids": {source: pointer["run_id"] for source, pointer in {"aramkit": ranking, **optional}.items()},
        "units": units,
        "production_pool_id": str(pool.get("pool_id") or pool.get("production_pool_id") or ""),
        "production_pool_count": len(pool["canonical_ids"]),
        "full_catalog_count": int(pool.get("full_catalog_count") or 0), "file_count": len(ordered),
    })

__all__ = ["COHORT_SEED_DIR", "CohortSeed", "collect_cohort_seed"]

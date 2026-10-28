"""A validated v4 release is its own recovery point; no mutable mirror journal."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Any
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.release_store import closure

@dataclass(frozen=True)
class CohortCandidate:
    generation_id: str
    generation_created_at: str
    manifest_health: str
    pointers: Mapping[str, Mapping[str, Any]]
    production_pool_id: str
    production_pool_count: int
    snapshot_schema_version: int = 4
    units: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    @property
    def sort_time(self):
        return datetime.fromisoformat(self.generation_created_at.replace("Z", "+00:00"))

def validate_generation_cohort(root, generation_id):
    view = DataSnapshotClient(Path(root) / "snapshots").open_generation(generation_id)
    catalog, ranking, heroes, optional = closure(root, view.manifest)
    pointer = {"schema_version": 2, "catalog_generation_id": catalog.generation_id,
               "content_sha256": catalog.content_sha256, "manifest_sha256": catalog.manifest_sha256}
    import json
    identity_path = catalog.root / "augment_identities.v2.json"
    pool = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.is_file() else {}
    return CohortCandidate(generation_id, view.manifest.created_at, view.manifest.health,
        {"catalog": pointer, "aramkit": ranking, **optional}, str(pool.get("pool_id", "")), len(pool.get("canonical_ids", ())),
        units={"ranking": ranking, **{f"hero:{hero}": p for hero, p in heroes.items()}})

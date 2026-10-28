"""Vision admission uses current recognition Catalog; statistics keep their own binding."""
from pathlib import Path
from typing import Any, Mapping
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.release_store import closure, read_head, catalog_paths, checked_file, object_at
from hextech.modules.data.catalog.versioned import CatalogView
from hextech.contracts import CatalogManifestV2
from hextech.infrastructure.persistence.production_pool_binding import _catalog_production_pool


def recognition_catalog_view(root: Path, head=None):
    head = read_head(root / "snapshots") if head is None else head
    pointer = head["recognition_catalog"]
    catalog_paths(root, pointer)
    path = checked_file(root, f"catalog/generations/{pointer['catalog_generation_id']}/manifest.json",
                        digest=pointer["manifest_sha256"])
    return CatalogView(path.parent, CatalogManifestV2.from_mapping(object_at(path)), pointer["manifest_sha256"])


def recognition_pool_contract(root: Path, cohort: Mapping[str, Any]) -> dict[str, Any]:
    view = DataSnapshotClient(root / "snapshots").open_generation(str(cohort["generation_id"]))
    statistics_catalog, *_ = closure(root, view.manifest)
    catalog = recognition_catalog_view(root)
    expected = str(cohort.get("recognition_catalog_generation_id") or cohort.get("catalog_generation_id") or "")
    if expected != catalog.generation_id:
        raise ValueError("release Catalog does not match expected recognition identity")
    pool = _catalog_production_pool(catalog)
    if str(cohort.get("catalog_generation_id")) != statistics_catalog.generation_id:
        raise ValueError("statistics Catalog does not match release metadata")
    return {"catalog_generation_id": catalog.generation_id, "recognition_catalog_id": catalog.generation_id,
            "production_pool_id": str(pool["pool_id"]), "production_pool_count": len(pool["canonical_ids"]),
            "full_catalog_count": int(pool["full_catalog_count"])}

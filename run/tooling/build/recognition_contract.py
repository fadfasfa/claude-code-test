"""Sidecar admission follows its validated recognition Catalog, not statistics provenance."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from hextech.infrastructure.persistence.cohort_seed_catalog import validated_catalog_manifest
from hextech.infrastructure.persistence.production_pool_binding import _catalog_production_pool
from hextech.modules.data.catalog.versioned import CatalogValidationError, CatalogView


def recognition_pool_contract(root: Path, cohort: Mapping[str, Any]) -> dict[str, Any]:
    catalog_id = str(cohort.get("recognition_catalog_generation_id") or cohort.get("catalog_generation_id") or "")
    if not cohort.get("recognition_catalog_generation_id"):
        # Legacy single-Catalog metadata already binds this pool to that Catalog.
        return {
            "catalog_generation_id": catalog_id, "recognition_catalog_id": catalog_id,
            "production_pool_id": str(cohort.get("production_pool_id") or ""),
            "production_pool_count": int(cohort.get("production_pool_count") or 0),
            "full_catalog_count": int(cohort.get("full_catalog_count") or 0),
        }
    pointer = json.loads((root / "catalog/current.v2.json").read_text(encoding="utf-8"))
    if not isinstance(pointer, dict) or pointer.get("catalog_generation_id") != catalog_id:
        raise ValueError("recognition Catalog current does not match expected identity")
    try:
        manifest = validated_catalog_manifest(root, pointer)
    except CatalogValidationError as exc:
        raise ValueError("recognition Catalog artifact validation failed") from exc
    catalog = CatalogView(root / "catalog/generations" / catalog_id, manifest, str(pointer["manifest_sha256"]))
    pool = _catalog_production_pool(catalog)
    return {
        "catalog_generation_id": catalog_id, "recognition_catalog_id": catalog_id,
        "production_pool_id": str(pool["pool_id"]),
        "production_pool_count": len(pool["canonical_ids"]),
        "full_catalog_count": int(pool["full_catalog_count"]),
    }

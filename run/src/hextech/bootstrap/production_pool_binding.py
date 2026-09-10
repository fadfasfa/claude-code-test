"""把 Hextech source run 的生产识别池绑定到待发布 snapshot。

只处理 generation/Catalog 一致性与 fail-closed 语义；不构建视觉模板，也不读取
完整 Catalog 作为候选回退。
"""

from __future__ import annotations

import json
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool
from hextech.modules.data.source_runs import load_source_run_manifest


def _catalog_production_pool(catalog: Any) -> dict[str, Any]:
    """从 Catalog 已冻结的 augment assets 恢复原生产识别闭集。"""

    from hextech.modules.acquisition.common.icons import normalize_augment_name
    from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries

    assets_path = Path(catalog.root) / "augment_assets.v1.json"
    try:
        assets_payload = json.loads(assets_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("production_pool_catalog_assets_unavailable") from exc
    assets = assets_payload.get("entries") if isinstance(assets_payload, Mapping) else None
    if not isinstance(assets, list) or not assets:
        raise ValueError("production_pool_catalog_assets_unavailable")
    catalog_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in load_augment_manifest_entries(catalog.root):
        if not isinstance(entry, Mapping):
            continue
        raw_id = entry.get("cdragon_id")
        try:
            canonical_id = str(int(raw_id))
        except (TypeError, ValueError):
            continue
        if int(canonical_id) <= 0:
            continue
        if canonical_id in catalog_by_id:
            raise ValueError(f"production_pool_catalog_duplicate_identity:{canonical_id}")
        catalog_by_id[canonical_id] = entry
    identities: list[dict[str, Any]] = []
    for raw_asset in assets:
        if not isinstance(raw_asset, Mapping):
            raise ValueError("production_pool_catalog_assets_invalid")
        canonical_id = str(raw_asset.get("canonical_id") or "")
        catalog_entry = catalog_by_id.get(canonical_id)
        if not isinstance(catalog_entry, Mapping):
            raise ValueError(f"production_pool_catalog_identity_missing:{canonical_id}")
        name = str(catalog_entry.get("name") or "").strip()
        augment_name_id = str(catalog_entry.get("augment_name_id") or raw_asset.get("augment_name_id") or "")
        identities.append(
            {
                "canonical_id": canonical_id,
                "name": name,
                "normalized_name": normalize_augment_name(name),
                "augment_name_id": augment_name_id,
                "tier": str(catalog_entry.get("tier") or ""),
                "enabled": True,
                "visual_variants": [
                    {
                        "variant_id": augment_name_id or canonical_id,
                        "name": name,
                        "augment_name_id": augment_name_id,
                        "filename": Path(str(raw_asset.get("relative_path") or "")).name,
                        "local_path": str(raw_asset.get("relative_path") or ""),
                        "source_icon_path": str(raw_asset.get("source_icon_path") or ""),
                        "source_icon_url": str(catalog_entry.get("source_icon_url") or ""),
                        "icon_sha256": str(raw_asset.get("sha256") or ""),
                    }
                ],
                "icon_ambiguous": False,
            }
        )
    identities.sort(
        key=lambda item: (
            not str(item["canonical_id"]).isdigit(),
            int(item["canonical_id"]) if str(item["canonical_id"]).isdigit() else str(item["canonical_id"]),
        )
    )
    marker = hashlib.sha256(
        json.dumps(
            {
                "catalog_generation_id": catalog.generation_id,
                "asset_ids": [item["canonical_id"] for item in identities],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    pool: dict[str, Any] = {
        "schema_version": 1,
        "catalog_generation_id": catalog.generation_id,
        "catalog_sha256": catalog.content_sha256,
        "catalog_manifest_sha256": catalog.manifest_sha256,
        "metadata_marker_sha256": marker,
        "metadata_total_count": len(identities),
        "full_catalog_count": next(
            item.record_count for item in catalog.manifest.files if item.role == "augments"
        ),
        "enabled_count": len(identities),
        "disabled_count": 0,
        "canonical_ids": [str(item["canonical_id"]) for item in identities],
        "identities": identities,
        "disabled_ids": [],
        "unresolved_ids": [],
        "duplicate_ids": [],
        "name_conflicts": [],
        "migration": {
            "baseline": "catalog_augment_assets",
            "baseline_count": len(identities),
            "added_count": 0,
            "removed_count": 0,
            "added_ids": [],
            "removed_ids": [],
            "renamed_count": 0,
            "renamed": [],
            "marker_changed": False,
            "change_without_marker": False,
        },
        "state": "ready",
    }
    pool["pool_id"] = f"production-pool-v1-{marker[:32]}"
    return pool


def _normalized_asset_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("/")


def validate_production_pool_assets(pool: Mapping[str, Any], catalog_root: Path) -> None:
    """逐 ID 绑定 production pool 与 Catalog 冻结资产，阻断混合 generation。"""

    path = Path(catalog_root) / "augment_assets.v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("production_pool_catalog_assets_unavailable") from exc
    entries = payload.get("entries") if isinstance(payload, Mapping) else None
    if not isinstance(entries, list):
        raise ValueError("production_pool_catalog_assets_unavailable")

    assets_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise ValueError("production_pool_catalog_assets_invalid")
        canonical_id = str(raw.get("canonical_id") or "").strip()
        if not canonical_id:
            raise ValueError("production_pool_catalog_assets_invalid")
        if canonical_id in assets_by_id:
            duplicate_ids.add(canonical_id)
        assets_by_id[canonical_id] = raw

    canonical_ids = [str(value).strip() for value in pool.get("canonical_ids", ())]
    pool_ids = set(canonical_ids)
    asset_ids = set(assets_by_id)
    def sort_key(value: str) -> tuple[bool, int | str]:
        return not value.isdigit(), int(value) if value.isdigit() else value

    missing_ids = sorted(pool_ids - asset_ids, key=sort_key)
    unknown_ids = sorted(asset_ids - pool_ids, key=sort_key)
    identities = pool.get("identities")
    identity_items = identities if isinstance(identities, list) else []
    identities_by_id = {
        str(item.get("canonical_id") or "").strip(): item
        for item in identity_items
        if isinstance(item, Mapping)
    }
    unbound_ids: list[str] = []
    for canonical_id in canonical_ids:
        identity = identities_by_id.get(canonical_id)
        asset = assets_by_id.get(canonical_id)
        if identity is None or asset is None:
            continue
        expected_path = _normalized_asset_path(asset.get("relative_path"))
        expected_sha = str(asset.get("sha256") or "").strip()
        variants = identity.get("visual_variants")
        variant_items = variants if isinstance(variants, list) else []
        matched = any(
            _normalized_asset_path(variant.get("local_path")) == expected_path
            and str(variant.get("icon_sha256") or "").strip() == expected_sha
            for variant in variant_items
            if isinstance(variant, Mapping)
        )
        if not expected_path or not expected_sha or not matched:
            unbound_ids.append(canonical_id)

    if missing_ids or unknown_ids or duplicate_ids or unbound_ids:
        raise ValueError(
            "production_pool_catalog_asset_mismatch "
            f"missing={missing_ids[:10]} unknown={unknown_ids[:10]} "
            f"duplicate={sorted(duplicate_ids)[:10]} unbound={unbound_ids[:10]}"
        )


def bind_production_pool(
    overlay_hints: dict[str, Any],
    *,
    catalog: Any,
    stats_pointer: Mapping[str, Any] | None = None,
    hextech_pointer: Mapping[str, Any] | None = None,
    legacy_baseline: bool,
) -> None:
    if legacy_baseline:
        pool: Mapping[str, Any] = {
            "schema_version": 1,
            "state": "unavailable",
            "reason": "legacy_baseline_without_production_pool",
            "identities": [],
            "canonical_ids": [],
        }
    elif stats_pointer is not None:
        pool = _catalog_production_pool(catalog)
        validate_production_augment_pool(pool)
        validate_production_pool_assets(pool, Path(catalog.root))
    elif hextech_pointer is not None:
        manifest = load_source_run_manifest("hextech", str(hextech_pointer.get("run_id") or ""))
        coverage = manifest.metadata.get("coverage") if manifest is not None else None
        candidate = coverage.get("production_augment_pool") if isinstance(coverage, Mapping) else None
        if not isinstance(candidate, Mapping):
            raise ValueError("production_pool_unavailable")
        validate_production_augment_pool(candidate)
        if (
            str(candidate.get("catalog_generation_id") or "") != catalog.generation_id
            or str(candidate.get("catalog_sha256") or "") != catalog.content_sha256
            or str(candidate.get("catalog_manifest_sha256") or "") != catalog.manifest_sha256
        ):
            raise ValueError("production pool 与 generation Catalog 不一致")
        validate_production_pool_assets(candidate, Path(catalog.root))
        pool = candidate
    else:
        raise ValueError("production_pool_source_pointer_missing")
    source = overlay_hints.setdefault("source", {})
    if not isinstance(source, dict):
        raise ValueError("overlay_hints.source 必须是对象")
    source["production_augment_pool"] = dict(pool)


__all__ = ["bind_production_pool", "validate_production_pool_assets"]

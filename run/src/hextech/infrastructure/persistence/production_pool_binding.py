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

from hextech.modules.acquisition.hextech.production_pool import (
    validate_production_augment_pool, validate_pool_asset_descriptors,
)


def _catalog_production_pool(catalog: Any) -> dict[str, Any]:
    """新目录以 enabled metadata 为身份权威；资产反推仅兼容旧 v1。"""

    identity_path = Path(catalog.root) / "augment_identities.v2.json"
    if identity_path.is_file():
        try:
            pool = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("production_pool_identity_catalog_invalid") from exc
        if not isinstance(pool, dict) or pool.get("schema_version") != 2 or pool.get("mode") != "aram-mayhem":
            raise ValueError("production_pool_identity_catalog_invalid")
        # Identity artifact intentionally omits its enclosing generation hash to
        # avoid circular content addressing. Bind only after Catalog validation.
        descriptor = next((item for item in catalog.manifest.files if item.role == "augment_identities"), None)
        if descriptor is None or hashlib.sha256(identity_path.read_bytes()).hexdigest() != descriptor.sha256:
            raise ValueError("production_pool_identity_catalog_unbound")
        pool.update(catalog_generation_id=catalog.generation_id, catalog_sha256=catalog.content_sha256,
                    catalog_manifest_sha256=catalog.manifest_sha256)
        validate_production_augment_pool(pool)
        validate_production_pool_assets(pool, Path(catalog.root))
        return pool

    raise ValueError("production requires authoritative v2 identity Catalog")


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
    if pool.get("schema_version") != 2:
        raise ValueError("production_pool_schema_invalid")
    validate_pool_asset_descriptors(pool, entries)


def bind_production_pool(
    overlay_hints: dict[str, Any],
    *,
    catalog: Any,
    stats_pointer: Mapping[str, Any] | None = None,
    hextech_pointer: Mapping[str, Any] | None = None,
    legacy_baseline: bool,
) -> None:
    if legacy_baseline or hextech_pointer is not None:
        raise ValueError("legacy production pool input retired; migrate offline")
    pool = _catalog_production_pool(catalog)
    validate_production_pool_assets(pool, Path(catalog.root))
    source = overlay_hints.setdefault("source", {})
    if not isinstance(source, dict):
        raise ValueError("overlay_hints.source 必须是对象")
    source["production_augment_pool"] = dict(pool)


__all__ = ["bind_production_pool", "validate_production_pool_assets"]

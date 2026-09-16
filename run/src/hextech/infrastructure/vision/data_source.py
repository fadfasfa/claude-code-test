"""Vision Catalog 读取适配器；统计 generation 仅作为观察信息。"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from hextech.contracts.data_pipeline import require_identifier
from hextech.infrastructure.persistence.production_pool_binding import (
    _catalog_production_pool,
    validate_production_pool_assets,
)
from hextech.modules.data.catalog.versioned import (
    catalog_root,
    load_runtime_catalog,
    load_runtime_catalog_from_pointer,
    sha256_file,
)
from hextech.modules.data.overlay_source import SharedOverlayDataSource

VISION_TARGET_CATALOG_ENV = "HEXTECH_VISION_TARGET_CATALOG_ID"


class CatalogVisionDataSource(SharedOverlayDataSource):
    def __init__(self, *, catalog_id: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._catalog_id = catalog_id

    def read_hint_cache(self) -> dict[str, Any]:
        if self._catalog_id:
            identifier = require_identifier(self._catalog_id, field_name="catalog_generation_id")
            manifest_path = catalog_root() / "generations" / identifier / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            catalog = load_runtime_catalog_from_pointer({
                "schema_version": 2, "catalog_generation_id": identifier,
                "content_sha256": manifest["content_sha256"],
                "manifest_sha256": sha256_file(manifest_path),
            })
        else:
            catalog = load_runtime_catalog()
        if catalog is None:
            if self._catalog_id:
                raise ValueError("requested_recognition_catalog_unavailable")
            # 旧 seed 尚无独立 Catalog 发布时仅允许原已验证快照路径。
            return super().read_hint_cache()
        pool = _catalog_production_pool(catalog)
        validate_production_pool_assets(pool, catalog.root)
        hints: dict[str, Any] = {}
        names: dict[str, str] = {}
        for item in pool.get("identities", []):
            if not isinstance(item, Mapping) or not item.get("name"):
                continue
            identifier = str(item["canonical_id"])
            hints[identifier] = {"augment_id": identifier, "name": item["name"],
                                 "tier": item.get("tier", ""), "summary": ""}
            names[str(item["name"])] = identifier
        # 不读取统计正文，也不要求统计 snapshot 存在才能识别。
        generation_id = self._generation_id
        if not generation_id:
            from hextech.modules.data.generation import default_snapshot_root

            try:
                pointer = json.loads((default_snapshot_root() / "current.v2.json").read_text(encoding="utf-8"))
                generation_id = str(pointer.get("current_generation_id") or "")
            except (OSError, ValueError, AttributeError):
                generation_id = ""
        return {
            "schema_version": 1,
            "source": {"private_policy_stats_enabled": False, "production_augment_pool": pool},
            "hints": hints, "name_index": names,
            "snapshot": {"generation_id": generation_id},
            "vision": {"catalog_generation_id": catalog.generation_id,
                       "catalog_manifest_sha256": catalog.manifest_sha256},
        }


def prepare_catalog_vision_data() -> dict[str, Any]:
    return CatalogVisionDataSource().read_hint_cache()


def recognition_switch_blocked() -> bool:
    """消费 DataService 的短寿命游戏上下文；未知时不切换矩阵。"""
    from hextech.modules.data.ports.paths import get_var_dir

    try:
        payload = json.loads((get_var_dir() / "state/data-service/download_context.v1.json").read_text(encoding="utf-8"))
        age = time.time() - float(payload["observed_at"])
        return not (0 <= age <= 5 and payload.get("in_game") is False)
    except (OSError, ValueError, KeyError, TypeError):
        return True

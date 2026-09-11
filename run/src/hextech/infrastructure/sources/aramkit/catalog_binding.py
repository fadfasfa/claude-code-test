"""ARAMKit 活动 Catalog 与 blocked adoption Catalog 的窄兼容绑定。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.catalog.version_catalog import (
    load_augment_manifest_entries,
    load_champion_core_data,
)
from hextech.modules.data.catalog.versioned import (
    load_active_catalog,
    load_runtime_catalog_from_pointer,
)


class AramkitRefreshError(RuntimeError):
    """ARAMKit 候选无法满足完整、同版本、Catalog 绑定合同。"""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def _positive_catalog_augment_ids(catalog_root: Path) -> frozenset[int]:
    raw_ids = [
        int(value)
        for item in load_augment_manifest_entries(catalog_root)
        if (value := item.get("cdragon_id")) is not None
        and not isinstance(value, bool)
        and str(value).lstrip("-").isdigit()
        and int(value) > 0
    ]
    if len(raw_ids) != len(set(raw_ids)):
        raise AramkitRefreshError("catalog_duplicate_augment_id", "Catalog 正 cdragon_id 不唯一")
    return frozenset(raw_ids)


@dataclass(frozen=True)
class CatalogBinding:
    """一次抓取固定使用的活动闭集，以及 adoption 证明的新身份。"""

    generation_id: str
    content_sha256: str
    champion_ids: frozenset[str]
    augment_ids: frozenset[int]
    compatible_extra_augment_ids: frozenset[int] = frozenset()

    @classmethod
    def active(
        cls,
        *,
        compatibility_pointer: str | Path | None = None,
    ) -> "CatalogBinding":
        catalog = load_active_catalog()
        champions = frozenset(str(item) for item in load_champion_core_data(catalog.root))
        augment_ids = _positive_catalog_augment_ids(catalog.root)
        compatible_extra_augment_ids = frozenset()
        if compatibility_pointer is not None:
            try:
                payload = json.loads(Path(compatibility_pointer).read_text(encoding="utf-8"))
                compatibility_catalog = (
                    load_runtime_catalog_from_pointer(payload)
                    if isinstance(payload, Mapping)
                    else None
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise AramkitRefreshError(
                    "catalog_compatibility_invalid",
                    "待采用 Catalog pointer 无法验证",
                ) from exc
            if compatibility_catalog is None:
                raise AramkitRefreshError(
                    "catalog_compatibility_invalid",
                    "待采用 Catalog pointer 无法打开",
                )
            compatibility_ids = _positive_catalog_augment_ids(compatibility_catalog.root)
            compatible_extra_augment_ids = compatibility_ids - augment_ids
        if not champions or not augment_ids:
            raise AramkitRefreshError("catalog_empty", "Catalog 英雄或海克斯闭集为空")
        return cls(
            generation_id=catalog.generation_id,
            content_sha256=catalog.content_sha256,
            champion_ids=champions,
            augment_ids=augment_ids,
            compatible_extra_augment_ids=compatible_extra_augment_ids,
        )


def filter_detail_to_catalog(
    detail: Mapping[str, Any],
    allowed_augment_ids: frozenset[int],
) -> dict[str, Any]:
    """投影回活动 Catalog；调用方只可传 adoption 已证明的新身份。"""

    augments = detail.get("augments")
    if not isinstance(augments, Mapping):
        return dict(detail)
    all_rows = [
        dict(item)
        for item in (augments.get("all") or [])
        if isinstance(item, Mapping) and int(item["id"]) in allowed_augment_ids
    ]
    stages = augments.get("stages")
    filtered_stages = (
        {
            str(stage): [
                dict(item)
                for item in rows
                if isinstance(item, Mapping) and int(item["id"]) in allowed_augment_ids
            ]
            for stage, rows in stages.items()
            if isinstance(rows, list)
        }
        if isinstance(stages, Mapping)
        else {}
    )
    return {
        **dict(detail),
        "augments": {
            **dict(augments),
            "all": all_rows,
            "stages": filtered_stages,
        },
    }


__all__ = ["AramkitRefreshError", "CatalogBinding", "filter_detail_to_catalog"]

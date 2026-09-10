"""动态 ARAM: Mayhem 生产识别闭集合同。"""

from __future__ import annotations

import json

import pytest

from hextech.bootstrap.production_pool_binding import validate_production_pool_assets
from hextech.modules.acquisition.hextech.production_pool import (
    LEGACY_SELECTION_POOL_IDS,
    build_production_augment_pool,
    validate_production_augment_pool,
)


def _metadata(ids: list[int], *, enabled: bool = True) -> dict[str, dict]:
    return {
        str(value): {
            "displayName": f"强化 {value}",
            "name": f"ARAM_Augment_{value}",
            "rarity": 1,
            "enabled": enabled,
        }
        for value in ids
    }


def _catalog(ids: list[int]) -> list[dict]:
    return [
        {
            "source_id": str(value),
            "name": f"强化 {value}",
            "augment_name_id": f"ARAM_Augment_{value}",
            "filename": f"{value}.png",
            "local_path": f"assets/augments/{value}.png",
            "icon_sha256": f"{index:064x}",
        }
        for index, value in enumerate(ids, start=1)
    ]


def test_legacy_206_baseline_is_fixed_and_current_migration_reports_30_added() -> None:
    added = [
        1310, 1339, 1342, 1343, 1372, 1400, 1406, 1409, 1414, 1424,
        2003, 2108, 2109, 2126, 2148,
        *range(7001, 7016),
    ]
    current_ids = sorted([int(value) for value in LEGACY_SELECTION_POOL_IDS] + added)
    pool = build_production_augment_pool(
        _metadata(current_ids),
        _catalog(current_ids),
        upstream_marker_sha256="a" * 64,
    )

    validate_production_augment_pool(pool)
    assert len(LEGACY_SELECTION_POOL_IDS) == 206
    assert pool["enabled_count"] == 236
    assert pool["migration"]["baseline_count"] == 206
    assert pool["migration"]["added_ids"] == [str(value) for value in added]
    assert pool["migration"]["removed_ids"] == []


def test_disabled_identity_is_reported_but_never_ranked() -> None:
    metadata = _metadata([1001, 1002])
    metadata.update(_metadata([1064], enabled=False))

    pool = build_production_augment_pool(metadata, _catalog([1001, 1002, 1064]))

    assert pool["canonical_ids"] == ["1001", "1002"]
    assert pool["disabled_ids"] == ["1064"]


def test_same_icon_marks_both_identities_ambiguous_without_merging_them() -> None:
    catalog = _catalog([7003, 7011])
    for entry in catalog:
        entry["icon_sha256"] = "a" * 64
        entry["local_path"] = "assets/augments/shared.png"

    pool = build_production_augment_pool(_metadata([7003, 7011]), catalog)

    assert pool["canonical_ids"] == ["7003", "7011"]
    assert all(item["icon_ambiguous"] for item in pool["identities"])


def test_pool_change_against_last_good_requires_metadata_marker_change() -> None:
    previous = build_production_augment_pool(
        _metadata([1001, 1002]),
        _catalog([1001, 1002]),
        upstream_marker_sha256="a" * 64,
    )
    current = build_production_augment_pool(
        _metadata([1001, 1002, 1003]),
        _catalog([1001, 1002, 1003]),
        upstream_marker_sha256="a" * 64,
        last_good_pool=previous,
    )

    assert current["state"] == "unavailable"
    assert current["migration"]["change_without_marker"] is True


def test_production_pool_assets_require_exact_id_path_and_hash_binding(tmp_path) -> None:
    catalog = _catalog([1001, 1002, 1003])
    pool = build_production_augment_pool(
        _metadata([1001, 1002, 1003]),
        catalog,
        upstream_marker_sha256="b" * 64,
    )
    assets = [
        {
            "canonical_id": entry["source_id"],
            "relative_path": entry["local_path"],
            "sha256": entry["icon_sha256"],
        }
        for entry in catalog
    ]
    path = tmp_path / "augment_assets.v1.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": assets}), encoding="utf-8")

    validate_production_pool_assets(pool, tmp_path)

    assets.pop()
    assets.append(
        {
            "canonical_id": "7008",
            "relative_path": "assets/augments/7008.png",
            "sha256": "f" * 64,
        }
    )
    path.write_text(json.dumps({"schema_version": 1, "entries": assets}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing=\\['1003'\\].*unknown=\\['7008'\\]"):
        validate_production_pool_assets(pool, tmp_path)

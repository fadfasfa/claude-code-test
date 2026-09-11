"""Hextech 候选代的统计覆盖报告与发布门禁。

报告的分母始终来自本轮上游 metadata，而不是视觉 Catalog 的全部条目。Catalog
只参与可投影条目的回归检查，避免把不同模式的视觉资源误判为统计来源缺口。
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pandas as pd

from hextech.modules.acquisition.hextech.production_pool import (
    build_production_augment_pool,
    validate_production_augment_pool,
)


AVAILABILITY_POLICY = "public_non_null_v1"
MIN_RECORDS_PER_CHAMPION = 10
MIN_TOTAL_RECORDS = 9_000
MIN_MEDIAN_RECORDS_PER_CHAMPION = 50
MIN_METADATA_COVERAGE_RATIO = 0.80
MIN_CATALOG_PROJECTION_COVERAGE_RATIO = 0.85
MAX_COVERAGE_REGRESSION = 0.05


class HextechCoverageError(ValueError):
    """候选统计覆盖不满足发布门禁。"""

    coverage: Mapping[str, Any] | None = None

    def __init__(self, message: str, *, reason_code: str = "coverage_gate_failed") -> None:
        super().__init__(message)
        self.reason_code = str(reason_code or "coverage_gate_failed")


def normalize_source_id(value: object) -> str:
    """统一 JSON、CSV 与 Catalog 中可能出现的整数型 ID。"""

    text = str(value or "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _unique_ids(values: Iterable[object]) -> set[str]:
    return {normalized for value in values if (normalized := normalize_source_id(value))}


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _catalog_source_ids(entries: Sequence[Mapping[str, Any]]) -> tuple[set[str], int]:
    """只提取能映射到统计 metadata 的 Catalog source ID。

    `cdragon_id` 是当前 Catalog 与上游 metadata 之间稳定的桥；保留其他字段是为了
    兼容以后补入明确 source ID 的 Catalog，而不是让 637 个视觉条目成为分母。
    """

    ids: set[str] = set()
    entry_count = 0
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        candidates = (
            entry.get("source_id"),
            entry.get("augment_id"),
            entry.get("cdragon_id"),
        )
        source_id = next((normalize_source_id(value) for value in candidates if normalize_source_id(value)), "")
        if source_id and source_id != "-1":
            ids.add(source_id)
            entry_count += 1
    return ids, entry_count


def _metadata_identity_map(
    metadata_entries: Mapping[object, object] | None,
    metadata_ids: Iterable[object],
) -> dict[str, dict[str, Any]]:
    if isinstance(metadata_entries, Mapping):
        result: dict[str, dict[str, Any]] = {}
        for raw_id, raw_value in metadata_entries.items():
            raw = raw_value if isinstance(raw_value, Mapping) else {}
            source_id = normalize_source_id(raw_id)
            if not source_id:
                continue
            has_identity = any(str(raw.get(field) or "").strip() for field in ("displayName", "name", "id", "augmentId"))
            if has_identity:
                result[source_id] = dict(raw)
        if result:
            return result
    return {
        source_id: {"displayName": source_id, "name": source_id, "enabled": True}
        for source_id in _unique_ids(metadata_ids)
    }


def _sample_ids(values: Iterable[str], *, limit: int = 10) -> list[str]:
    return sorted({str(value) for value in values if str(value)}, key=lambda value: (not value.isdigit(), value))[
        : max(0, int(limit))
    ]


def _coverage_value(report: Mapping[str, Any] | None, *path: str) -> float | None:
    current: Any = report
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    try:
        return float(current)
    except (TypeError, ValueError):
        return None


def build_hextech_coverage_report(
    frame: pd.DataFrame,
    *,
    metadata_ids: Iterable[object],
    metadata_entries: Mapping[object, object] | None = None,
    catalog_entries: Sequence[Mapping[str, Any]] = (),
    catalog_generation_id: str = "",
    catalog_sha256: str = "",
    catalog_manifest_sha256: str = "",
    upstream_version: str = "",
    upstream_date: str = "",
    upstream_marker_sha256: str = "",
    observed_source_ids: Iterable[object] = (),
    last_good: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造不可变 source run 可携带的覆盖摘要。"""

    metadata_map = _metadata_identity_map(metadata_entries, metadata_ids)
    metadata_total = set(metadata_map)
    enabled_metadata = {
        source_id for source_id, item in metadata_map.items() if bool(item.get("enabled", True))
    }
    disabled_metadata = metadata_total.difference(enabled_metadata)
    actual_ids = _unique_ids(frame.get("海克斯ID", pd.Series(dtype=object)).tolist())
    hero_ids = frame.get("英雄ID", pd.Series(dtype=object)).map(normalize_source_id)
    stat_ids = frame.get("海克斯ID", pd.Series(dtype=object)).map(normalize_source_id)
    per_hero: dict[str, dict[str, Any]] = {}
    for hero_id in sorted({value for value in hero_ids.tolist() if value}):
        mask = hero_ids == hero_id
        hero_stat_ids = {value for value in stat_ids[mask].tolist() if value}
        covered_metadata = hero_stat_ids.intersection(enabled_metadata)
        per_hero[hero_id] = {
            "record_count": int(mask.sum()),
            "stat_id_count": len(hero_stat_ids),
            "metadata_id_count": len(covered_metadata),
            "metadata_coverage_ratio": _ratio(len(covered_metadata), len(enabled_metadata)),
        }

    catalog_ids, catalog_entry_count = _catalog_source_ids(catalog_entries)
    projectable_catalog_ids = catalog_ids.intersection(enabled_metadata)
    projected_catalog_ids = projectable_catalog_ids.intersection(actual_ids)
    metadata_covered = actual_ids.intersection(enabled_metadata)
    catalog_identity_ratio = _ratio(len(projectable_catalog_ids), len(enabled_metadata))
    non_null_statistics_ratio = _ratio(len(metadata_covered), len(enabled_metadata))
    observed_ids = _unique_ids(observed_source_ids)
    observed_metadata_unknown = observed_ids.difference(metadata_total)
    observed_disabled = observed_ids.intersection(disabled_metadata)
    unresolved_metadata_ids = enabled_metadata.difference(catalog_ids)
    production_pool = build_production_augment_pool(
        metadata_map,
        catalog_entries,
        upstream_marker_sha256=upstream_marker_sha256,
        catalog_generation_id=catalog_generation_id,
        catalog_sha256=catalog_sha256,
        catalog_manifest_sha256=catalog_manifest_sha256,
        last_good_pool=(
            last_good.get("production_augment_pool")
            if isinstance(last_good, Mapping) and isinstance(last_good.get("production_augment_pool"), Mapping)
            else None
        ),
    )
    per_hero_counts = [int(item["record_count"]) for item in per_hero.values()]
    report: dict[str, Any] = {
        "schema_version": 2,
        "availability_policy": AVAILABILITY_POLICY,
        "availability": {
            "hero_count": len(per_hero_counts),
            "record_count": int(len(frame)),
            "per_hero_min": min(per_hero_counts, default=0),
            "per_hero_median": float(statistics.median(per_hero_counts)) if per_hero_counts else 0.0,
            "per_hero_max": max(per_hero_counts, default=0),
        },
        "identity_coverage": {
            "metadata_total": len(metadata_total),
            "enabled_pool": len(enabled_metadata),
            "disabled_pool": len(disabled_metadata),
            "non_null_statistics": len(metadata_covered),
            "catalog_identity_mapping": len(projectable_catalog_ids),
        },
        # marker_sha256 是条目子集内容哈希：version/date 为空的源靠它感知上游变化。
        "upstream": {
            "version": str(upstream_version or ""),
            "date": str(upstream_date or ""),
            "marker_sha256": str(upstream_marker_sha256 or ""),
        },
        "metadata": {
            "id_count": len(metadata_total),
            "enabled_id_count": len(enabled_metadata),
            "disabled_id_count": len(disabled_metadata),
            "actual_stat_id_count": len(actual_ids),
            "covered_id_count": len(metadata_covered),
            "coverage_ratio": _ratio(len(metadata_covered), len(enabled_metadata)),
            "disabled_sample_ids": _sample_ids(disabled_metadata),
        },
        "non_null_statistics": {
            "enabled_pool_count": len(enabled_metadata),
            "non_null_statistics_count": len(metadata_covered),
            "coverage_ratio": non_null_statistics_ratio,
            "sample_ids": _sample_ids(metadata_covered),
        },
        "catalog_identity_mapping": {
            "enabled_pool_count": len(enabled_metadata),
            "catalog_resolved_id_count": len(projectable_catalog_ids),
            "unresolved_id_count": len(enabled_metadata.difference(catalog_ids)),
            "coverage_ratio": catalog_identity_ratio,
            "available": bool(projectable_catalog_ids),
            "unresolved_sample_ids": _sample_ids(enabled_metadata.difference(catalog_ids)),
        },
        "catalog_projection": {
            "catalog_entry_count": catalog_entry_count,
            "catalog_source_id_count": len(catalog_ids),
            "projectable_source_id_count": len(projectable_catalog_ids),
            "covered_source_id_count": len(projected_catalog_ids),
            # 兼容旧消费者，但分母现在固定为启用身份池；统计非空数量另报在
            # non_null_statistics，不能再把两种覆盖口径混在一个比例里。
            "coverage_ratio": catalog_identity_ratio,
            "statistics_coverage_ratio": non_null_statistics_ratio,
            "available": bool(projectable_catalog_ids),
        },
        "active_augment_pool": {
            "schema_version": 1,
            "metadata_id_count": len(metadata_total),
            "enabled_metadata_id_count": len(enabled_metadata),
            "disabled_metadata_id_count": len(disabled_metadata),
            "catalog_resolved_id_count": len(projectable_catalog_ids),
            "unresolved_metadata_id_count": len(unresolved_metadata_ids),
            "source_ids": _sample_ids(projectable_catalog_ids, limit=len(projectable_catalog_ids)),
            "unresolved_metadata_sample_ids": _sample_ids(unresolved_metadata_ids),
            "observed_detail_id_count": len(observed_ids),
            "observed_catalog_unknown_count": len(observed_metadata_unknown),
            "observed_catalog_unknown_sample_ids": _sample_ids(observed_metadata_unknown),
            "observed_disabled_id_count": len(observed_disabled),
            "observed_disabled_sample_ids": _sample_ids(observed_disabled),
        },
        "production_augment_pool": production_pool,
        "heroes": per_hero,
    }

    previous_policy = str(last_good.get("availability_policy") or "") if isinstance(last_good, Mapping) else ""
    same_policy = previous_policy == AVAILABILITY_POLICY
    previous_metadata = _coverage_value(last_good, "metadata", "coverage_ratio") if same_policy else None
    previous_catalog = (
        _coverage_value(last_good, "catalog_identity_mapping", "coverage_ratio")
        if same_policy
        else None
    )
    if previous_catalog is None and same_policy:
        previous_catalog = _coverage_value(last_good, "catalog_projection", "coverage_ratio")
    report["last_good_comparison"] = {
        "state": (
            "same_policy"
            if same_policy
            else ("skipped_policy_change" if isinstance(last_good, Mapping) else "not_available")
        ),
        "previous_policy": previous_policy,
        "current_policy": AVAILABILITY_POLICY,
    }
    report["last_good_delta"] = {
        "metadata_coverage_ratio": (
            round(float(report["metadata"]["coverage_ratio"]) - previous_metadata, 6)
            if previous_metadata is not None
            else None
        ),
        "catalog_identity_mapping_coverage_ratio": (
            # 上一代可投影而本代突然没有 source ID 时必须视为 0，不能以
            # “available=false”跳过回归检查；否则 Catalog 桥接断裂会被静默放行。
            round(
                (
                    float(report["catalog_projection"]["coverage_ratio"])
                    if bool(report["catalog_projection"]["available"])
                    else 0.0
                )
                - previous_catalog,
                6,
            )
            if previous_catalog is not None
            else None
        ),
        # 旧字段继续写出，便于历史诊断器读取；门禁优先使用新的 identity mapping。
        "catalog_projection_coverage_ratio": (
            round(float(report["catalog_projection"]["coverage_ratio"]) - previous_catalog, 6)
            if previous_catalog is not None
            else None
        ),
    }
    return report


def validate_hextech_coverage_report(
    report: Mapping[str, Any],
    *,
    expected_hero_ids: Iterable[object],
    outcomes: Iterable[object],
) -> None:
    """执行完整候选代门禁；错误信息直接可用于 source run 诊断。"""

    expected = _unique_ids(expected_hero_ids)
    heroes = report.get("heroes") if isinstance(report.get("heroes"), Mapping) else {}
    missing = sorted(expected.difference(heroes))
    if missing:
        raise HextechCoverageError(f"Hextech 覆盖缺少英雄记录：{missing}")
    non_success = [
        str(getattr(outcome, "item_id", ""))
        for outcome in outcomes
        if str(getattr(outcome, "state", "")) != "success"
    ]
    if non_success:
        raise HextechCoverageError(f"Hextech 存在未成功英雄：{non_success}")
    active_pool = report.get("active_augment_pool") if isinstance(report.get("active_augment_pool"), Mapping) else {}
    unknown_count = int(active_pool.get("observed_catalog_unknown_count") or 0)
    if unknown_count:
        samples = list(active_pool.get("observed_catalog_unknown_sample_ids") or [])
        raise HextechCoverageError(
            f"Hextech 详情出现 Catalog 未登记的活动海克斯：count={unknown_count} samples={samples}",
            reason_code="active_catalog_mismatch",
        )
    low_records = [
        hero_id
        for hero_id in sorted(expected)
        if int((heroes.get(hero_id) or {}).get("record_count") or 0) < MIN_RECORDS_PER_CHAMPION
    ]
    if low_records:
        raise HextechCoverageError(
            f"Hextech 英雄有效统计不足 {MIN_RECORDS_PER_CHAMPION} 条：{low_records}"
        )
    availability = report.get("availability") if isinstance(report.get("availability"), Mapping) else {}
    record_count = int(availability.get("record_count") or 0)
    if record_count < MIN_TOTAL_RECORDS:
        raise HextechCoverageError(f"Hextech 有效统计总量不足：{record_count} < {MIN_TOTAL_RECORDS}")
    median_records = float(availability.get("per_hero_median") or 0.0)
    if median_records < MIN_MEDIAN_RECORDS_PER_CHAMPION:
        raise HextechCoverageError(
            "Hextech 每英雄有效统计中位数不足："
            f"{median_records:g} < {MIN_MEDIAN_RECORDS_PER_CHAMPION}"
        )
    metadata_ratio = _coverage_value(report, "metadata", "coverage_ratio") or 0.0
    if metadata_ratio < MIN_METADATA_COVERAGE_RATIO:
        raise HextechCoverageError(
            f"Hextech metadata 覆盖不足：{metadata_ratio:.1%} < {MIN_METADATA_COVERAGE_RATIO:.0%}"
        )
    catalog_mapping = report.get("catalog_identity_mapping") if isinstance(report.get("catalog_identity_mapping"), Mapping) else {}
    catalog_ratio = _coverage_value(catalog_mapping, "coverage_ratio") or 0.0
    if not bool(catalog_mapping.get("available")) or catalog_ratio < MIN_CATALOG_PROJECTION_COVERAGE_RATIO:
        raise HextechCoverageError(
            "Hextech Catalog 投影覆盖不足："
            f"{catalog_ratio:.1%} < {MIN_CATALOG_PROJECTION_COVERAGE_RATIO:.0%}"
        )
    production_pool = report.get("production_augment_pool")
    if not isinstance(production_pool, Mapping):
        raise HextechCoverageError(
            "Hextech production pool 缺失",
            reason_code="production_pool_unavailable",
        )
    try:
        validate_production_augment_pool(production_pool)
    except ValueError as exc:
        raise HextechCoverageError(
            "Hextech production pool 不可发布："
            f"unresolved={list(production_pool.get('unresolved_ids') or [])[:10]} "
            f"duplicates={list(production_pool.get('duplicate_ids') or [])[:10]}",
            reason_code="production_pool_unavailable",
        ) from exc
    delta = report.get("last_good_delta") if isinstance(report.get("last_good_delta"), Mapping) else {}
    metadata_delta = _coverage_value(delta, "metadata_coverage_ratio")
    if metadata_delta is not None and metadata_delta < -MAX_COVERAGE_REGRESSION:
        raise HextechCoverageError(f"Hextech metadata 覆盖相对 last-good 下降：{metadata_delta:.1%}")
    catalog_delta = _coverage_value(delta, "catalog_identity_mapping_coverage_ratio")
    if catalog_delta is None:
        catalog_delta = _coverage_value(delta, "catalog_projection_coverage_ratio")
    if catalog_delta is not None and catalog_delta < -MAX_COVERAGE_REGRESSION:
        raise HextechCoverageError(f"Hextech Catalog 投影覆盖相对 last-good 下降：{catalog_delta:.1%}")


__all__ = [
    "HextechCoverageError",
    "AVAILABILITY_POLICY",
    "MAX_COVERAGE_REGRESSION",
    "MIN_CATALOG_PROJECTION_COVERAGE_RATIO",
    "MIN_MEDIAN_RECORDS_PER_CHAMPION",
    "MIN_METADATA_COVERAGE_RATIO",
    "MIN_RECORDS_PER_CHAMPION",
    "MIN_TOTAL_RECORDS",
    "build_hextech_coverage_report",
    "normalize_source_id",
    "validate_hextech_coverage_report",
]

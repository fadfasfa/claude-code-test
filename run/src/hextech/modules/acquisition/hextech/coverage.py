"""Hextech 候选代的统计覆盖报告与发布门禁。

报告的分母始终来自本轮上游 metadata，而不是视觉 Catalog 的全部条目。Catalog
只参与可投影条目的回归检查，避免把不同模式的视觉资源误判为统计来源缺口。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import pandas as pd


MIN_RECORDS_PER_CHAMPION = 70
MIN_METADATA_COVERAGE_RATIO = 0.90
MAX_COVERAGE_REGRESSION = 0.05


class HextechCoverageError(ValueError):
    """候选统计覆盖不满足发布门禁。"""

    coverage: Mapping[str, Any] | None = None


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
    catalog_entries: Sequence[Mapping[str, Any]] = (),
    upstream_version: str = "",
    upstream_date: str = "",
    upstream_marker_sha256: str = "",
    last_good: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造不可变 source run 可携带的覆盖摘要。"""

    metadata = _unique_ids(metadata_ids)
    actual_ids = _unique_ids(frame.get("海克斯ID", pd.Series(dtype=object)).tolist())
    hero_ids = frame.get("英雄ID", pd.Series(dtype=object)).map(normalize_source_id)
    stat_ids = frame.get("海克斯ID", pd.Series(dtype=object)).map(normalize_source_id)
    per_hero: dict[str, dict[str, Any]] = {}
    for hero_id in sorted({value for value in hero_ids.tolist() if value}):
        mask = hero_ids == hero_id
        hero_stat_ids = {value for value in stat_ids[mask].tolist() if value}
        covered_metadata = hero_stat_ids.intersection(metadata)
        per_hero[hero_id] = {
            "record_count": int(mask.sum()),
            "stat_id_count": len(hero_stat_ids),
            "metadata_id_count": len(covered_metadata),
            "metadata_coverage_ratio": _ratio(len(covered_metadata), len(metadata)),
        }

    catalog_ids, catalog_entry_count = _catalog_source_ids(catalog_entries)
    projectable_catalog_ids = catalog_ids.intersection(metadata)
    projected_catalog_ids = projectable_catalog_ids.intersection(actual_ids)
    metadata_covered = actual_ids.intersection(metadata)
    report: dict[str, Any] = {
        "schema_version": 1,
        # marker_sha256 是条目子集内容哈希：version/date 为空的源靠它感知上游变化。
        "upstream": {
            "version": str(upstream_version or ""),
            "date": str(upstream_date or ""),
            "marker_sha256": str(upstream_marker_sha256 or ""),
        },
        "metadata": {
            "id_count": len(metadata),
            "actual_stat_id_count": len(actual_ids),
            "covered_id_count": len(metadata_covered),
            "coverage_ratio": _ratio(len(metadata_covered), len(metadata)),
        },
        "catalog_projection": {
            "catalog_entry_count": catalog_entry_count,
            "catalog_source_id_count": len(catalog_ids),
            "projectable_source_id_count": len(projectable_catalog_ids),
            "covered_source_id_count": len(projected_catalog_ids),
            "coverage_ratio": _ratio(len(projected_catalog_ids), len(projectable_catalog_ids)),
            "available": bool(projectable_catalog_ids),
        },
        "heroes": per_hero,
    }

    previous_metadata = _coverage_value(last_good, "metadata", "coverage_ratio")
    previous_catalog = _coverage_value(last_good, "catalog_projection", "coverage_ratio")
    previous_heroes = last_good.get("heroes") if isinstance(last_good, Mapping) else {}
    hero_deltas: dict[str, float] = {}
    if isinstance(previous_heroes, Mapping):
        for hero_id, current in per_hero.items():
            previous = _coverage_value(previous_heroes.get(hero_id), "metadata_coverage_ratio")
            if previous is not None:
                hero_deltas[hero_id] = round(float(current["metadata_coverage_ratio"]) - previous, 6)
    report["last_good_delta"] = {
        "metadata_coverage_ratio": (
            round(float(report["metadata"]["coverage_ratio"]) - previous_metadata, 6)
            if previous_metadata is not None
            else None
        ),
        "catalog_projection_coverage_ratio": (
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
        "per_hero_metadata_coverage_ratio": hero_deltas,
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
    low_records = [
        hero_id
        for hero_id in sorted(expected)
        if int((heroes.get(hero_id) or {}).get("record_count") or 0) < MIN_RECORDS_PER_CHAMPION
    ]
    if low_records:
        raise HextechCoverageError(
            f"Hextech 英雄有效统计不足 {MIN_RECORDS_PER_CHAMPION} 条：{low_records}"
        )
    metadata_ratio = _coverage_value(report, "metadata", "coverage_ratio") or 0.0
    if metadata_ratio < MIN_METADATA_COVERAGE_RATIO:
        raise HextechCoverageError(
            f"Hextech metadata 覆盖不足：{metadata_ratio:.1%} < {MIN_METADATA_COVERAGE_RATIO:.0%}"
        )
    delta = report.get("last_good_delta") if isinstance(report.get("last_good_delta"), Mapping) else {}
    metadata_delta = _coverage_value(delta, "metadata_coverage_ratio")
    if metadata_delta is not None and metadata_delta < -MAX_COVERAGE_REGRESSION:
        raise HextechCoverageError(f"Hextech metadata 覆盖相对 last-good 下降：{metadata_delta:.1%}")
    catalog_delta = _coverage_value(delta, "catalog_projection_coverage_ratio")
    if catalog_delta is not None and catalog_delta < -MAX_COVERAGE_REGRESSION:
        raise HextechCoverageError(f"Hextech Catalog 投影覆盖相对 last-good 下降：{catalog_delta:.1%}")
    per_hero_delta = delta.get("per_hero_metadata_coverage_ratio") if isinstance(delta, Mapping) else {}
    if isinstance(per_hero_delta, Mapping):
        regressed: list[str] = []
        for hero_id, value in per_hero_delta.items():
            try:
                hero_delta = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(hero_delta) and hero_delta < -MAX_COVERAGE_REGRESSION:
                regressed.append(str(hero_id))
        if regressed:
            raise HextechCoverageError(f"Hextech 英雄覆盖相对 last-good 下降：{sorted(regressed)}")


__all__ = [
    "HextechCoverageError",
    "MAX_COVERAGE_REGRESSION",
    "MIN_METADATA_COVERAGE_RATIO",
    "MIN_RECORDS_PER_CHAMPION",
    "build_hextech_coverage_report",
    "normalize_source_id",
    "validate_hextech_coverage_report",
]

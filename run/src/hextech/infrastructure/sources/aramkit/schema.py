"""ARAMKit 静态统计的纯 schema 与最小投影。

本模块只校验版本、英雄排行与海克斯统计字段，不执行网络、落盘或 generation
切换。生产抓取和开发 probe 共用这些纯函数，避免两条链路对同一上游产生不同解释。
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


STAGES = ("1", "2", "3", "4")
RATE_FIELDS = ("winRate", "pickRate", "blueWinRate", "redWinRate")


class SchemaValidationError(ValueError):
    """ARAMKit 响应不满足本项目所需的最小公开统计合同。"""


def decode_object(body: bytes | str, *, context: str) -> dict[str, Any]:
    try:
        text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"{context} 不是有效 UTF-8 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"{context} 顶层必须是对象")
    return payload


def _mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError(f"{context} 必须是对象")
    return value


def _rows(value: Any, *, context: str, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaValidationError(f"{context} 必须是数组")
    if nonempty and not value:
        raise SchemaValidationError(f"{context} 不能为空")
    return value


def _positive_int(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise SchemaValidationError(f"{context} 必须是正整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{context} 必须是正整数") from exc
    if normalized <= 0 or float(value) != float(normalized):
        raise SchemaValidationError(f"{context} 必须是正整数")
    return normalized


def _nonnegative_int(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise SchemaValidationError(f"{context} 必须是非负整数")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{context} 必须是非负整数") from exc
    if normalized < 0 or float(value) != float(normalized):
        raise SchemaValidationError(f"{context} 必须是非负整数")
    return normalized


def _rate(value: Any, *, context: str) -> float:
    if isinstance(value, bool):
        raise SchemaValidationError(f"{context} 必须是 [0,1] 有限数")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"{context} 必须是 [0,1] 有限数") from exc
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise SchemaValidationError(f"{context} 必须是 [0,1] 有限数")
    return normalized


def _champion_summary(payload: Mapping[str, Any], *, context: str, nested_stats: bool) -> dict[str, Any]:
    champion_id = str(payload.get("id", "")).strip()
    if not champion_id or not champion_id.isdigit():
        raise SchemaValidationError(f"{context}.id 必须是数字英雄 ID")
    stats = _mapping(payload.get("stats"), context=f"{context}.stats") if nested_stats else payload
    tier = str(payload.get("tier", "")).strip()
    if not tier:
        raise SchemaValidationError(f"{context}.tier 不能为空")
    result: dict[str, Any] = {
        "id": champion_id,
        "rank": _positive_int(payload.get("rank"), context=f"{context}.rank"),
        "tier": tier,
        "stats": {
            "sampleCount": _nonnegative_int(stats.get("sampleCount"), context=f"{context}.sampleCount")
        },
    }
    for field in RATE_FIELDS:
        result["stats"][field] = _rate(stats.get(field), context=f"{context}.{field}")
    return result


def _augment(raw: Any, *, context: str, is_all: bool) -> dict[str, Any]:
    item = _mapping(raw, context=context)
    result: dict[str, Any] = {
        "id": _positive_int(item.get("id"), context=f"{context}.id"),
        "rank": _positive_int(item.get("rank"), context=f"{context}.rank"),
        "sampleCount": _nonnegative_int(item.get("sampleCount"), context=f"{context}.sampleCount"),
    }
    for field in RATE_FIELDS:
        result[field] = _rate(item.get(field), context=f"{context}.{field}")
    if is_all:
        stage_agnostic = item.get("stageAgnostic")
        if not isinstance(stage_agnostic, bool):
            raise SchemaValidationError(f"{context}.stageAgnostic 必须是布尔值")
        available = [str(value) for value in _rows(item.get("availableStages"), context=f"{context}.availableStages")]
        if any(value not in STAGES for value in available) or len(set(available)) != len(available):
            raise SchemaValidationError(f"{context}.availableStages 非法")
        result["stageAgnostic"] = stage_agnostic
        result["availableStages"] = available
    return result


def _augment_scope(raw: Any, *, context: str, is_all: bool) -> list[dict[str, Any]]:
    normalized = [
        _augment(row, context=f"{context}[{index}]", is_all=is_all)
        for index, row in enumerate(_rows(raw, context=context, nonempty=True))
    ]
    ids = [row["id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise SchemaValidationError(f"{context} 存在重复海克斯 ID")
    return normalized


def normalize_detail(payload: Mapping[str, Any], ranking: Mapping[str, Any]) -> dict[str, Any]:
    expected_id = str(ranking["id"])
    champion = _mapping(payload.get("champion"), context=f"champion[{expected_id}]")
    normalized_champion = _champion_summary(
        champion,
        context=f"champion[{expected_id}]",
        nested_stats=True,
    )
    if normalized_champion != dict(ranking):
        raise SchemaValidationError(f"champion[{expected_id}] 与排行概要不一致，疑似版本混用")

    augments = _mapping(payload.get("augments"), context=f"champion[{expected_id}].augments")
    stages = _mapping(augments.get("stages"), context=f"champion[{expected_id}].augments.stages")
    normalized_champion["augments"] = {
        "all": _augment_scope(
            augments.get("all"),
            context=f"champion[{expected_id}].augments.all",
            is_all=True,
        ),
        "stages": {
            stage: _augment_scope(
                stages.get(stage),
                context=f"champion[{expected_id}].augments.stages.{stage}",
                is_all=False,
            )
            for stage in STAGES
        },
    }
    return normalized_champion


def normalize_rankings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    normalized = [
        _champion_summary(
            _mapping(row, context=f"champion-rankings.rows[{index}]"),
            context=f"ranking[{index}]",
            nested_stats=False,
        )
        for index, row in enumerate(
            _rows(payload.get("rows"), context="champion-rankings.rows", nonempty=True)
        )
    ]
    ids = [row["id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise SchemaValidationError("champion-rankings 存在重复英雄 ID")
    return normalized


def resolve_version(payload: Mapping[str, Any], requested: str = "latest") -> dict[str, Any]:
    versions = _rows(payload.get("versions"), context="versions", nonempty=True)
    latest = str(payload.get("latest", "")).strip()
    target = latest if requested == "latest" else str(requested).strip()
    for raw in versions:
        row = _mapping(raw, context="versions[]")
        if str(row.get("version", "")).strip() != target:
            continue
        data_path = str(row.get("dataPath", "")).strip().strip("/")
        if not data_path:
            raise SchemaValidationError(f"版本 {target} 缺少 dataPath")
        result = {
            "version": target,
            "dataPath": data_path,
            "allMatches": _nonnegative_int(row.get("allMatches"), context=f"version[{target}].allMatches"),
            "buildTimeUnixMs": _nonnegative_int(
                row.get("buildTimeUnixMs"),
                context=f"version[{target}].buildTimeUnixMs",
            ),
        }
        if row.get("highMatches") is not None:
            result["highMatches"] = _nonnegative_int(
                row.get("highMatches"),
                context=f"version[{target}].highMatches",
            )
        return result
    raise SchemaValidationError(f"未找到公开版本：{target}")


def version_marker(version: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": str(version.get("version") or ""),
        "dataPath": str(version.get("dataPath") or ""),
        "buildTimeUnixMs": _nonnegative_int(
            version.get("buildTimeUnixMs"),
            context="marker.buildTimeUnixMs",
        ),
        "allMatches": _nonnegative_int(version.get("allMatches"), context="marker.allMatches"),
    }


__all__ = [
    "RATE_FIELDS",
    "STAGES",
    "SchemaValidationError",
    "decode_object",
    "normalize_detail",
    "normalize_rankings",
    "resolve_version",
    "version_marker",
]

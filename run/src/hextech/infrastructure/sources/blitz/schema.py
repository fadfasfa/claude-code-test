"""Blitz ARAM Mayhem 排名 JSON 的纯解析与稳定 marker。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Mapping


_PATCH_RE = re.compile(r"^\d+\.\d+$")


class BlitzSchemaError(ValueError):
    """上游或 immutable artifact 不满足排名合同。"""


def _mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BlitzSchemaError(f"{context} 必须是对象")
    return value


def _positive_id(value: Any, *, context: str) -> str:
    if isinstance(value, bool):
        raise BlitzSchemaError(f"{context} 必须是正整数 ID")
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise BlitzSchemaError(f"{context} 必须是正整数 ID")
    return str(int(text))


def _tier(value: Any, *, context: str) -> int:
    if isinstance(value, bool):
        raise BlitzSchemaError(f"{context} 必须是 1..5")
    try:
        tier = int(value)
    except (TypeError, ValueError) as exc:
        raise BlitzSchemaError(f"{context} 必须是 1..5") from exc
    if tier not in range(1, 6) or float(value) != float(tier):
        raise BlitzSchemaError(f"{context} 必须是 1..5")
    return tier


def decode_object(text: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise BlitzSchemaError("Blitz 响应不是合法 JSON") from exc
    return _mapping(payload, context="payload")


def _marker(rows: list[dict[str, Any]], *, patch: str, data_date: str) -> dict[str, Any]:
    content = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "patch": patch,
        "data_date": data_date,
        "record_count": len(rows),
        "content_sha256": hashlib.sha256(content).hexdigest(),
    }


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise BlitzSchemaError("Blitz data 必须是非空数组")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    patches: set[str] = set()
    dates: set[str] = set()
    for index, raw in enumerate(raw_rows):
        item = _mapping(raw, context=f"data[{index}]")
        augment_id = _positive_id(item.get("augment_id"), context=f"data[{index}].augment_id")
        if augment_id in seen_ids:
            raise BlitzSchemaError(f"Blitz 存在重复海克斯 ID：{augment_id}")
        seen_ids.add(augment_id)
        patch = str(item.get("patch") or "").strip()
        if not _PATCH_RE.fullmatch(patch):
            raise BlitzSchemaError(f"data[{index}].patch 无效")
        data_date = str(item.get("dt") or "").strip()
        try:
            date.fromisoformat(data_date)
        except ValueError as exc:
            raise BlitzSchemaError(f"data[{index}].dt 无效") from exc
        patches.add(patch)
        dates.add(data_date)
        stats = _mapping(item.get("stats"), context=f"data[{index}].stats")
        source_top = stats.get("top_champions")
        if source_top is None:
            source_top = []
        if not isinstance(source_top, list) or len(source_top) > 5:
            raise BlitzSchemaError(f"data[{index}].stats.top_champions 必须是至多 5 项数组或 null")
        top_champions: list[dict[str, Any]] = []
        seen_champions: set[str] = set()
        for champion_index, raw_champion in enumerate(source_top):
            champion = _mapping(
                raw_champion,
                context=f"data[{index}].stats.top_champions[{champion_index}]",
            )
            champion_id = _positive_id(
                champion.get("champion_id"),
                context=f"data[{index}].stats.top_champions[{champion_index}].champion_id",
            )
            if champion_id in seen_champions:
                raise BlitzSchemaError(f"海克斯 {augment_id} 的 top_champions 重复：{champion_id}")
            seen_champions.add(champion_id)
            top_champions.append(
                {
                    "champion_id": champion_id,
                    "tier": _tier(
                        champion.get("tier"),
                        context=f"data[{index}].stats.top_champions[{champion_index}].tier",
                    ),
                }
            )
        rows.append(
            {
                "augment_id": augment_id,
                "tier": _tier(stats.get("tier"), context=f"data[{index}].stats.tier"),
                "top_champions": top_champions,
            }
        )
    if len(patches) != 1 or len(dates) != 1:
        raise BlitzSchemaError("Blitz 响应混用了多个 patch 或数据日期")
    rows.sort(key=lambda item: int(item["augment_id"]))
    patch = next(iter(patches))
    data_date = next(iter(dates))
    return {
        "schema_version": 1,
        "source": "blitz_mayhem",
        "patch": patch,
        "data_date": data_date,
        "marker": _marker(rows, patch=patch, data_date=data_date),
        "rows": rows,
    }


def project_normalized_rows(
    payload: Mapping[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """把已规范化 payload 投影为活动 Catalog 子集并重算 artifact marker。"""

    patch = str(payload.get("patch") or "").strip()
    data_date = str(payload.get("data_date") or "").strip()
    if not _PATCH_RE.fullmatch(patch):
        raise BlitzSchemaError("Blitz 投影 patch 无效")
    try:
        date.fromisoformat(data_date)
    except ValueError as exc:
        raise BlitzSchemaError("Blitz 投影数据日期无效") from exc
    projected = [dict(item) for item in rows]
    return {
        "schema_version": 1,
        "source": "blitz_mayhem",
        "patch": patch,
        "data_date": data_date,
        "marker": _marker(projected, patch=patch, data_date=data_date),
        "rows": projected,
    }


def validate_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != 1 or payload.get("source") != "blitz_mayhem":
        raise BlitzSchemaError("Blitz artifact 身份或 schema_version 无效")
    patch = str(payload.get("patch") or "").strip()
    data_date = str(payload.get("data_date") or "").strip()
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise BlitzSchemaError("Blitz artifact rows 无效")
    upstream = {
        "data": [
            {
                "augment_id": row.get("augment_id"),
                "patch": patch,
                "dt": data_date,
                "stats": {
                    "tier": row.get("tier"),
                    "top_champions": row.get("top_champions"),
                },
            }
            for row in raw_rows
            if isinstance(row, Mapping)
        ]
    }
    if len(upstream["data"]) != len(raw_rows):
        raise BlitzSchemaError("Blitz artifact rows 必须全部是对象")
    normalized = normalize_payload(upstream)
    if dict(payload.get("marker") or {}) != normalized["marker"] or raw_rows != normalized["rows"]:
        raise BlitzSchemaError("Blitz artifact marker 或规范化内容不一致")
    return normalized


__all__ = [
    "BlitzSchemaError",
    "decode_object",
    "normalize_payload",
    "project_normalized_rows",
    "validate_artifact",
]

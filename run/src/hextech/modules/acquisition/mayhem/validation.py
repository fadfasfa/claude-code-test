"""Mayhem 完整分页、分类守恒、reject 和规模变化共享门禁。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


class MayhemValidationError(ValueError):
    pass


def validate_mayhem_removal_evidence(items: object) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise MayhemValidationError("Mayhem removal evidence 必须是列表")
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise MayhemValidationError("Mayhem removal evidence 元素必须是对象")
        payload = dict(item)
        if (
            not str(payload.get("combo_key") or "")
            or payload.get("reason") != "not_present_in_complete_payload"
            or payload.get("pagination_complete") is not True
        ):
            raise MayhemValidationError("Mayhem removal evidence 缺少完整分页或删除原因")
        normalized.append(payload)
    return normalized


def _items(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    values = payload.get("items") if isinstance(payload, Mapping) else None
    return [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []


def _combo_key(item: Mapping[str, Any]) -> str:
    identity = {
        "champion_id": str(item.get("champion_id") or item.get("hero_id") or ""),
        "augment_names": sorted(
            str(value).strip().casefold()
            for value in (item.get("augment_names") or item.get("augments") or [])
            if str(value).strip()
        ),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_mayhem_run(
    payload: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    previous_payload: Mapping[str, Any] | None = None,
    automated_min_ratio: float = 0.50,
    max_reject_ratio: float = 0.15,
) -> dict[str, Any]:
    items = _items(payload)
    if not items:
        raise MayhemValidationError("Mayhem valid combo 为空")
    raw_count = int(report.get("raw_items") or report.get("raw_item_count") or 0)
    valid_count = int(report.get("valid_items") or len(items))
    duplicate_count = int(report.get("duplicate_items") or report.get("duplicate_count") or 0)
    reject_count = int(report.get("rejected_items") or report.get("reject_count") or 0)
    if raw_count <= 0 or raw_count != valid_count + duplicate_count + reject_count:
        raise MayhemValidationError(
            f"Mayhem 分类不守恒：raw={raw_count} valid={valid_count} duplicate={duplicate_count} reject={reject_count}"
        )
    if valid_count != len(items):
        raise MayhemValidationError(f"Mayhem valid 计数与 artifact 不一致：{valid_count} != {len(items)}")
    if reject_count / raw_count > max_reject_ratio:
        raise MayhemValidationError(f"Mayhem reject 比例越界：{reject_count}/{raw_count}")
    if int(report.get("max_pages") or 0) != 0:
        raise MayhemValidationError("Mayhem 正式刷新必须使用 max_pages=0")
    total = int(report.get("total") or 0)
    selected = int(report.get("selected") or 0)
    if total <= 0 or selected != total or report.get("pagination_complete") is not True:
        raise MayhemValidationError(
            f"Mayhem 分页不完整：selected={selected} total={total} mode={report.get('parse_mode')}"
        )
    rejects = report.get("rejects")
    if reject_count and not isinstance(rejects, list):
        raise MayhemValidationError("Mayhem reject 缺少稳定原因和样本")
    if isinstance(rejects, list):
        invalid = [item for item in rejects if not isinstance(item, Mapping) or not str(item.get("reason_code") or "")]
        if invalid:
            raise MayhemValidationError("Mayhem reject 存在空 reason_code")

    current_keys = {_combo_key(item) for item in items}
    removals = validate_mayhem_removal_evidence([
        {
            "combo_key": _combo_key(item),
            "reason": "not_present_in_complete_payload",
            "pagination_complete": True,
        }
        for item in _items(previous_payload)
        if _combo_key(item) not in current_keys
    ])
    previous_count = len(_items(previous_payload))
    if previous_count and valid_count < int(previous_count * automated_min_ratio):
        raise MayhemValidationError(
            f"Mayhem 自动更新规模异常：current={valid_count} previous={previous_count} ratio={automated_min_ratio}"
        )
    return {
        "raw_items": raw_count,
        "valid_items": valid_count,
        "duplicate_items": duplicate_count,
        "rejected_items": reject_count,
        "previous_valid_items": previous_count,
        "removals": removals,
    }


__all__ = ["MayhemValidationError", "validate_mayhem_removal_evidence", "validate_mayhem_run"]

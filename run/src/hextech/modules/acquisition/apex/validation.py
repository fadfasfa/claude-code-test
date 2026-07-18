"""Apex 全英雄三态、记录计数和删除证据的共享门禁。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from hextech.contracts import ItemOutcome


class ApexValidationError(ValueError):
    pass


def validate_apex_removal_evidence(items: object) -> list[dict[str, Any]]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise ApexValidationError("Apex removal evidence 必须是列表")
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ApexValidationError("Apex removal evidence 元素必须是对象")
        payload = dict(item)
        if (
            not str(payload.get("champion_id") or "")
            or not str(payload.get("combo_key") or "")
            or payload.get("reason") != "not_present_in_complete_payload"
            or payload.get("page_identity_verified") is not True
            or not str(payload.get("evidence") or "")
        ):
            raise ApexValidationError("Apex removal evidence 缺少页面身份或删除原因")
        normalized.append(payload)
    return normalized


def _items(entry: object) -> list[Mapping[str, Any]]:
    if not isinstance(entry, Mapping):
        return []
    values = entry.get("synergy_items")
    return [item for item in values if isinstance(item, Mapping)] if isinstance(values, list) else []


def apex_record_count(payload: Mapping[str, Any]) -> int:
    return sum(len(_items(entry)) for entry in payload.values())


def _combo_key(champion_id: str, item: Mapping[str, Any]) -> str:
    names = item.get("augment_names")
    normalized_names = sorted(str(value).strip().casefold() for value in names if str(value).strip()) if isinstance(names, list) else []
    identity = {
        "champion_id": str(champion_id),
        "augment_names": normalized_names,
        "tag": str(item.get("tag") or "").strip().casefold(),
        "content": str(item.get("content") or "").strip(),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_apex_removal_evidence(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    outcomes: Sequence[ItemOutcome],
) -> list[dict[str, Any]]:
    if not previous:
        return []
    outcome_by_id = {str(item.item_id): item for item in outcomes}
    current_keys = {
        (str(champion_id), _combo_key(str(champion_id), item))
        for champion_id, entry in current.items()
        for item in _items(entry)
    }
    removals: list[dict[str, Any]] = []
    for champion_id, entry in previous.items():
        for item in _items(entry):
            key = _combo_key(str(champion_id), item)
            if (str(champion_id), key) in current_keys:
                continue
            outcome = outcome_by_id.get(str(champion_id))
            details = dict(outcome.details) if outcome is not None else {}
            removals.append(
                {
                    "champion_id": str(champion_id),
                    "combo_key": key,
                    "reason": "not_present_in_complete_payload",
                    "outcome_state": outcome.state if outcome is not None else "missing",
                    "page_identity_verified": bool(details.get("page_identity_verified")),
                    "evidence": str(details.get("evidence") or ""),
                    "url": str(details.get("url") or ""),
                }
            )
    return removals


def validate_apex_run(
    payload: Mapping[str, Any],
    outcomes: Sequence[ItemOutcome],
    *,
    expected_champion_ids: Sequence[str] | None = None,
    previous_payload: Mapping[str, Any] | None = None,
    automated_min_ratio: float = 0.70,
) -> dict[str, Any]:
    expected = {str(value) for value in (expected_champion_ids or payload.keys())}
    outcome_by_id = {str(item.item_id): item for item in outcomes}
    if set(outcome_by_id) != expected or set(str(key) for key in payload) != expected:
        raise ApexValidationError(
            f"Apex 英雄覆盖不完整：outcome_missing={sorted(expected - set(outcome_by_id))} "
            f"payload_missing={sorted(expected - set(str(key) for key in payload))}"
        )
    for champion_id in sorted(expected):
        outcome = outcome_by_id[champion_id]
        items = _items(payload.get(champion_id))
        details = dict(outcome.details)
        if outcome.state == "success":
            if not items or outcome.record_count != len(items):
                raise ApexValidationError(f"Apex success 英雄记录为空或计数不符：{champion_id}")
        elif outcome.state == "confirmed_empty":
            if items:
                raise ApexValidationError(f"Apex confirmed_empty 携带联动：{champion_id}")
            if not details.get("page_identity_verified") or details.get("evidence") != "explicit_empty_state":
                raise ApexValidationError(f"Apex confirmed_empty 缺少页面身份或明确空态：{champion_id}")
        else:
            raise ApexValidationError(f"Apex 英雄存在明确失败：{champion_id}")

    record_count = apex_record_count(payload)
    if record_count <= 0:
        raise ApexValidationError("Apex 联动记录为空")
    removals = build_apex_removal_evidence(previous_payload, payload, outcomes)
    removals = validate_apex_removal_evidence(removals)
    previous_count = apex_record_count(previous_payload or {})
    if previous_count and record_count < int(previous_count * automated_min_ratio):
        raise ApexValidationError(
            f"Apex 自动更新规模异常：current={record_count} previous={previous_count} ratio={automated_min_ratio}"
        )
    return {
        "expected_champions": len(expected),
        "successful_champions": sum(item.state == "success" for item in outcomes),
        "confirmed_empty_champions": sum(item.state == "confirmed_empty" for item in outcomes),
        "record_count": record_count,
        "previous_record_count": previous_count,
        "removals": removals,
    }


__all__ = [
    "ApexValidationError",
    "apex_record_count",
    "build_apex_removal_evidence",
    "validate_apex_removal_evidence",
    "validate_apex_run",
]

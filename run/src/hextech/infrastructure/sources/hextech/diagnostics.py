"""Hextech 单英雄结果到统一来源诊断契约的转换。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from hextech.contracts import FailureKind
from hextech.modules.acquisition.common.contracts import ItemOutcome


def _failure_kind(reason: str, status_code: int | None) -> FailureKind:
    if status_code == 403:
        return FailureKind.HTTP_403
    if status_code == 429:
        return FailureKind.HTTP_429
    if status_code is not None and 500 <= status_code <= 599:
        return FailureKind.HTTP_5XX
    try:
        return FailureKind(reason)
    except ValueError:
        return FailureKind.SCHEMA_CHANGED if "parse" in reason or "schema" in reason else FailureKind.INVALID_PAYLOAD


def item_outcomes(results: Iterable[Mapping[str, object]]) -> tuple[ItemOutcome, ...]:
    outcomes: list[ItemOutcome] = []
    for item in results:
        champion = item.get("champ") if isinstance(item.get("champ"), Mapping) else {}
        champion_id = str(champion.get("championId") or item.get("champion_id") or "")
        rows = item.get("rows") if isinstance(item.get("rows"), list) else []
        reason = str(item.get("reason") or "")
        status_code = item.get("status_code") if isinstance(item.get("status_code"), int) else None
        outcomes.append(
            ItemOutcome(
                item_id=champion_id,
                state="success" if rows else "failed",
                stage=str(item.get("stage") or "detail"),
                record_count=len(rows),
                failure_kind=None if rows else _failure_kind(reason, status_code),
                details={
                    "backend": str(item.get("backend") or ""),
                    "status_code": status_code,
                    "reason": reason,
                    "url": str(item.get("url") or ""),
                },
            )
        )
    return tuple(outcomes)


__all__ = ["item_outcomes"]

"""Apex 页面身份、空态证据与阻断页分类。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from hextech.contracts import FailureKind


class ApexPageState(StrEnum):
    HAS_SYNERGY = "has_synergy"
    CONFIRMED_EMPTY = "confirmed_empty"
    FAILED = "failed"


@dataclass(frozen=True)
class ApexPageOutcome:
    state: ApexPageState
    failure_kind: FailureKind | None = None
    evidence: str = ""


_BLOCK_MARKERS = ("cloudflare", "cf-chl-", "access denied", "attention required")
_EMPTY_MARKERS = ("暂无联动", "暂无关联套装", "还没有关联套装", "no synergies", "no synergy builds")


def classify_apex_page(html: str, *, expected_slug: str, entry_count: int, status_code: int | None) -> ApexPageOutcome:
    text = str(html or "")
    lowered = text.casefold()
    if status_code == 403:
        return ApexPageOutcome(ApexPageState.FAILED, FailureKind.HTTP_403, "http_status")
    if status_code == 429:
        return ApexPageOutcome(ApexPageState.FAILED, FailureKind.HTTP_429, "http_status")
    if status_code is not None and 500 <= status_code <= 599:
        return ApexPageOutcome(ApexPageState.FAILED, FailureKind.HTTP_5XX, "http_status")
    if any(marker in lowered for marker in _BLOCK_MARKERS):
        return ApexPageOutcome(ApexPageState.FAILED, FailureKind.HTTP_403, "blocked_page")
    if not text.strip():
        return ApexPageOutcome(ApexPageState.FAILED, FailureKind.INVALID_PAYLOAD, "empty_html")

    normalized_slug = str(expected_slug or "").casefold().replace(" ", "-")
    identity_markers = (
        f"/champions/{normalized_slug}",
        f'"slug":"{normalized_slug}"',
        f'"championslug":"{normalized_slug}"',
    )
    identity_ok = any(marker in lowered.replace(" ", "") for marker in identity_markers)
    if not identity_ok:
        return ApexPageOutcome(ApexPageState.FAILED, FailureKind.SCHEMA_CHANGED, "page_identity_missing")
    if entry_count > 0:
        return ApexPageOutcome(ApexPageState.HAS_SYNERGY, evidence="parsed_entries")
    if any(marker in lowered for marker in _EMPTY_MARKERS):
        return ApexPageOutcome(ApexPageState.CONFIRMED_EMPTY, FailureKind.CONFIRMED_EMPTY, "explicit_empty_state")
    return ApexPageOutcome(ApexPageState.FAILED, FailureKind.SCHEMA_CHANGED, "synergy_markers_missing")


__all__ = ["ApexPageOutcome", "ApexPageState", "classify_apex_page"]

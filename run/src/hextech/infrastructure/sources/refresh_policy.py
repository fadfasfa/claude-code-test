"""Version check scheduling; never use upstream data age as a download trigger."""
from __future__ import annotations

import math
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from hextech.contracts import RefreshSourceState
from hextech.modules.data.freshness import SOURCE_INTERVALS, parse_refresh_time
from hextech.infrastructure.transport.conditional_response import parse_retry_after_seconds


def exception_source_result(exc: BaseException) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    if response is None:
        return {}
    failure = getattr(response, "failure_kind", None)
    failure_kind = str(getattr(failure, "value", failure) or getattr(exc, "reason", ""))
    retry_after = parse_retry_after_seconds(getattr(response, "response_headers", None))
    reason = str(getattr(exc, "reason", "") or "")
    return {
        "check_status": "unknown",
        "failure_stage": (
            "validation"
            if reason in {"schema_changed", "catalog_binding_failed", "marker_drift"}
            else "fetch"
        ),
        "failure_kind": failure_kind,
        "retry_after_seconds": retry_after,
    }


def source_failure_kind(result: Mapping[str, Any], exc: BaseException) -> str:
    """Prefer worker evidence over ambiguous exception prose."""

    diagnostics = result.get("diagnostics")
    diagnostic = diagnostics if isinstance(diagnostics, Mapping) else {}
    reported = str(
        result.get("failure_kind")
        or result.get("error_kind")
        or diagnostic.get("failure_kind")
        or diagnostic.get("error_kind")
        or ""
    ).casefold()
    if reported == "timeout":
        return "timeout"
    if reported in {
        "tls_error", "network_error", "http_403", "http_429", "http_5xx", "transient_network",
    }:
        return "transient_network"
    if reported in {"invalid_payload", "schema_changed", "validation"}:
        return "validation"
    if str(result.get("failure_stage") or "").casefold() == "validation" or result.get(
        "failure_fingerprint"
    ):
        return "validation"
    reason = str(exc).casefold()
    if "shutdown" in reason or "cancel" in reason:
        return "cancelled"
    if "timeout" in reason or "timed out" in reason:
        return "timeout"
    if any(token in reason for token in ("http_", "connection", "network", "dns", "discovery unavailable")):
        return "transient_network"
    if isinstance(exc, ValueError) or any(
        token in reason for token in ("schema", "invalid", "mismatch", "rejected", "insufficient")
    ):
        return "validation"
    if isinstance(exc, OSError):
        return "io"
    return "source_runtime"


def migrate_check_schedule(schedule):
    sources = dict(schedule.sources)
    for source in ("apex", "mayhem"):
        old = sources.get(source)
        interval = int(SOURCE_INTERVALS[source].total_seconds())
        if old is None or old.state == "backoff" or old.check_interval_seconds == interval:
            continue
        checked = parse_refresh_time(old.last_checked_at or old.last_success_at)
        old_due = parse_refresh_time(old.next_due_at)
        due = checked + timedelta(seconds=interval) if checked else None
        sources[source] = replace(old, check_interval_seconds=interval,
            next_due_at=min(due, old_due).isoformat() if due and old_due else due.isoformat() if due else "")
    return replace(schedule, sources=sources)


def checked_source_state(old: RefreshSourceState, source: str, pointer: Mapping[str, Any],
                         result: Mapping[str, Any], *, error: str = "", applied_revision: str = "",
                         capability_pending: bool = False, now: datetime | None = None,
                         jitter=None) -> RefreshSourceState:
    now = now or datetime.now(timezone.utc)
    interval = int(SOURCE_INTERVALS[source].total_seconds())
    failures = old.consecutive_failures + 1 if error else 0
    delay = interval
    if error:
        delay = 21600 if error == "validation" else (300, 900, 3600, 14400)[min(failures-1, 3)]
        if error != "validation":
            delay = min(14400, delay * (jitter or random.uniform)(1.0, 1.10))
        try:
            retry_after = float(result.get("retry_after_seconds") or 0)
        except (TypeError, ValueError):
            retry_after = 0
        if math.isfinite(retry_after) and retry_after > 0:
            delay = max(delay, retry_after)
    elif capability_pending:
        delay = 300
    revision = str(
        result.get("upstream_revision")
        or (old.upstream_revision if error else applied_revision or old.upstream_revision)
    )
    applied = str(result.get("applied_revision") or applied_revision or old.applied_revision)
    reported_status = str(result.get("check_status") or "")
    if reported_status not in {"unknown", "changed", "up_to_date"}:
        reported_status = "up_to_date" if revision and applied == revision else "unknown"
    if reported_status == "changed" and revision and applied == revision:
        reported_status = "up_to_date"
    return replace(old, last_attempt_at=now.isoformat(),
        last_success_at=old.last_success_at if error else now.isoformat(),
        last_checked_at=old.last_checked_at if error else now.isoformat(),
        next_due_at=(now + timedelta(seconds=delay)).isoformat(),
        state="backoff" if error else "ready", failure_kind=error,
        current_run_id=str(pointer.get("run_id") or pointer.get("catalog_generation_id") or old.current_run_id),
        check_status="failed" if error else reported_status,
        upstream_revision=revision, applied_revision=applied, check_interval_seconds=interval,
        consecutive_failures=failures,
        failure_fingerprint=str(result.get("failure_fingerprint") or old.failure_fingerprint) if error else "")


__all__ = [
    "checked_source_state",
    "exception_source_result",
    "migrate_check_schedule",
    "source_failure_kind",
]

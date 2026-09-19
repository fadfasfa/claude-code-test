"""Schedule persistence and per-source check evidence for refresh_service."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from typing import Any, Mapping

from hextech.contracts import RefreshSourceState, utc_now_iso
from hextech.infrastructure.persistence.cohort_validation_receipt import write_validation_receipt
from hextech.modules.data.freshness import parse_refresh_time
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.catalog.versioned import sha256_file

from .refresh_policy import checked_source_state
from hextech.infrastructure.observability.refresh_attempts import record_refresh_attempt


REFRESH_SOURCES = ("catalog", "aramkit", "apex", "mayhem")
_CHECK_EVIDENCE_FIELDS = (
    "check_status",
    "upstream_revision",
    "applied_revision",
    "retry_after_seconds",
    "failure_fingerprint",
    "not_modified",
    "from_cache",
)


def _manifest_metadata(root: Path, pointer: Mapping[str, Any], source: str) -> dict[str, Any]:
    run_id = str(pointer.get("run_id") or "")
    if not run_id:
        return {}
    path = root / "sources" / source / "runs" / run_id / "manifest.json"
    try:
        if sha256_file(path) != pointer.get("manifest_sha256"):
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
    return dict(metadata) if isinstance(metadata, Mapping) else {}


class RefreshScheduleMixin:
    """Mixin intentionally depends only on the coordinator's stable attributes."""

    def _record_skipped_checks(self, result: Mapping[str, Any]) -> None:
        with self._lock:
            for source, outcome in result.get("source_outcomes", {}).items():
                if outcome.get("state") == "not_due":
                    record_refresh_attempt(self.root, source=source, checked_at=utc_now_iso(),
                        error="", result=outcome, generation_id=self.publisher.current_generation_id())

    def _backoff_pending(self, source: str) -> bool:
        """Missing local data may bypass a normal check interval, not a retry deadline."""
        state = self.schedule_store.load().sources.get(source, RefreshSourceState())
        due = parse_refresh_time(state.next_due_at)
        return bool(state.state == "backoff" and due is not None and due > datetime.now(timezone.utc))

    def _due(self, source: str, force: bool = False) -> bool:
        if source == "catalog" and not force and time.monotonic() < self._catalog_retry_at:
            return False
        state = self.schedule_store.load().sources.get(source, RefreshSourceState())
        due = parse_refresh_time(state.next_due_at)
        if source in {"apex", "mayhem"} and source not in self._optional and state.state != "backoff":
            return True
        return force or due is None or due <= datetime.now(timezone.utc)

    def seconds_until_due(self) -> float:
        now = datetime.now(timezone.utc)
        times = [
            (
                now + timedelta(seconds=self._catalog_retry_at - time.monotonic())
                if source == "catalog" and time.monotonic() < self._catalog_retry_at
                else parse_refresh_time(state.next_due_at)
            )
            for source, state in self.schedule_store.load().sources.items()
            if source in REFRESH_SOURCES and not (
                source in {"apex", "mayhem"}
                and self._optional_thread is not None
                and self._optional_thread.is_alive()
            )
        ]
        return max(
            1.0,
            min((max(0.0, (item - now).total_seconds()) if item else 0.0 for item in times), default=1.0),
        )

    def _applied_revision(
        self,
        source: str,
        pointer: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> str:
        reported = str(result.get("applied_revision") or "")
        if reported:
            return reported
        if source == "catalog":
            return str(pointer.get("catalog_generation_id") or "")
        metadata = _manifest_metadata(self.root, pointer, source)
        if source == "aramkit":
            version = metadata.get("version")
            return str(
                metadata.get("source_version")
                or (version.get("dataPath") if isinstance(version, Mapping) else "")
                or ""
            )
        marker = metadata.get("marker")
        return str(
            metadata.get("applied_revision")
            or metadata.get("upstream_revision")
            or (marker.get("content_sha256") if isinstance(marker, Mapping) else "")
            or ""
        )

    def _mark_source(
        self,
        source: str,
        *,
        error: str = "",
        result: Mapping[str, Any] | None = None,
        pointer: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            schedule = self.schedule_store.load()
            old = schedule.sources.get(source, RefreshSourceState())
            sources = dict(schedule.sources)
            selected = dict(pointer or self._source_pointer(source))
            evidence = dict(result or self._source_results.get(source, {}))
            applied = self._applied_revision(source, selected, evidence)
            if source == "catalog":
                revision = str(selected.get("catalog_generation_id") or applied)
                evidence.setdefault("upstream_revision", revision)
                evidence.setdefault("applied_revision", revision)
                evidence.setdefault(
                    "check_status",
                    "changed" if evidence.get("changed") else "up_to_date",
                )
            sources[source] = checked_source_state(
                old,
                source,
                selected,
                evidence,
                error=error,
                applied_revision=applied,
                capability_pending=(source == "catalog" and bool(self._catalog_capability_pending)),
            )
            now = datetime.now(timezone.utc)
            self.schedule_store.save(
                replace(
                    schedule,
                    updated_at=now.isoformat(),
                    generation_id=self.publisher.current_generation_id(),
                    sources=sources,
                )
            )
            if (
                self._last_candidate is not None
                and self._last_candidate.generation_id == self.publisher.current_generation_id()
            ):
                write_validation_receipt(self.root, self._last_candidate)
            record_refresh_attempt(self.root, source=source, checked_at=now.isoformat(),
                                   error=error, result=evidence,
                                   generation_id=self.publisher.current_generation_id())

    def _published_source_status(self) -> dict[str, Mapping[str, Any]]:
        try:
            status = DataSnapshotClient(self.publisher.root).open_view().status().get("source_status")
        except (OSError, ValueError, RuntimeError):
            return {}
        if not isinstance(status, Mapping):
            return {}
        return {
            str(source): dict(value)
            for source, value in status.items()
            if isinstance(source, str) and isinstance(value, Mapping)
        }

    def _source_outcomes(
        self,
        *,
        attempted: set[str],
        failures: Mapping[str, str],
        deferred: set[str],
        initial_identities: Mapping[str, str],
    ) -> dict[str, dict[str, Any]]:
        published = self._published_source_status()
        schedule = self.schedule_store.load().sources
        outcomes: dict[str, dict[str, Any]] = {}
        for source in REFRESH_SOURCES:
            identity = self._source_identity(source)
            detail = published.get(source, {})
            data_status = str(detail.get("data_status") or "unknown")
            if source == "catalog":
                try:
                    self._validate_catalog(self._source_pointer(source))
                    availability = "available"
                except (OSError, ValueError, RuntimeError):
                    availability = "unavailable"
            elif data_status == "confirmed_empty":
                availability = "confirmed_empty"
            elif identity and int(detail.get("record_count") or 0) > 0 and data_status not in {
                "pending",
                "unavailable",
            }:
                availability = "available"
            else:
                availability = "unavailable"
            changed = bool(identity and identity != str(initial_identities.get(source) or ""))
            optional_result = self._optional_results.get(source, {})
            optional_current = bool(
                float(optional_result.get("started_at") or 0.0)
                >= float(self._progress.started_at or 0.0)
                > 0.0
            )
            observed = source in attempted or optional_current
            optional_failure = (
                str(optional_result.get("reason_code") or "")
                if source not in attempted and optional_current and optional_result.get("state") == "failed"
                else ""
            )
            failure_reason = str(failures.get(source) or optional_failure)
            failed = bool(failure_reason)
            used_last_good = failed and availability == "available"
            state = (
                "last_good" if used_last_good else
                "unavailable" if failed else
                "deferred" if source in deferred else
                "confirmed_empty" if availability == "confirmed_empty" else
                "updated" if observed and changed else
                "unchanged" if observed else
                "not_due" if availability == "available" else
                "unavailable"
            )
            source_schedule = schedule.get(source, RefreshSourceState())
            evidence = self._source_results.get(source, {}) if observed else {}
            check_evidence = {
                key: evidence[key]
                for key in _CHECK_EVIDENCE_FIELDS
                if key in evidence and evidence[key] not in (None, "")
            }
            outcomes[source] = {
                "state": state,
                "checked": bool(
                    observed
                    and not failed
                    and source not in deferred
                    and source_schedule.check_status in {"changed", "up_to_date"}
                ),
                "changed": changed,
                "availability": availability,
                "used_last_good": used_last_good,
                "data_status": data_status,
                "reason_code": str(failure_reason or detail.get("data_reason") or ""),
                "failure_kind": str(source_schedule.failure_kind or ""),
                "next_retry_at": str(source_schedule.next_due_at or "") if failed else "",
                "current_run_id": str(source_schedule.current_run_id or ""),
                "check_status": source_schedule.check_status,
                "upstream_revision": source_schedule.upstream_revision,
                "applied_revision": source_schedule.applied_revision,
                "last_checked_at": source_schedule.last_checked_at,
                "check_evidence": check_evidence,
            }
        return outcomes


__all__ = ["REFRESH_SOURCES", "RefreshScheduleMixin"]

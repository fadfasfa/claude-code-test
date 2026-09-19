"""Bounded refresh evidence. Logical checks are not an HTTP availability estimate."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.ports.atomic import atomic_write_json


MAX_RECORDS = 200
MAX_BYTES = 128 * 1024


def record_refresh_attempt(root: Path, *, source: str, checked_at: str,
                           error: str, result: Mapping[str, Any],
                           generation_id: str) -> None:
    """Called under the DataService lock; diagnostic failure must not fail publication."""
    path = root / "state" / "refresh_attempts.v1.json"
    try:
        records = []
        if path.exists():
            if path.stat().st_size > MAX_BYTES:
                return  # Unknown/oversized evidence is not ours to overwrite.
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1 or payload.get("owner") != "data-service":
                return
            records = list(payload.get("records", []))
        record = {"source": source, "checked_at": checked_at,
                  "outcome": "skipped" if result.get("state") == "not_due" else "failed" if error else "completed",
                  "failure_kind": error,
                  "generation_id": generation_id,
                  "check_status": str(result.get("check_status") or "unknown"),
                  "upstream_revision": str(result.get("upstream_revision") or "")[:160],
                  "applied_revision": str(result.get("applied_revision") or "")[:160],
                  "from_cache": result.get("from_cache"), "not_modified": result.get("not_modified"),
                  "http_summary": result.get("http_summary"),
                  "http_denominator_complete": isinstance(result.get("http_summary"), Mapping)}
        records = [*records, record][-MAX_RECORDS:]
        payload = {"schema_version": 1, "owner": "data-service", "records": records,
                   "population": "retained_logical_checks_not_http_requests"}
        while records and len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")) > MAX_BYTES:
            records.pop(0)
        if records:
            atomic_write_json(path, payload, ensure_ascii=False, indent=2)
    except (OSError, ValueError, TypeError, AttributeError):
        return

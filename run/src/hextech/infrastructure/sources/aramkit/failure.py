"""Bounded worker failure evidence for ARAMKit source checks."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from typing import Any, Mapping

from hextech.infrastructure.transport.conditional_response import parse_retry_after_seconds

from .http_response import _Response
from .revisions import PARSER_REVISION, PROJECTION_REVISION


def validation_input_fingerprint(
    marker: Mapping[str, Any],
    *,
    catalog_generation_id: str,
    catalog_sha256: str,
    response_body: bytes = b"",
) -> str:
    payload = {
        "marker": dict(marker),
        "response_sha256": hashlib.sha256(response_body).hexdigest() if response_body else "",
        "parser_revision": PARSER_REVISION,
        "projection_revision": PROJECTION_REVISION,
        "catalog_generation_id": catalog_generation_id,
        "catalog_sha256": catalog_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def failure_fields(
    reason: str,
    responses: Iterable[_Response],
    *,
    failure_fingerprint: str = "",
) -> dict[str, Any]:
    items = tuple(responses)
    retry_values = [
        value
        for item in items
        if item.failure_kind is not None
        if (value := parse_retry_after_seconds(item.response_headers)) is not None
    ]
    failed = next((item for item in items if item.failure_kind is not None), None)
    return {
        "failure_kind": failed.failure_kind.value if failed is not None else reason,
        "failure_stage": (
            "validation"
            if reason in {"schema_changed", "catalog_binding_failed", "marker_drift"}
            else "fetch"
        ),
        "retry_after_seconds": max(retry_values, default=None),
        "failure_fingerprint": failure_fingerprint,
    }


def duplicate_validation_result(
    marker: Mapping[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "reason": "validation_unchanged",
        "failure_stage": "validation",
        "failure_kind": "validation",
        "failure_fingerprint": fingerprint,
        "check_status": "unknown",
        "upstream_revision": str(marker.get("dataPath") or ""),
        "upstream_marker": dict(marker),
        "applied_revision": None,
        "retry_after_seconds": 21600,
        "diagnostics": {"failure_fingerprint": fingerprint, "deduplicated": True},
    }


__all__ = ["duplicate_validation_result", "failure_fields", "validation_input_fingerprint"]

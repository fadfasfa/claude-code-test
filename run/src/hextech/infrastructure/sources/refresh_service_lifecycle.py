"""Small lifecycle helpers shared by the incremental refresh coordinator."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from hextech.infrastructure.persistence.retention import apply_cohort_retention


class CatalogRefreshDeferred(RuntimeError):
    """Markers were checked; expensive recognition acquisition waits for safe load."""


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return dict(value) if isinstance(value, Mapping) else {}
    except (OSError, ValueError):
        return {}


def try_postcommit_retention(service: Any) -> None:
    """Run a pending governance pass while the coordinator lock proves quiescence."""

    with service._lock:
        optional = service._optional_thread
        workers_idle = (
            not service._refresh_active
            and not service._cancels
            and not getattr(service, "_optional_active", False)
            and (optional is None or not optional.is_alive())
        )
        if not service._retention_pending:
            return
        try:
            result = apply_cohort_retention(service.root, workers_idle=workers_idle)
        except Exception as exc:
            result = {"disposition": "failed", "reason": f"{type(exc).__name__}:{exc}"}
        service._last_retention_result = result
        if result.get("disposition") == "completed":
            service._retention_pending = False


def run_optional_refresh(service: Any) -> None:
    try:
        service._refresh_optional_sources()
    finally:
        with service._lock:
            service._optional_force_requested = False
            service._optional_active = False
            service._optional_thread = None
        service._try_retention()


__all__ = [
    "CatalogRefreshDeferred",
    "read_object",
    "run_optional_refresh",
    "try_postcommit_retention",
]

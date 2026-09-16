# Frozen V2 regression baseline only; not a production runtime entry point.
"""活动 Catalog refresh 与 blocked adoption checkpoint 的分轨辅助逻辑。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Mapping

from .source_freshness import iso_utc
from hextech.contracts import RefreshScheduleV1


def normalize_pending_sources(coordinator: Any, payload: Mapping[str, Any]) -> list[str]:
    completed = payload.get("completed_sources")
    completed_names = set(completed) if isinstance(completed, Mapping) else set()
    raw_pending = payload.get("pending_sources")
    pending = {str(source) for source in raw_pending} if isinstance(raw_pending, list) else set()
    return coordinator._ordered_sources(pending - completed_names)


def reconcile_active_catalog_schedule(
    coordinator: Any,
    current: Mapping[str, Mapping[str, Any]],
    schedule: RefreshScheduleV1,
) -> RefreshScheduleV1:
    """让 activity schedule 的来源身份始终指向已提交的 active pointers。

    Catalog adoption candidate 可能在 worker 目录或旧 checkpoint 中存在，但在
    promotion 完成前不能成为活动 schedule 的 current_run_id。发现残留漂移时只
    修复 schedule 的身份字段，保留原有 due/backoff 证据，并在同一 promotion 锁
    下重新读取后原子写回，避免覆盖并发刷新进度。
    """

    def expected_state(
        source: str,
        pointer: Mapping[str, Any],
    ) -> tuple[str, str]:
        run_id = str(
            pointer.get("catalog_generation_id")
            if source == "catalog"
            else pointer.get("run_id")
            or ""
        )
        success_at = str(pointer.get("last_success_at") or pointer.get("completed_at") or "")
        return run_id, success_at

    def needs_reconcile(
        source: str,
        pointer: Mapping[str, Any],
        state: Any,
    ) -> bool:
        run_id, success_at = expected_state(source, pointer)
        return bool(
            run_id
            and state is not None
            and (
                state.current_run_id != run_id
                or (success_at and state.last_success_at != success_at)
            )
        )

    if not any(
        needs_reconcile(source, current.get(source) or {}, schedule.sources.get(source))
        for source in schedule.sources
    ):
        return schedule

    with coordinator.promotion.exclusive():
        latest = coordinator.schedule_store.load()
        latest_current = {
            source: coordinator._current_pointer(source) or current.get(source) or {}
            for source in latest.sources
        }
        if not any(
            needs_reconcile(source, latest_current[source], latest.sources.get(source))
            for source in latest.sources
        ):
            return latest
        sources = dict(latest.sources)
        for source, state in tuple(sources.items()):
            pointer = latest_current.get(source) or {}
            if not needs_reconcile(source, pointer, state):
                continue
            run_id, success_at = expected_state(source, pointer)
            sources[source] = replace(
                state,
                current_run_id=run_id,
                last_success_at=success_at or state.last_success_at,
            )
        reconciled = RefreshScheduleV1(
            updated_at=iso_utc(coordinator.now()),
            generation_id=latest.generation_id,
            sources=sources,
        )
        coordinator.schedule_store.save(reconciled)
        return reconciled


def migrate_foreign_catalog_checkpoint(
    coordinator: Any,
    current: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """把未发布的新 Catalog 周期移入 adoption lane，并释放活动刷新通道。"""

    raw = coordinator.checkpoint_store.load()
    if raw.get("state") != "in_progress" or str(raw.get("core_generation_id") or ""):
        return raw
    catalog = raw.get("catalog")
    current_catalog = current.get("catalog") or {}
    if not isinstance(catalog, Mapping) or not current_catalog:
        return raw
    if coordinator._catalog_identity(catalog) == coordinator._catalog_identity(current_catalog):
        return raw
    migrated = {
        **raw,
        "state": "blocked",
        "lane": "catalog_adoption",
        "blocked_reason": str(raw.get("blocked_reason") or "catalog_adoption_incomplete"),
        "pending_sources": normalize_pending_sources(coordinator, raw),
        "migrated_at": iso_utc(coordinator.now()),
    }
    coordinator.adoption_checkpoint_store.save(migrated)
    coordinator.checkpoint_store.save(
        {
            **raw,
            "state": "migrated",
            "lane": "catalog_adoption",
            "migrated_to": coordinator.adoption_checkpoint_store.path.name,
            "pending_sources": normalize_pending_sources(coordinator, raw),
            "migrated_at": migrated["migrated_at"],
        }
    )
    return {}


def blocked_catalog_adoption(
    coordinator: Any,
    current_catalog: Mapping[str, Any],
) -> dict[str, Any]:
    """返回已验证的待采用 Catalog；活动刷新只用它证明可过滤的新身份。"""

    adoption = coordinator.adoption_checkpoint_store.load()
    catalog = adoption.get("catalog")
    blocked = bool(
        adoption.get("state") == "blocked"
        and current_catalog
        and isinstance(catalog, Mapping)
        and coordinator._catalog_identity(catalog)
        != coordinator._catalog_identity(current_catalog)
    )
    if not blocked or not isinstance(catalog, Mapping):
        return {}
    try:
        return coordinator._validate_catalog_candidate(catalog)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


__all__ = [
    "blocked_catalog_adoption",
    "migrate_foreign_catalog_checkpoint",
    "normalize_pending_sources",
    "reconcile_active_catalog_schedule",
]

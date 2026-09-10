"""刷新候选的原子 generation 晋升事务。

本模块只封装 coordinator 已验证 targets 的 publish、recovery point、journal 与
retention 收口；来源调度、worker 执行和候选选择仍由 refresh_coordinator 负责。
"""

from __future__ import annotations

from typing import Any, Mapping

from hextech.infrastructure.persistence.cohort_recovery import (
    CohortCandidate,
    build_recovery_point,
    due_schedule,
    validate_generation_cohort,
)
from hextech.infrastructure.persistence.cohort_validation_receipt import write_validation_receipt
from hextech.infrastructure.persistence.refresh_schedule import SCHEDULE_SOURCES
from hextech.infrastructure.persistence.retention import apply_retention
from hextech.modules.data.freshness import SOURCE_INTERVALS
from hextech.modules.data.generation import DataSnapshotClient
from hextech.bootstrap.source_freshness import evaluate_source_expiry, iso_utc


def _source_status(
    coordinator: Any,
    targets: Mapping[str, Mapping[str, Any]],
    *,
    degraded_sources: set[str],
    pending_sources: set[str],
    completed: Any,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in SCHEDULE_SOURCES:
        target = targets[source]
        if source == "catalog":
            run_id = str(target.get("catalog_generation_id") or "")
            artifact_sha = str(target.get("content_sha256") or "")
            manifest_sha = str(target.get("manifest_sha256") or "")
            record_count = 0
        else:
            run_id, _, artifact_sha, record_count, manifest_sha = coordinator._pointer_identity(target)
        data_at_text = str(
            target.get("last_success_at")
            or target.get("completed_at")
            or target.get("created_at")
            or ""
        )
        expired, stale_age_seconds = evaluate_source_expiry(
            data_at_text,
            SOURCE_INTERVALS[source],
            completed,
        )
        if source in degraded_sources:
            data_reason = "candidate_rejected_last_good_preserved"
        elif source in pending_sources:
            data_reason = "refresh_pending_last_good_preserved"
        elif expired:
            data_reason = "source_data_expired"
        else:
            data_reason = ""
        last_good = (
            source in degraded_sources
            or source in pending_sources
            or (source != "catalog" and coordinator._is_baseline(target))
        )
        result[source] = {
            "catalog_id": str(
                target.get("catalog_generation_id")
                or targets["catalog"].get("catalog_generation_id")
                or ""
            ),
            "data_at": data_at_text,
            "checked_at": iso_utc(completed),
            "freshness": "last_good" if last_good else "fresh",
            "run_id": run_id,
            "origin_generation_id": str(target.get("origin_generation_id") or ""),
            "artifact_sha256": artifact_sha,
            "manifest_sha256": manifest_sha,
            "record_count": record_count,
            "data_status": "data_stale" if source in degraded_sources or expired else "fresh",
            "data_reason": data_reason,
            "stale_age_seconds": stale_age_seconds,
            "coverage": coordinator._source_coverage(source, target),
        }
    return result


def _semantic_status(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        normalized = dict(value)
        normalized.pop("checked_at", None)
        normalized.pop("stale_age_seconds", None)
        result[str(source)] = normalized
    return result


def _same_target_identity(
    coordinator: Any,
    targets: Mapping[str, Mapping[str, Any]],
    candidate: CohortCandidate,
) -> bool:
    expected_catalog = targets["catalog"]
    current_catalog = candidate.pointers["catalog"]
    if any(
        str(expected_catalog.get(field) or "") != str(current_catalog.get(field) or "")
        for field in ("catalog_generation_id", "content_sha256", "manifest_sha256")
    ):
        return False
    return all(
        coordinator._pointer_identity(targets[source])
        == coordinator._pointer_identity(candidate.pointers[source])
        for source in SCHEDULE_SOURCES
        if source != "catalog"
    )


def _matching_current_manifest(
    publisher: Any,
    payloads: Mapping[str, Any],
    *,
    source_files: Any,
    health: str,
    degraded_sources: list[str],
    source_status: Mapping[str, Mapping[str, Any]],
) -> Any | None:
    """兼容未提供语义 matcher 的 822 publisher；缺失时继续原生 publish。"""

    matcher = getattr(publisher, "matching_current_manifest", None)
    if not callable(matcher):
        return None
    return matcher(
        payloads,
        source_files=source_files,
        health=health,
        degraded_sources=degraded_sources,
        source_status=source_status,
    )


def promote_targets(
    coordinator: Any,
    targets: Mapping[str, Mapping[str, Any]],
    *,
    degraded_sources: set[str],
    pending_sources: set[str],
    refreshed_sources: list[str],
) -> tuple[Any, dict[str, Any]]:
    """把一组同 Catalog targets 原子发布为 immutable generation。"""

    coordinator._cohort_is_bound(targets)
    completed = coordinator.now()
    source_status = _source_status(
        coordinator,
        targets,
        degraded_sources=degraded_sources,
        pending_sources=pending_sources,
        completed=completed,
    )
    health = "degraded" if degraded_sources else "healthy"
    current_generation_id = coordinator.publisher.current_generation_id()
    if current_generation_id:
        try:
            current_candidate = validate_generation_cohort(
                coordinator.root,
                current_generation_id,
            )
            current_manifest = DataSnapshotClient(
                coordinator.root / "snapshots"
            ).open_generation(current_generation_id).manifest
        except (OSError, TypeError, ValueError):
            current_candidate = None
            current_manifest = None
        if (
            current_candidate is not None
            and current_manifest is not None
            and _same_target_identity(coordinator, targets, current_candidate)
            and current_manifest.health == health
            and current_manifest.degraded_sources
            == tuple(sorted(str(item) for item in degraded_sources))
            and _semantic_status(
                {
                    source: status.to_dict()
                    for source, status in current_manifest.source_status.items()
                }
            )
            == _semantic_status(source_status)
        ):
            write_validation_receipt(coordinator.root, current_candidate)
            coordinator.publisher.last_promotion_disposition = "unchanged"
            return current_manifest, {
                "promotion_disposition": "unchanged",
                "reason_code": "no_content_change",
            }
    journal_started = False
    try:
        journal = coordinator.promotion.begin()
        journal_started = True
        for source in SCHEDULE_SOURCES:
            if not coordinator._is_baseline(targets[source]):
                coordinator.promotion.record_target(source, targets[source])
        coordinator.promotion.promote_dependencies()
        build = coordinator.builder(targets)
        coordinator._build_matches_targets(build, targets)
        current = _matching_current_manifest(
            coordinator.publisher,
            build.payloads,
            source_files=build.source_files,
            health=health,
            degraded_sources=sorted(degraded_sources),
            source_status=source_status,
        )
        if current is not None:
            coordinator.promotion.rollback()
            journal_started = False
            try:
                candidate = validate_generation_cohort(coordinator.root, current.generation_id)
            except (OSError, TypeError, ValueError):
                candidate = None
            if candidate is not None:
                write_validation_receipt(coordinator.root, candidate)
            coordinator.publisher.last_promotion_disposition = "unchanged"
            return current, {
                "promotion_disposition": "unchanged",
                "reason_code": "no_content_change",
            }
        manifest = coordinator.publisher.publish(
            build.payloads,
            source_files=build.source_files,
            require_complete_provenance=True,
            health=health,
            refreshed_sources=refreshed_sources,
            degraded_sources=sorted(degraded_sources),
            source_status=source_status,
        )
        try:
            candidate = validate_generation_cohort(coordinator.root, manifest.generation_id)
        except (OSError, TypeError, ValueError):
            # 部分历史/单测 builder 没有 production pool；仍保留 journal 原子性，
            # 但这种 recovery point 之后无法通过严格选择门，不会冒充健康 cohort。
            candidate = CohortCandidate(
                generation_id=manifest.generation_id,
                generation_created_at=manifest.created_at,
                manifest_health=manifest.health,
                pointers={source: dict(targets[source]) for source in SCHEDULE_SOURCES},
                production_pool_id="",
                production_pool_count=0,
            )
        old_generation = journal.old_pointers.get("generation", {})
        old_current = old_generation.get("current") if isinstance(old_generation, Mapping) else None
        previous_generation_id = (
            str(old_current.get("current_generation_id") or "")
            if isinstance(old_current, Mapping)
            else ""
        )
        recovery_schedule = due_schedule(candidate, updated_at=iso_utc(completed))
        recovery_point = build_recovery_point(
            candidate,
            previous_generation_id=previous_generation_id,
            schedule=recovery_schedule,
            recorded_at=iso_utc(completed),
        )
        coordinator.promotion.stage_generation_state(
            schedule=recovery_schedule.to_dict(),
            recovery_point=recovery_point.to_dict(),
        )
        coordinator.promotion.record_generation_promoted(manifest.generation_id)
        coordinator.promotion.commit()
        write_validation_receipt(coordinator.root, candidate)
        try:
            retention = apply_retention(coordinator.root, now=coordinator.now())
        except OSError as exc:
            retention = {"error": exc.__class__.__name__}
        retention["promotion_disposition"] = "published"
        return manifest, retention
    except Exception:
        if journal_started:
            coordinator.promotion.rollback()
        raise


__all__ = ["promote_targets"]

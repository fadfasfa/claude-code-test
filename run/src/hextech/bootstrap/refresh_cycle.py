"""DataService 单次两阶段刷新事务。

本模块只承载 ``CohortRefreshCoordinator.refresh`` 的事务编排，以控制主协调器体积；
具体 pointer 校验、worker 执行、发布和 schedule 持久化仍由协调器提供。它不创建第二
个 worker，也不拥有外部 API。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from hextech.bootstrap.game_refresh_gate import (
    GameRefreshDeferred,
    RefreshStopRequested,
    normalize_refresh_scope,
)
from hextech.bootstrap.source_freshness import iso_utc
from hextech.bootstrap.source_worker_failure import SourceWorkerFailure
from hextech.infrastructure.persistence.refresh_schedule import SCHEDULE_SOURCES
from hextech.modules.data.ports.atomic import atomic_write_json


CORE_SOURCES = ("aramkit", "blitz")
OPTIONAL_SOURCES = ("apex", "mayhem")


class RefreshCycleMixin:
    """为协调器提供单次 refresh；``self`` 由组合类注入。"""

    def refresh(self: Any, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        if self._stop.is_set():
            return {
                "state": "degraded",
                "reason_code": "shutdown_requested",
                "generation_id": self.publisher.current_generation_id(),
            }
        now = self.now()
        schedule = self.schedule_store.load()
        current = {source: self._current_pointer(source) for source in SCHEDULE_SOURCES}
        schedule = self._reconcile_active_catalog_schedule(current, schedule)
        states = dict(schedule.sources)
        checkpoint = self._load_resumable_checkpoint(current)
        adoption_catalog = self._blocked_catalog_adoption(current["catalog"])
        adoption_blocks_catalog = bool(adoption_catalog)
        resumed = bool(checkpoint)
        requested_scope = normalize_refresh_scope(scope)
        checkpoint_scope = normalize_refresh_scope(checkpoint.get("scope"))
        full_force_requested = bool(force) and requested_scope == "due"
        full_force_checkpoint = bool(checkpoint.get("force")) and checkpoint_scope == "due"
        effective_scope = (
            "due"
            if full_force_requested or full_force_checkpoint
            else "core"
            if "core" in {requested_scope, checkpoint_scope}
            else "due"
        )
        effective_force = bool(force or checkpoint.get("force"))
        # poll_deferred_refresh() 与 worker 并行运行，需要知道被取消请求的完整
        # 语义，不能把赛后恢复降级成普通 due check。
        self._active_refresh_request = {
            "force": effective_force,
            "scope": effective_scope,
        }
        checkpoint_core_generation_id = str(checkpoint.get("core_generation_id") or "")
        checkpoint_pending = set(checkpoint.get("pending_sources") or [])
        checkpoint_failure_payload = checkpoint.get("failures")
        checkpoint_failure_sources = {
            str(source)
            for source, payload in (
                checkpoint_failure_payload.items()
                if isinstance(checkpoint_failure_payload, Mapping)
                else ()
            )
            if isinstance(payload, Mapping)
        }
        if self._game_refresh.in_progress():
            deferred = dict(
                self._game_refresh.defer_result(force=effective_force, scope=effective_scope)
            )
            deferred.update(
                {
                    "refresh_phase": str(checkpoint.get("refresh_phase") or "core"),
                    "core_generation_id": checkpoint_core_generation_id,
                    "pending_sources": self._ordered_sources(checkpoint_pending),
                    "resumed_from_checkpoint": resumed,
                }
            )
            return deferred
        def force_source(source: str) -> bool:
            requested = bool(force) and (
                requested_scope == "due" or source in CORE_SOURCES
            )
            resumed_request = bool(checkpoint.get("force")) and (
                source not in checkpoint_failure_sources
                and (checkpoint_scope == "due" or source in CORE_SOURCES)
            )
            return requested or resumed_request

        due = {
            source: self._due(
                source,
                states[source],
                current[source],
                force=bool(
                    force_source(source)
                    or (
                        source in checkpoint_pending
                        and source not in checkpoint_failure_sources
                    )
                ),
            )
            for source in SCHEDULE_SOURCES
        }
        if adoption_blocks_catalog:
            due["catalog"] = False
        upstream_changed = False
        upstream_marker: dict[str, Any] = {}
        if resumed:
            for source in set((checkpoint.get("completed_sources") or {}).keys()):
                due[source] = False
        # checkpoint 已固定 cohort；恢复时不再由新 marker probe 改写本轮 Catalog。
        aramkit_backoff_active = states["aramkit"].state == "backoff" and not due["aramkit"]
        if (
            not resumed
            and (not effective_force or effective_scope == "core")
            and not aramkit_backoff_active
        ):
            upstream_changed, upstream_marker = self._probe_aramkit_upstream_change(current["aramkit"])
            if upstream_changed:
                due.update(
                    {
                        source: True
                        for source in SCHEDULE_SOURCES
                        if (source != "catalog" or not adoption_blocks_catalog)
                        and not (
                            states[source].state == "backoff"
                            and not due[source]
                        )
                    }
                )
        if not any(due.values()):
            if resumed and checkpoint_pending:
                return {
                    "state": "degraded" if self.publisher.current_generation_id() else "failed",
                    "reason_code": "refresh_backoff_pending",
                    "generation_id": self.publisher.current_generation_id(),
                    "core_generation_id": checkpoint_core_generation_id,
                    "refresh_phase": str(checkpoint.get("refresh_phase") or "core"),
                    "pending_sources": self._ordered_sources(checkpoint_pending),
                    "resumed_from_checkpoint": True,
                    "failures": {
                        source: dict(payload)
                        for source, payload in (
                            checkpoint_failure_payload.items()
                            if isinstance(checkpoint_failure_payload, Mapping)
                            else ()
                        )
                        if source in checkpoint_pending and isinstance(payload, Mapping)
                    },
                }
            if not resumed:
                return {
                    "state": "ready",
                    "reason_code": "not_stale",
                    "generation_id": self.publisher.current_generation_id(),
                }
            # worker 成功后、promotion 前进程退出时，checkpoint 可能已经没有
            # pending source。这里仍须继续组合并原子晋升已验证 candidates；把它
            # 当作普通 not_stale 会永久遗留未发布 run 与旧 backoff 证据。

        cycle_id = str(checkpoint.get("cycle_id") or "") or (
            now.strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(4).hex()
        )
        checkpoint_created_at = str(checkpoint.get("created_at") or "") or iso_utc(now)
        work = self.root / "snapshots" / "staging" / f"refresh-{cycle_id}"
        work.mkdir(parents=True, exist_ok=resumed)
        compatibility_catalog_pointer: Path | None = None
        if adoption_catalog:
            compatibility_catalog_pointer = work / "catalog.compatibility.v2.json"
            atomic_write_json(
                compatibility_catalog_pointer,
                adoption_catalog,
                ensure_ascii=False,
                indent=2,
            )
        targets = {source: dict(current[source]) for source in SCHEDULE_SOURCES}
        baseline = self._baseline_contributions(current["catalog"])
        for source in ("aramkit", "blitz", "apex", "mayhem"):
            if not targets[source] and source in baseline:
                targets[source] = dict(baseline[source])
        results: dict[str, Any] = {}
        checkpoint_failures = checkpoint.get("failures")
        failures: dict[str, Any] = {
            str(source): dict(payload)
            for source, payload in (
                checkpoint_failures.items() if isinstance(checkpoint_failures, Mapping) else ()
            )
            if str(source) in checkpoint_pending and isinstance(payload, Mapping)
        }
        completed_sources: dict[str, dict[str, Any]] = {
            str(source): dict(pointer)
            for source, pointer in (checkpoint.get("completed_sources") or {}).items()
            if isinstance(pointer, Mapping)
        }
        pending_sources = (
            set(checkpoint_pending)
            if resumed
            else {source for source, needed in due.items() if needed}
        )
        newly_completed: set[str] = set()
        attempted_this_run: set[str] = set()

        def shutdown_result(phase: str) -> dict[str, Any]:
            return {
                "state": "degraded" if self.publisher.current_generation_id() else "failed",
                "reason_code": "shutdown_requested",
                "generation_id": self.publisher.current_generation_id(),
                "refresh_phase": phase,
                "core_generation_id": checkpoint_core_generation_id,
                "pending_sources": self._ordered_sources(pending_sources),
                "resumed_from_checkpoint": resumed,
            }
        for source, pointer in completed_sources.items():
            targets[source] = dict(pointer)
            results[source] = {
                "state": "ready",
                "source": source,
                "reason_code": "checkpoint_candidate",
            }
        if resumed and isinstance(checkpoint.get("catalog"), Mapping):
            targets["catalog"] = dict(checkpoint["catalog"])

        catalog_pointer_path: Path | None = (
            self.root / "catalog" / "current.v2.json" if current["catalog"] else None
        )
        if (
            current["catalog"]
            and catalog_pointer_path is not None
            and not catalog_pointer_path.is_file()
        ):
            # recovered baseline pointer 只存在内存中；worker 需要一个受限于本轮 staging
            # 的 candidate path，不能把它写回正式 current。
            catalog_pointer_path = work / "catalog.pointer.v2.json"
            atomic_write_json(catalog_pointer_path, current["catalog"], ensure_ascii=False, indent=2)
        if resumed:
            catalog_pointer_path = work / "catalog.pointer.v2.json"
            atomic_write_json(catalog_pointer_path, targets["catalog"], ensure_ascii=False, indent=2)
        catalog_changed = False
        if due["catalog"] and "catalog" not in completed_sources:
            attempted_this_run.add("catalog")
            try:
                pointer, result = self._run_source(
                    "catalog", work, None, force=force_source("catalog")
                )
                targets["catalog"] = pointer
                results["catalog"] = result
                catalog_pointer_path = self._candidate_catalog_path(work, pointer)
                completed_sources["catalog"] = dict(pointer)
                failures.pop("catalog", None)
                pending_sources.discard("catalog")
                newly_completed.add("catalog")
                changed = (
                    str(pointer.get("catalog_generation_id") or ""),
                    str(pointer.get("content_sha256") or ""),
                ) != (
                    str(current["catalog"].get("catalog_generation_id") or ""),
                    str(current["catalog"].get("content_sha256") or ""),
                )
                if changed:
                    catalog_changed = True
                    due.update({"aramkit": True, "blitz": True, "apex": True, "mayhem": True})
                    pending_sources.update({"aramkit", "blitz", "apex", "mayhem"})
                self._save_refresh_checkpoint(
                    cycle_id=cycle_id,
                    created_at=checkpoint_created_at,
                    force=effective_force,
                    scope=effective_scope,
                    phase="core",
                    state="in_progress",
                    catalog=targets["catalog"],
                    completed_sources=completed_sources,
                    pending_sources=pending_sources,
                    core_generation_id=checkpoint_core_generation_id,
                    failures=failures,
                )
            except GameRefreshDeferred:
                if targets["catalog"]:
                    self._save_refresh_checkpoint(
                        cycle_id=cycle_id,
                        created_at=checkpoint_created_at,
                        force=effective_force,
                        scope=effective_scope,
                        phase="core",
                        state="in_progress",
                        catalog=targets["catalog"],
                        completed_sources=completed_sources,
                        pending_sources=pending_sources,
                        core_generation_id=checkpoint_core_generation_id,
                        failures=failures,
                    )
                deferred = dict(
                    self._game_refresh.defer_result(
                        force=effective_force,
                        scope=effective_scope,
                    )
                )
                deferred.update(
                    {
                        "refresh_phase": "core",
                        "core_generation_id": checkpoint_core_generation_id,
                        "pending_sources": self._ordered_sources(pending_sources),
                        "resumed_from_checkpoint": resumed,
                    }
                )
                return deferred
            except RefreshStopRequested:
                return shutdown_result("core")
            except SourceWorkerFailure as exc:
                failures["catalog"] = dict(exc.payload)
            except Exception as exc:
                failures["catalog"] = {"error_type": exc.__class__.__name__, "error": str(exc)}

        target_catalog_identity = (
            str(targets["catalog"].get("catalog_generation_id") or ""),
            str(targets["catalog"].get("content_sha256") or ""),
        )
        current_catalog_identity = (
            str(current["catalog"].get("catalog_generation_id") or ""),
            str(current["catalog"].get("content_sha256") or ""),
        )
        catalog_changed = target_catalog_identity != current_catalog_identity
        if catalog_changed:
            for source in (*CORE_SOURCES, *OPTIONAL_SOURCES):
                pointer = targets[source]
                if (
                    str(pointer.get("catalog_generation_id") or "") != target_catalog_identity[0]
                    or str(pointer.get("catalog_sha256") or "") != target_catalog_identity[1]
                ):
                    targets[source] = {}
                    completed_sources.pop(source, None)
                    results.pop(source, None)
                due[source] = True
                pending_sources.add(source)
        if not targets["catalog"]:
            failures.setdefault(
                "catalog", {"error_type": "cohort_incomplete", "error": "catalog_missing"}
            )
        elif catalog_pointer_path is None or not catalog_pointer_path.is_file():
            catalog_pointer_path = work / "catalog.pointer.v2.json"
            atomic_write_json(catalog_pointer_path, targets["catalog"], ensure_ascii=False, indent=2)

        def save_checkpoint(phase: str, *, state: str = "in_progress") -> None:
            if not targets["catalog"]:
                return
            self._save_refresh_checkpoint(
                cycle_id=cycle_id,
                created_at=checkpoint_created_at,
                force=effective_force,
                scope=effective_scope,
                phase=phase,
                state=state,
                catalog=targets["catalog"],
                completed_sources=completed_sources,
                pending_sources=pending_sources,
                core_generation_id=checkpoint_core_generation_id,
                failures=failures,
            )

        def mark_last_good_failure(source: str) -> None:
            if source not in failures or not targets.get(source):
                return
            failures[source] = {
                **dict(failures[source]),
                "fallback_used": True,
                "last_good_available": True,
            }

        def phase_deferred(phase: str) -> dict[str, Any]:
            save_checkpoint(phase)
            deferred = dict(
                self._game_refresh.defer_result(
                    force=effective_force,
                    scope=effective_scope,
                )
            )
            deferred.update(
                {
                    "refresh_phase": phase,
                    "core_generation_id": checkpoint_core_generation_id,
                    "pending_sources": self._ordered_sources(pending_sources),
                    "resumed_from_checkpoint": resumed,
                }
            )
            return deferred

        def run_phase(source_names: tuple[str, ...], phase: str) -> dict[str, Any] | None:
            for source in source_names:
                if self._stop.is_set():
                    failures[source] = {"error_type": "Cancelled", "error": "shutdown_requested"}
                    continue
                if not due[source] or source in completed_sources:
                    continue
                attempted_this_run.add(source)
                try:
                    pointer, result = self._run_source(
                        source,
                        work,
                        catalog_pointer_path,
                        force=(
                            force_source(source)
                            or catalog_changed
                            or upstream_changed
                        ),
                        compatibility_catalog_pointer=compatibility_catalog_pointer,
                        coverage_policy=(
                            "catalog_adoption" if catalog_changed else "active_partial"
                        ),
                    )
                    targets[source] = pointer
                    results[source] = result
                    self._save_candidate(source, pointer)
                    completed_sources[source] = dict(pointer)
                    failures.pop(source, None)
                    pending_sources.discard(source)
                    newly_completed.add(source)
                    save_checkpoint(phase)
                except GameRefreshDeferred:
                    return phase_deferred(phase)
                except RefreshStopRequested:
                    return shutdown_result(phase)
                except SourceWorkerFailure as exc:
                    failures[source] = dict(exc.payload)
                except Exception as exc:
                    failures[source] = {"error_type": exc.__class__.__name__, "error": str(exc)}
            return None

        deferred = run_phase(CORE_SOURCES, "core")
        if deferred is not None:
            return deferred

        for source in tuple(failures):
            if source not in CORE_SOURCES:
                continue
            saved = self._load_saved_candidate(source, targets["catalog"])
            if saved and (
                not current[source]
                or self._pointer_identity(saved) != self._pointer_identity(current[source])
            ):
                targets[source] = saved
                results[source] = {
                    "state": "failed",
                    "source": source,
                    "reason_code": str(failures[source].get("reason_code") or "refresh_failed"),
                    "fallback_used": True,
                    "last_good_available": True,
                    "fallback_pointer_run_id": self._pointer_identity(saved)[0],
                }
                # saved candidate 只是本轮消费的 last-good，不是本轮成功完成。
                # 保留 failure/pending 才能让 promotion、checkpoint 和 schedule
                # 如实反映 backoff，而不是把网络失败洗成 ready。
            mark_last_good_failure(source)
            save_checkpoint("core")

        baseline = self._baseline_contributions(targets["catalog"])
        for source in (*CORE_SOURCES, *OPTIONAL_SOURCES):
            if not targets[source] and source in baseline:
                targets[source] = dict(baseline[source])

        core_missing = [source for source in ("catalog", *CORE_SOURCES) if not targets[source]]
        optional_targets_available = all(targets[source] for source in OPTIONAL_SOURCES)
        core_activity = any(due[source] for source in ("catalog", *CORE_SOURCES))
        core_changed_now = bool(newly_completed.intersection({"catalog", *CORE_SOURCES}))
        if "aramkit" in failures:
            # ARAMKit 是百分比/阶段统计的核心来源；它失败时不得仅因 Blitz 或
            # Optional 成功就制造一代“新鲜” generation。
            self._save_schedule_progress(
                states,
                attempted_sources={source for source in attempted_this_run if source in {"catalog", *CORE_SOURCES}},
                failures={source: payload for source, payload in failures.items() if source in {"catalog", *CORE_SOURCES}},
                targets=targets,
                generation_id=self.publisher.current_generation_id(),
            )
            save_checkpoint("full_catalog_rebind" if catalog_changed else "core")
            return {
                "state": "degraded" if self.publisher.current_generation_id() else "failed",
                "reason_code": "aramkit_refresh_failed",
                "generation_id": self.publisher.current_generation_id(),
                "data_status": "data_stale" if self.publisher.current_generation_id() else "unavailable",
                "data_reason": "aramkit_refresh_failed_last_good_preserved",
                "refresh_phase": "full_catalog_rebind" if catalog_changed else "core",
                "core_generation_id": checkpoint_core_generation_id,
                "pending_sources": self._ordered_sources(pending_sources),
                "resumed_from_checkpoint": resumed,
                "failures": {"aramkit": failures["aramkit"]},
                "source_results": results,
                "upstream_marker": upstream_marker,
            }
        if (
            not catalog_changed
            and not core_missing
            and optional_targets_available
            and core_activity
            and (not checkpoint_core_generation_id or core_changed_now)
        ):
            degraded_core = {source for source in failures if source in {"catalog", *CORE_SOURCES}}
            degraded_core.update(
                source for source in CORE_SOURCES if self._is_baseline(targets[source])
            )
            pending_optional = {
                source for source in OPTIONAL_SOURCES if due[source] and source not in completed_sources
            }
            refreshed_core = [
                source
                for source in SCHEDULE_SOURCES
                if source in results and source not in degraded_core and source not in pending_optional
            ]
            manifest, retention = self._promote_targets(
                targets,
                degraded_sources=degraded_core,
                pending_sources=pending_optional,
                refreshed_sources=refreshed_core,
            )
            promotion_disposition = str(retention.get("promotion_disposition") or "published")
            checkpoint_core_generation_id = manifest.generation_id
            self._save_schedule_progress(
                states,
                attempted_sources={
                    source
                    for source in {"catalog", *CORE_SOURCES}
                    if source in results or source in failures
                },
                failures={
                    source: payload
                    for source, payload in failures.items()
                    if source in {"catalog", *CORE_SOURCES}
                },
                targets=targets,
                generation_id=manifest.generation_id,
            )
            save_checkpoint("optional" if pending_optional or pending_sources else "complete")
            if not pending_optional and not pending_sources:
                save_checkpoint("complete", state="complete")
                return {
                    "state": "degraded" if degraded_core else "ready",
                    "reason_code": (
                        "optional_source_stale"
                        if degraded_core == {"blitz"}
                        else "no_content_change"
                        if promotion_disposition == "unchanged"
                        else "core_cohort_promoted"
                    ),
                    "data_status": "fresh" if "aramkit" not in degraded_core else "data_stale",
                    "data_reason": "optional_source_stale" if degraded_core == {"blitz"} else "",
                    "generation_id": manifest.generation_id,
                    "core_generation_id": manifest.generation_id,
                    "refresh_phase": "complete",
                    "pending_sources": [],
                    "resumed_from_checkpoint": resumed,
                    "refreshed_sources": refreshed_core,
                    "degraded_sources": sorted(degraded_core),
                    "source_results": results,
                    "upstream_marker": upstream_marker,
                    "retention": retention,
                    "promotion_disposition": promotion_disposition,
                }
            if not pending_optional and pending_sources:
                # CORE source fallback (尤其是 Blitz) 仍待重试；不能把带 pending
                # 的 checkpoint 标成 complete，否则下一轮会失去失败证据。
                save_checkpoint("optional", state="in_progress")
                return {
                    "state": "degraded" if degraded_core else "ready",
                    "reason_code": (
                        "optional_source_stale"
                        if degraded_core == {"blitz"}
                        else "no_content_change"
                        if promotion_disposition == "unchanged"
                        else "core_cohort_promoted"
                    ),
                    "data_status": "fresh" if "aramkit" not in degraded_core else "data_stale",
                    "data_reason": "optional_source_stale" if degraded_core == {"blitz"} else "",
                    "generation_id": manifest.generation_id,
                    "core_generation_id": manifest.generation_id,
                    "refresh_phase": "optional",
                    "pending_sources": self._ordered_sources(pending_sources),
                    "resumed_from_checkpoint": resumed,
                    "refreshed_sources": refreshed_core,
                    "degraded_sources": sorted(degraded_core),
                    "source_results": results,
                    "upstream_marker": upstream_marker,
                    "retention": retention,
                    "promotion_disposition": promotion_disposition,
                }

        deferred = run_phase(
            OPTIONAL_SOURCES,
            "full_catalog_rebind" if catalog_changed else "optional",
        )
        if deferred is not None:
            return deferred

        for source in tuple(failures):
            if source == "catalog":
                continue
            saved = self._load_saved_candidate(source, targets["catalog"])
            if saved and (
                not current[source]
                or self._pointer_identity(saved) != self._pointer_identity(current[source])
            ):
                targets[source] = saved
                results[source] = {
                    "state": "failed",
                    "source": source,
                    "reason_code": str(failures[source].get("reason_code") or "refresh_failed"),
                    "fallback_used": True,
                    "last_good_available": True,
                    "fallback_pointer_run_id": self._pointer_identity(saved)[0],
                }
                # fallback 不计入 completed_sources，也不能从 pending 移除。
            mark_last_good_failure(source)
            save_checkpoint("full_catalog_rebind" if catalog_changed else "optional")

        optional_failures = {
            source: failures[source] for source in OPTIONAL_SOURCES if source in failures
        }
        if optional_failures and not catalog_changed:
            # Apex/Mayhem 是同一联动 cohort；一侧失败时保留已经发布的 Core，
            # 成功 candidate 留在 checkpoint，不能发布半套 Optional。
            self._save_schedule_progress(
                states,
                attempted_sources=set(optional_failures).intersection(attempted_this_run),
                failures=optional_failures,
                targets=targets,
                generation_id=(
                    checkpoint_core_generation_id or self.publisher.current_generation_id()
                ),
            )
            save_checkpoint("optional")
            return {
                "state": "degraded" if self.publisher.current_generation_id() else "failed",
                "reason_code": "optional_refresh_deferred",
                "generation_id": (
                    checkpoint_core_generation_id or self.publisher.current_generation_id()
                ),
                "core_generation_id": checkpoint_core_generation_id,
                "refresh_phase": "optional",
                "pending_sources": self._ordered_sources(pending_sources),
                "resumed_from_checkpoint": resumed,
                "failures": optional_failures,
                "source_results": results,
                "upstream_marker": upstream_marker,
            }

        baseline = self._baseline_contributions(targets["catalog"])
        for source in ("aramkit", "blitz", "apex", "mayhem"):
            if not targets[source] and source in baseline:
                targets[source] = dict(baseline[source])
        missing = [source for source, pointer in targets.items() if not pointer]
        cannot_fallback = bool(missing or (catalog_changed and failures))
        if cannot_fallback:
            failure_payload = {"failures": failures, "missing": missing}
            effective_failures = dict(failures)
            for source in attempted_this_run:
                if source not in results and source not in effective_failures:
                    effective_failures[source] = {"error_type": "cohort_incomplete"}
            self._save_schedule_progress(
                states,
                attempted_sources=attempted_this_run,
                failures=effective_failures,
                targets=targets,
                generation_id=self.publisher.current_generation_id(),
            )
            save_checkpoint("full_catalog_rebind" if catalog_changed else "core")
            return {
                "state": "degraded" if self.publisher.current_generation_id() else "failed",
                "reason_code": (
                    "data_stale" if self.publisher.current_generation_id() else "cohort_refresh_failed"
                ),
                "generation_id": self.publisher.current_generation_id(),
                "data_status": (
                    "data_stale" if self.publisher.current_generation_id() else "unavailable"
                ),
                "data_reason": (
                    "candidate_rejected_last_good_preserved"
                    if self.publisher.current_generation_id()
                    else "no_snapshot"
                ),
                "refresh_phase": "full_catalog_rebind" if catalog_changed else "core",
                "core_generation_id": checkpoint_core_generation_id,
                "pending_sources": self._ordered_sources(pending_sources),
                "resumed_from_checkpoint": resumed,
                "upstream_marker": upstream_marker,
                **failure_payload,
            }

        degraded_sources = set(failures)
        degraded_sources.update(
            source
            for source in ("aramkit", "blitz", "apex", "mayhem")
            if self._is_baseline(targets[source])
        )
        # Apex 与 Mayhem 在消费者 payload 中共同构成联动数据；任一降级时两者都复用同一 last-good。
        if degraded_sources.intersection({"apex", "mayhem"}):
            degraded_sources.update({"apex", "mayhem"})
            for source in ("apex", "mayhem"):
                fallback = current[source] or baseline.get(source, {})
                if fallback:
                    targets[source] = dict(fallback)

        refreshed_sources = [
            source
            for source in SCHEDULE_SOURCES
            if source in results and source not in degraded_sources
        ]
        manifest, retention = self._promote_targets(
            targets,
            degraded_sources=degraded_sources,
            pending_sources=set(),
            refreshed_sources=refreshed_sources,
        )
        promotion_disposition = str(retention.get("promotion_disposition") or "published")
        self._save_schedule_progress(
            states,
            # checkpoint 中已完成但尚未 promotion 的来源也属于本次成功结果。
            # 一并更新 schedule，才能在崩溃恢复后清除旧 failure/backoff，并让
            # current_run_id/last_success_at 与刚提交的正式 pointer 同步。
            attempted_sources=set(attempted_this_run).union(completed_sources),
            failures=failures,
            targets=targets,
            generation_id=manifest.generation_id,
        )
        checkpoint_core_generation_id = checkpoint_core_generation_id or manifest.generation_id
        if pending_sources:
            save_checkpoint("optional", state="in_progress")
        else:
            save_checkpoint("complete", state="complete")
        return {
            "state": "degraded" if degraded_sources else "ready",
            "reason_code": (
                "optional_source_stale"
                if degraded_sources and "aramkit" not in degraded_sources
                else "no_content_change"
                if promotion_disposition == "unchanged"
                else "cohort_promoted"
            ),
            "data_status": "fresh" if "aramkit" not in degraded_sources else "data_stale",
            "data_reason": (
                "optional_source_stale"
                if degraded_sources and "aramkit" not in degraded_sources
                else "candidate_rejected_last_good_preserved"
                if "aramkit" in degraded_sources
                else ""
            ),
            "generation_id": manifest.generation_id,
            "core_generation_id": checkpoint_core_generation_id,
            "refresh_phase": "optional" if pending_sources else "complete",
            "pending_sources": self._ordered_sources(pending_sources),
            "resumed_from_checkpoint": resumed,
            "refreshed_sources": refreshed_sources,
            "degraded_sources": sorted(degraded_sources),
            "source_results": results,
            "upstream_marker": upstream_marker,
            "retention": retention,
            "promotion_disposition": promotion_disposition,
        }


__all__ = ["RefreshCycleMixin"]

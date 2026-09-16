"""DataService 的增量来源执行器。

Core 与 Optional 分别执行，只有发布锁串行化短事务。下载没有对局取消开关；
worker 按短寿命上下文调整领取优先级。每个 immutable unit 通过投影器再发布。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.contracts import CatalogManifestV2, SourcePointerV2, SourceRunManifestV2
from hextech.contracts.refresh import RefreshProgress
from hextech.modules.acquisition.load_guard import BackgroundLoadGuard
from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.infrastructure.persistence.cohort_recovery import build_recovery_point, validate_generation_cohort
from hextech.infrastructure.persistence.refresh_schedule import RefreshScheduleStore
from hextech.infrastructure.processes import run_isolated_process
from hextech.modules.data.catalog.versioned import CatalogView, sha256_file, validate_catalog_files
from hextech.modules.data.freshness import parse_refresh_time
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from .refresh_service_schedule import REFRESH_SOURCES, RefreshScheduleMixin
from .refresh_policy import exception_source_result, source_failure_kind
from .aramkit.schema import version_marker


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return dict(value) if isinstance(value, Mapping) else {}
    except (OSError, ValueError):
        return {}


class CatalogRefreshDeferred(RuntimeError):
    """Markers were checked; expensive recognition acquisition waits for safe load."""


class IncrementalRefreshService(RefreshScheduleMixin):
    def __init__(self, *, publisher: DataSnapshotPublisher, root: Path,
                 game_state_probe: Callable[[], bool], champion_probe: Callable[[], str],
                 process_runner=run_isolated_process, projection_factory=None,
                 marker_probe: Callable[[], Mapping[str, Any]] | None = None):
        self.publisher, self.root = publisher, Path(root)
        self.game_state_probe, self.champion_probe = game_state_probe, champion_probe
        self.process_runner = process_runner
        self.marker_probe = marker_probe
        if projection_factory is None:
            from .aramkit.incremental_projection import IncrementalProjection
            projection_factory = IncrementalProjection
        self.projection_factory = projection_factory
        self.schedule_store = RefreshScheduleStore(self.root)
        self.promotion = CohortPromotionStore(self.root)
        self.promotion.recover()
        self._lock = threading.RLock()
        self._refresh_condition = threading.Condition(self._lock)
        self._refresh_active = False
        self._last_refresh_result: dict[str, Any] = {}
        self._last_refresh_error: Exception | None = None
        self._stop = threading.Event()
        self._cancels: set[Path] = set()
        self._optional_thread: threading.Thread | None = None
        self._progress = RefreshProgress()
        self._optional_progress = RefreshProgress()
        self._optional_results: dict[str, dict[str, Any]] = {}
        self._source_results: dict[str, dict[str, Any]] = {}
        self._last_upstream_marker: dict[str, Any] = {}
        self._optional_force_requested = False
        self._catalog: dict[str, Any] = {}
        self._recognition: dict[str, Any] = {}
        self._catalog_retry_at = 0.0
        self._ranking: dict[str, Any] = {}
        self._heroes: dict[str, dict[str, Any]] = {}
        self._optional: dict[str, dict[str, Any]] = {}
        self._projection = None
        self._catalog_capability_pending = 0
        self._last_candidate = None
        self._context: dict[str, Any] = {"champion_id": "", "in_game": True, "pause_background": True}
        self._load_guard = BackgroundLoadGuard()
        self._restore()
        active_catalog = _object(self.root / "catalog" / "current.v2.json")
        try:
            self._validate_catalog(active_catalog)
            self._recognition = active_catalog
        except (OSError, ValueError, RuntimeError):
            self._recognition = dict(self._catalog)
        self.poll_context()

    def _restore(self) -> None:
        try:
            view = DataSnapshotClient(self.publisher.root).open_view()
            candidate = validate_generation_cohort(self.root, view.manifest.generation_id)
            self._last_candidate = candidate
            self._catalog = dict(candidate.pointers["catalog"])
            units = getattr(candidate, "units", {})
            components = getattr(view.manifest, "components", {})
            for item in view.manifest.source_files:
                if item.source == "catalog":
                    continue
                manifest_path = self.root / "sources" / item.source / "runs" / item.run_id / "manifest.json"
                manifest = SourceRunManifestV2.from_mapping(_object(manifest_path))
                if manifest.artifact is None or sha256_file(manifest_path) != item.manifest_sha256:
                    raise ValueError("source manifest changed during restore")
                pointer = SourcePointerV2(item.source, item.run_id, manifest.catalog_generation_id,
                    manifest.catalog_sha256, item.manifest_sha256, manifest.artifact,
                    manifest.completed_at, manifest.completed_at).to_dict()
                if item.source == "aramkit" and item.artifact_role == "hero_rankings":
                    self._ranking = pointer
                elif item.source == "aramkit" and item.artifact_role == "scoped_stats":
                    for hero in view.get_champions():
                        hero_id = str(hero["id"])
                        descriptor = components.get("champions", {}).get(hero_id, {})
                        if not units or not descriptor or descriptor.get("run_id") == item.run_id:
                            self._heroes[hero_id] = pointer
                elif item.source in {"blitz", "apex", "mayhem"}:
                    self._optional[item.source] = pointer
        except (OSError, ValueError, RuntimeError):
            # Existing current remains untouched; a refresh must validate a new candidate.
            self._catalog = _object(self.root / "catalog" / "current.v2.json")
            self._ranking, self._heroes, self._optional = {}, {}, {}
            self._last_candidate = None

    def poll_context(self) -> bool:
        try:
            in_game = bool(self.game_state_probe())
            hero = str(self.champion_probe() or "")
            if hero and not hero.isdecimal():
                hero = ""
            pause = self._load_guard.observe(_object(self.root / "state" / "game_overlay_slots.v1.json")) if in_game else False
            context = {"in_game": in_game, "champion_id": hero, "pause_background": pause}
        except Exception:
            context = {"in_game": True, "champion_id": "", "pause_background": True}
        previous = self._context
        self._context = context
        atomic_write_json(self.root / "state" / "data-service" / "download_context.v1.json",
                          {**context, "observed_at": time.time()}, indent=2)
        return bool((context["champion_id"] and context["champion_id"] != previous.get("champion_id"))
                    or (previous.get("in_game") and not context["in_game"]))

    def _upstream_changed(self) -> bool | None:
        if self.marker_probe is None:
            return None
        marker = version_marker(self.marker_probe())
        if not marker["dataPath"] or not marker["version"]:
            raise ValueError("aramkit_marker_unavailable")
        self._last_upstream_marker = marker
        manifest = _object(self.root / "sources" / "aramkit" / "runs" /
                           str(self._ranking.get("run_id") or "") / "manifest.json")
        old = manifest.get("metadata", {}).get("version", {})
        try:
            old_marker = version_marker(old) if isinstance(old, Mapping) else {}
        except (TypeError, ValueError):
            old_marker = {}
        return marker != old_marker

    def _validate_catalog(self, pointer: Mapping[str, Any]) -> None:
        root = self.root / "catalog" / "generations" / str(pointer.get("catalog_generation_id") or "")
        manifest = CatalogManifestV2.from_mapping(_object(root / "manifest.json"))
        if (sha256_file(root / "manifest.json") != pointer.get("manifest_sha256")
                or manifest.catalog_generation_id != pointer.get("catalog_generation_id")):
            raise ValueError("catalog manifest mismatch")
        validate_catalog_files(root, manifest)

    def _publish_catalog(self, pointer: Mapping[str, Any]) -> None:
        """Publish recognition independently; statistics retain their original binding."""
        self._validate_catalog(pointer)
        atomic_write_json(self.root / "catalog" / "current.v2.json", dict(pointer), indent=2)
        self._recognition = dict(pointer)

    def progress(self) -> dict[str, Any]:
        # Immutable records can be copied without waiting for the publish IO lock.
        return {**self._progress.to_dict(), "optional": self._optional_progress.to_dict(),
                "optional_sources": dict(self._optional_results)}

    def _open_catalog(self):
        root = self.root / "catalog" / "generations" / str(self._catalog.get("catalog_generation_id") or "")
        manifest = CatalogManifestV2.from_mapping(_object(root / "manifest.json"))
        if sha256_file(root / "manifest.json") != self._catalog.get("manifest_sha256"):
            raise ValueError("catalog manifest mismatch")
        validate_catalog_files(root, manifest)
        return CatalogView(root=root, manifest=manifest, manifest_sha256=self._catalog["manifest_sha256"])

    def _core_units_complete(self) -> bool:
        from .aramkit.revisions import PARSER_REVISION, PROJECTION_REVISION

        manifest = _object(self.root / "sources/aramkit/runs" / str(self._ranking.get("run_id") or "") / "manifest.json")
        ranking_metadata = manifest.get("metadata", {})
        version = ranking_metadata.get("version", {}).get("dataPath")
        expected = {str(item.get("item_id") or "") for item in manifest.get("outcomes", [])}
        if (
            not version
            or not expected
            or not expected <= self._heroes.keys()
            or ranking_metadata.get("parser_revision") != PARSER_REVISION
            or ranking_metadata.get("projection_revision") != PROJECTION_REVISION
            or not self._pointer_artifact_valid(self._ranking, expected_role="hero_rankings")
        ):
            return False
        for hero in expected:
            unit = self._heroes[hero]
            path = self.root / "sources/aramkit/runs" / str(unit.get("run_id") or "") / "manifest.json"
            metadata = _object(path).get("metadata", {})
            if (
                not self._pointer_artifact_valid(unit, expected_role="scoped_stats")
                or metadata.get("version", {}).get("dataPath") != version
                or metadata.get("parser_revision") != PARSER_REVISION
                or metadata.get("projection_revision") != PROJECTION_REVISION
            ):
                return False
        return True

    def _pointer_artifact_valid(
        self,
        pointer: Mapping[str, Any],
        *,
        expected_role: str,
    ) -> bool:
        try:
            parsed = SourcePointerV2.from_mapping(pointer)
            if parsed.source != "aramkit" or parsed.artifact.role != expected_role:
                return False
            run = self.root / "sources" / "aramkit" / "runs" / parsed.run_id
            manifest_path = run / "manifest.json"
            artifact_path = run / parsed.artifact.relative_path
            manifest = SourceRunManifestV2.from_mapping(_object(manifest_path))
            if (
                sha256_file(manifest_path) != parsed.manifest_sha256
                or manifest.artifact != parsed.artifact
                or sha256_file(artifact_path) != parsed.artifact.sha256
                or artifact_path.stat().st_size != parsed.artifact.size
            ):
                return False
            if expected_role == "hero_rankings":
                payload = _object(artifact_path)
                return bool(payload.get("version", {}).get("dataPath") and payload.get("rows"))
            from .aramkit.service import validate_scoped_stats_artifact

            validate_scoped_stats_artifact(pointer)
            return True
        except (OSError, ValueError, RuntimeError, TypeError):
            return False

    def _ranking_data_at(self) -> str:
        """Source age is upstream age, never the local unit publication time."""
        path = self.root / "sources/aramkit/runs" / str(self._ranking.get("run_id") or "") / "manifest.json"
        try:
            if sha256_file(path) != self._ranking.get("manifest_sha256"):
                return ""
            manifest = SourceRunManifestV2.from_mapping(_object(path))
            version = manifest.metadata.get("version", {})
            stamp = version.get("buildTimeUnixMs") if isinstance(version, Mapping) else None
            if stamp is not None and not isinstance(stamp, bool) and float(stamp) > 0:
                return datetime.fromtimestamp(float(stamp) / 1000, timezone.utc).isoformat()
            value = manifest.metadata.get("data_at")
            parsed = parse_refresh_time(str(value or ""))
            return parsed.astimezone(timezone.utc).isoformat() if parsed else ""
        except (OSError, ValueError, TypeError, OverflowError):
            return ""

    def _source_pointer(self, source: str) -> Mapping[str, Any]:
        if source == "catalog":
            return self._recognition
        if source == "aramkit":
            return self._ranking
        return self._optional.get(source, {})

    def _source_identity(self, source: str) -> str:
        pointer = self._source_pointer(source)
        if source == "catalog":
            try:
                root = self.root / "catalog" / "generations" / str(pointer.get("catalog_generation_id") or "")
                manifest = CatalogManifestV2.from_mapping(_object(root / "manifest.json"))
                # Catalog generation is a live Vision binding, while content_sha256
                # proves the assets behind that binding; both are display-relevant.
                return f"{manifest.catalog_generation_id}:{manifest.content_sha256}"
            except (OSError, ValueError, RuntimeError):
                return ""
        artifact = pointer.get("artifact")
        base = str(artifact.get("sha256") or "") if isinstance(artifact, Mapping) else ""
        if source != "aramkit":
            return base
        heroes = ",".join(
            f"{hero}:{value.get('artifact', {}).get('sha256', '')}"
            for hero, value in sorted(self._heroes.items())
            if isinstance(value.get("artifact"), Mapping)
        )
        return f"{base}|{heroes}" if base else ""

    def _source_identities(self) -> dict[str, str]:
        return {source: self._source_identity(source) for source in REFRESH_SOURCES}

    def _business_content_identity(self) -> tuple[tuple[str, str], ...]:
        """Hash the published payload roles, excluding generation/run bookkeeping."""

        try:
            manifest = DataSnapshotClient(self.publisher.root).open_view().manifest
        except (OSError, ValueError, RuntimeError):
            return ()
        return tuple(sorted((item.role, item.sha256) for item in manifest.files))

    def _publish(self) -> str:
        if not self._ranking or self._stop.is_set():
            return ""
        with self._lock:
            if self._projection is None:
                self._projection = self.projection_factory(self._open_catalog())
            build = self._projection.build(self._ranking, self._heroes, self._optional)
            if self._stop.is_set():
                raise RuntimeError("shutdown_requested")
            accepted_sources = {item.source for item in build.source_files}
            if set(self._optional) - accepted_sources:
                raise ValueError("optional_candidate_rejected")
            degraded = [source for source in ("blitz", "apex", "mayhem") if source not in accepted_sources]
            unchanged = self.publisher.matching_current_manifest(build.payloads, source_files=build.source_files,
                components=build.components, source_status=build.source_status,
                health="degraded" if degraded else "healthy", degraded_sources=degraded)
            if unchanged is not None:
                self.publisher.last_promotion_disposition = "unchanged"
                self._progress = self._progress.advance(generation_id=unchanged.generation_id)
                return unchanged.generation_id
            journal = self.promotion.begin()
            try:
                self.promotion.record_target("catalog", self._recognition or self._catalog)
                self.promotion.record_target("aramkit", self._ranking)
                for source in ("blitz", "apex", "mayhem"):
                    self.promotion.record_target(source, self._optional.get(source, {}))
                self.promotion.promote_dependencies()
                if self._stop.is_set():
                    raise RuntimeError("shutdown_requested")
                manifest = self.publisher.publish(build.payloads, source_files=build.source_files,
                    components=build.components, source_status=build.source_status,
                    health="degraded" if degraded else "healthy", degraded_sources=degraded,
                    require_complete_provenance=True)
                candidate = validate_generation_cohort(self.root, manifest.generation_id)
                old = journal.old_pointers.get("generation", {}).get("current", {}).get("current_generation_id", "")
                schedule = replace(self.schedule_store.load(), generation_id=manifest.generation_id)
                recovery = build_recovery_point(candidate, previous_generation_id=str(old), schedule=schedule)
                self.promotion.stage_generation_state(schedule=schedule.to_dict(), recovery_point=recovery.to_dict())
                if self._stop.is_set():
                    raise RuntimeError("shutdown_requested")
                self.promotion.record_generation_promoted(manifest.generation_id)
                self.promotion.commit()
                self._last_candidate = candidate
            except Exception:
                self.promotion.rollback()
                raise
            self._progress = self._progress.advance(core_published_at=time.time(), generation_id=manifest.generation_id)
            return manifest.generation_id

    def _run(self, source: str, work: Path, *, force: bool = False) -> dict[str, Any]:
        work.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._source_results.pop(source, None)
        pointer, result, cancel = (work / name for name in ("pointer.json", "result.json", "cancel"))
        command = ([sys.executable, "--acquisition-worker"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "hextech.bootstrap.acquisition_worker"])
        command += ["--source", source, "--pointer-output", str(pointer), "--result-output", str(result),
                    "--cancel-file", str(cancel), "--incremental"]
        if force:
            command.append("--force")
        if source != "catalog":
            catalog_path = work / "catalog.json"
            atomic_write_json(catalog_path, self._catalog, indent=2)
            command += ["--catalog-pointer", str(catalog_path)]
        if source == "blitz":
            command += ["--coverage-policy", "active_partial"]
        observed: set[tuple[str, str]] = set()
        observed_heroes: set[str] = set()
        observer_errors: dict[tuple[str, str], str] = {}
        last_observe = 0.0
        pending_publication = False
        pending_priority = ""

        def observe(*, flush: bool = False) -> None:
            nonlocal last_observe, pending_publication, pending_priority
            if time.monotonic() - last_observe < 0.2:
                return
            last_observe = time.monotonic()
            if source != "aramkit":
                report = _object(work / "source-progress.json")
                if report.get("source") == source:
                    try:
                        progress = self._optional_progress if source in {"apex", "mayhem"} else self._progress
                        progress = progress.advance(
                            completed_items=int(report["completed_items"]), total_items=int(report["total_items"]),
                            phase=str(report["phase"]))
                        if source in {"apex", "mayhem"}:
                            self._optional_progress = progress
                        else:
                            self._progress = progress
                    except (ValueError, KeyError, TypeError):
                        pass
                return
            units = work / "units"
            if not units.is_dir():
                return
            priority_name = "hero-" + str(self._context.get("champion_id") or "") + ".pointer.json"
            for path in sorted(units.glob("*.pointer.json"),
                               key=lambda p: (p.name != "ranking.pointer.json", p.name != priority_name, p.name)):
                unit = _object(path)
                key = (path.name, str(unit.get("manifest_sha256") or ""))
                if key in observed or not key[1]:
                    continue
                try:
                    parsed = SourcePointerV2.from_mapping(unit)
                    if parsed.catalog_generation_id != self._catalog.get("catalog_generation_id"):
                        raise ValueError("unit catalog mismatch")
                    with self._lock:
                        old_ranking, old_heroes = self._ranking, dict(self._heroes)
                        if path.name == "ranking.pointer.json":
                            self._ranking = unit
                            self._progress = self._progress.advance(total_items=parsed.artifact.record_count,
                                                                   completed_items=0)
                        else:
                            hero = path.name.removeprefix("hero-").removesuffix(".pointer.json")
                            if not hero.isdecimal():
                                raise ValueError("invalid hero unit")
                            if self._projection is None:
                                self._projection = self.projection_factory(self._open_catalog())
                            self._projection.validate_champion(hero, unit)
                            self._heroes[hero] = unit
                        priority = str(self._context.get("champion_id") or "")
                        publish_now = path.name == "ranking.pointer.json" or (
                            path.name == f"hero-{priority}.pointer.json" and pending_priority != priority)
                        try:
                            if publish_now:
                                self._publish()
                                pending_publication = False
                                pending_priority = priority if path.name != "ranking.pointer.json" else ""
                            else:
                                pending_publication = True
                        except Exception:
                            self._ranking, self._heroes = old_ranking, old_heroes
                            raise
                        if path.name != "ranking.pointer.json":
                            observed_heroes.add(hero)
                        priority_ready = str(self._context.get("champion_id") or "") in observed_heroes
                        self._progress = self._progress.advance(phase="details",
                            state="core_ready" if priority_ready else "running",
                            completed_items=len(observed_heroes),
                            total_items=max(len(observed_heroes), self._progress.total_items))
                    observed.add(key)
                    for old_key in tuple(observer_errors):
                        if old_key[0] == key[0]:
                            observer_errors.pop(old_key, None)
                except (OSError, ValueError, RuntimeError) as exc:
                    observer_errors[key] = type(exc).__name__
            priority = str(self._context.get("champion_id") or "")
            if pending_publication and (flush or (priority in observed_heroes and pending_priority != priority)):
                try:
                    self._publish()
                except (OSError, ValueError, RuntimeError):
                    self._restore()
                    self._projection = None
                    raise
                pending_publication = False
                pending_priority = priority
        with self._lock:
            self._cancels.add(cancel)
        try:
            if self._stop.is_set():
                raise RuntimeError("shutdown_requested")
            env = {**os.environ, "HEXTECH_VAR_DIR": str(self.root.resolve())}
            execution = self.process_runner(command, timeout_seconds=3600, cancel_file=cancel,
                                            env=env, observe=observe)
            last_observe = 0.0
            observe(flush=True)
            outcome = _object(result)
            source_result = outcome.get("source_result")
            with self._lock:
                self._source_results[source] = (
                    dict(source_result) if isinstance(source_result, Mapping) else {}
                )
            if self._stop.is_set():
                raise RuntimeError("shutdown_requested")
            if source == "catalog" and execution.returncode == 0 and outcome.get("state") == "deferred":
                raise CatalogRefreshDeferred(str(outcome.get("reason_code") or "catalog_build_deferred_in_game"))
            if execution.returncode != 0 or outcome.get("state") != "ready":
                raise RuntimeError(str(outcome.get("reason_code") or outcome.get("error_type") or "source_failed"))
            if observer_errors:
                raise RuntimeError("incremental_publish_failed:" + next(iter(observer_errors.values())))
            if source == "catalog":
                self._catalog_capability_pending = int(outcome.get("source_result", {}).get("capability_retry_pending_count") or 0)
            return dict(outcome["pointer"])
        finally:
            with self._lock:
                self._cancels.discard(cancel)

    def refresh(self, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        with self._refresh_condition:
            if self._refresh_active:
                if force:
                    self._optional_force_requested = True
                while self._refresh_active and not self._stop.is_set():
                    self._refresh_condition.wait(timeout=0.2)
                if self._refresh_active:
                    return {"state": "failed", "reason_code": "shutdown_requested"}
                if self._last_refresh_error is not None:
                    raise RuntimeError(str(self._last_refresh_error)) from self._last_refresh_error
                if self._last_refresh_result:
                    return dict(self._last_refresh_result)
            self._refresh_active = True
            self._last_refresh_result = {}
            self._last_refresh_error = None
        try:
            result = self._refresh_once(force=force, scope=scope)
        except Exception as exc:
            with self._refresh_condition:
                self._last_refresh_error = exc
            raise
        finally:
            with self._refresh_condition:
                self._refresh_active = False
                if "result" in locals():
                    self._last_refresh_result = dict(result)
                self._refresh_condition.notify_all()
        return result

    def _refresh_once(self, *, force: bool = False, scope: str = "due") -> dict[str, Any]:
        if self._stop.is_set():
            return {"state": "failed", "reason_code": "shutdown_requested"}
        request_id = uuid.uuid4().hex
        with self._lock:
            self._progress = RefreshProgress(request_id=request_id, state="running", phase="catalog",
                                              started_at=time.time(), updated_at=time.time())
        work = self.root / "snapshots" / "staging" / ("incremental-" + request_id)
        errors: dict[str, str] = {}
        attempted: set[str] = set()
        catalog_deferred = False
        initial_identities = self._source_identities()
        initial_business_identity = self._business_content_identity()
        check_core = force or scope == "core"
        if (force or not self._backoff_pending("catalog")) and (not self._catalog or self._due("catalog", check_core)):
            attempted.add("catalog")
            try:
                # Always check recognition's own markers, independent of ARAMKit.
                catalog = self._run("catalog", work / "catalog")
                with self._lock:
                    self._publish_catalog(catalog)
                    adopt = not self._context["in_game"] or not self._catalog
                    if adopt and catalog.get("catalog_generation_id") != self._catalog.get("catalog_generation_id"):
                        self._ranking, self._heroes, self._optional = {}, {}, {}
                        self._projection = None
                    if adopt:
                        self._catalog = catalog
                self._mark_source("catalog")
                self._catalog_retry_at = 0.0
            except CatalogRefreshDeferred:
                catalog_deferred = True
                self._catalog_retry_at = time.monotonic() + 30.0
            except (OSError, ValueError, RuntimeError) as exc:
                errors["catalog"] = str(exc)
                self._source_results.setdefault("catalog", exception_source_result(exc))
                self._mark_source(
                    "catalog",
                    error=source_failure_kind(self._source_results.get("catalog", {}), exc),
                )
        if not self._catalog:
            checked_at = time.time()
            source_outcomes = self._source_outcomes(
                attempted=attempted,
                failures=errors,
                deferred={"catalog"} if catalog_deferred else set(),
                initial_identities=initial_identities,
            )
            self._progress = self._progress.advance(state="failed", reason_code="catalog_unavailable",
                checked=False, content_changed=False, catalog_changed=False,
                source_outcomes=source_outcomes, checked_at=checked_at, completed_at=checked_at)
            return {"state": "failed", "reason_code": "catalog_unavailable", "checked": False,
                    "content_changed": False, "catalog_changed": False,
                    "source_outcomes": source_outcomes, "checked_at": checked_at, "failures": errors}
        if force:
            with self._lock:
                self._optional_force_requested = True
        self._start_optional()
        for source in ("aramkit", "blitz"):
            if self._stop.is_set():
                break
            if not force and self._backoff_pending(source):
                continue
            missing_priority = source == "aramkit" and self._context["champion_id"] and self._context["champion_id"] not in self._heroes
            core_complete = bool(self._ranking) and self._core_units_complete() if source == "aramkit" else True
            if not self._due(source, check_core) and core_complete and not missing_priority:
                continue
            attempted.add(source)
            with self._lock:
                self._source_results.pop(source, None)
            with self._lock:
                self._progress = self._progress.advance(source=source, phase="rankings" if source == "aramkit" else "ranking_fallback")
            try:
                upstream_changed = self._upstream_changed() if source == "aramkit" else None
                if source == "aramkit" and upstream_changed is False and self._ranking:
                    marker = dict(self._last_upstream_marker)
                    evidence = {
                        "check_status": "up_to_date",
                        "upstream_revision": str(marker.get("dataPath") or ""),
                        "applied_revision": str(marker.get("dataPath") or ""),
                        "upstream_marker": marker,
                    }
                    self._source_results[source] = evidence
                    if core_complete and not missing_priority:
                        self._mark_source(source, result=evidence)
                        continue
                pointer = self._run(source, work / source, force=source == "aramkit")
                if source == "aramkit" and upstream_changed is False:
                    marker = dict(self._last_upstream_marker)
                    worker_result = self._source_results.get(source, {})
                    worker_revision = str(worker_result.get("upstream_revision") or "")
                    probe_revision = str(marker.get("dataPath") or "")
                    if not worker_revision or worker_revision == probe_revision:
                        self._source_results[source] = {
                            **worker_result,
                            "check_status": "up_to_date",
                            "upstream_revision": probe_revision,
                            "applied_revision": probe_revision,
                            "upstream_marker": marker,
                        }
                if source != "aramkit":
                    with self._lock:
                        previous = self._optional.get(source)
                        self._optional[source] = pointer
                        try:
                            self._publish()
                        except Exception:
                            if previous is None:
                                self._optional.pop(source, None)
                            else:
                                self._optional[source] = previous
                            raise
                self._mark_source(source)
            except (OSError, ValueError, RuntimeError) as exc:
                errors[source] = str(exc)
                self._source_results.setdefault(source, exception_source_result(exc))
                self._mark_source(
                    source,
                    error=source_failure_kind(self._source_results.get(source, {}), exc),
                )
        if self._stop.is_set():
            errors["runtime"] = "shutdown_requested"
        with self._lock:
            primary_failed = bool(set(errors) & {"catalog", "aramkit", "runtime"})
            current_generation = self.publisher.current_generation_id()
            current_business_identity = self._business_content_identity()
            content_changed = bool(
                current_business_identity and initial_business_identity != current_business_identity
            )
            catalog_changed = bool(
                self._source_identity("catalog")
                and initial_identities.get("catalog") != self._source_identity("catalog")
            )
            unchanged = not content_changed and not catalog_changed
            checked_at = time.time()
            data_at = self._ranking_data_at()
            source_outcomes = self._source_outcomes(
                attempted=attempted,
                failures=errors,
                deferred={"catalog"} if catalog_deferred else set(),
                initial_identities=initial_identities,
            )
            healthy_attempts = {
                source
                for source in attempted
                if source_outcomes.get(source, {}).get("checked") is True
            }
            checked = bool(healthy_attempts) and not primary_failed
            reason_code = (
                "partial_refresh_failed" if primary_failed else
                "core_complete_optional_failed" if errors else
                "no_content_change" if unchanged else
                "catalog_complete" if catalog_changed and not content_changed else
                "core_complete"
            )
            self._progress = self._progress.advance(state="failed" if primary_failed else "unchanged" if unchanged else "completed", phase="complete",
                reason_code=reason_code, checked=checked, content_changed=content_changed,
                catalog_changed=catalog_changed, source_outcomes=source_outcomes,
                checked_at=checked_at, data_at=data_at, completed_at=checked_at,
                catalog_state="deferred" if catalog_deferred else "failed" if "catalog" in errors else "checked")
        return {"state": "degraded" if errors else "ready", "reason_code": reason_code,
                "promotion_disposition": "unchanged" if unchanged else "published",
                "checked": checked, "content_changed": content_changed, "catalog_changed": catalog_changed,
                "source_outcomes": source_outcomes, "checked_at": checked_at, "data_at": data_at,
                "catalog_state": "deferred" if catalog_deferred else "failed" if "catalog" in errors else "checked",
                "generation_id": current_generation, "refresh_scope": scope, "failures": errors}

    def _start_optional(self, *, force: bool = False) -> None:
        with self._lock:
            if force:
                self._optional_force_requested = True
            if self._stop.is_set() or (
                self._optional_thread is not None and self._optional_thread.is_alive()
            ):
                return
            self._optional_thread = threading.Thread(
                target=self._refresh_optional,
                daemon=True,
                name="optional-sources",
            )
            self._optional_thread.start()

    def _refresh_optional(self) -> None:
        catalog_id = str(self._catalog.get("catalog_generation_id") or "")
        force_check = False
        for source in ("apex", "mayhem"):
            with self._lock:
                force_check = force_check or self._optional_force_requested
                self._optional_force_requested = False
            if self._stop.is_set() or not self._due(source, force_check):
                continue
            work = self.root / "snapshots" / "staging" / ("optional-" + uuid.uuid4().hex) / source
            self._optional_progress = RefreshProgress(state="running", source=source, phase="download", started_at=time.time())
            initial_identities = self._source_identities()
            initial_business_identity = self._business_content_identity()
            failure_reason = ""
            try:
                pointer = self._run(source, work, force=source == "mayhem")
                with self._lock:
                    if str(pointer.get("catalog_generation_id")) != catalog_id or self._catalog.get("catalog_generation_id") != catalog_id:
                        raise ValueError("optional_catalog_superseded")
                    previous = self._optional.get(source)
                    self._optional[source] = pointer
                    try:
                        self._publish()
                    except Exception:
                        if previous is None:
                            self._optional.pop(source, None)
                        else:
                            self._optional[source] = previous
                        raise
                self._mark_source(source)
            except (OSError, ValueError, RuntimeError) as exc:
                failure_reason = str(exc)
                self._source_results.setdefault(source, exception_source_result(exc))
                self._mark_source(
                    source,
                    error=source_failure_kind(self._source_results.get(source, {}), exc),
                )
            source_outcome = self._source_outcomes(
                attempted={source},
                failures={source: failure_reason} if failure_reason else {},
                deferred=set(),
                initial_identities=initial_identities,
            )[source]
            final_business_identity = self._business_content_identity()
            optional_content_changed = bool(
                final_business_identity and final_business_identity != initial_business_identity
            )
            completed_at = time.time()
            self._optional_progress = self._optional_progress.advance(
                state="failed" if failure_reason else "completed",
                reason_code=failure_reason,
                checked=not failure_reason,
                content_changed=optional_content_changed,
                source_outcomes={source: source_outcome},
                completed_at=completed_at,
            )
            with self._lock:
                merged = dict(self._progress.source_outcomes)
                merged[source] = source_outcome
                changes: dict[str, Any] = {"source_outcomes": merged}
                if self._progress.state in {"completed", "unchanged"}:
                    changed = self._progress.content_changed or optional_content_changed
                    changes.update(
                        content_changed=changed,
                        state="completed" if changed or self._progress.catalog_changed else "unchanged",
                        reason_code=(
                            "core_complete_optional_failed" if failure_reason else
                            "core_complete" if changed else self._progress.reason_code
                        ),
                    )
                self._progress = self._progress.advance(**changes)
                self._optional_results[source] = self._optional_progress.to_dict()
        with self._lock:
            self._optional_force_requested = False

    def request_stop(self) -> None:
        self._stop.set()
        with self._lock:
            for path in self._cancels:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("shutdown_requested", encoding="utf-8")

    def wait_optional(self) -> None:
        thread = self._optional_thread
        if thread is not None:
            while thread.is_alive() and not self._stop.is_set():
                thread.join(timeout=0.5)

"""DataService 的 cohort 刷新协调器。

协调器串行运行隔离来源 worker、记录 freshness/backoff，并在全部 candidate 绑定同一
Catalog 后通过 promotion journal 切换依赖和 generation。它不拥有抓取解析规则。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from hextech.contracts import CatalogManifestV2, RefreshScheduleV1, RefreshSourceState, SourcePointerV2, utc_now_iso
from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.infrastructure.persistence.refresh_schedule import RefreshScheduleStore, SCHEDULE_SOURCES
from hextech.infrastructure.persistence.retention import apply_retention
from hextech.infrastructure.processes import IsolatedProcessResult, run_isolated_process
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.modules.data.ports.paths import get_var_dir


SOURCE_INTERVALS = {
    "catalog": timedelta(hours=24),
    "hextech": timedelta(hours=4),
    "apex": timedelta(days=7),
    "mayhem": timedelta(hours=72),
}
SOURCE_TIMEOUTS = {
    "catalog": 5 * 60,
    "hextech": 30 * 60,
    "apex": 60 * 60,
    "mayhem": 10 * 60,
}


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _pointer_success_at(pointer: Mapping[str, Any]) -> str:
    return str(pointer.get("last_success_at") or "")


def _is_blocked_failure(payload: Mapping[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=True).lower()
    return any(token in text for token in ("http_403", "http_429", '"status_code": 403', '"status_code": 429'))


class CohortRefreshCoordinator:
    def __init__(
        self,
        *,
        publisher: DataSnapshotPublisher,
        builder: Callable[[], Any],
        root: str | Path | None = None,
        process_runner: Callable[..., IsolatedProcessResult] = run_isolated_process,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else get_var_dir()
        self.publisher = publisher
        self.builder = builder
        self.promotion = CohortPromotionStore(self.root)
        self.schedule_store = RefreshScheduleStore(self.root)
        self.process_runner = process_runner
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._active_cancel: Path | None = None
        self.promotion.recover()

    def request_stop(self) -> None:
        self._stop.set()
        if self._active_cancel is not None:
            self._active_cancel.parent.mkdir(parents=True, exist_ok=True)
            self._active_cancel.touch()

    def _current_pointer(self, source: str) -> dict[str, Any]:
        if source == "catalog":
            try:
                payload = json.loads((self.root / "catalog" / "current.v2.json").read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("schema_version") != 2:
                    return {}
                generation_id = str(payload.get("catalog_generation_id") or "")
                generation_root = self.root / "catalog" / "generations" / generation_id
                manifest_path = generation_root / "manifest.json"
                if not manifest_path.is_file() or sha256_file(manifest_path) != str(payload.get("manifest_sha256") or ""):
                    return {}
                manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = CatalogManifestV2.from_mapping(manifest_payload)
                if (
                    manifest.catalog_generation_id != generation_id
                    or manifest.content_sha256 != str(payload.get("content_sha256") or "")
                ):
                    return {}
                validate_catalog_files(generation_root, manifest)
            except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return payload
        try:
            payload = json.loads((self.root / "sources" / source / "current.v2.json").read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            pointer = SourcePointerV2.from_mapping(payload)
            if pointer.source != source:
                return {}
            run_root = (self.root / "sources" / source / "runs" / pointer.run_id).resolve()
            manifest_path = run_root / "manifest.json"
            artifact_path = (run_root / pointer.artifact.relative_path).resolve()
            if run_root not in artifact_path.parents or not manifest_path.is_file() or not artifact_path.is_file():
                return {}
            if (
                sha256_file(manifest_path) != pointer.manifest_sha256
                or sha256_file(artifact_path) != pointer.artifact.sha256
                or artifact_path.stat().st_size != pointer.artifact.size
            ):
                return {}
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return pointer.to_dict()

    def _due(self, source: str, state: RefreshSourceState, pointer: Mapping[str, Any], *, force: bool) -> bool:
        if force or not pointer:
            return True
        current = self.now()
        next_due = _parse_time(state.next_due_at)
        if next_due is not None:
            return current >= next_due
        success = _parse_time(state.last_success_at or _pointer_success_at(pointer))
        return success is None or current >= success + SOURCE_INTERVALS[source]

    def _worker_command(self, source: str, work: Path, catalog_pointer: Path | None, *, force: bool) -> list[str]:
        pointer_path = work / f"{source}.pointer.v2.json"
        result_path = work / f"{source}.result.json"
        cancel_path = work / f"{source}.cancel"
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--acquisition-worker"]
        else:
            command = [sys.executable, "-m", "hextech.bootstrap.acquisition_worker"]
        command.extend(
            [
                "--source",
                source,
                "--pointer-output",
                os.fspath(pointer_path),
                "--result-output",
                os.fspath(result_path),
                "--cancel-file",
                os.fspath(cancel_path),
            ]
        )
        if catalog_pointer is not None and source != "catalog":
            command.extend(["--catalog-pointer", os.fspath(catalog_pointer)])
        if force:
            command.append("--force")
        return command

    def _run_source(
        self,
        source: str,
        work: Path,
        catalog_pointer: Path | None,
        *,
        force: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        command = self._worker_command(source, work, catalog_pointer, force=force)
        cancel_path = work / f"{source}.cancel"
        self._active_cancel = cancel_path
        try:
            worker_env = os.environ.copy()
            worker_env["HEXTECH_VAR_DIR"] = os.fspath(self.root.resolve())
            execution = self.process_runner(
                command,
                timeout_seconds=SOURCE_TIMEOUTS[source],
                cancel_file=cancel_path,
                cancel_grace_seconds=2.0,
                env=worker_env,
            )
        finally:
            self._active_cancel = None
        result_path = work / f"{source}.result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result = {}
        if execution.timed_out:
            raise TimeoutError(f"{source} worker 超过硬上限")
        if execution.returncode != 0 or not isinstance(result, dict) or result.get("state") != "ready":
            detail = result or {"stderr": execution.stderr[-2000:]}
            raise RuntimeError(f"{source} worker 失败：{json.dumps(detail, ensure_ascii=False)}")
        pointer = result.get("pointer")
        if not isinstance(pointer, dict):
            raise RuntimeError(f"{source} worker 未返回 candidate pointer")
        return pointer, result

    def _candidate_catalog_path(self, work: Path, pointer: Mapping[str, Any]) -> Path:
        path = work / "catalog.pointer.v2.json"
        if not path.is_file():
            raise RuntimeError("Catalog candidate pointer 文件缺失")
        if str(pointer.get("catalog_generation_id") or "") == "":
            raise RuntimeError("Catalog candidate 缺少 generation ID")
        return path

    def _cohort_is_bound(self, pointers: Mapping[str, Mapping[str, Any]]) -> None:
        catalog = pointers["catalog"]
        catalog_id = str(catalog.get("catalog_generation_id") or "")
        catalog_sha = str(catalog.get("content_sha256") or "")
        if not catalog_id or not catalog_sha:
            raise RuntimeError("cohort Catalog pointer 不完整")
        for source in ("hextech", "apex", "mayhem"):
            pointer = SourcePointerV2.from_mapping(pointers[source])
            if pointer.catalog_generation_id != catalog_id or pointer.catalog_sha256 != catalog_sha:
                raise RuntimeError(f"{source} candidate 未绑定目标 Catalog")

    @staticmethod
    def _build_matches_targets(build: Any, pointers: Mapping[str, Mapping[str, Any]]) -> None:
        provenance = tuple(getattr(build, "source_files", ()))
        by_source: dict[str, Any] = {
            item.source: item for item in provenance if item.source != "catalog"
        }
        for source in ("hextech", "apex", "mayhem"):
            pointer = SourcePointerV2.from_mapping(pointers[source])
            item = by_source.get(source)
            if item is None:
                raise RuntimeError(f"generation 缺少 {source} provenance")
            if (
                item.run_id != pointer.run_id
                or item.catalog_generation_id != pointer.catalog_generation_id
                or item.artifact_sha256 != pointer.artifact.sha256
                or item.manifest_sha256 != pointer.manifest_sha256
                or item.record_count != pointer.artifact.record_count
            ):
                raise RuntimeError(f"generation {source} provenance 与 candidate pointer 不一致")
        catalog_id = str(pointers["catalog"].get("catalog_generation_id") or "")
        catalog_manifest_sha = str(pointers["catalog"].get("manifest_sha256") or "")
        catalog_items = [item for item in provenance if item.source == "catalog"]
        if len(catalog_items) != 3 or any(
            item.catalog_generation_id != catalog_id
            or item.run_id != catalog_id
            or item.manifest_sha256 != catalog_manifest_sha
            for item in catalog_items
        ):
            raise RuntimeError("generation Catalog provenance 与 candidate pointer 不一致")

    def _failure_state(self, previous: RefreshSourceState, payload: Mapping[str, Any]) -> RefreshSourceState:
        now = self.now()
        blocked = _is_blocked_failure(payload)
        delay = timedelta(hours=6) if blocked else timedelta(minutes=30)
        if not blocked:
            seed = hashlib.blake2b(json.dumps(payload, sort_keys=True).encode("utf-8"), digest_size=2).digest()
            delay += timedelta(seconds=int.from_bytes(seed, "big") % 301)
        return replace(
            previous,
            last_attempt_at=_iso(now),
            next_due_at=_iso(now + delay),
            failure_kind="http_blocked" if blocked else str(payload.get("error_type") or "worker_failed"),
            state="backoff",
        )

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        if self._stop.is_set():
            return {"state": "degraded", "reason_code": "shutdown_requested", "generation_id": self.publisher.current_generation_id()}
        now = self.now()
        schedule = self.schedule_store.load()
        states = dict(schedule.sources)
        current = {source: self._current_pointer(source) for source in SCHEDULE_SOURCES}
        due = {source: self._due(source, states[source], current[source], force=force) for source in SCHEDULE_SOURCES}
        if not any(due.values()):
            return {
                "state": "ready",
                "reason_code": "not_stale",
                "generation_id": self.publisher.current_generation_id(),
            }

        cycle_id = now.strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(4).hex()
        work = self.root / "snapshots" / "staging" / f"refresh-{cycle_id}"
        work.mkdir(parents=True, exist_ok=False)
        targets = {source: dict(current[source]) for source in SCHEDULE_SOURCES}
        results: dict[str, Any] = {}
        failures: dict[str, Any] = {}

        catalog_pointer_path: Path | None = (
            self.root / "catalog" / "current.v2.json" if current["catalog"] else None
        )
        if due["catalog"]:
            try:
                pointer, result = self._run_source("catalog", work, None, force=force)
                targets["catalog"] = pointer
                results["catalog"] = result
                catalog_pointer_path = self._candidate_catalog_path(work, pointer)
                changed = (
                    str(pointer.get("content_sha256") or "")
                    != str(current["catalog"].get("content_sha256") or "")
                )
                if changed:
                    due.update({"hextech": True, "apex": True, "mayhem": True})
            except Exception as exc:
                failures["catalog"] = {"error_type": exc.__class__.__name__, "error": str(exc)}

        for source in ("hextech", "apex", "mayhem"):
            if self._stop.is_set():
                failures[source] = {"error_type": "Cancelled", "error": "shutdown_requested"}
                continue
            if not due[source]:
                continue
            try:
                pointer, result = self._run_source(source, work, catalog_pointer_path, force=force)
                targets[source] = pointer
                results[source] = result
            except Exception as exc:
                failures[source] = {"error_type": exc.__class__.__name__, "error": str(exc)}

        missing = [source for source, pointer in targets.items() if not pointer]
        if failures or missing:
            failure_payload = {"failures": failures, "missing": missing}
            for source in SCHEDULE_SOURCES:
                if due[source]:
                    states[source] = self._failure_state(states[source], failures.get(source, {"error_type": "cohort_incomplete"}))
            self.schedule_store.save(
                RefreshScheduleV1(
                    updated_at=utc_now_iso(),
                    generation_id=self.publisher.current_generation_id(),
                    sources=states,
                )
            )
            return {
                "state": "degraded" if self.publisher.current_generation_id() else "failed",
                "reason_code": "cohort_refresh_failed",
                "generation_id": self.publisher.current_generation_id(),
                **failure_payload,
            }

        journal_started = False
        try:
            self._cohort_is_bound(targets)
            self.promotion.begin()
            journal_started = True
            for source in SCHEDULE_SOURCES:
                self.promotion.record_target(source, targets[source])
            self.promotion.promote_dependencies()
            build = self.builder()
            self._build_matches_targets(build, targets)
            manifest = self.publisher.publish(
                build.payloads,
                source_files=build.source_files,
                require_complete_provenance=True,
            )
            self.promotion.record_generation_promoted()
            self.promotion.commit()
            try:
                retention = apply_retention(self.root, now=self.now())
            except OSError as exc:
                retention = {"error": exc.__class__.__name__}
        except Exception:
            if journal_started:
                self.promotion.rollback()
            raise

        completed = self.now()
        for source in SCHEDULE_SOURCES:
            if not due[source]:
                continue
            pointer = targets[source]
            states[source] = RefreshSourceState(
                last_attempt_at=_iso(completed),
                last_success_at=str(pointer.get("last_success_at") or _iso(completed)),
                next_due_at=_iso(completed + SOURCE_INTERVALS[source]),
                failure_kind="",
                current_run_id=str(pointer.get("run_id") or pointer.get("catalog_generation_id") or ""),
                state="ready",
            )
        self.schedule_store.save(
            RefreshScheduleV1(
                updated_at=_iso(completed),
                generation_id=manifest.generation_id,
                sources=states,
            )
        )
        return {
            "state": "ready",
            "reason_code": "cohort_promoted",
            "generation_id": manifest.generation_id,
            "refreshed_sources": [source for source in SCHEDULE_SOURCES if due[source]],
            "source_results": results,
            "retention": retention,
        }


__all__ = ["CohortRefreshCoordinator", "SOURCE_INTERVALS", "SOURCE_TIMEOUTS"]

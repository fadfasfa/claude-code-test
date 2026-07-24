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

from hextech.contracts import (
    BaselineContributionV2,
    CatalogManifestV2,
    RefreshScheduleV1,
    RefreshSourceState,
    SourcePointerV2,
    utc_now_iso,
)
from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.infrastructure.persistence.refresh_schedule import RefreshScheduleStore, SCHEDULE_SOURCES
from hextech.infrastructure.persistence.retention import apply_retention
from hextech.infrastructure.processes import IsolatedProcessResult, run_isolated_process
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir


SOURCE_INTERVALS = {
    "catalog": timedelta(hours=24),
    "hextech": timedelta(hours=4),
    "apex": timedelta(hours=72),
    "mayhem": timedelta(hours=72),
}
SOURCE_TIMEOUTS = {
    "catalog": 5 * 60,
    "hextech": 30 * 60,
    "apex": 60 * 60,
    "mayhem": 10 * 60,
}

ContributionMap = Mapping[str, Mapping[str, Any]]
SnapshotBuilder = Callable[[ContributionMap], Any]


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
        builder: SnapshotBuilder,
        root: str | Path | None = None,
        process_runner: Callable[..., IsolatedProcessResult] = run_isolated_process,
        now: Callable[[], datetime] | None = None,
        upstream_marker_probe: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else get_var_dir()
        self.publisher = publisher
        self.builder = builder
        self.promotion = CohortPromotionStore(self.root)
        self.schedule_store = RefreshScheduleStore(self.root)
        self.process_runner = process_runner
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._upstream_marker_probe = upstream_marker_probe
        self._stop = threading.Event()
        self._cancel_lock = threading.Lock()
        self._active_cancel: Path | None = None
        self.promotion.recover()

    def _baseline_contributions(self, catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """从已验证 generation 提取不落 source current 的 last-good 身份。"""

        from hextech.modules.data.generation import DataSnapshotClient, SnapshotValidationError

        try:
            view = DataSnapshotClient(self.publisher.root).open_view()
        except SnapshotValidationError:
            return {}
        catalog_id = str(catalog.get("catalog_generation_id") or "")
        try:
            catalog_sha, expected_catalog_provenance = self._validated_catalog_provenance(catalog)
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        origin_catalog_provenance = tuple(
            sorted(
                (
                    item.run_id,
                    item.catalog_generation_id,
                    item.artifact_role,
                    item.artifact_sha256,
                    item.record_count,
                    item.content_schema_version,
                )
                for item in view.manifest.source_files
                if item.source == "catalog"
            )
        )
        if origin_catalog_provenance != expected_catalog_provenance:
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in view.manifest.source_files:
            if item.source not in {"hextech", "apex", "mayhem"} or item.catalog_generation_id != catalog_id:
                continue
            result[item.source] = BaselineContributionV2(
                source=item.source,
                origin_generation_id=view.manifest.generation_id,
                catalog_generation_id=item.catalog_generation_id,
                catalog_sha256=catalog_sha,
                created_at=view.manifest.created_at,
                provenance=item,
                snapshot_files=view.manifest.files,
            ).to_dict()
        return result

    def _validated_catalog_provenance(
        self, catalog: Mapping[str, Any]
    ) -> tuple[str, tuple[tuple[str, str, str, str, int, int], ...]]:
        """验证目标 Catalog 文件，并返回可与 origin generation 精确比较的身份。"""

        catalog_id = str(catalog.get("catalog_generation_id") or "")
        catalog_root = self.root / "catalog" / "generations" / catalog_id
        manifest_path = catalog_root / "manifest.json"
        manifest = CatalogManifestV2.from_mapping(json.loads(manifest_path.read_text(encoding="utf-8")))
        manifest_sha256 = sha256_file(manifest_path)
        if (
            manifest.catalog_generation_id != catalog_id
            or manifest.content_sha256 != str(catalog.get("content_sha256") or "")
            or manifest_sha256 != str(catalog.get("manifest_sha256") or "")
        ):
            raise ValueError("Catalog pointer 与 manifest 身份不一致")
        validate_catalog_files(catalog_root, manifest)
        provenance = tuple(
            sorted(
                (
                    catalog_id,
                    catalog_id,
                    item.role,
                    item.sha256,
                    item.record_count,
                    item.content_schema_version,
                )
                for item in manifest.files
            )
        )
        return manifest.content_sha256, provenance

    @staticmethod
    def _is_baseline(pointer: Mapping[str, Any]) -> bool:
        return pointer.get("kind") == "baseline_generation"

    @staticmethod
    def _pointer_identity(pointer: Mapping[str, Any]) -> tuple[str, str, str, int, str]:
        if CohortRefreshCoordinator._is_baseline(pointer):
            baseline = BaselineContributionV2.from_mapping(pointer)
            item = baseline.provenance
            return (
                item.run_id,
                item.catalog_generation_id,
                item.artifact_sha256,
                item.record_count,
                item.manifest_sha256,
            )
        artifact = pointer.get("artifact") if isinstance(pointer.get("artifact"), Mapping) else {}
        return (
            str(pointer.get("run_id") or ""),
            str(pointer.get("catalog_generation_id") or ""),
            str(artifact.get("sha256") or ""),
            int(artifact.get("record_count") or 0),
            str(pointer.get("manifest_sha256") or ""),
        )

    def _candidate_path(self, source: str) -> Path:
        return self.root / "state" / "data-service" / "candidates" / f"{source}.v2.json"

    def _load_saved_candidate(self, source: str, catalog: Mapping[str, Any]) -> dict[str, Any]:
        path = self._candidate_path(source)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pointer = SourcePointerV2.from_mapping(payload)
            if (
                pointer.source != source
                or pointer.catalog_generation_id != str(catalog.get("catalog_generation_id") or "")
                or pointer.catalog_sha256 != str(catalog.get("content_sha256") or "")
            ):
                return {}
            run_root = self.root / "sources" / source / "runs" / pointer.run_id
            artifact = run_root / pointer.artifact.relative_path
            manifest = run_root / "manifest.json"
            if (
                not artifact.is_file()
                or not manifest.is_file()
                or sha256_file(artifact) != pointer.artifact.sha256
                or sha256_file(manifest) != pointer.manifest_sha256
            ):
                return {}
            return pointer.to_dict()
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _save_candidate(self, source: str, pointer: Mapping[str, Any]) -> None:
        """保存已通过 worker 门禁的 immutable run pointer，供后续 refresh cycle 组合。"""

        atomic_write_json(self._candidate_path(source), dict(pointer), ensure_ascii=False, indent=2)

    def request_stop(self) -> None:
        self._stop.set()
        with self._cancel_lock:
            cancel_path = self._active_cancel
            if cancel_path is not None:
                cancel_path.parent.mkdir(parents=True, exist_ok=True)
                cancel_path.touch()

    def _current_pointer(self, source: str) -> dict[str, Any]:
        if source == "catalog":
            pointer_path = self.root / "catalog" / "current.v2.json"
            if not pointer_path.is_file():
                # 旧安装可能只有已验证 generation，没有依赖 pointer；从 generation
                # manifest 恢复只读 Catalog 身份，不写回 current。
                try:
                    snapshot_pointer = json.loads((self.root / "snapshots" / "current.v2.json").read_text(encoding="utf-8"))
                    generation_id = str(snapshot_pointer.get("current_generation_id") or "")
                    generation_manifest = json.loads(
                        (self.root / "snapshots" / "generations" / generation_id / "manifest.json").read_text(encoding="utf-8")
                    )
                    items = [item for item in generation_manifest.get("source_files", []) if item.get("source") == "catalog"]
                    if len(items) != 3:
                        return {}
                    catalog_id = str(items[0].get("catalog_generation_id") or "")
                    catalog_root = self.root / "catalog" / "generations" / catalog_id
                    manifest_path = catalog_root / "manifest.json"
                    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest = CatalogManifestV2.from_mapping(manifest_payload)
                    validate_catalog_files(catalog_root, manifest)
                    generation_files = {
                        str(item.get("artifact_role") or ""): (
                            str(item.get("artifact_sha256") or ""),
                            int(item.get("record_count") or 0),
                        )
                        for item in items
                    }
                    if manifest.catalog_generation_id != catalog_id or any(
                        generation_files.get(item.role) != (item.sha256, item.record_count)
                        for item in manifest.files
                    ):
                        return {}
                    return {
                        "schema_version": 2,
                        "catalog_generation_id": catalog_id,
                        "content_sha256": str(manifest_payload.get("content_sha256") or ""),
                        "manifest_sha256": sha256_file(manifest_path),
                        "completed_at": str(generation_manifest.get("created_at") or ""),
                        "last_success_at": str(generation_manifest.get("created_at") or ""),
                    }
                except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    return {}
            try:
                payload = json.loads(pointer_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("schema_version") != 2:
                    raise ValueError("Catalog pointer schema 无效")
                generation_id = str(payload.get("catalog_generation_id") or "")
                generation_root = self.root / "catalog" / "generations" / generation_id
                manifest_path = generation_root / "manifest.json"
                if not manifest_path.is_file() or sha256_file(manifest_path) != str(payload.get("manifest_sha256") or ""):
                    raise ValueError("Catalog manifest 缺失或哈希不匹配")
                manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = CatalogManifestV2.from_mapping(manifest_payload)
                if (
                    manifest.catalog_generation_id != generation_id
                    or manifest.content_sha256 != str(payload.get("content_sha256") or "")
                ):
                    raise ValueError("Catalog pointer 身份不匹配")
                validate_catalog_files(generation_root, manifest)
            except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Catalog current 无效：{exc}") from exc
            return payload
        pointer_path = self.root / "sources" / source / "current.v2.json"
        if not pointer_path.is_file():
            return {}
        try:
            payload = json.loads(pointer_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("source pointer 必须是对象")
            pointer = SourcePointerV2.from_mapping(payload)
            if pointer.source != source:
                raise ValueError(f"source pointer 身份不匹配：{pointer.source}")
            run_root = (self.root / "sources" / source / "runs" / pointer.run_id).resolve()
            manifest_path = run_root / "manifest.json"
            artifact_path = (run_root / pointer.artifact.relative_path).resolve()
            if run_root not in artifact_path.parents or not manifest_path.is_file() or not artifact_path.is_file():
                raise ValueError("source current 引用文件缺失或越界")
            if (
                sha256_file(manifest_path) != pointer.manifest_sha256
                or sha256_file(artifact_path) != pointer.artifact.sha256
                or artifact_path.stat().st_size != pointer.artifact.size
            ):
                raise ValueError("source current 哈希或大小不匹配")
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{source} current 无效：{exc}") from exc
        return pointer.to_dict()

    def _due(self, source: str, state: RefreshSourceState, pointer: Mapping[str, Any], *, force: bool) -> bool:
        if force:
            return True
        current = self.now()
        next_due = _parse_time(state.next_due_at)
        if next_due is not None:
            return current >= next_due
        # source current 缺失不代表首次尝试；失败后的 backoff 必须先于立即刷新语义。
        if not pointer:
            return True
        success = _parse_time(state.last_success_at or _pointer_success_at(pointer))
        return success is None or current >= success + SOURCE_INTERVALS[source]

    def _source_coverage(self, source: str, pointer: Mapping[str, Any]) -> dict[str, Any]:
        """从 immutable run 读取覆盖摘要；历史 run 缺失时明确返回空对象。"""

        if source == "catalog" or self._is_baseline(pointer):
            return {}
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            return {}
        path = self.root / "sources" / source / "runs" / run_id / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        metadata = payload.get("metadata") if isinstance(payload, Mapping) else {}
        coverage = metadata.get("coverage") if isinstance(metadata, Mapping) else {}
        return dict(coverage) if isinstance(coverage, Mapping) else {}

    def _probe_hextech_upstream_change(self, pointer: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        """仅以稳定上游版本加速候选；日期继续保留为诊断信息。"""

        if self._upstream_marker_probe is None or not pointer:
            return False, {}
        try:
            marker = dict(self._upstream_marker_probe() or {})
        except Exception:
            # probe 只负责加速刷新，网络瞬断不能升级为 source 失败。
            return False, {}
        version = str(marker.get("version") or "").strip()
        date = str(marker.get("date") or "").strip()
        if not version and not date:
            return False, {}
        # updated_at / Last-Modified 可能随 CDN 响应变化；只把稳定版本作为提前刷新信号。
        if not version:
            return False, {"version": version, "date": date}
        coverage = self._source_coverage("hextech", pointer)
        upstream = coverage.get("upstream") if isinstance(coverage.get("upstream"), Mapping) else {}
        previous_version = str(upstream.get("version") or "").strip()
        changed = version != previous_version
        return changed, {"version": version, "date": date}

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
        cancel_path.unlink(missing_ok=True)
        with self._cancel_lock:
            self._active_cancel = cancel_path
            if self._stop.is_set():
                cancel_path.parent.mkdir(parents=True, exist_ok=True)
                cancel_path.touch()
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
            with self._cancel_lock:
                if self._active_cancel == cancel_path:
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
            pointer = pointers[source]
            if (
                str(pointer.get("catalog_generation_id") or "") != catalog_id
                or str(pointer.get("catalog_sha256") or "") != catalog_sha
            ):
                raise RuntimeError(f"{source} candidate 未绑定目标 Catalog")

    @staticmethod
    def _build_matches_targets(build: Any, pointers: Mapping[str, Mapping[str, Any]]) -> None:
        provenance = tuple(getattr(build, "source_files", ()))
        by_source: dict[str, Any] = {
            item.source: item for item in provenance if item.source != "catalog"
        }
        for source in ("hextech", "apex", "mayhem"):
            pointer = pointers[source]
            item = by_source.get(source)
            if item is None:
                raise RuntimeError(f"generation 缺少 {source} provenance")
            run_id, catalog_id, artifact_sha, record_count, manifest_sha = CohortRefreshCoordinator._pointer_identity(pointer)
            if (
                item.run_id != run_id
                or item.catalog_generation_id != catalog_id
                or item.artifact_sha256 != artifact_sha
                or item.manifest_sha256 != manifest_sha
                or item.record_count != record_count
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
        upstream_changed = False
        upstream_marker: dict[str, Any] = {}
        # 候选失败后的 backoff 必须优先于 marker 加速，避免相同版本反复全量抓取。
        if not force and not due["hextech"] and states["hextech"].state != "backoff":
            upstream_changed, upstream_marker = self._probe_hextech_upstream_change(current["hextech"])
            if upstream_changed:
                due["hextech"] = True
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
        baseline = self._baseline_contributions(current["catalog"])
        for source in ("hextech", "apex", "mayhem"):
            if not targets[source] and source in baseline:
                targets[source] = dict(baseline[source])
        results: dict[str, Any] = {}
        failures: dict[str, Any] = {}

        catalog_pointer_path: Path | None = (
            self.root / "catalog" / "current.v2.json" if current["catalog"] else None
        )
        if current["catalog"] and catalog_pointer_path is not None and not catalog_pointer_path.is_file():
            # recovered baseline pointer 只存在内存中；worker 需要一个受限于本轮 staging
            # 的 candidate path，不能把它写回正式 current。
            catalog_pointer_path = work / "catalog.pointer.v2.json"
            atomic_write_json(catalog_pointer_path, current["catalog"], ensure_ascii=False, indent=2)
        catalog_changed = False
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
                    catalog_changed = True
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
                pointer, result = self._run_source(
                    source,
                    work,
                    catalog_pointer_path,
                    force=force or (source == "hextech" and upstream_changed),
                )
                targets[source] = pointer
                results[source] = result
                self._save_candidate(source, pointer)
            except Exception as exc:
                failures[source] = {"error_type": exc.__class__.__name__, "error": str(exc)}

        for source in tuple(failures):
            if source == "catalog":
                continue
            saved = self._load_saved_candidate(source, targets["catalog"])
            if saved and (
                not current[source]
                or self._pointer_identity(saved) != self._pointer_identity(current[source])
            ):
                targets[source] = saved
                results[source] = {"state": "ready", "source": source, "reason_code": "saved_candidate"}
                failures.pop(source, None)

        missing = [source for source, pointer in targets.items() if not pointer]
        baseline = self._baseline_contributions(targets["catalog"])
        for source in ("hextech", "apex", "mayhem"):
            if not targets[source] and source in baseline:
                targets[source] = dict(baseline[source])
        missing = [source for source, pointer in targets.items() if not pointer]
        cannot_fallback = bool(missing or (catalog_changed and failures))
        if cannot_fallback:
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
                "reason_code": "data_stale" if self.publisher.current_generation_id() else "cohort_refresh_failed",
                "generation_id": self.publisher.current_generation_id(),
                "data_status": "data_stale" if self.publisher.current_generation_id() else "unavailable",
                "data_reason": "candidate_rejected_last_good_preserved" if self.publisher.current_generation_id() else "no_snapshot",
                "upstream_marker": upstream_marker,
                **failure_payload,
            }

        degraded_sources = set(failures)
        degraded_sources.update(
            source for source in ("hextech", "apex", "mayhem") if self._is_baseline(targets[source])
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

        journal_started = False
        try:
            self._cohort_is_bound(targets)
            self.promotion.begin()
            journal_started = True
            for source in SCHEDULE_SOURCES:
                if not self._is_baseline(targets[source]):
                    self.promotion.record_target(source, targets[source])
            self.promotion.promote_dependencies()
            build = self.builder(targets)
            self._build_matches_targets(build, targets)
            completed = self.now()
            source_status: dict[str, dict[str, Any]] = {}
            for source in SCHEDULE_SOURCES:
                target = targets[source]
                if source == "catalog":
                    run_id = str(target.get("catalog_generation_id") or "")
                    artifact_sha = str(target.get("content_sha256") or "")
                    manifest_sha = str(target.get("manifest_sha256") or "")
                    record_count = 0
                else:
                    run_id, _, artifact_sha, record_count, manifest_sha = self._pointer_identity(target)
                source_status[source] = {
                    "catalog_id": str(
                        target.get("catalog_generation_id")
                        or targets["catalog"].get("catalog_generation_id")
                        or ""
                    ),
                    "data_at": str(
                        target.get("last_success_at") or target.get("completed_at") or target.get("created_at") or ""
                    ),
                    "checked_at": _iso(completed),
                    "freshness": "last_good" if source in degraded_sources else "fresh",
                    "run_id": run_id,
                    "origin_generation_id": str(target.get("origin_generation_id") or ""),
                    "artifact_sha256": artifact_sha,
                    "manifest_sha256": manifest_sha,
                    "record_count": record_count,
                    "data_status": "data_stale" if source in degraded_sources else "fresh",
                    # UI 只消费稳定诊断码；原始 worker 错误仍保留在本轮 source 诊断中。
                    "data_reason": "candidate_rejected_last_good_preserved" if source in degraded_sources else "",
                    "coverage": self._source_coverage(source, target),
                }
            manifest = self.publisher.publish(
                build.payloads,
                source_files=build.source_files,
                require_complete_provenance=True,
                health="degraded" if degraded_sources else "healthy",
                refreshed_sources=refreshed_sources,
                degraded_sources=sorted(degraded_sources),
                source_status=source_status,
            )
            self.promotion.record_generation_promoted(manifest.generation_id)
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
            if source in failures:
                states[source] = self._failure_state(states[source], failures[source])
                continue
            pointer = targets[source]
            states[source] = RefreshSourceState(
                last_attempt_at=_iso(completed),
                last_success_at=str(pointer.get("last_success_at") or _iso(completed)),
                next_due_at=_iso(completed + SOURCE_INTERVALS[source]),
                failure_kind="",
                current_run_id=(
                    self._pointer_identity(pointer)[0]
                    if source != "catalog"
                    else str(pointer.get("catalog_generation_id") or "")
                ),
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
            "state": "degraded" if degraded_sources else "ready",
            "reason_code": "cohort_promoted",
            "generation_id": manifest.generation_id,
            "refreshed_sources": refreshed_sources,
            "degraded_sources": sorted(degraded_sources),
            "source_results": results,
            "upstream_marker": upstream_marker,
            "retention": retention,
        }


__all__ = ["CohortRefreshCoordinator", "SOURCE_INTERVALS", "SOURCE_TIMEOUTS"]

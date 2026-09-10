"""串行运行来源 worker，并以同一 Catalog 原子晋升 DataService generation。"""

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
)
from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.infrastructure.persistence.cohort_recovery import refresh_recovery_schedule
from hextech.infrastructure.persistence.refresh_schedule import RefreshScheduleStore, SCHEDULE_SOURCES
from hextech.infrastructure.persistence.refresh_checkpoint import (
    CatalogAdoptionCheckpointStore,
    RefreshCheckpointStore,
    checkpoint_failure_evidence,
)
from hextech.infrastructure.processes import IsolatedProcessResult, run_isolated_process
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.modules.data.freshness import SOURCE_INTERVALS, source_reuse_allowed
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir
from hextech.bootstrap.game_refresh_gate import GameRefreshDeferred, GameRefreshGate, RefreshStopRequested
from hextech.bootstrap.refresh_cycle import RefreshCycleMixin
from hextech.bootstrap.refresh_adoption import (
    blocked_catalog_adoption,
    migrate_foreign_catalog_checkpoint,
    normalize_pending_sources,
    reconcile_active_catalog_schedule,
)
from hextech.bootstrap.refresh_promotion import promote_targets
from hextech.bootstrap.source_freshness import is_blocked_failure, iso_utc, parse_refresh_time, pointer_success_at
from hextech.bootstrap.source_worker_failure import SourceWorkerFailure, refresh_failure_kind
SOURCE_TIMEOUTS = {"catalog": 5 * 60, "aramkit": 10 * 60, "blitz": 2 * 60, "apex": 60 * 60, "mayhem": 10 * 60}
ContributionMap = Mapping[str, Mapping[str, Any]]
SnapshotBuilder = Callable[[ContributionMap], Any]

class CohortRefreshCoordinator(RefreshCycleMixin):
    def __init__(
        self,
        *,
        publisher: DataSnapshotPublisher,
        builder: SnapshotBuilder,
        root: str | Path | None = None,
        process_runner: Callable[..., IsolatedProcessResult] = run_isolated_process,
        now: Callable[[], datetime] | None = None,
        upstream_marker_probe: Callable[[], Mapping[str, Any]] | None = None,
        game_state_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else get_var_dir()
        self.publisher = publisher
        self.builder = builder
        self.promotion = CohortPromotionStore(self.root)
        self.schedule_store = RefreshScheduleStore(self.root)
        self.checkpoint_store = RefreshCheckpointStore(self.root)
        self.adoption_checkpoint_store = CatalogAdoptionCheckpointStore(self.root)
        self.process_runner = process_runner
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._upstream_marker_probe = upstream_marker_probe
        self._stop = threading.Event()
        self._cancel_lock = threading.Lock()
        self._active_cancel: Path | None = None
        self._game_cancel_requested = threading.Event()
        self._active_refresh_request = {"force": False, "scope": "due"}
        self._game_refresh = GameRefreshGate(
            root=self.root,
            current_generation_id=self.publisher.current_generation_id,
            now=self.now,
            game_state_probe=game_state_probe,
        )
        self.promotion.recover()
    @staticmethod
    def _catalog_identity(pointer: Mapping[str, Any]) -> tuple[str, str]:
        return (
            str(pointer.get("catalog_generation_id") or ""),
            str(pointer.get("content_sha256") or ""),
        )
    def _normalize_pending_sources(self, payload: Mapping[str, Any]) -> list[str]:
        return normalize_pending_sources(self, payload)

    def _migrate_foreign_catalog_checkpoint(
        self,
        current: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        return migrate_foreign_catalog_checkpoint(self, current)

    def _blocked_catalog_adoption(self, current_catalog: Mapping[str, Any]) -> dict[str, Any]:
        return blocked_catalog_adoption(self, current_catalog)

    def _reconcile_active_catalog_schedule(
        self,
        current: Mapping[str, Mapping[str, Any]],
        schedule: RefreshScheduleV1,
    ) -> RefreshScheduleV1:
        return reconcile_active_catalog_schedule(self, current, schedule)

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
            if item.source not in {"aramkit", "blitz", "apex", "mayhem"} or item.catalog_generation_id != catalog_id:
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
            return self._validate_source_candidate(source, payload, catalog)
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _validate_source_candidate(
        self,
        source: str,
        payload: Mapping[str, Any],
        catalog: Mapping[str, Any],
    ) -> dict[str, Any]:
        pointer = SourcePointerV2.from_mapping(payload)
        if (
            pointer.source != source
            or pointer.catalog_generation_id != str(catalog.get("catalog_generation_id") or "")
            or pointer.catalog_sha256 != str(catalog.get("content_sha256") or "")
        ):
            raise ValueError(f"{source} candidate 未绑定 checkpoint Catalog")
        run_root = (self.root / "sources" / source / "runs" / pointer.run_id).resolve()
        artifact = (run_root / pointer.artifact.relative_path).resolve()
        manifest = run_root / "manifest.json"
        if (
            run_root not in artifact.parents
            or not artifact.is_file()
            or not manifest.is_file()
            or artifact.stat().st_size != pointer.artifact.size
            or sha256_file(artifact) != pointer.artifact.sha256
            or sha256_file(manifest) != pointer.manifest_sha256
        ):
            raise ValueError(f"{source} candidate 文件、大小或哈希不匹配")
        return pointer.to_dict()

    def _validate_catalog_candidate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(payload)
        self._validated_catalog_provenance(candidate)
        return candidate

    @staticmethod
    def _ordered_sources(sources: set[str] | list[str] | tuple[str, ...]) -> list[str]:
        selected = set(sources)
        return [source for source in SCHEDULE_SOURCES if source in selected]

    def _load_resumable_checkpoint(self, current: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        raw = self._migrate_foreign_catalog_checkpoint(current)
        if raw.get("state") != "in_progress":
            return {}
        try:
            catalog_raw = raw.get("catalog")
            if not isinstance(catalog_raw, Mapping):
                raise ValueError("checkpoint Catalog 缺失")
            catalog = self._validate_catalog_candidate(catalog_raw)
            completed_raw = raw.get("completed_sources")
            if not isinstance(completed_raw, Mapping):
                raise ValueError("checkpoint completed_sources 无效")
            completed: dict[str, dict[str, Any]] = {}
            for source, pointer in completed_raw.items():
                source_name = str(source)
                if source_name not in SCHEDULE_SOURCES or not isinstance(pointer, Mapping):
                    raise ValueError("checkpoint 包含未知 candidate")
                if source_name == "catalog":
                    validated = self._validate_catalog_candidate(pointer)
                    if (
                        str(validated.get("catalog_generation_id") or "")
                        != str(catalog.get("catalog_generation_id") or "")
                        or str(validated.get("content_sha256") or "")
                        != str(catalog.get("content_sha256") or "")
                    ):
                        raise ValueError("checkpoint Catalog 身份不一致")
                else:
                    validated = self._validate_source_candidate(source_name, pointer, catalog)
                completed[source_name] = validated
            pending_raw = raw.get("pending_sources")
            if not isinstance(pending_raw, list):
                raise ValueError("checkpoint pending_sources 无效")
            pending = {str(source) for source in pending_raw} - set(completed)
            if not pending.issubset(set(SCHEDULE_SOURCES)):
                raise ValueError("checkpoint pending_sources 包含未知来源")
            core_generation_id = str(raw.get("core_generation_id") or "")
            if core_generation_id and self.publisher.current_generation_id() != core_generation_id:
                raise ValueError("checkpoint core generation 已被其他发布替换")
            current_catalog = current.get("catalog") or {}
            if (
                not core_generation_id
                and "catalog" not in completed
                and current_catalog
                and (
                    str(current_catalog.get("catalog_generation_id") or "")
                    != str(catalog.get("catalog_generation_id") or "")
                    or str(current_catalog.get("content_sha256") or "")
                    != str(catalog.get("content_sha256") or "")
                )
            ):
                raise ValueError("checkpoint Catalog 与 current 不一致")
            return {
                **raw,
                "catalog": catalog,
                "completed_sources": completed,
                "pending_sources": self._ordered_sources(pending),
            }
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _save_refresh_checkpoint(
        self,
        *,
        cycle_id: str,
        created_at: str,
        force: bool,
        scope: str = "due",
        phase: str,
        state: str,
        catalog: Mapping[str, Any],
        completed_sources: Mapping[str, Mapping[str, Any]],
        pending_sources: set[str] | list[str] | tuple[str, ...],
        core_generation_id: str = "",
        failures: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        completed_names = set(completed_sources)
        normalized_pending = set(pending_sources) - completed_names
        failure_payloads = failures or {}
        checkpoint_failures: dict[str, dict[str, Any]] = {}
        for source in self._ordered_sources(normalized_pending):
            payload = failure_payloads.get(source)
            if not isinstance(payload, Mapping):
                continue
            evidence = checkpoint_failure_evidence(payload)
            if evidence:
                checkpoint_failures[source] = evidence
        self.checkpoint_store.save(
            {
                "state": state,
                "cycle_id": cycle_id,
                "created_at": created_at,
                "updated_at": iso_utc(self.now()),
                "force": bool(force),
                "scope": str(scope),
                "refresh_phase": phase,
                "catalog": dict(catalog),
                "completed_sources": {
                    source: dict(completed_sources[source])
                    for source in SCHEDULE_SOURCES
                    if source in completed_sources
                },
                "pending_sources": self._ordered_sources(normalized_pending),
                "core_generation_id": str(core_generation_id or ""),
                "failures": checkpoint_failures,
            }
        )

    def _save_candidate(self, source: str, pointer: Mapping[str, Any]) -> None:
        """保存已通过 worker 门禁的 immutable run pointer，供后续 refresh cycle 组合。"""

        atomic_write_json(self._candidate_path(source), dict(pointer), ensure_ascii=False, indent=2)

    def request_stop(self) -> None:
        self._stop.set()
        with self._cancel_lock:
            cancel_path = self._active_cancel
            if cancel_path is not None:
                cancel_path.parent.mkdir(parents=True, exist_ok=True)
                cancel_path.write_text("shutdown_requested", encoding="utf-8")

    def poll_deferred_refresh(self) -> dict[str, Any] | None:
        """取消对局中 worker，并在赛后 30 秒返回一次合并后的恢复请求。"""

        in_game = self._game_refresh.in_progress()
        if in_game:
            with self._cancel_lock:
                cancel_path = self._active_cancel
                if cancel_path is not None:
                    self._game_cancel_requested.set()
                    cancel_path.parent.mkdir(parents=True, exist_ok=True)
                    cancel_path.write_text("game_in_progress", encoding="utf-8")
            if cancel_path is not None:
                self._game_refresh.mark_worker_cancelled(**self._active_refresh_request)
        return self._game_refresh.poll(in_game=in_game)

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
                    if len(items) not in {3, 4}:
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
                    manifest_roles = {item.role for item in manifest.files}
                    if (set(generation_files) != manifest_roles
                        or manifest.catalog_generation_id != catalog_id or any(
                        generation_files.get(item.role) != (item.sha256, item.record_count)
                        for item in manifest.files
                    )):
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
        next_due = parse_refresh_time(state.next_due_at)
        # 旧 schedule 可能只有 ``state=backoff`` 而没有 failure_kind；两种
        # 表示都必须先服从退避窗口，避免时间字段缺失时立即循环重试。
        if (state.state == "backoff" or state.failure_kind) and next_due is not None and current < next_due:
            return False
        if pointer:
            success_text = state.last_success_at or pointer_success_at(pointer)
            if not source_reuse_allowed(source, success_text, current):
                identity_field = "catalog_generation_id" if source == "catalog" else "run_id"
                expected_run_id = str(pointer.get(identity_field) or "")
                last_attempt = parse_refresh_time(state.last_attempt_at)
                recently_checked = bool(
                    expected_run_id
                    and state.current_run_id == expected_run_id
                    and last_attempt is not None
                    and current < last_attempt + SOURCE_INTERVALS[source]
                )
                if recently_checked:
                    # immutable pointer 的 data_at 可以保持旧值；刚成功确认来源
                    # 没有新内容时仍须服从固定 cadence，不能在每次启动重复抓取。
                    return False
                return True
        if next_due is not None:
            return current >= next_due
        # source current 缺失不代表首次尝试；失败后的 backoff 必须先于立即刷新语义。
        if not pointer:
            return True
        success = parse_refresh_time(state.last_success_at or pointer_success_at(pointer))
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

    def _source_metadata(self, source: str, pointer: Mapping[str, Any]) -> dict[str, Any]:
        if source == "catalog" or self._is_baseline(pointer):
            return {}
        run_id = str(pointer.get("run_id") or "")
        path = self.root / "sources" / source / "runs" / run_id / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        metadata = payload.get("metadata") if isinstance(payload, Mapping) else {}
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    def _probe_aramkit_upstream_change(self, pointer: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        """比较 ARAMKit 固定四字段 marker；probe 失败只取消加速，不污染来源健康。"""

        if self._upstream_marker_probe is None or not pointer:
            return False, {}
        try:
            marker = dict(self._upstream_marker_probe() or {})
        except Exception:
            # probe 只负责加速刷新，网络瞬断不能升级为 source 失败。
            return False, {}
        required = {"version", "dataPath", "buildTimeUnixMs", "allMatches"}
        if set(marker) != required or not str(marker.get("version") or "").strip() or not str(
            marker.get("dataPath") or ""
        ).strip():
            return False, {}
        previous = self._source_metadata("aramkit", pointer).get("marker")
        return isinstance(previous, Mapping) and dict(previous) != marker, marker

    def _worker_command(
        self,
        source: str,
        work: Path,
        catalog_pointer: Path | None,
        *,
        force: bool,
        compatibility_catalog_pointer: Path | None = None,
        coverage_policy: str = "catalog_adoption",
    ) -> list[str]:
        pointer_path = work / f"{source}.pointer.v2.json"
        result_path = work / f"{source}.result.json"
        cancel_path = work / f"{source}.cancel"
        command = ([sys.executable, "--acquisition-worker"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "hextech.bootstrap.acquisition_worker"])
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
        if compatibility_catalog_pointer is not None and source in {"aramkit", "blitz"}:
            command.extend(
                ["--catalog-compatibility-pointer", os.fspath(compatibility_catalog_pointer)]
            )
        if source == "blitz":
            command.extend(["--coverage-policy", coverage_policy])
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
        compatibility_catalog_pointer: Path | None = None,
        coverage_policy: str = "catalog_adoption",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._game_refresh.in_progress():
            raise GameRefreshDeferred("game_in_progress")
        self._game_cancel_requested.clear()
        command = self._worker_command(
            source,
            work,
            catalog_pointer,
            force=force,
            compatibility_catalog_pointer=compatibility_catalog_pointer,
            coverage_policy=coverage_policy,
        )
        cancel_path = work / f"{source}.cancel"
        cancel_path.unlink(missing_ok=True)
        with self._cancel_lock:
            self._active_cancel = cancel_path
            if self._stop.is_set():
                cancel_path.parent.mkdir(parents=True, exist_ok=True)
                cancel_path.write_text("shutdown_requested", encoding="utf-8")
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
        if self._game_cancel_requested.is_set():
            raise GameRefreshDeferred("game_in_progress")
        if execution.cancelled:
            if execution.cancel_reason == "game_in_progress":
                raise GameRefreshDeferred("game_in_progress")
            if self._stop.is_set() or execution.cancel_reason == "shutdown_requested":
                raise RefreshStopRequested("shutdown_requested")
        result_path = work / f"{source}.result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            result = {}
        if execution.timed_out:
            raise TimeoutError(f"{source} worker 超过硬上限")
        if execution.returncode != 0 or not isinstance(result, dict) or result.get("state") != "ready":
            detail = result or {"error_type": "WorkerFailed", "error": execution.stderr[-2000:]}
            raise SourceWorkerFailure(source, detail)
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
        for source in ("aramkit", "blitz", "apex", "mayhem"):
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
        for source in ("aramkit", "blitz", "apex", "mayhem"):
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
        catalog_roles: set[str] = {item.artifact_role for item in catalog_items}
        required_catalog_roles = {"champions", "augments", "versions"}
        valid_catalog_roles = required_catalog_roles.issubset(catalog_roles) and len(catalog_roles) in {3, 4}
        if not valid_catalog_roles or len(catalog_roles) != len(catalog_items) or catalog_roles - {"champions", "augments", "versions", "augment_assets"} or any(
            item.catalog_generation_id != catalog_id
            or item.run_id != catalog_id
            or item.manifest_sha256 != catalog_manifest_sha
            for item in catalog_items
        ):
            raise RuntimeError("generation Catalog provenance 与 candidate pointer 不一致")

    def _failure_state(self, previous: RefreshSourceState, payload: Mapping[str, Any]) -> RefreshSourceState:
        now = self.now()
        blocked = is_blocked_failure(payload)
        delay = timedelta(hours=6) if blocked else timedelta(minutes=30)
        if not blocked:
            seed = hashlib.blake2b(json.dumps(payload, sort_keys=True).encode("utf-8"), digest_size=2).digest()
            delay += timedelta(seconds=int.from_bytes(seed, "big") % 301)
        return replace(
            previous,
            last_attempt_at=iso_utc(now),
            next_due_at=iso_utc(now + delay),
            failure_kind=refresh_failure_kind(payload, blocked=blocked),
            state="backoff",
        )

    def _promote_targets(
        self,
        targets: Mapping[str, Mapping[str, Any]],
        *,
        degraded_sources: set[str],
        pending_sources: set[str],
        refreshed_sources: list[str],
    ) -> tuple[Any, dict[str, Any]]:
        return promote_targets(
            self,
            targets,
            degraded_sources=degraded_sources,
            pending_sources=pending_sources,
            refreshed_sources=refreshed_sources,
        )

    def _save_schedule_progress(
        self,
        states: dict[str, RefreshSourceState],
        *,
        attempted_sources: set[str],
        failures: Mapping[str, Mapping[str, Any]],
        targets: Mapping[str, Mapping[str, Any]],
        generation_id: str,
    ) -> None:
        completed = self.now()
        for source in self._ordered_sources(attempted_sources):
            if source in failures:
                states[source] = self._failure_state(states[source], failures[source])
                continue
            pointer = targets.get(source) or {}
            if not pointer:
                continue
            normal_due = completed + SOURCE_INTERVALS[source]
            states[source] = RefreshSourceState(
                last_attempt_at=iso_utc(completed),
                last_success_at=str(pointer.get("last_success_at") or iso_utc(completed)),
                next_due_at=iso_utc(normal_due),
                failure_kind="",
                current_run_id=(
                    self._pointer_identity(pointer)[0]
                    if source != "catalog"
                    else str(pointer.get("catalog_generation_id") or "")
                ),
                state="ready",
            )
        schedule = RefreshScheduleV1(
            updated_at=iso_utc(completed),
            generation_id=generation_id,
            sources=states,
        )
        self.schedule_store.save(schedule)
        refresh_recovery_schedule(self.root, schedule)
__all__ = [
    "CohortRefreshCoordinator",
    "RefreshStopRequested",
    "SOURCE_INTERVALS",
    "SOURCE_TIMEOUTS",
]

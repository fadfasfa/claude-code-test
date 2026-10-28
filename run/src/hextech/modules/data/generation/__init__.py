"""Production v4 releases: immutable unit references and one atomic head."""
from __future__ import annotations
import uuid
import shutil
import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING
from hextech.contracts import SourceProvenance, SourceStatusV2
from hextech.contracts.release import ReleaseManifest
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.freshness import SOURCE_INTERVALS, parse_refresh_time
from .validation import SnapshotValidationError, champion_detail_complete
from .release_store import closure, fingerprint, object_at, checked_file, read_head, update_head, HEAD_LOCK
SNAPSHOT_SCHEMA_VERSION = 4
DataSnapshotManifest = ReleaseManifest

if TYPE_CHECKING:
    from .query import ReleaseQuery

def default_snapshot_root():
    return get_var_dir() / "snapshots"

def _read_json(path):
    return object_at(path)

class DataSnapshotPublisher:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_snapshot_root()
        self.generations_dir = self.root / "generations"
        self.staging_dir = self.root / "staging"
        self.current_path = self.root / "current.v4.json"
        # Same physical head: previous is never independently committed.
        self.previous_path = self.current_path
        self.last_promotion_disposition = "unknown"

    def current_generation_id(self):
        try:
            return DataSnapshotClient(self.root).open_view().manifest.generation_id
        except (OSError, ValueError):
            return ""

    def _candidate(self, *, source_files, components, source_status=None, health="healthy", degraded_sources=(), **kwargs):
        if components is None:
            raise SnapshotValidationError("v4 requires explicit unit components; legacy payload publication retired")
        refs = tuple(sorted((p if isinstance(p, SourceProvenance) else SourceProvenance.from_mapping(p) for p in source_files),
                            key=lambda p: (p.source, p.run_id, p.artifact_role)))
        states = {k: v if isinstance(v, SourceStatusV2) else SourceStatusV2.from_mapping(v) for k, v in (source_status or {}).items()}
        digest = fingerprint(refs, components, states, health, degraded_sources)
        return ReleaseManifest(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:10],
            datetime.now(timezone.utc).isoformat(), digest, refs, deepcopy(dict(components)), states,
            health, tuple(degraded_sources))

    def matching_current_manifest(self, payloads=None, *, source_files=(), components=None, **kwargs):
        candidate = self._candidate(source_files=source_files, components=components, **kwargs)
        try:
            current = DataSnapshotClient(self.root).open_view()
        except (ValueError, OSError):
            return None
        return current.manifest if not current.degraded and current.manifest.content_fingerprint == candidate.content_fingerprint else None

    def publish(self, payloads=None, *, source_files=(), components=None, **kwargs):
        with HEAD_LOCK:
            return self._publish_locked(payloads, source_files=source_files, components=components, **kwargs)

    def _publish_locked(self, payloads=None, *, source_files=(), components=None, **kwargs):
        candidate = self._candidate(source_files=source_files, components=components, **kwargs)
        catalog, _, _, _ = closure(self.root.parent, candidate)
        try:
            current = DataSnapshotClient(self.root).open_view()
        except (OSError, ValueError):
            current = None
        if current is not None and not current.degraded and current.manifest.content_fingerprint == candidate.content_fingerprint:
            self.last_promotion_disposition = "unchanged"
            return current.manifest
        directory = self.generations_dir / candidate.generation_id
        directory.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write_json(directory / "manifest.json", candidate.to_dict(), ensure_ascii=False, indent=2)
            DataSnapshotClient(self.root).open_generation(candidate.generation_id)
            # One replace is the only commit point; source and Catalog pointers are not rewritten.
            head = read_head(self.root) if self.current_path.exists() else {}
            recognition = None if head.get("recognition_catalog") else {
                "schema_version": 2, "catalog_generation_id": catalog.generation_id,
                "content_sha256": catalog.content_sha256, "manifest_sha256": catalog.manifest_sha256}
            update_head(self.root, current_generation_id=candidate.generation_id,
                previous_generation_id=current.manifest.generation_id if current else "", recognition_catalog=recognition)
        except BaseException as exc:
            # An exception after replace is not proof that commit failed. Never
            # delete a directory that the committed atomic head already owns.
            try:
                head = read_head(self.root) if self.current_path.exists() else {}
            except (OSError, ValueError):
                raise exc
            if head.get("current_generation_id") == candidate.generation_id:
                self.last_promotion_disposition = "published"
                if isinstance(exc, Exception):
                    return candidate
                raise
            if head.get("previous_generation_id") != candidate.generation_id:
                shutil.rmtree(directory)
            raise
        self.last_promotion_disposition = "published"
        return candidate

@dataclass(frozen=True)
class DataSnapshotView:
    """固定在一次完整 generation 上的只读查询视图。"""

    manifest: DataSnapshotManifest
    _payloads: ReleaseQuery
    degraded: bool = False
    failed_generation_id: str = ""
    _runtime_root: Path | None = None

    def _runtime_check_states(self) -> Mapping[str, Mapping[str, Any]]:
        """读取 snapshot 同一 runtime 的易变检测证据；失败时不虚构 fresh。"""

        if self._runtime_root is None:
            return {}
        path = self._runtime_root / "state" / "data-service" / "refresh_schedule.v1.json"
        try:
            payload = _read_json(path)
        except SnapshotValidationError:
            return {}
        sources = payload.get("sources") if isinstance(payload, Mapping) else None
        if not isinstance(sources, Mapping):
            return {}
        return {
            str(source): state
            for source, state in sources.items()
            if isinstance(source, str) and isinstance(state, Mapping)
        }

    def _bound_check_state(
        self,
        source: str,
        projected: Mapping[str, Any],
        state: Mapping[str, Any] | None,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """只接受与本 snapshot 实际来源绑定的上游检查证据。"""

        unknown = {
            "check_status": "unknown",
            "upstream_revision": "",
            "applied_revision": "",
            "last_checked_at": "",
            "check_interval_seconds": int(SOURCE_INTERVALS.get(source, timedelta()).total_seconds()),
            "check_evidence_bound": False,
        }
        if not isinstance(state, Mapping):
            return unknown
        current_run_id = str(state.get("current_run_id") or "")
        projected_run_id = str(
            projected.get("run_id")
            or (projected.get("catalog_id") if source == "catalog" else "")
            or ""
        )
        actual_run_ids = {
            item.run_id for item in self.manifest.source_files if item.source == source
        }
        if (
            not current_run_id
            or not projected_run_id
            or current_run_id != projected_run_id
            or current_run_id not in actual_run_ids
        ):
            return unknown
        check_status = str(state.get("check_status") or "unknown")
        if check_status not in {"up_to_date", "changed", "unknown", "failed", "never_checked"}:
            return unknown
        upstream_revision = str(state.get("upstream_revision") or "")
        applied_revision = str(state.get("applied_revision") or "")
        last_checked_at = str(state.get("last_checked_at") or "")
        try:
            interval_seconds = int(state.get("check_interval_seconds") or 0)
        except (TypeError, ValueError):
            return unknown
        expected_interval = int(SOURCE_INTERVALS.get(source, timedelta()).total_seconds())
        if interval_seconds <= 0 or interval_seconds != expected_interval:
            return unknown
        check_time = parse_refresh_time(last_checked_at)
        observation = observed_at or datetime.now(timezone.utc)
        if observation.tzinfo is None:
            observation = observation.replace(tzinfo=timezone.utc)
        checked = check_time is not None and check_time <= observation + timedelta(minutes=5)
        if check_status == "up_to_date" and (
            not checked
            or not upstream_revision
            or upstream_revision != applied_revision
        ):
            return unknown
        if check_status == "up_to_date" and source in {"aramkit", "catalog"}:
            ranking = self.manifest.components.get("ranking")
            if not isinstance(ranking, Mapping):
                return unknown
            if source == "aramkit" and (
                str(ranking.get("run_id") or "") != current_run_id
                or str(ranking.get("source_version") or "") != applied_revision
            ):
                return unknown
            if source == "catalog" and str(ranking.get("catalog_id") or "") != applied_revision:
                return unknown
        if check_status == "changed" and (
            not checked
            or not upstream_revision
            or not applied_revision
            or upstream_revision == applied_revision
        ):
            return unknown
        if check_status in {"unknown", "never_checked"}:
            return {
                **unknown,
                "check_status": check_status,
                "check_interval_seconds": interval_seconds,
            }
        return {
            "check_status": check_status,
            "upstream_revision": upstream_revision,
            "applied_revision": applied_revision,
            "last_checked_at": last_checked_at,
            "check_interval_seconds": interval_seconds,
            "check_evidence_bound": True,
        }

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        # The observation clock validates check evidence, never expires business data by age.
        source_status: dict[str, dict[str, Any]] = {}
        effective_degraded_sources = set(self.manifest.degraded_sources)
        runtime_states = self._runtime_check_states()
        for source, value in self.manifest.source_status.items():
            projected = value.to_dict()
            raw_data_at = str(projected.get("data_at") or "").strip()
            data_at = raw_data_at or self.manifest.created_at
            if not raw_data_at and data_at:
                # 旧 generation 没有 data_at；统一把 created_at 作为可见回退，
                # 让推荐、桌面和报告使用同一时间，而不是各自猜测。
                projected["data_at"] = data_at
            evidence = self._bound_check_state(source, projected, runtime_states.get(source), now)
            projected.update(evidence)
            check_status = str(evidence["check_status"])
            if str(projected.get("data_reason") or "") == "source_data_expired":
                # 旧构建曾把“数据生成时间超过检测周期”固化为 stale。该原因
                # 没有证明上游变化，读取时先还原为有效数据，再由检查证据投影。
                projected.update(
                    freshness="fresh",
                    data_status="fresh",
                    data_reason="",
                    stale_age_seconds=0,
                )
            data_status = str(projected.get("data_status") or "unknown")
            if check_status == "changed" and data_status not in {
                "pending",
                "confirmed_empty",
                "unavailable",
            }:
                projected.update(
                    freshness="last_good",
                    data_status="data_stale",
                    data_reason="upstream_revision_changed",
                )
            elif check_status == "failed":
                if str(projected.get("freshness") or "unknown") == "fresh":
                    projected["freshness"] = "last_good"
                if not str(projected.get("data_reason") or ""):
                    projected["data_reason"] = "upstream_check_failed"
            elif check_status in {"unknown", "never_checked"}:
                if str(projected.get("freshness") or "unknown") == "fresh":
                    projected["freshness"] = "unknown"
                if not str(projected.get("data_reason") or ""):
                    projected["data_reason"] = "upstream_check_unknown"
            # Catalog 是否过期由 adoption-held 独立门处理；它不是用户统计来源。
            # manifest 若明确把 Catalog 标为 degraded，初始化集合仍会保留该 lineage。
            if source != "catalog" and (
                str(projected.get("freshness") or "unknown") != "fresh"
                or str(projected.get("data_status") or "unknown") != "fresh"
            ):
                effective_degraded_sources.add(source)
            source_status[source] = projected
        return {
            "state": "degraded" if self.degraded or self.manifest.health == "degraded" else "ready",
            "generation_id": self.manifest.generation_id,
            "failed_generation_id": self.failed_generation_id,
            "content_fingerprint": self.manifest.content_fingerprint,
            "created_at": self.manifest.created_at,
            "health": self.manifest.health,
            "refreshed_sources": list(self.manifest.refreshed_sources),
            "degraded_sources": list(self.manifest.degraded_sources),
            # manifest 字段是发布时 immutable lineage；effective 字段叠加读取时
            # 的绝对时效，供 Desktop 与严格验收消费，二者不能互相覆盖。
            "effective_degraded_sources": sorted(effective_degraded_sources),
            "source_status": source_status,
            "components": deepcopy(dict(self.manifest.components)),
        }

    def get_champions(self):
        return self._payloads.champions()
    def get_champion(self, value):
        return self._payloads.champion(value)
    def get_champion_detail(self, value):
        return self._payloads.detail(value)
    def is_champion_complete(self, value):
        row = self.get_champion(value)
        if row is None:
            return False
        component = self.manifest.components["champions"].get(row["id"], {})
        return component.get("complete") is True and champion_detail_complete(self.get_champion_detail(row["id"]))
    def get_champion_augments(self, value):
        detail = self.get_champion_detail(value)
        return detail.get("augments", []) if detail else []
    def get_synergy_data(self):
        return self._payloads.synergy()
    def get_combo_stats(self, champion, augment):
        identity = self.resolve_augment(augment)
        if identity is None:
            return None
        return next((r for r in self.get_champion_augments(champion) if r["id"] == identity["canonical_id"]), None)
    def resolve_augment(self, value):
        return self._payloads.resolve(value)
    def get_overlay_hints(self):
        return self._payloads.hints()
    def get_overlay_display_hints(self):
        payload = self._payloads.hints()
        return {"schema_version": 1, "source": deepcopy(payload.get("source", {})),
                "name_index": deepcopy(payload.get("name_index", {})),
                "hints": {key: {field: deepcopy(row[field]) for field in ("augment_id", "name", "tier", "synergies") if field in row}
                          for key, row in payload.get("hints", {}).items()}}
    def get_identity_indexes(self):
        return self._payloads.identities()

class DataSnapshotClient:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else default_snapshot_root()
        # Keep only this client's last two proven releases. Recognition's independently
        # moving head is deliberately not part of the statistics query cache.
        self._query_cache = OrderedDict()
        self._query_lock = threading.RLock()
    def _pointer(self):
        return read_head(self.root)
    def _load_generation(self, generation_id):
        from hextech.contracts.data_validation import require_identifier
        require_identifier(generation_id, field_name="generation_id")
        with self._query_lock:
            path = checked_file(self.root, f"generations/{generation_id}/manifest.json")
            try:
                raw = path.read_bytes()
                key = (self.root.resolve(strict=True), generation_id, hashlib.sha256(raw).digest())
                cached = self._query_cache.get(key)
                if cached is None:
                    manifest = ReleaseManifest.from_mapping(json.loads(raw))
                    if manifest.generation_id != generation_id:
                        raise ValueError("release directory identity mismatch")
                    if fingerprint(manifest.source_files, manifest.components, manifest.source_status,
                                   manifest.health, manifest.degraded_sources) != manifest.content_fingerprint:
                        raise ValueError("release content fingerprint mismatch")
                    from .query import ReleaseQuery
                    cached = ReleaseQuery(self.root.parent.absolute(), manifest)
                    # A changed manifest for the same generation is a new proof, not
                    # permission to reuse any of the old query's projections.
                    for old in tuple(self._query_cache):
                        if old[:2] == key[:2]:
                            del self._query_cache[old]
                    self._query_cache[key] = cached
                    while len(self._query_cache) > 2:
                        self._query_cache.popitem(last=False)
                self._query_cache.move_to_end(key)
                # Frozen dataclasses still contain mutable mappings. Public views
                # must never expose the manifest that owns the cached query.
                # Provenance is a tuple of frozen scalar-only DTOs; reuse it
                # while copying every mutable mapping (including status.coverage).
                manifest = cached.manifest
                return deepcopy(manifest, {id(manifest.source_files): manifest.source_files}), cached
            except (KeyError, TypeError, ValueError, OSError) as exc:
                raise SnapshotValidationError(str(exc)) from exc
    def _load(self):
        head = self._pointer()
        try:
            return (*self._load_generation(head["current_generation_id"]), False, "")
        except (ValueError, OSError):
            if not head["previous_generation_id"]:
                raise
            return (*self._load_generation(head["previous_generation_id"]), True, head["current_generation_id"])
    def open_view(self):
        manifest, payloads, degraded, failed = self._load()
        return DataSnapshotView(manifest, payloads, degraded, failed, self.root.parent)
    def open_generation(self, generation_id):
        manifest, payloads = self._load_generation(str(generation_id))
        return DataSnapshotView(manifest, payloads, _runtime_root=self.root.parent)
    def status(self, now=None):
        try:
            return self.open_view().status(now=now)
        except (OSError, ValueError) as exc:
            reason = "migration_required" if (self.root / "current.v2.json").exists() else str(exc)
            return {"state": "unavailable", "generation_id": "", "reason": reason}
    def load_manifest(self):
        return self.open_view().manifest
    def get_champions(self, *args, **kwargs):
        return self.open_view().get_champions(*args, **kwargs)
    def get_champion(self, *args, **kwargs):
        return self.open_view().get_champion(*args, **kwargs)
    def get_champion_detail(self, *args, **kwargs):
        return self.open_view().get_champion_detail(*args, **kwargs)
    def get_champion_augments(self, *args, **kwargs):
        return self.open_view().get_champion_augments(*args, **kwargs)
    def get_synergy_data(self, *args, **kwargs):
        return self.open_view().get_synergy_data(*args, **kwargs)
    def get_combo_stats(self, *args, **kwargs):
        return self.open_view().get_combo_stats(*args, **kwargs)
    def resolve_augment(self, *args, **kwargs):
        return self.open_view().resolve_augment(*args, **kwargs)
    def get_overlay_hints(self, *args, **kwargs):
        return self.open_view().get_overlay_hints(*args, **kwargs)
    def get_identity_indexes(self, *args, **kwargs):
        return self.open_view().get_identity_indexes(*args, **kwargs)

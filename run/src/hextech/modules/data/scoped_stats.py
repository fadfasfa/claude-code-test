"""Overlay 使用的 ARAMKit 单英雄阶段统计只读视图。

本模块从固定 generation 的 provenance 定位 immutable source run，先校验 run
manifest 与 scoped index，再只读取当前英雄文件。它不抓取网络、不切换 current、
不参与 Vision；损坏或缺失统一返回可诊断结果，调用方可安全回退 Blitz tier。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from hextech.contracts import DataContractError, SourceProvenance, SourceRunManifestV2
from hextech.modules.data.ports import SnapshotViewPort
from hextech.modules.data.source_runs import source_run_dir


SCOPED_STATS_SCHEMA_VERSION = 1
DEFAULT_SCOPED_STATS_CACHE_CAPACITY = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ScopedStatsError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "scoped_stats_invalid")
        self.detail = str(detail or "")
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


def _sha256_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
    except OSError as exc:
        raise ScopedStatsError("scoped_stats_file_missing", str(path)) from exc
    return digest.hexdigest(), size


def _read_mapping(path: Path, *, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScopedStatsError(code, str(path)) from exc
    if not isinstance(payload, Mapping):
        raise ScopedStatsError(code, str(path))
    return dict(payload)


def _safe_child(root: Path, relative_path: object, *, code: str) -> Path:
    relative = str(relative_path or "").replace("\\", "/")
    if not relative or relative.startswith("/") or ".." in relative.split("/"):
        raise ScopedStatsError(code, relative)
    resolved_root = root.resolve()
    target = (resolved_root / relative).resolve()
    if resolved_root not in target.parents:
        raise ScopedStatsError(code, relative)
    return target


def _positive_id(value: object, *, code: str) -> str:
    raw = str(value or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        raise ScopedStatsError(code, raw)
    return str(int(raw))


def _nonnegative_int(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScopedStatsError(code, str(value))
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ScopedStatsError(code, str(value)) from exc
    if not numeric.is_integer() or numeric < 0:
        raise ScopedStatsError(code, str(value))
    return int(numeric)


def _positive_int(value: object, *, code: str) -> int:
    parsed = _nonnegative_int(value, code=code)
    if parsed <= 0:
        raise ScopedStatsError(code, str(value))
    return parsed


def _rate(value: object, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ScopedStatsError(code, str(value))
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ScopedStatsError(code, str(value)) from exc
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ScopedStatsError(code, str(value))
    return parsed


def _normalize_record(raw: object, *, scope: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ScopedStatsError("scoped_stats_record_invalid", scope)
    augment_id = _positive_id(raw.get("id"), code="scoped_stats_augment_id_invalid")
    result: dict[str, Any] = {
        "id": augment_id,
        "source_rank": _positive_int(raw.get("source_rank"), code="scoped_stats_rank_invalid"),
        "sample_count": _nonnegative_int(raw.get("sample_count"), code="scoped_stats_sample_invalid"),
    }
    for field in ("win_rate", "pick_rate", "blue_win_rate", "red_win_rate"):
        result[field] = _rate(raw.get(field), code=f"scoped_stats_{field}_invalid")
    if scope == "all":
        stage_agnostic = raw.get("stage_agnostic")
        if not isinstance(stage_agnostic, bool):
            raise ScopedStatsError("scoped_stats_stage_agnostic_invalid", augment_id)
        available = raw.get("available_stages")
        if not isinstance(available, list):
            raise ScopedStatsError("scoped_stats_available_stages_invalid", augment_id)
        normalized_stages = [str(item) for item in available]
        if any(item not in {"1", "2", "3", "4"} for item in normalized_stages):
            raise ScopedStatsError("scoped_stats_available_stages_invalid", augment_id)
        result["stage_agnostic"] = stage_agnostic
        result["available_stages"] = normalized_stages
    return result


def _index_records(rows: object, *, scope: str) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(rows, list):
        raise ScopedStatsError("scoped_stats_scope_invalid", scope)
    indexed: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        record = _normalize_record(raw, scope=scope)
        augment_id = str(record["id"])
        if augment_id in indexed:
            raise ScopedStatsError("scoped_stats_duplicate_augment", f"{scope}/{augment_id}")
        indexed[augment_id] = MappingProxyType(record)
    return MappingProxyType(indexed)


def _snapshot_provenance(snapshot_view: SnapshotViewPort) -> tuple[str, SourceProvenance | None]:
    status = snapshot_view.status()
    generation_id = str(status.get("generation_id") or "") if isinstance(status, Mapping) else ""
    manifest = getattr(snapshot_view, "manifest", None)
    source_files = getattr(manifest, "source_files", ())
    for item in source_files:
        if getattr(item, "source", "") == "aramkit" and getattr(item, "artifact_role", "") == "scoped_stats":
            return generation_id, item
    return generation_id, None


@dataclass(frozen=True)
class ScopedStatsSelection:
    record: Mapping[str, Any] | None
    requested_stage: int | None
    stats_scope: str
    scope_label: str
    fallback_reason: str = ""

    def to_stats_dict(self) -> dict[str, Any]:
        if self.record is None:
            return {}
        sample_count = int(self.record.get("sample_count") or 0)
        quality = "insufficient" if sample_count < 100 else "low" if sample_count < 1000 else "normal"
        return {
            **dict(self.record),
            "winrate": self.record.get("win_rate"),
            "pickrate": self.record.get("pick_rate"),
            "stats_source": "aramkit",
            "stats_scope": self.stats_scope,
            "scope_label": self.scope_label,
            "requested_stage": self.requested_stage,
            "sample_quality": quality,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class ScopedStatsView:
    generation_id: str
    run_id: str
    champion_id: str
    version: str
    data_path: str
    all_stats: Mapping[str, Mapping[str, Any]]
    stage_stats: Mapping[int, Mapping[str, Mapping[str, Any]]]

    def select(self, augment_id: object, stage: int | None) -> ScopedStatsSelection:
        canonical_id = str(augment_id or "").strip()
        requested_stage = stage if stage in {1, 2, 3, 4} else None
        if requested_stage is not None:
            record = self.stage_stats.get(requested_stage, {}).get(canonical_id)
            if record is not None:
                return ScopedStatsSelection(
                    record=record,
                    requested_stage=requested_stage,
                    stats_scope=f"stage_{requested_stage}",
                    scope_label=f"阶段 {requested_stage}",
                )
        all_record = self.all_stats.get(canonical_id)
        if all_record is not None:
            return ScopedStatsSelection(
                record=all_record,
                requested_stage=requested_stage,
                stats_scope="all",
                scope_label="综合",
                fallback_reason=(
                    "stage_stat_missing_fallback_all"
                    if requested_stage is not None
                    else "stage_unknown_fallback_all"
                ),
            )
        return ScopedStatsSelection(
            record=None,
            requested_stage=requested_stage,
            stats_scope="missing",
            scope_label="",
            fallback_reason="aramkit_augment_missing",
        )

    def status(self) -> dict[str, Any]:
        return {
            "available": True,
            "generation_id": self.generation_id,
            "run_id": self.run_id,
            "champion_id": self.champion_id,
            "version": self.version,
            "data_path": self.data_path,
            "all_record_count": len(self.all_stats),
            "stage_record_counts": {str(stage): len(rows) for stage, rows in self.stage_stats.items()},
        }


@dataclass(frozen=True)
class ScopedStatsLoadResult:
    view: ScopedStatsView | None
    generation_id: str
    run_id: str
    champion_id: str
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.view is not None


class ScopedStatsCache:
    """容量固定为 2 的单英雄 LRU；失败结果也缓存，避免每帧重复读盘。"""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_SCOPED_STATS_CACHE_CAPACITY,
        run_dir_resolver: Callable[[str], Path] | None = None,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self._run_dir_resolver = run_dir_resolver or (lambda run_id: source_run_dir("aramkit", run_id))
        self._cache: OrderedDict[tuple[str, str, str], ScopedStatsLoadResult] = OrderedDict()

    def load(self, snapshot_view: SnapshotViewPort, champion_id: object) -> ScopedStatsLoadResult:
        generation_id, provenance = _snapshot_provenance(snapshot_view)
        champion_key = str(champion_id or "").strip()
        run_id = str(getattr(provenance, "run_id", "") or "")
        key = (generation_id, run_id, champion_key)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if provenance is None:
            result = ScopedStatsLoadResult(None, generation_id, "", champion_key, "aramkit_provenance_missing")
        elif not champion_key:
            result = ScopedStatsLoadResult(None, generation_id, run_id, champion_key, "champion_context_missing")
        else:
            try:
                result = self._load_view(generation_id, provenance, champion_key)
            except ScopedStatsError as exc:
                result = ScopedStatsLoadResult(None, generation_id, run_id, champion_key, exc.code)
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        return result

    def _load_view(
        self,
        generation_id: str,
        provenance: SourceProvenance,
        champion_id: str,
    ) -> ScopedStatsLoadResult:
        run_id = provenance.run_id
        run_dir = self._run_dir_resolver(run_id).resolve()
        manifest_path = run_dir / "manifest.json"
        manifest_sha, _ = _sha256_size(manifest_path)
        if manifest_sha != provenance.manifest_sha256:
            raise ScopedStatsError("aramkit_run_manifest_hash_mismatch", run_id)
        manifest_payload = _read_mapping(manifest_path, code="aramkit_run_manifest_invalid")
        try:
            manifest = SourceRunManifestV2.from_mapping(manifest_payload)
        except (DataContractError, TypeError, ValueError) as exc:
            raise ScopedStatsError("aramkit_run_manifest_invalid", run_id) from exc
        artifact = manifest.artifact
        if (
            manifest.source != "aramkit"
            or manifest.run_id != run_id
            or manifest.catalog_generation_id != provenance.catalog_generation_id
            or not manifest.publishable
            or artifact is None
            or artifact.role != "scoped_stats"
            or artifact.sha256 != provenance.artifact_sha256
            or artifact.record_count != provenance.record_count
            or artifact.content_schema_version != provenance.content_schema_version
        ):
            raise ScopedStatsError("aramkit_provenance_mismatch", run_id)

        index_path = _safe_child(run_dir, artifact.relative_path, code="aramkit_scoped_index_path_invalid")
        index_sha, index_size = _sha256_size(index_path)
        if index_sha != artifact.sha256 or index_size != artifact.size:
            raise ScopedStatsError("aramkit_scoped_index_hash_mismatch", run_id)
        index = _read_mapping(index_path, code="aramkit_scoped_index_invalid")
        if (
            index.get("schema_version") != SCOPED_STATS_SCHEMA_VERSION
            or str(index.get("source") or "") != "aramkit"
            or str(index.get("dataset") or "") != "all"
        ):
            raise ScopedStatsError("aramkit_scoped_index_schema_invalid", run_id)
        files = index.get("files")
        if not isinstance(files, list) or len(files) != _nonnegative_int(
            index.get("champion_count"), code="aramkit_scoped_index_count_invalid"
        ):
            raise ScopedStatsError("aramkit_scoped_index_incomplete", run_id)
        data_path = str(index.get("data_path") or "").strip()
        if not data_path:
            raise ScopedStatsError("aramkit_scoped_index_data_path_missing", run_id)

        selected: Mapping[str, Any] | None = None
        seen: set[str] = set()
        records = 0
        for raw in files:
            if not isinstance(raw, Mapping):
                raise ScopedStatsError("aramkit_scoped_descriptor_invalid", run_id)
            descriptor_champion = _positive_id(
                raw.get("champion_id"), code="aramkit_scoped_descriptor_champion_invalid"
            )
            if descriptor_champion in seen:
                raise ScopedStatsError("aramkit_scoped_descriptor_duplicate", descriptor_champion)
            seen.add(descriptor_champion)
            relative = str(raw.get("relative_path") or "").replace("\\", "/")
            _safe_child(index_path.parent, relative, code="aramkit_scoped_child_path_invalid")
            sha256 = str(raw.get("sha256") or "").lower()
            if not _SHA256_RE.fullmatch(sha256):
                raise ScopedStatsError("aramkit_scoped_child_hash_invalid", descriptor_champion)
            _nonnegative_int(raw.get("size"), code="aramkit_scoped_child_size_invalid")
            record_count = _nonnegative_int(
                raw.get("record_count"), code="aramkit_scoped_child_count_invalid"
            )
            records += record_count
            if str(raw.get("data_path") or "") != data_path:
                raise ScopedStatsError("aramkit_scoped_child_data_path_mismatch", descriptor_champion)
            if descriptor_champion == champion_id:
                selected = raw
        if records != _nonnegative_int(index.get("record_count"), code="aramkit_scoped_index_count_invalid"):
            raise ScopedStatsError("aramkit_scoped_index_record_mismatch", run_id)
        if records != artifact.record_count:
            raise ScopedStatsError("aramkit_scoped_artifact_record_mismatch", run_id)
        if selected is None:
            return ScopedStatsLoadResult(None, generation_id, run_id, champion_id, "aramkit_champion_missing")

        champion_path = _safe_child(
            index_path.parent,
            selected.get("relative_path"),
            code="aramkit_scoped_child_path_invalid",
        )
        child_sha, child_size = _sha256_size(champion_path)
        if child_sha != str(selected.get("sha256") or "") or child_size != int(selected.get("size") or -1):
            raise ScopedStatsError("aramkit_scoped_child_hash_mismatch", champion_id)
        payload = _read_mapping(champion_path, code="aramkit_scoped_child_invalid")
        champion = payload.get("champion")
        stages = payload.get("stages")
        if (
            payload.get("schema_version") != SCOPED_STATS_SCHEMA_VERSION
            or str(payload.get("source") or "") != "aramkit"
            or str(payload.get("dataset") or "") != "all"
            or str(payload.get("data_path") or "") != data_path
            or not isinstance(champion, Mapping)
            or _positive_id(champion.get("id"), code="aramkit_scoped_payload_champion_invalid") != champion_id
            or not isinstance(stages, Mapping)
            or set(str(key) for key in stages) != {"1", "2", "3", "4"}
        ):
            raise ScopedStatsError("aramkit_scoped_child_schema_invalid", champion_id)
        all_stats = _index_records(payload.get("all"), scope="all")
        if len(all_stats) != int(selected.get("record_count") or -1):
            raise ScopedStatsError("aramkit_scoped_child_record_mismatch", champion_id)
        stage_stats = MappingProxyType(
            {
                stage: _index_records(stages.get(str(stage)), scope=f"stage_{stage}")
                for stage in (1, 2, 3, 4)
            }
        )
        view = ScopedStatsView(
            generation_id=generation_id,
            run_id=run_id,
            champion_id=champion_id,
            version=str(payload.get("version") or index.get("version") or ""),
            data_path=data_path,
            all_stats=all_stats,
            stage_stats=stage_stats,
        )
        return ScopedStatsLoadResult(view, generation_id, run_id, champion_id)

    def status(self) -> dict[str, Any]:
        return {
            "capacity": self.capacity,
            "size": len(self._cache),
            "keys": [list(key) for key in self._cache],
        }


__all__ = [
    "DEFAULT_SCOPED_STATS_CACHE_CAPACITY",
    "SCOPED_STATS_SCHEMA_VERSION",
    "ScopedStatsCache",
    "ScopedStatsError",
    "ScopedStatsLoadResult",
    "ScopedStatsSelection",
    "ScopedStatsView",
]

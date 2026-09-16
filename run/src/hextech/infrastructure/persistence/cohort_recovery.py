"""完整 cohort 的验证、重建与单调恢复点。

冻结包启动和 DataService promotion 都通过这里把 immutable generation 还原为精确
Catalog/来源 pointer。模块不选择 bundle、不刷新网络，也不会只凭 recovery point
宣称数据健康；每次使用恢复点都重新执行完整哈希与 provenance 校验。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import (
    CatalogManifestV2,
    CohortRecoveryPointV1,
    RefreshScheduleV1,
    RefreshSourceState,
    SourcePointerV2,
    SourceRunManifestV2,
    utc_now_iso,
)
from hextech.infrastructure.persistence.refresh_schedule import SCHEDULE_SOURCES
from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.validation import validate_complete_provenance
from hextech.modules.data.ports.atomic import atomic_write_json


SOURCE_ROLES = ("aramkit", "blitz", "apex", "mayhem")


@dataclass(frozen=True)
class CohortCandidate:
    generation_id: str
    generation_created_at: str
    manifest_health: str
    pointers: Mapping[str, Mapping[str, Any]]
    production_pool_id: str
    production_pool_count: int
    snapshot_schema_version: int = 2
    units: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def sort_time(self) -> datetime:
        return parse_utc(self.generation_created_at)


def recovery_point_path(root: str | Path) -> Path:
    return Path(root) / "state" / "data-service" / "cohort_recovery_point.v1.json"


def parse_utc(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("cohort 时间不能为空")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cohort JSON 无法读取：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"cohort JSON 必须是对象：{path}")
    return payload


def _catalog_pointer(root: Path, catalog_id: str, *, expected_manifest_sha: str) -> tuple[dict[str, Any], CatalogManifestV2]:
    catalog_root = root / "catalog" / "generations" / catalog_id
    manifest_path = catalog_root / "manifest.json"
    manifest = CatalogManifestV2.from_mapping(_read_object(manifest_path))
    actual_manifest_sha = sha256_file(manifest_path)
    if (
        manifest.catalog_generation_id != catalog_id
        or actual_manifest_sha != expected_manifest_sha
    ):
        raise ValueError("Catalog provenance 与 immutable manifest 不一致")
    validate_catalog_files(catalog_root, manifest)
    return (
        {
            "schema_version": 2,
            "catalog_generation_id": catalog_id,
            "content_sha256": manifest.content_sha256,
            "manifest_sha256": actual_manifest_sha,
            "completed_at": manifest.created_at,
            "last_success_at": manifest.created_at,
        },
        manifest,
    )


def validate_generation_cohort(root: str | Path, generation_id: str) -> CohortCandidate:
    """从 immutable generation/provenance 重建并验证一整套 cohort pointer。"""

    runtime = Path(root)
    view = DataSnapshotClient(runtime / "snapshots").open_generation(generation_id)
    validate_complete_provenance(view.manifest.source_files, schema_version=view.manifest.schema_version)
    hints = view.get_overlay_hints()
    source = hints.get("source") if isinstance(hints, Mapping) else None
    pool = source.get("production_augment_pool") if isinstance(source, Mapping) else None
    if not isinstance(pool, Mapping):
        raise ValueError("generation 缺少 production_augment_pool")
    validate_production_augment_pool(pool)
    catalog_id = str(pool.get("catalog_generation_id") or "")
    catalog_provenance = [item for item in view.manifest.source_files if item.source == "catalog"]
    if not catalog_id or not catalog_provenance:
        raise ValueError("generation 缺少 Catalog provenance")
    manifest_shas = {item.manifest_sha256 for item in catalog_provenance}
    if len(manifest_shas) != 1:
        raise ValueError("generation Catalog provenance 摘要不唯一")
    catalog_pointer, catalog_manifest = _catalog_pointer(
        runtime,
        catalog_id,
        expected_manifest_sha=next(iter(manifest_shas)),
    )
    descriptors = {item.role: item for item in catalog_manifest.files}
    if (
        len(catalog_provenance) != len(descriptors)
        or any(
            item.run_id != catalog_id
            or item.catalog_generation_id != catalog_id
            or item.artifact_role not in descriptors
            or item.artifact_sha256 != descriptors[item.artifact_role].sha256
            or item.record_count != descriptors[item.artifact_role].record_count
            or item.content_schema_version != descriptors[item.artifact_role].content_schema_version
            for item in catalog_provenance
        )
    ):
        raise ValueError("generation Catalog provenance 不完整")
    if (
        str(pool.get("catalog_sha256") or "") != catalog_manifest.content_sha256
        or str(pool.get("catalog_manifest_sha256") or "") != str(catalog_pointer["manifest_sha256"])
        or int(pool.get("enabled_count") or 0) != len(pool.get("canonical_ids") or ())
        or len(pool.get("identities") or ()) != len(pool.get("canonical_ids") or ())
    ):
        raise ValueError("generation production pool 与 Catalog 不一致")
    if view.manifest.schema_version == 3 and (
        int(pool.get("full_catalog_count") or 0) != descriptors["augments"].record_count
        or "augment_assets" not in descriptors
        or descriptors["augment_assets"].record_count != (
            sum(item.get("icon_ready") is True for item in pool.get("identities", []))
            if pool.get("schema_version") == 2 else len(pool.get("canonical_ids") or ()))
    ):
        raise ValueError("v3 production pool 完整目录/资源数量不一致")

    provenance = [item for item in view.manifest.source_files if item.source in SOURCE_ROLES]
    if view.manifest.schema_version == 2 and {item.source for item in provenance} != set(SOURCE_ROLES):
        raise ValueError("generation provenance 缺少来源")
    pointers: dict[str, Mapping[str, Any]] = {"catalog": catalog_pointer}
    units: dict[str, Mapping[str, Any]] = {}
    for item in provenance:
        source_name = item.source
        run_root = runtime / "sources" / source_name / "runs" / item.run_id
        manifest_path = run_root / "manifest.json"
        run_manifest = SourceRunManifestV2.from_mapping(_read_object(manifest_path))
        if run_manifest.artifact is None:
            raise ValueError(f"{source_name} immutable run 缺少 artifact")
        if view.manifest.schema_version == 3 and not run_manifest.publishable:
            raise ValueError(f"{source_name} v3 unit 未通过来源完整性门")
        artifact_path = (run_root / run_manifest.artifact.relative_path).resolve()
        if (
            run_manifest.source != source_name
            or run_manifest.run_id != item.run_id
            or run_manifest.catalog_generation_id != catalog_id
            or run_manifest.catalog_sha256 != catalog_manifest.content_sha256
            or sha256_file(manifest_path) != item.manifest_sha256
            or run_manifest.artifact.sha256 != item.artifact_sha256
            or run_manifest.artifact.record_count != item.record_count
            or run_manifest.artifact.role != item.artifact_role
            or run_manifest.artifact.content_schema_version != item.content_schema_version
            or run_root.resolve() not in artifact_path.parents
            or not artifact_path.is_file()
            or artifact_path.stat().st_size != run_manifest.artifact.size
            or sha256_file(artifact_path) != run_manifest.artifact.sha256
        ):
            raise ValueError(f"{source_name} immutable run 与 generation provenance 不一致")
        pointer = SourcePointerV2(
            source=source_name,
            run_id=run_manifest.run_id,
            catalog_generation_id=catalog_id,
            catalog_sha256=catalog_manifest.content_sha256,
            manifest_sha256=item.manifest_sha256,
            artifact=run_manifest.artifact,
            completed_at=run_manifest.completed_at,
            last_success_at=run_manifest.completed_at,
        ).to_dict()
        units[f"{source_name}/{item.run_id}"] = pointer
        primary_run = view.manifest.components.get("ranking", {}).get("run_id")
        if source_name not in pointers or item.run_id == primary_run:
            pointers[source_name] = pointer
        if view.manifest.schema_version == 3 and item.artifact_role == "scoped_stats":
            _validate_scoped_children(artifact_path, run_manifest.artifact.record_count)

    return CohortCandidate(
        generation_id=view.manifest.generation_id,
        generation_created_at=view.manifest.created_at,
        manifest_health=view.manifest.health,
        pointers=pointers,
        production_pool_id=str(pool.get("pool_id") or ""),
        production_pool_count=len(pool.get("canonical_ids") or ()),
        snapshot_schema_version=view.manifest.schema_version,
        units=units if view.manifest.schema_version == 3 else {},
    )


def _validate_scoped_children(index_path: Path, expected_records: int) -> None:
    """Root-local validation, never resolve a bundle through default live runtime."""
    index = _read_object(index_path)
    files = index.get("files")
    if index.get("schema_version") != 1 or not isinstance(files, list) or len(files) != index.get("champion_count"):
        raise ValueError("scoped_stats 子文件索引无效")
    seen: set[str] = set()
    records = 0
    for item in files:
        if not isinstance(item, Mapping):
            raise ValueError("scoped_stats 子文件描述无效")
        champion_id = str(item.get("champion_id") or "")
        relative = str(item.get("relative_path") or "")
        path = (index_path.parent / relative).resolve()
        if (not champion_id or champion_id in seen or index_path.parent.resolve() not in path.parents
                or not path.is_file() or path.stat().st_size != item.get("size")
                or sha256_file(path) != item.get("sha256")):
            raise ValueError("scoped_stats 子文件身份/路径/摘要无效")
        count = item.get("record_count")
        if type(count) is not int or count < 0:
            raise ValueError("scoped_stats 子文件计数无效")
        records += count
        seen.add(champion_id)
    if records != expected_records or records != index.get("record_count"):
        raise ValueError("scoped_stats 子文件计数不一致")


def due_schedule(candidate: CohortCandidate, *, updated_at: str | None = None) -> RefreshScheduleV1:
    """没有同代 schedule 时生成安全的全来源 due 状态，绝不沿用别代进度。"""

    sources: dict[str, RefreshSourceState] = {}
    for source in SCHEDULE_SOURCES:
        pointer = candidate.pointers.get(source, {})
        raw_run_id = pointer.get("catalog_generation_id") if source == "catalog" else pointer.get("run_id")
        current_run_id = str(raw_run_id or "")
        sources[source] = RefreshSourceState(current_run_id=current_run_id, state="due")
    return RefreshScheduleV1(
        updated_at=updated_at or utc_now_iso(),
        generation_id=candidate.generation_id,
        sources=sources,
    )


def normalize_schedule(candidate: CohortCandidate, payload: Mapping[str, Any] | None) -> RefreshScheduleV1:
    if isinstance(payload, Mapping):
        try:
            schedule = RefreshScheduleV1.from_mapping(payload)
            if schedule.generation_id == candidate.generation_id and set(schedule.sources) == set(SCHEDULE_SOURCES):
                return schedule
        except (TypeError, ValueError):
            pass
    return due_schedule(candidate)


def build_recovery_point(
    candidate: CohortCandidate,
    *,
    previous_generation_id: str,
    schedule: Mapping[str, Any] | RefreshScheduleV1 | None = None,
    recorded_at: str | None = None,
) -> CohortRecoveryPointV1:
    normalized_schedule = (
        schedule
        if isinstance(schedule, RefreshScheduleV1)
        else normalize_schedule(candidate, schedule if isinstance(schedule, Mapping) else None)
    )
    return CohortRecoveryPointV1(
        schema_version=2 if candidate.snapshot_schema_version == 3 else 1,
        snapshot_schema_version=candidate.snapshot_schema_version,
        units=candidate.units,
        generation_id=candidate.generation_id,
        generation_created_at=candidate.generation_created_at,
        recorded_at=recorded_at or utc_now_iso(),
        manifest_health=candidate.manifest_health,
        pointers={
            **{role: dict(pointer) for role, pointer in candidate.pointers.items()},
            "generation": {
                "current": {"schema_version": 2, "current_generation_id": candidate.generation_id},
                "previous": (
                    {"schema_version": 2, "generation_id": previous_generation_id}
                    if previous_generation_id
                    else {}
                ),
            },
        },
        schedule=normalized_schedule.to_dict(),
    )


def load_recovery_point(root: str | Path) -> CohortRecoveryPointV1 | None:
    path = recovery_point_path(root)
    if not path.is_file():
        return None
    return CohortRecoveryPointV1.from_mapping(_read_object(path))


def write_recovery_point(root: str | Path, point: CohortRecoveryPointV1) -> None:
    atomic_write_json(recovery_point_path(root), point.to_dict(), ensure_ascii=False, indent=2)


def refresh_recovery_schedule(root: str | Path, schedule: RefreshScheduleV1) -> bool:
    """只更新当前高水位同代 schedule；较旧 generation 不得倒退恢复点。"""

    try:
        point = load_recovery_point(root)
    except (TypeError, ValueError):
        return False
    if point is None or point.generation_id != schedule.generation_id:
        return False
    write_recovery_point(
        root,
        CohortRecoveryPointV1(
            schema_version=point.schema_version,
            snapshot_schema_version=point.snapshot_schema_version,
            units=point.units,
            generation_id=point.generation_id,
            generation_created_at=point.generation_created_at,
            recorded_at=utc_now_iso(),
            manifest_health=point.manifest_health,
            pointers=point.pointers,
            schedule=schedule.to_dict(),
        ),
    )
    return True


__all__ = [
    "CohortCandidate",
    "build_recovery_point",
    "due_schedule",
    "load_recovery_point",
    "normalize_schedule",
    "parse_utc",
    "recovery_point_path",
    "refresh_recovery_schedule",
    "validate_generation_cohort",
    "write_recovery_point",
]

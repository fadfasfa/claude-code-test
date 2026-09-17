"""构建完整、generation-bound 的运行态 cohort seed。

仅在调用方显式传入已验收 snapshot root 时读取其同级 ``catalog`` 与
``sources``。本模块不刷新网络、不切换 current，也不负责运行时安装。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import CatalogManifestV2, SourcePointerV2, SourceRunManifestV2
from hextech.contracts.data_pipeline import require_identifier
from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.validation import validate_complete_provenance


COHORT_SEED_DIR = Path("resources") / "cohort-seed"
SOURCE_ROLES = ("aramkit", "blitz", "apex", "mayhem")


@dataclass(frozen=True)
class CohortSeed:
    """一套可独立安装的 Catalog、来源 run 与 snapshot。"""

    runtime_root: Path
    files: tuple[Path, ...]
    metadata: dict[str, Any]

    def bundled_name(self, path: Path) -> str:
        return (COHORT_SEED_DIR / path.relative_to(self.runtime_root)).as_posix()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cohort seed JSON 无法读取：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"cohort seed JSON 必须是对象：{path}")
    return payload


def _files_under(root: Path) -> list[Path]:
    resolved = root.resolve()
    if not resolved.is_dir():
        raise ValueError(f"cohort seed 目录不存在：{root}")
    files = [path.resolve() for path in resolved.rglob("*") if path.is_file()]
    if not files or any(resolved not in path.parents for path in files):
        raise ValueError(f"cohort seed 目录为空或越界：{root}")
    return files


def _production_pool(snapshot_client: DataSnapshotClient) -> Mapping[str, Any]:
    hints = snapshot_client.open_view().get_overlay_hints()
    source = hints.get("source") if isinstance(hints, Mapping) else None
    pool = source.get("production_augment_pool") if isinstance(source, Mapping) else None
    if not isinstance(pool, Mapping):
        raise ValueError("verified snapshot 缺少 production_augment_pool")
    validate_production_augment_pool(pool)
    return pool


def _validate_catalog_pointer(runtime: Path, pointer: Mapping[str, Any]) -> CatalogManifestV2:
    catalog_id = require_identifier(
        pointer.get("catalog_generation_id"),
        field_name="catalog_generation_id",
    )
    root = runtime / "catalog" / "generations" / catalog_id
    manifest_path = root / "manifest.json"
    try:
        manifest = CatalogManifestV2.from_mapping(_read_object(manifest_path))
        if (
            pointer.get("schema_version") != 2
            or manifest.catalog_generation_id != catalog_id
            or pointer.get("content_sha256") != manifest.content_sha256
            or pointer.get("manifest_sha256") != sha256_file(manifest_path)
        ):
            raise ValueError("身份不一致")
        validate_catalog_files(root, manifest)
        return manifest
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("active recognition Catalog pointer/manifest 校验失败") from exc


def collect_cohort_seed(snapshot_root: Path) -> CohortSeed:
    """验证 current cohort，并返回需要进入发布包的精确文件集。"""

    snapshots = snapshot_root.resolve()
    if snapshots.name.casefold() != "snapshots":
        raise ValueError("verified snapshot root 必须是运行态 snapshots 目录")
    runtime_root = snapshots.parent
    client = DataSnapshotClient(snapshots)
    view = client.open_view()
    if view.manifest.schema_version == 3:
        return _collect_v3_cohort_seed(runtime_root, view)
    status = view.status()
    source_status = status.get("source_status") if isinstance(status.get("source_status"), Mapping) else {}
    aramkit = source_status.get("aramkit") if isinstance(source_status.get("aramkit"), Mapping) else {}
    degraded_sources = set(status.get("degraded_sources") or [])
    optional_degraded = bool(
        status.get("state") == "degraded"
        and str(aramkit.get("freshness") or "") == "fresh"
        and str(aramkit.get("data_status") or "") == "fresh"
        and degraded_sources
        and degraded_sources.issubset({"blitz", "apex", "mayhem"})
    )
    if status.get("state") != "ready" and not optional_degraded:
        raise ValueError("verified snapshot 必须为 ready 或 fresh ARAMKit + optional degraded")
    validate_complete_provenance(view.manifest.source_files)

    generation_pointer_path = snapshots / "current.v2.json"
    generation_pointer = _read_object(generation_pointer_path)
    generation_id = str(generation_pointer.get("current_generation_id") or "")
    if generation_id != view.manifest.generation_id:
        raise ValueError("snapshot current 与已验证 generation 不一致")

    pool = _production_pool(client)
    catalog_id = str(pool.get("catalog_generation_id") or "")
    catalog_pointer_path = runtime_root / "catalog" / "current.v2.json"
    catalog_pointer = _read_object(catalog_pointer_path)
    if (
        str(catalog_pointer.get("catalog_generation_id") or "") != catalog_id
        or str(catalog_pointer.get("content_sha256") or "") != str(pool.get("catalog_sha256") or "")
        or str(catalog_pointer.get("manifest_sha256") or "")
        != str(pool.get("catalog_manifest_sha256") or "")
    ):
        raise ValueError("production pool 与 Catalog current 不一致")
    catalog_generation = runtime_root / "catalog" / "generations" / catalog_id
    catalog_manifest_path = catalog_generation / "manifest.json"
    catalog_manifest = CatalogManifestV2.from_mapping(_read_object(catalog_manifest_path))
    if (
        catalog_manifest.catalog_generation_id != catalog_id
        or catalog_manifest.content_sha256 != str(catalog_pointer.get("content_sha256") or "")
        or sha256_file(catalog_manifest_path) != str(catalog_pointer.get("manifest_sha256") or "")
    ):
        raise ValueError("Catalog generation 身份或摘要不一致")
    validate_catalog_files(catalog_generation, catalog_manifest)
    catalog_provenance = [item for item in view.manifest.source_files if item.source == "catalog"]
    descriptors_by_role = {item.role: item for item in catalog_manifest.files}
    if len(catalog_provenance) != len(descriptors_by_role) or any(
        item.run_id != catalog_id
        or item.catalog_generation_id != catalog_id
        or item.manifest_sha256 != str(catalog_pointer.get("manifest_sha256") or "")
        or item.artifact_role not in descriptors_by_role
        or item.artifact_sha256 != descriptors_by_role[item.artifact_role].sha256
        or item.record_count != descriptors_by_role[item.artifact_role].record_count
        or item.content_schema_version != descriptors_by_role[item.artifact_role].content_schema_version
        for item in catalog_provenance
    ):
        raise ValueError("snapshot Catalog provenance 与 Catalog generation 不一致")

    provenance_by_source = {
        item.source: item for item in view.manifest.source_files if item.source in SOURCE_ROLES
    }
    source_run_ids: dict[str, str] = {}
    # snapshot 文件继续复用现有 ``resources/seeds``，避免在包内重复一份十余 MB
    # generation；cohort seed 只补它之前缺失的 Catalog、来源 run 与调度状态。
    files = [catalog_pointer_path]
    files.extend(_files_under(catalog_generation))
    for source_name in SOURCE_ROLES:
        provenance = provenance_by_source.get(source_name)
        if provenance is None:
            raise ValueError(f"snapshot provenance 缺少来源：{source_name}")
        pointer_path = runtime_root / "sources" / source_name / "current.v2.json"
        pointer_payload = _read_object(pointer_path)
        pointer = SourcePointerV2.from_mapping(pointer_payload)
        if (
            pointer.source != source_name
            or pointer.run_id != provenance.run_id
            or pointer.catalog_generation_id != catalog_id
            or pointer.catalog_sha256 != str(pool.get("catalog_sha256") or "")
            or pointer.manifest_sha256 != provenance.manifest_sha256
            or pointer.artifact.sha256 != provenance.artifact_sha256
            or pointer.artifact.record_count != provenance.record_count
        ):
            raise ValueError(f"{source_name} current 与 snapshot provenance 不一致")
        run_root = runtime_root / "sources" / source_name / "runs" / pointer.run_id
        run_manifest_path = run_root / "manifest.json"
        run_manifest = SourceRunManifestV2.from_mapping(_read_object(run_manifest_path))
        artifact_path = (run_root / pointer.artifact.relative_path).resolve()
        if (
            run_root.resolve() not in artifact_path.parents
            or run_manifest.source != source_name
            or run_manifest.run_id != pointer.run_id
            or run_manifest.catalog_generation_id != catalog_id
            or run_manifest.catalog_sha256 != str(pool.get("catalog_sha256") or "")
            or run_manifest.artifact is None
            or run_manifest.artifact != pointer.artifact
            or sha256_file(run_manifest_path) != pointer.manifest_sha256
            or not artifact_path.is_file()
            or artifact_path.stat().st_size != pointer.artifact.size
            or sha256_file(artifact_path) != pointer.artifact.sha256
        ):
            raise ValueError(f"{source_name} immutable run 校验失败")
        source_run_ids[source_name] = pointer.run_id
        files.append(pointer_path)
        # ARAMKit 的 artifact 是受哈希绑定的索引，逐英雄子文件也必须进入包。
        files.extend(_files_under(run_root))
    schedule_path = runtime_root / "state" / "data-service" / "refresh_schedule.v1.json"
    if schedule_path.is_file():
        schedule = _read_object(schedule_path)
        schedule_generation_id = str(schedule.get("generation_id") or "")
        if schedule_generation_id != generation_id:
            raise ValueError("refresh schedule 与 snapshot current generation 不一致")
        schedule_sources = schedule.get("sources")
        schedule_catalog = schedule_sources.get("catalog") if isinstance(schedule_sources, Mapping) else None
        schedule_catalog_id = (
            str(schedule_catalog.get("current_run_id") or "")
            if isinstance(schedule_catalog, Mapping)
            else ""
        )
        if schedule_catalog_id != catalog_id:
            raise ValueError("refresh schedule Catalog current_run_id 与 active Catalog 不一致")
        files.append(schedule_path)

    canonical_ids = pool.get("canonical_ids")
    identities = pool.get("identities")
    pool_count = len(canonical_ids) if isinstance(canonical_ids, list) else 0
    if pool_count <= 0 or not isinstance(identities, list) or len(identities) != pool_count:
        raise ValueError("production pool identity 数量无效")
    if (
        int(pool.get("enabled_count") or 0) != pool_count
        or int(pool.get("full_catalog_count") or 0) != descriptors_by_role["augments"].record_count
        or descriptors_by_role.get("augment_assets") is None
        or descriptors_by_role["augment_assets"].record_count != (
            sum(item.get("icon_ready") is True for item in identities)
            if pool.get("schema_version") == 2 else pool_count
        )
    ):
        raise ValueError("production pool 数量与 Catalog generation 不一致")
    unique_files = tuple(sorted(set(path.resolve() for path in files), key=lambda path: path.as_posix()))
    return CohortSeed(
        runtime_root=runtime_root,
        files=unique_files,
        metadata={
            "schema_version": 1,
            "generation_id": generation_id,
            "catalog_generation_id": catalog_id,
            "source_run_ids": source_run_ids,
            "production_pool_id": str(pool.get("pool_id") or ""),
            "production_pool_count": pool_count,
            "full_catalog_count": int(pool.get("full_catalog_count") or 0),
            "file_count": len(unique_files),
        },
    )


def _collect_v3_cohort_seed(runtime: Path, view: Any) -> CohortSeed:
    from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort

    if (view.degraded or _read_object(runtime / "snapshots" / "current.v2.json").get("current_generation_id")
            != view.manifest.generation_id):
        raise ValueError("v3 cohort seed 不得使用 fallback generation")
    candidate = validate_generation_cohort(runtime, view.manifest.generation_id)
    files: list[Path] = []
    for source, pointer in candidate.pointers.items():
        if source == "catalog":
            continue
        path = runtime / "sources" / source / "current.v2.json"
        actual = _read_object(path)
        fields = ("source", "run_id", "catalog_generation_id", "catalog_sha256", "manifest_sha256", "artifact")
        if any(actual.get(key) != pointer.get(key) for key in fields):
            raise ValueError(f"v3 cohort {source} current 与精确 unit 不一致")
        files.append(path)
    statistics_catalog_id = str(candidate.pointers["catalog"]["catalog_generation_id"])
    active_catalog_path = runtime / "catalog" / "current.v2.json"
    active_catalog = _read_object(active_catalog_path)
    _validate_catalog_pointer(runtime, active_catalog)
    recognition_catalog_id = str(active_catalog["catalog_generation_id"])
    files.append(active_catalog_path)
    files.extend(_files_under(runtime / "catalog" / "generations" / statistics_catalog_id))
    if recognition_catalog_id != statistics_catalog_id:
        files.extend(_files_under(runtime / "catalog" / "generations" / recognition_catalog_id))
    for pointer in candidate.units.values():
        files.extend(_files_under(runtime / "sources" / str(pointer["source"]) / "runs" / str(pointer["run_id"])))
    schedule = runtime / "state" / "data-service" / "refresh_schedule.v1.json"
    if schedule.is_file():
        if _read_object(schedule).get("generation_id") != candidate.generation_id:
            raise ValueError("v3 cohort schedule 与 generation 不一致")
        files.append(schedule)
    pool = _production_pool(DataSnapshotClient(runtime / "snapshots"))
    unique_files = tuple(sorted(set(path.resolve() for path in files), key=lambda path: path.as_posix()))
    return CohortSeed(runtime, unique_files, {
        "schema_version": 2, "snapshot_schema_version": 3,
        "generation_id": candidate.generation_id,
        # ``catalog_generation_id`` remains the statistics snapshot binding for
        # old readers.  Recognition may advance independently in v3.
        "catalog_generation_id": statistics_catalog_id,
        "recognition_catalog_generation_id": recognition_catalog_id,
        "source_run_ids": {source: pointer["run_id"] for source, pointer in candidate.pointers.items() if source != "catalog"},
        "units": {key: dict(value) for key, value in candidate.units.items()},
        "production_pool_id": candidate.production_pool_id, "production_pool_count": candidate.production_pool_count,
        "full_catalog_count": int(pool.get("full_catalog_count") or 0), "file_count": len(unique_files),
    })


__all__ = ["COHORT_SEED_DIR", "CohortSeed", "collect_cohort_seed"]

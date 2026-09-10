"""安装发布包内经过验证的 generation-bound cohort seed。

不可变文件全部通过 bundle manifest 摘要校验后才会触碰 current；五个 pointer
沿用现有 promotion journal 切换。旧 generation、来源 run 和用户数据均保留。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from hextech.contracts import CatalogManifestV2, SourcePointerV2, SourceRunManifestV2
from hextech.infrastructure.persistence.cohort import CohortPromotionStore
from hextech.infrastructure.persistence.cohort_recovery import (
    CohortCandidate,
    build_recovery_point,
    load_recovery_point,
    normalize_schedule,
    parse_utc,
    validate_generation_cohort,
    write_recovery_point,
)
from hextech.infrastructure.persistence.cohort_validation_receipt import (
    load_valid_validation_receipt,
    write_validation_receipt,
)
from hextech.infrastructure.persistence.file_lock import InterProcessFileLock
from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files
from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.validation import SnapshotValidationError, validate_complete_provenance
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.session.build_identity import current_build_id, get_build_identity


COHORT_PREFIX = PurePosixPath("resources/cohort-seed")
SNAPSHOT_PREFIX = PurePosixPath("resources/seeds")
SOURCE_ROLES = ("aramkit", "blitz", "apex", "mayhem")
POINTER_PATHS = {
    "catalog/current.v2.json",
    "sources/aramkit/current.v2.json",
    "sources/blitz/current.v2.json",
    "sources/apex/current.v2.json",
    "sources/mayhem/current.v2.json",
}
InstallState = Literal["installed", "already_current", "runtime_newer", "runtime_restored", "unavailable"]


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cohort seed JSON 无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"cohort seed JSON 必须是对象：{path.name}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: object, prefix: PurePosixPath) -> PurePosixPath | None:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts or prefix not in path.parents:
        return None
    return path.relative_to(prefix)


def _verified_files(
    bundle_root: Path,
    manifest: Mapping[str, Any],
    *,
    files_key: str,
    hashes_key: str,
    prefix: PurePosixPath,
) -> list[tuple[PurePosixPath, Path]]:
    raw_files = manifest.get(files_key)
    hashes = manifest.get(hashes_key)
    if not isinstance(raw_files, list) or not raw_files or not isinstance(hashes, Mapping):
        raise ValueError(f"bundle manifest 缺少 {files_key}/{hashes_key}")
    result: list[tuple[PurePosixPath, Path]] = []
    seen: set[PurePosixPath] = set()
    for raw_path in raw_files:
        relative = _safe_relative(raw_path, prefix)
        normalized = PurePosixPath(str(raw_path).replace("\\", "/"))
        source = bundle_root.joinpath(*normalized.parts)
        expected = str(hashes.get(normalized.as_posix()) or "").lower()
        if (
            relative is None
            or relative in seen
            or len(expected) != 64
            or not source.is_file()
            or _sha256(source) != expected
        ):
            raise ValueError(f"bundle seed 路径或摘要无效：{raw_path}")
        seen.add(relative)
        result.append((relative, source))
    return result


def _copy_immutable(source: Path, target: Path) -> None:
    if target.is_file():
        if target.stat().st_size != source.stat().st_size or _sha256(target) != _sha256(source):
            raise ValueError(f"运行态同名 immutable 文件摘要冲突：{target}")
        return
    if target.exists():
        raise ValueError(f"运行态 immutable 路径类型冲突：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.bundle-seed-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size or _sha256(temporary) != _sha256(source):
            raise ValueError(f"cohort seed 复制摘要不一致：{target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _install_files(
    runtime_root: Path,
    cohort_files: list[tuple[PurePosixPath, Path]],
    snapshot_files: list[tuple[PurePosixPath, Path]],
) -> None:
    for relative, source in cohort_files:
        if relative.as_posix() in POINTER_PATHS or relative.as_posix() == "state/data-service/refresh_schedule.v1.json":
            continue
        _copy_immutable(source, runtime_root.joinpath(*relative.parts))
    for relative, source in snapshot_files:
        if relative.as_posix() == "current.v2.json":
            continue
        _copy_immutable(source, runtime_root / "snapshots" / Path(*relative.parts))


def _pointer_payloads(
    cohort_files: list[tuple[PurePosixPath, Path]],
    snapshot_files: list[tuple[PurePosixPath, Path]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], Path | None]:
    sources = {relative.as_posix(): source for relative, source in cohort_files}
    pointer_payloads = {
        "catalog": _read_object(sources["catalog/current.v2.json"]),
        **{
            source: _read_object(sources[f"sources/{source}/current.v2.json"])
            for source in SOURCE_ROLES
        },
    }
    snapshot_sources = {relative.as_posix(): source for relative, source in snapshot_files}
    generation_pointer = _read_object(snapshot_sources["current.v2.json"])
    schedule_path = sources.get("state/data-service/refresh_schedule.v1.json")
    return pointer_payloads, generation_pointer, schedule_path


def _validate_installed_cohort(
    runtime_root: Path,
    metadata: Mapping[str, Any],
    pointers: Mapping[str, Mapping[str, Any]],
    generation_pointer: Mapping[str, Any],
) -> None:
    generation_id = str(metadata.get("generation_id") or "")
    catalog_id = str(metadata.get("catalog_generation_id") or "")
    if str(generation_pointer.get("current_generation_id") or "") != generation_id:
        raise ValueError("cohort seed snapshot pointer 身份不一致")
    catalog_pointer = pointers["catalog"]
    if str(catalog_pointer.get("catalog_generation_id") or "") != catalog_id:
        raise ValueError("cohort seed Catalog pointer 身份不一致")
    catalog_root = runtime_root / "catalog" / "generations" / catalog_id
    catalog_manifest_path = catalog_root / "manifest.json"
    catalog_manifest = CatalogManifestV2.from_mapping(_read_object(catalog_manifest_path))
    if (
        catalog_manifest.catalog_generation_id != catalog_id
        or catalog_manifest.content_sha256 != str(catalog_pointer.get("content_sha256") or "")
        or sha256_file(catalog_manifest_path) != str(catalog_pointer.get("manifest_sha256") or "")
    ):
        raise ValueError("cohort seed Catalog manifest 身份不一致")
    validate_catalog_files(catalog_root, catalog_manifest)

    view = DataSnapshotClient(runtime_root / "snapshots").open_generation(generation_id)
    validate_complete_provenance(view.manifest.source_files)
    hints = view.get_overlay_hints()
    source = hints.get("source") if isinstance(hints, Mapping) else None
    pool = source.get("production_augment_pool") if isinstance(source, Mapping) else None
    if not isinstance(pool, Mapping):
        raise ValueError("cohort seed snapshot 缺少 production pool")
    validate_production_augment_pool(pool)
    descriptors_by_role = {item.role: item for item in catalog_manifest.files}
    catalog_provenance = [item for item in view.manifest.source_files if item.source == "catalog"]
    if (
        str(pool.get("pool_id") or "") != str(metadata.get("production_pool_id") or "")
        or len(pool.get("canonical_ids") or []) != int(metadata.get("production_pool_count") or 0)
        or int(pool.get("enabled_count") or 0) != int(metadata.get("production_pool_count") or 0)
        or int(pool.get("full_catalog_count") or 0) != descriptors_by_role["augments"].record_count
        or descriptors_by_role.get("augment_assets") is None
        or descriptors_by_role["augment_assets"].record_count != int(metadata.get("production_pool_count") or 0)
        or str(pool.get("catalog_generation_id") or "") != catalog_id
        or str(pool.get("catalog_sha256") or "") != str(catalog_pointer.get("content_sha256") or "")
    ):
        raise ValueError("cohort seed production pool 绑定不一致")
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
        raise ValueError("cohort seed Catalog provenance 不一致")

    provenance = {item.source: item for item in view.manifest.source_files if item.source in SOURCE_ROLES}
    for source_name in SOURCE_ROLES:
        pointer = SourcePointerV2.from_mapping(pointers[source_name])
        item = provenance.get(source_name)
        run_root = runtime_root / "sources" / source_name / "runs" / pointer.run_id
        manifest_path = run_root / "manifest.json"
        run_manifest = SourceRunManifestV2.from_mapping(_read_object(manifest_path))
        artifact_path = (run_root / pointer.artifact.relative_path).resolve()
        if (
            item is None
            or pointer.run_id != item.run_id
            or pointer.catalog_generation_id != catalog_id
            or pointer.catalog_sha256 != catalog_manifest.content_sha256
            or pointer.manifest_sha256 != item.manifest_sha256
            or pointer.artifact.sha256 != item.artifact_sha256
            or run_manifest.source != source_name
            or run_manifest.run_id != pointer.run_id
            or run_manifest.catalog_generation_id != catalog_id
            or run_manifest.catalog_sha256 != catalog_manifest.content_sha256
            or run_manifest.artifact is None
            or run_manifest.artifact != pointer.artifact
            or sha256_file(manifest_path) != pointer.manifest_sha256
            or run_root.resolve() not in artifact_path.parents
            or not artifact_path.is_file()
            or artifact_path.stat().st_size != pointer.artifact.size
            or sha256_file(artifact_path) != pointer.artifact.sha256
        ):
            raise ValueError(f"cohort seed {source_name} run 绑定不一致")


def _same_current(
    runtime_root: Path,
    pointers: Mapping[str, Mapping[str, Any]],
    generation_pointer: Mapping[str, Any],
) -> bool:
    try:
        current_generation = _read_object(runtime_root / "snapshots" / "current.v2.json")
        current_catalog = _read_object(runtime_root / "catalog" / "current.v2.json")
        expected_catalog = pointers["catalog"]
        if (
            str(current_generation.get("current_generation_id") or "")
            != str(generation_pointer.get("current_generation_id") or "")
            or any(
                str(current_catalog.get(field) or "") != str(expected_catalog.get(field) or "")
                for field in ("catalog_generation_id", "content_sha256", "manifest_sha256")
            )
        ):
            return False
        for source_name in SOURCE_ROLES:
            current = SourcePointerV2.from_mapping(
                _read_object(runtime_root / "sources" / source_name / "current.v2.json")
            )
            expected = SourcePointerV2.from_mapping(pointers[source_name])
            if (
                current.source != expected.source
                or current.run_id != expected.run_id
                or current.catalog_generation_id != expected.catalog_generation_id
                or current.catalog_sha256 != expected.catalog_sha256
                or current.manifest_sha256 != expected.manifest_sha256
                or current.artifact != expected.artifact
            ):
                return False
        return True
    except (TypeError, ValueError):
        return False


def _optional_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return _read_object(path)
    except ValueError:
        return {}


def _candidate_ids(
    runtime: Path,
    bundle_generation_id: str,
    *,
    include_local_generations: bool = False,
) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    """枚举候选来源；任何单个损坏 pointer 都不能阻断其余本地 generation。"""

    sources: dict[str, set[str]] = {}
    diagnostics: list[dict[str, str]] = []

    def add(source: str, generation_id: object) -> None:
        normalized = str(generation_id or "").strip()
        if normalized:
            sources.setdefault(normalized, set()).add(source)

    current = _optional_object(runtime / "snapshots" / "current.v2.json")
    previous = _optional_object(runtime / "snapshots" / "previous.v2.json")
    add("current", current.get("current_generation_id"))
    add("previous", previous.get("generation_id"))
    try:
        point = load_recovery_point(runtime)
    except (TypeError, ValueError) as exc:
        point = None
        diagnostics.append({"source": "recovery_point", "state": "rejected", "reason": type(exc).__name__})
    if point is not None:
        add("recovery_point", point.generation_id)
    generations_root = runtime / "snapshots" / "generations"
    if include_local_generations and generations_root.is_dir():
        for path in generations_root.iterdir():
            if path.is_dir():
                add("local_generation", path.name)
    add("bundle", bundle_generation_id)
    return sources, diagnostics


def _manifest_generation_class(runtime: Path, generation_id: str) -> str:
    """仅读 manifest 角色，廉价排除永久保留的 legacy 统计代。"""

    manifest = _optional_object(runtime / "snapshots" / "generations" / generation_id / "manifest.json")
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list):
        return "unknown"
    roles = {
        (str(item.get("source") or ""), str(item.get("artifact_role") or ""))
        for item in source_files
        if isinstance(item, Mapping)
    }
    current_stats = {("aramkit", "scoped_stats"), ("blitz", "augment_ranking")}
    if ("hextech", "stats") in roles and not current_stats.issubset(roles):
        return "legacy_incompatible"
    return "current_candidate" if current_stats.issubset(roles) else "unknown"


def _validated_candidates(
    runtime: Path,
    sources_by_id: Mapping[str, set[str]],
    diagnostics: list[dict[str, str]],
) -> list[tuple[CohortCandidate, set[str]]]:
    valid: list[tuple[CohortCandidate, set[str]]] = []
    for generation_id, sources in sources_by_id.items():
        generation_class = _manifest_generation_class(runtime, generation_id)
        if generation_class == "legacy_incompatible":
            diagnostics.append(
                {
                    "source": ",".join(sorted(sources)),
                    "generation_id": generation_id,
                    "state": "rejected",
                    "reason": generation_class,
                }
            )
            continue
        try:
            candidate = validate_generation_cohort(runtime, generation_id)
            parse_utc(candidate.generation_created_at)
        except (OSError, TypeError, ValueError, SnapshotValidationError) as exc:
            diagnostics.append(
                {
                    "source": ",".join(sorted(sources)),
                    "generation_id": generation_id,
                    "state": "rejected",
                    "reason": type(exc).__name__,
                }
            )
            continue
        valid.append((candidate, set(sources)))
        diagnostics.append(
            {
                "source": ",".join(sorted(sources)),
                "generation_id": generation_id,
                "state": "valid",
                "reason": "",
            }
        )
    return valid


def _select_candidate(
    runtime: Path,
    bundle_generation_id: str,
) -> tuple[CohortCandidate, str, list[dict[str, str]]]:
    sources_by_id, diagnostics = _candidate_ids(runtime, bundle_generation_id)
    valid = _validated_candidates(runtime, sources_by_id, diagnostics)
    if not valid:
        fallback_sources, fallback_diagnostics = _candidate_ids(
            runtime,
            bundle_generation_id,
            include_local_generations=True,
        )
        diagnostics.extend(fallback_diagnostics)
        untested = {
            generation_id: sources
            for generation_id, sources in fallback_sources.items()
            if generation_id not in sources_by_id
        }
        valid.extend(_validated_candidates(runtime, untested, diagnostics))
    if not valid:
        raise ValueError("runtime 与 bundle 均没有完整可验证 cohort")
    priority = {"current": 5, "previous": 4, "recovery_point": 3, "local_generation": 2, "bundle": 1}
    candidate, sources = max(
        valid,
        key=lambda item: (
            item[0].sort_time,
            max((priority.get(source, 0) for source in item[1]), default=0),
            item[0].generation_id,
        ),
    )
    selected_source = max(sources, key=lambda source: priority.get(source, 0))
    return candidate, selected_source, diagnostics


def _candidate_schedule(
    runtime: Path,
    candidate: CohortCandidate,
    *,
    selected_source: str,
    bundled_schedule_path: Path | None,
) -> Mapping[str, Any]:
    payload: Mapping[str, Any] | None = None
    runtime_schedule = _optional_object(runtime / "state" / "data-service" / "refresh_schedule.v1.json")
    if str(runtime_schedule.get("generation_id") or "") == candidate.generation_id:
        payload = runtime_schedule
    if payload is None and selected_source == "recovery_point":
        try:
            point = load_recovery_point(runtime)
        except (TypeError, ValueError):
            point = None
        if point is not None and point.generation_id == candidate.generation_id:
            payload = point.schedule
    if payload is None and bundled_schedule_path is not None:
        bundled = _read_object(bundled_schedule_path)
        if str(bundled.get("generation_id") or "") == candidate.generation_id:
            payload = bundled
    return normalize_schedule(candidate, payload).to_dict()


def _write_selection_status(
    runtime: Path,
    *,
    state: InstallState,
    selected_source: str,
    selected_generation_id: str,
    bundle_generation_id: str,
    candidates: list[dict[str, str]],
    transaction_id: str = "",
) -> None:
    import psutil
    path = runtime / "state" / "data-service" / "cohort_selection.v1.json"
    semantic = {
        "schema_version": 2,
        "install_state": state,
        "selected_source": selected_source,
        "selected_generation_id": selected_generation_id,
        "bundle_generation_id": bundle_generation_id,
        "candidates": candidates,
        "writer_build_id": current_build_id(),
        "writer_role": "desktop_seed",
        "writer_pid": os.getpid(),
        "writer_pid_started_at": psutil.Process(os.getpid()).create_time(),
        "writer_source_fingerprint": str(get_build_identity().get("source_fingerprint") or ""),
        "transaction_id": str(transaction_id or ""),
    }
    existing = _optional_object(path)
    comparable_existing = {key: existing.get(key) for key in semantic}
    if comparable_existing == semantic:
        return
    atomic_write_json(
        path,
        # 部署器的 launch_started_at 带亚秒精度；selection 也必须保留亚秒，
        # 否则同一秒内的真实 Desktop owner 会被向下截断成“早于启动”。
        {**semantic, "updated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds")},
        ensure_ascii=False,
        indent=2,
    )


def _install_bundled_cohort(bundle_base: Path, runtime: Path) -> InstallState:
    manifest_path = bundle_base / "bundle_manifest.json"
    if not bundle_base.is_dir() or not manifest_path.is_file():
        return "unavailable"
    manifest = _read_object(manifest_path)
    metadata = manifest.get("cohort_seed")
    if not isinstance(metadata, Mapping) or int(metadata.get("schema_version") or 0) != 1:
        return "unavailable"
    bundle_generation_id = str(metadata.get("generation_id") or "")
    cohort_files = _verified_files(
        bundle_base,
        manifest,
        files_key="cohort_seed_files",
        hashes_key="cohort_seed_sha256",
        prefix=COHORT_PREFIX,
    )
    snapshot_files = _verified_files(
        bundle_base,
        manifest,
        files_key="seed_files",
        hashes_key="seed_sha256",
        prefix=SNAPSHOT_PREFIX,
    )
    _install_files(runtime, cohort_files, snapshot_files)
    pointers, generation_pointer, schedule_path = _pointer_payloads(cohort_files, snapshot_files)
    _validate_installed_cohort(runtime, metadata, pointers, generation_pointer)
    # current receipt 只证明当前运行代完整，不能证明这个新 Build 自带的 baseline
    # 已经落入 runtime。先逐文件校验并幂等物化 bundle immutable，再允许快返；
    # 否则部署验收比较 bundle/current 时会读取一个从未安装的 generation。
    receipt_candidate = load_valid_validation_receipt(runtime)
    if receipt_candidate is not None:
        bundle_candidate = validate_generation_cohort(runtime, bundle_generation_id)
        # receipt 证明的是 current 完整性，不是新旧顺序。只有 current 不早于
        # 已完整验证的 bundle baseline 才能快返；旧 current 必须进入下面统一的
        # 单调候选选择与 promotion journal。排序时间必须来自 receipt 已绑定摘要的
        # immutable manifest；receipt 自身的缓存时间不是独立信任根。
        receipt_manifest = _read_object(
            runtime / "snapshots" / "generations" / receipt_candidate.generation_id / "manifest.json"
        )
        try:
            receipt_sort_time = parse_utc(str(receipt_manifest.get("created_at") or ""))
        except ValueError:
            receipt_candidate = None
        else:
            if receipt_sort_time < bundle_candidate.sort_time:
                receipt_candidate = None
    if receipt_candidate is not None:
        state: InstallState = (
            "already_current"
            if receipt_candidate.generation_id == bundle_generation_id
            else "runtime_newer"
        )
        _write_selection_status(
            runtime,
            state=state,
            selected_source="current_receipt",
            selected_generation_id=receipt_candidate.generation_id,
            bundle_generation_id=bundle_generation_id,
            candidates=[
                {
                    "source": "current_receipt",
                    "generation_id": receipt_candidate.generation_id,
                    "state": "valid",
                    "reason": "metadata_match",
                },
                {
                    "source": "bundle",
                    "generation_id": bundle_generation_id,
                    "state": "valid",
                    "reason": "immutable_materialized",
                },
            ],
        )
        return state
    store = CohortPromotionStore(runtime)
    store.recover()
    with store.exclusive(timeout_seconds=30.0):
        candidate, selected_source, diagnostics = _select_candidate(runtime, bundle_generation_id)
        selected_pointer = {
            "schema_version": 2,
            "current_generation_id": candidate.generation_id,
        }
        schedule = _candidate_schedule(
            runtime,
            candidate,
            selected_source=selected_source,
            bundled_schedule_path=schedule_path,
        )
        previous_pointer = _optional_object(runtime / "snapshots" / "previous.v2.json")
        previous_id = str(previous_pointer.get("generation_id") or "")
        if _same_current(runtime, candidate.pointers, selected_pointer):
            try:
                existing_point = load_recovery_point(runtime)
            except (TypeError, ValueError):
                existing_point = None
            # 正常 same-current 启动不得重写 recovery/schedule；只有缺失或损坏时
            # 才在同一 promotion 锁内修复，避免覆盖 DataService 的 backoff 高水位。
            if existing_point is None or existing_point.generation_id != candidate.generation_id:
                point = build_recovery_point(
                    candidate,
                    previous_generation_id=previous_id,
                    schedule=schedule,
                )
                write_recovery_point(runtime, point)
            state: InstallState = (
                "already_current" if candidate.generation_id == bundle_generation_id else "runtime_newer"
            )
            _write_selection_status(
                runtime,
                state=state,
                selected_source=selected_source,
                selected_generation_id=candidate.generation_id,
                bundle_generation_id=bundle_generation_id,
                candidates=diagnostics,
            )
            write_validation_receipt(runtime, candidate)
            return state

    journal_started = False
    try:
        journal = store.begin(timeout_seconds=30.0)
        journal_started = True
        # 锁等待期间 current 可能已被另一个合法 writer 推进，必须在事务内重选。
        candidate, selected_source, diagnostics = _select_candidate(runtime, bundle_generation_id)
        selected_pointer = {
            "schema_version": 2,
            "current_generation_id": candidate.generation_id,
        }
        schedule = _candidate_schedule(
            runtime,
            candidate,
            selected_source=selected_source,
            bundled_schedule_path=schedule_path,
        )
        for role in ("catalog", *SOURCE_ROLES):
            store.record_target(role, candidate.pointers[role])
        store.promote_dependencies()
        old_generation = journal.old_pointers.get("generation", {})
        old_current = old_generation.get("current") if isinstance(old_generation, Mapping) else None
        old_id = str(old_current.get("current_generation_id") or "") if isinstance(old_current, Mapping) else ""
        old_previous = old_generation.get("previous") if isinstance(old_generation, Mapping) else None
        old_previous_id = (
            str(old_previous.get("generation_id") or "")
            if isinstance(old_previous, Mapping)
            else ""
        )
        target_previous_id = old_previous_id if old_id == candidate.generation_id else old_id
        previous_path = runtime / "snapshots" / "previous.v2.json"
        if target_previous_id:
            atomic_write_json(
                previous_path,
                {"generation_id": target_previous_id, "schema_version": 2},
                indent=2,
            )
        else:
            previous_path.unlink(missing_ok=True)
        atomic_write_json(runtime / "snapshots" / "current.v2.json", selected_pointer, indent=2)
        point = build_recovery_point(
            candidate,
            previous_generation_id=target_previous_id,
            schedule=schedule,
        )
        store.stage_generation_state(
            schedule=schedule,
            recovery_point=point.to_dict(),
        )
        store.record_generation_promoted(candidate.generation_id)
        store.commit()
        journal_started = False
    except Exception:
        if journal_started:
            store.rollback()
        raise
    state = "installed" if candidate.generation_id == bundle_generation_id else "runtime_restored"
    with store.exclusive(timeout_seconds=30.0):
        current_pointer = _optional_object(runtime / "snapshots" / "current.v2.json")
        if str(current_pointer.get("current_generation_id") or "") == candidate.generation_id:
            _write_selection_status(
                runtime,
                state=state,
                selected_source=selected_source,
                selected_generation_id=candidate.generation_id,
                bundle_generation_id=bundle_generation_id,
                candidates=diagnostics,
                transaction_id=journal.transaction_id,
            )
            write_validation_receipt(runtime, candidate)
    return state


def install_bundled_cohort(
    *,
    bundle_root: str | Path,
    runtime_root: str | Path,
    lock_timeout_seconds: float = 30.0,
) -> InstallState:
    """串行安装一整套 bundle cohort；旧包无该字段时返回 ``unavailable``。"""

    bundle_base = Path(bundle_root)
    runtime = Path(runtime_root)
    lock = InterProcessFileLock(runtime / "locks" / "bundle-cohort-seed.lock")
    deadline = time.monotonic() + max(0.1, float(lock_timeout_seconds))
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise TimeoutError("等待 bundle cohort seed 安装锁超时")
        time.sleep(0.05)
    try:
        return _install_bundled_cohort(bundle_base, runtime)
    finally:
        lock.release()


__all__ = ["InstallState", "install_bundled_cohort"]

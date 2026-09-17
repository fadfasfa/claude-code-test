"""完整 cohort 校验后的可失效启动 receipt。

receipt 不是新的信任根：它只能由完整 ``validate_generation_cohort`` 成功后写入，
并以 Build、current pointer、manifest 摘要和全部已验证文件元数据失效。任一字段
不匹配时调用方必须回到完整哈希校验。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hextech.contracts.data_pipeline import require_identifier
from hextech.infrastructure.persistence.cohort_recovery import CohortCandidate, validate_generation_cohort
from hextech.infrastructure.persistence.cohort_seed_catalog import validated_catalog_manifest
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.session.build_identity import get_build_identity


RECEIPT_SCHEMA_VERSION = 1
VALIDATOR_CONTRACT_VERSION = 1
RECEIPT_RELATIVE_PATH = Path("state/data-service/cohort_validation_receipt.v1.json")
JOURNAL_RELATIVE_PATH = Path("state/data-service/promotion_journal.v1.json")
SOURCE_ROLES = ("aramkit", "blitz", "apex", "mayhem")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cohort receipt JSON 无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError("cohort receipt 必须是对象")
    return payload


def _safe_runtime_path(runtime: Path, relative_path: object) -> Path:
    relative = Path(str(relative_path or "").replace("/", "\\"))
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("cohort receipt 路径越界")
    target = (runtime / relative).resolve()
    if runtime.resolve() not in target.parents:
        raise ValueError("cohort receipt 路径不在 runtime 内")
    return target


def _verified_paths(runtime: Path, candidate: CohortCandidate) -> tuple[Path, ...]:
    generation_root = runtime / "snapshots" / "generations" / candidate.generation_id
    paths: set[Path] = {
        runtime / "snapshots" / "current.v2.json",
        generation_root / "manifest.json",
        runtime / "catalog" / "current.v2.json",
    }
    manifest = _read_object(generation_root / "manifest.json")
    for descriptor in manifest.get("files") or ():
        if isinstance(descriptor, Mapping):
            paths.add(_safe_runtime_path(generation_root, descriptor.get("relative_path")))

    catalog = candidate.pointers["catalog"]
    catalog_id = str(catalog.get("catalog_generation_id") or "")
    catalog_root = runtime / "catalog" / "generations" / catalog_id
    catalog_manifest = _read_object(catalog_root / "manifest.json")
    paths.add(catalog_root / "manifest.json")
    for descriptor in catalog_manifest.get("files") or ():
        if isinstance(descriptor, Mapping):
            paths.add(_safe_runtime_path(catalog_root, descriptor.get("relative_path")))

    if candidate.snapshot_schema_version == 3:
        # Catalog validation already verifies content-addressed assets; retain
        # every file in that exact immutable Catalog, not only its JSON index.
        paths.update(path for path in catalog_root.rglob("*") if path.is_file())
        paths.update(_recognition_catalog_paths(runtime))
        for pointer in candidate.units.values():
            source, run_id = str(pointer["source"]), str(pointer["run_id"])
            run_root = runtime / "sources" / source / "runs" / run_id
            artifact = pointer["artifact"]
            artifact_path = _safe_runtime_path(run_root, artifact["relative_path"])
            paths.update({run_root / "manifest.json", artifact_path})
            if artifact["role"] == "scoped_stats":
                index = _read_object(artifact_path)
                for child in index["files"]:
                    paths.add(_safe_runtime_path(artifact_path.parent, child["relative_path"]))
        for source in candidate.pointers:
            if source != "catalog":
                paths.add(runtime / "sources" / source / "current.v2.json")
        return tuple(sorted(paths, key=lambda item: item.as_posix()))

    for source in SOURCE_ROLES:
        pointer = candidate.pointers[source]
        run_id = str(pointer.get("run_id") or "")
        run_root = runtime / "sources" / source / "runs" / run_id
        paths.update(
            {
                runtime / "sources" / source / "current.v2.json",
                run_root / "manifest.json",
                _safe_runtime_path(run_root, (pointer.get("artifact") or {}).get("relative_path")),
            }
        )
    return tuple(sorted(paths, key=lambda item: item.as_posix()))


def _metadata(runtime: Path, paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    runtime_resolved = runtime.resolve()
    for path in paths:
        resolved = path.resolve()
        if runtime_resolved not in resolved.parents or not resolved.is_file():
            raise ValueError("cohort receipt 文件集合无效")
        stat = resolved.stat()
        result.append(
            {
                "relative_path": resolved.relative_to(runtime_resolved).as_posix(),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        )
    return result


def receipt_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root) / RECEIPT_RELATIVE_PATH


def _recognition_catalog_pointer(runtime: Path) -> dict[str, Any]:
    pointer = _read_object(runtime / "catalog/current.v2.json")
    require_identifier(
        pointer.get("catalog_generation_id"),
        field_name="catalog_generation_id",
    )
    return pointer


def _validate_recognition_catalog(runtime: Path) -> dict[str, Any]:
    pointer = _recognition_catalog_pointer(runtime)
    validated_catalog_manifest(runtime, pointer)
    return pointer


def _recognition_catalog_paths(runtime: Path) -> tuple[Path, ...]:
    pointer = _recognition_catalog_pointer(runtime)
    catalog_id = str(pointer["catalog_generation_id"])
    root = runtime / "catalog" / "generations" / catalog_id
    return tuple(sorted((path for path in root.rglob("*") if path.is_file()), key=lambda path: path.as_posix()))


def _recognition_catalog_header_paths(runtime: Path) -> tuple[Path, ...]:
    pointer = _recognition_catalog_pointer(runtime)
    root = runtime / "catalog" / "generations" / str(pointer["catalog_generation_id"])
    manifest_path = root / "manifest.json"
    manifest = _read_object(manifest_path)
    paths = {manifest_path}
    for descriptor in manifest.get("files") or ():
        if isinstance(descriptor, Mapping):
            paths.add(_safe_runtime_path(root, descriptor.get("relative_path")))
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def _inventory(runtime: Path) -> list[dict[str, Any]]:
    """Only stat history headers; a newly materialized candidate invalidates fast return."""
    result = []
    root = runtime / "snapshots" / "generations"
    for directory in sorted(root.iterdir()):
        if directory.is_dir():
            path = _safe_runtime_path(root, directory.name + "/manifest.json")
            stat = path.stat() if path.is_file() else None
            result.append({"generation": directory.name, "size": stat.st_size if stat else None,
                           "mtime_ns": stat.st_mtime_ns if stat else None})
    return result


def _header_bindings(runtime: Path, candidate: CohortCandidate) -> dict[str, str | None]:
    paths = {runtime / "snapshots" / name for name in ("current.v2.json", "previous.v2.json")}
    paths.add(runtime / "catalog/current.v2.json")
    paths.update(runtime / "sources" / source / "current.v2.json" for source in SOURCE_ROLES)
    paths.update(runtime / "state/data-service" / name for name in
                 ("cohort_recovery_point.v1.json", "refresh_schedule.v1.json"))
    paths.add(runtime / "snapshots/generations" / candidate.generation_id / "manifest.json")
    catalog_root = runtime / "catalog/generations" / str(candidate.pointers["catalog"]["catalog_generation_id"])
    paths.add(catalog_root / "manifest.json")
    for pointer in candidate.units.values():
        root = runtime / "sources" / str(pointer["source"]) / "runs" / str(pointer["run_id"])
        paths.add(root / "manifest.json")
        # An index may change its child list without changing directory entries.
        if pointer["artifact"]["role"] == "scoped_stats":
            paths.add(_safe_runtime_path(root, pointer["artifact"]["relative_path"]))
    for descriptor in _read_object(catalog_root / "manifest.json")["files"]:
        paths.add(_safe_runtime_path(catalog_root, descriptor["relative_path"]))
    paths.update(_recognition_catalog_header_paths(runtime))
    result = {}
    for path in paths:
        relative = path.relative_to(runtime).as_posix()
        safe = _safe_runtime_path(runtime, relative)
        if safe.exists() and not safe.is_file():
            raise ValueError("receipt header is not a file")
        result[relative] = _sha256(safe) if safe.is_file() else None
    return result


def _primary_bindings_match(runtime: Path, candidate: CohortCandidate) -> bool:
    for source, pointer in candidate.pointers.items():
        if source == "catalog":
            continue
        path = runtime / "sources" / source / "current.v2.json"
        actual = _read_object(path)
        fields = (
            "source", "run_id", "catalog_generation_id", "catalog_sha256",
            "manifest_sha256", "artifact",
        )
        if any(actual.get(field) != pointer.get(field) for field in fields):
            return False
    return True


def write_validation_receipt(runtime_root: str | Path, candidate: CohortCandidate) -> bool:
    """写入完整验证结果；失败只禁用下一次快路径。"""

    runtime = Path(runtime_root)
    current_path = runtime / "snapshots" / "current.v2.json"
    manifest_path = runtime / "snapshots" / "generations" / candidate.generation_id / "manifest.json"
    try:
        if (runtime / JOURNAL_RELATIVE_PATH).exists():
            return False
        current = _read_object(current_path)
        if str(current.get("current_generation_id") or "") != candidate.generation_id:
            return False
        identity = get_build_identity()
        if candidate.snapshot_schema_version == 3:
            if not _primary_bindings_match(runtime, candidate):
                return False
            before_files = _metadata(runtime, _verified_paths(runtime, candidate))
            before_headers = _header_bindings(runtime, candidate)
            before_inventory = _inventory(runtime)
            # Revalidate only current closure: cached candidates must not certify drift.
            if validate_generation_cohort(runtime, candidate.generation_id) != candidate:
                return False
            recognition_pointer = _validate_recognition_catalog(runtime)
            if (_metadata(runtime, _verified_paths(runtime, candidate)) != before_files
                    or _header_bindings(runtime, candidate) != before_headers
                    or _inventory(runtime) != before_inventory):
                return False
        payload = {
            "schema_version": 2 if candidate.snapshot_schema_version == 3 else RECEIPT_SCHEMA_VERSION,
            "validator_contract_version": VALIDATOR_CONTRACT_VERSION,
            "build_id": str(identity.get("build_id") or ""),
            "source_fingerprint": str(identity.get("source_fingerprint") or ""),
            "current_pointer_sha256": _sha256(current_path),
            "generation_id": candidate.generation_id,
            "generation_created_at": candidate.generation_created_at,
            "manifest_health": candidate.manifest_health,
            "manifest_sha256": _sha256(manifest_path),
            "production_pool_id": candidate.production_pool_id,
            "production_pool_count": candidate.production_pool_count,
            "pointers": {key: dict(value) for key, value in candidate.pointers.items()},
            "journal_state": "absent",
            "verified_files": before_files if candidate.snapshot_schema_version == 3 else
                _metadata(runtime, _verified_paths(runtime, candidate)),
        }
        if candidate.snapshot_schema_version == 3:
            payload.update(snapshot_schema_version=3, units={key: dict(value) for key, value in candidate.units.items()},
                           recognition_catalog_pointer=recognition_pointer,
                           header_bindings=before_headers, generation_inventory=before_inventory)
        from hextech.contracts import utc_now_iso

        payload["validated_at"] = utc_now_iso()
        atomic_write_json(receipt_path(runtime), payload, ensure_ascii=False, indent=2)
        return True
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return False


def load_valid_validation_receipt(runtime_root: str | Path) -> CohortCandidate | None:
    """元数据完全命中时返回已验证 candidate；否则不抛错并要求慢路径。"""

    runtime = Path(runtime_root)
    try:
        if (runtime / JOURNAL_RELATIVE_PATH).exists():
            return None
        payload = _read_object(receipt_path(runtime))
        identity = get_build_identity()
        schema = payload.get("schema_version")
        if (
            type(schema) is not int or schema not in {1, 2}
            or int(payload.get("validator_contract_version") or 0) != VALIDATOR_CONTRACT_VERSION
            or str(payload.get("build_id") or "") != str(identity.get("build_id") or "")
            or str(payload.get("source_fingerprint") or "")
            != str(identity.get("source_fingerprint") or "")
            or str(payload.get("journal_state") or "") != "absent"
        ):
            return None
        current_path = runtime / "snapshots" / "current.v2.json"
        generation_id = str(payload.get("generation_id") or "")
        current = _read_object(current_path)
        manifest_path = runtime / "snapshots" / "generations" / generation_id / "manifest.json"
        manifest_schema = _read_object(manifest_path).get("schema_version")
        if (schema == 1 and manifest_schema != 2) or (schema == 2 and manifest_schema != 3):
            return None
        if (
            str(current.get("current_generation_id") or "") != generation_id
            or _sha256(current_path) != str(payload.get("current_pointer_sha256") or "")
            or _sha256(manifest_path) != str(payload.get("manifest_sha256") or "")
        ):
            return None
        files = payload.get("verified_files")
        if not isinstance(files, list) or not files:
            return None
        for item in files:
            if not isinstance(item, Mapping):
                return None
            path = _safe_runtime_path(runtime, item.get("relative_path"))
            stat = path.stat()
            if int(stat.st_size) != int(item.get("size") or -1) or int(stat.st_mtime_ns) != int(
                item.get("mtime_ns") or -1
            ):
                return None
        pointers = payload.get("pointers")
        if not isinstance(pointers, Mapping):
            return None
        if schema == 1 and set(pointers) != {"catalog", *SOURCE_ROLES}:
            return None
        if schema == 2 and (not {"catalog", "aramkit"}.issubset(pointers)
                            or not set(pointers).issubset({"catalog", *SOURCE_ROLES})
                            or payload.get("snapshot_schema_version") != 3
                            or not isinstance(payload.get("units"), Mapping) or not payload["units"]):
            return None
        candidate = CohortCandidate(
            generation_id=generation_id,
            generation_created_at=str(payload.get("generation_created_at") or ""),
            manifest_health=str(payload.get("manifest_health") or "healthy"),
            pointers={str(key): dict(value) for key, value in pointers.items() if isinstance(value, Mapping)},
            production_pool_id=str(payload.get("production_pool_id") or ""),
            production_pool_count=int(payload.get("production_pool_count") or 0),
            snapshot_schema_version=3 if schema == 2 else 2,
            units=payload.get("units", {}) if schema == 2 else {},
        )
        if schema == 2:
            if not _primary_bindings_match(runtime, candidate):
                return None
            certified_recognition = payload.get("recognition_catalog_pointer")
            if not isinstance(certified_recognition, Mapping):
                return None
            active_recognition = _recognition_catalog_pointer(runtime)
            recognition_fields = (
                "schema_version", "catalog_generation_id", "content_sha256",
                "manifest_sha256",
            )
            if any(
                active_recognition.get(field) != certified_recognition.get(field)
                for field in recognition_fields
            ):
                return None
            manifest = _read_object(manifest_path)
            provenance = {f"{item['source']}/{item['run_id']}": item for item in manifest["source_files"]
                          if item["source"] in SOURCE_ROLES}
            if set(provenance) != set(candidate.units):
                return None
            for key, pointer in candidate.units.items():
                item = provenance[key]
                artifact = pointer["artifact"]
                if (pointer["source"] != item["source"] or pointer["run_id"] != item["run_id"]
                        or pointer["catalog_generation_id"] != item["catalog_generation_id"]
                        or pointer["manifest_sha256"] != item["manifest_sha256"]
                        or artifact["sha256"] != item["artifact_sha256"]
                        or artifact["role"] != item["artifact_role"]):
                    return None
            if (_inventory(runtime) != payload.get("generation_inventory")
                    or _header_bindings(runtime, candidate) != payload.get("header_bindings")
                    or _metadata(runtime, _verified_paths(runtime, candidate)) != files):
                return None
        return candidate
    except (OSError, KeyError, TypeError, ValueError):
        return None


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "VALIDATOR_CONTRACT_VERSION",
    "load_valid_validation_receipt",
    "receipt_path",
    "write_validation_receipt",
]

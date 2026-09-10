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

from hextech.infrastructure.persistence.cohort_recovery import CohortCandidate
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
        payload = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
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
            "verified_files": _metadata(runtime, _verified_paths(runtime, candidate)),
        }
        from hextech.contracts import utc_now_iso

        payload["validated_at"] = utc_now_iso()
        atomic_write_json(receipt_path(runtime), payload, ensure_ascii=False, indent=2)
        return True
    except (OSError, TypeError, ValueError):
        return False


def load_valid_validation_receipt(runtime_root: str | Path) -> CohortCandidate | None:
    """元数据完全命中时返回已验证 candidate；否则不抛错并要求慢路径。"""

    runtime = Path(runtime_root)
    try:
        if (runtime / JOURNAL_RELATIVE_PATH).exists():
            return None
        payload = _read_object(receipt_path(runtime))
        identity = get_build_identity()
        if (
            int(payload.get("schema_version") or 0) != RECEIPT_SCHEMA_VERSION
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
        if not isinstance(pointers, Mapping) or set(pointers) != {"catalog", *SOURCE_ROLES}:
            return None
        return CohortCandidate(
            generation_id=generation_id,
            generation_created_at=str(payload.get("generation_created_at") or ""),
            manifest_health=str(payload.get("manifest_health") or "healthy"),
            pointers={str(key): dict(value) for key, value in pointers.items() if isinstance(value, Mapping)},
            production_pool_id=str(payload.get("production_pool_id") or ""),
            production_pool_count=int(payload.get("production_pool_count") or 0),
        )
    except (OSError, TypeError, ValueError):
        return None


__all__ = [
    "RECEIPT_SCHEMA_VERSION",
    "VALIDATOR_CONTRACT_VERSION",
    "load_valid_validation_receipt",
    "receipt_path",
    "write_validation_receipt",
]

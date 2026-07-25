"""生成新布局发布包的可审计资源清单。"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tooling.build.rules import (
    BUNDLE_MANIFEST_NAME,
    FORBIDDEN_BUNDLE_PATH_PARTS,
    SEED_DIR,
    iter_seed_files,
    iter_source_files,
)
from tooling.build.resource_manifest import validate_resource_manifest


BUNDLE_MANIFEST_SCHEMA_VERSION = 3
RUNTIME_CONTRACT_VERSIONS = {
    "overlay_event": 3,
    "sidecar_status": 2,
    "overlay_session_report": 2,
}


def _iter_manifest_path_strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_manifest_path_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_manifest_path_strings(item)
    elif isinstance(value, str):
        normalized = value.replace("\\", "/").strip()
        if normalized:
            yield normalized


def _path_contains_forbidden_part(path_value: str, forbidden: str) -> bool:
    normalized = path_value.replace("\\", "/").strip("/")
    forbidden_norm = forbidden.replace("\\", "/").strip("/")
    if not normalized or not forbidden_norm:
        return False
    if forbidden_norm in {".pyc", ".pyo"}:
        return normalized.endswith(forbidden_norm)
    path_parts = PurePosixPath(normalized).parts
    forbidden_parts = PurePosixPath(forbidden_norm).parts
    return any(
        tuple(path_parts[index : index + len(forbidden_parts)]) == forbidden_parts
        for index in range(len(path_parts) - len(forbidden_parts) + 1)
    )


def manifest_contains_forbidden_path(manifest: dict, forbidden: str) -> bool:
    return any(_path_contains_forbidden_part(value, forbidden) for value in _iter_manifest_path_strings(manifest))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_snapshot_seed(snapshot_root: Path) -> dict[str, Any]:
    """确认 seed current 指向一代可完整读取且非空的 generation。"""

    from hextech.modules.data.generation import DataSnapshotClient

    root = snapshot_root.resolve()
    view = DataSnapshotClient(root).open_view()
    status = view.status()
    if status.get("state") != "ready":
        raise ValueError(f"seed generation invalid: {status.get('reason', 'unknown')}")
    manifest = view.manifest
    return {
        "valid": True,
        "generation_id": manifest.generation_id,
        "champion_count": manifest.champion_count,
        "augment_count": manifest.augment_count,
        "stat_record_count": manifest.stat_record_count,
        "content_fingerprint": manifest.content_fingerprint,
    }


def validate_bundle_manifest(manifest: dict) -> None:
    missing = [
        field
        for field in ("catalog_files", "asset_files", "seed_files", "source_files")
        if not isinstance(manifest.get(field), list) or not manifest[field]
    ]
    if not isinstance(manifest.get("seed_health"), dict) or not manifest["seed_health"].get("valid"):
        missing.append("seed_health")
    if int(manifest.get("schema_version") or 0) != BUNDLE_MANIFEST_SCHEMA_VERSION:
        missing.append("schema_version")
    for field in ("build_id", "built_at", "source_revision", "source_fingerprint"):
        if not str(manifest.get(field) or "").strip():
            missing.append(field)
    if manifest.get("runtime_contracts") != RUNTIME_CONTRACT_VERSIONS:
        missing.append("runtime_contracts")
    if missing:
        raise ValueError("bundle manifest missing critical fields: " + ", ".join(missing))
    for forbidden in FORBIDDEN_BUNDLE_PATH_PARTS:
        if manifest_contains_forbidden_path(manifest, forbidden):
            raise ValueError(f"bundle manifest contains forbidden path: {forbidden}")


def _source_revision(base_dir: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(base_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _source_content_fingerprint(base_dir: Path, source_files: list[str]) -> str:
    digest = hashlib.sha256()
    for relative_name in sorted(source_files):
        path = base_dir / Path(relative_name)
        if not path.is_file():
            raise ValueError(f"bundle source file missing: {relative_name}")
        digest.update(relative_name.replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def build_bundle_manifest(base_dir: Path, *, verified_snapshot_root: Path | None = None) -> dict:
    resource_report = validate_resource_manifest(base_dir)
    packaged_resources = set(resource_report["packaged_files"])
    seed_root = (verified_snapshot_root or (base_dir / SEED_DIR)).resolve()
    seed_sources = iter_seed_files(seed_root)
    seed_files = [(SEED_DIR / path.relative_to(seed_root)).as_posix() for path in seed_sources]
    if verified_snapshot_root is None:
        missing_seed_whitelist = sorted(set(seed_files) - packaged_resources)
        if missing_seed_whitelist:
            raise ValueError("v2 seed 未列入 resources manifest：" + ", ".join(missing_seed_whitelist[:5]))
    source_files = iter_source_files(base_dir)
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    source_fingerprint = _source_content_fingerprint(base_dir, source_files)
    build_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + source_fingerprint[:12]
    manifest = {
        "schema_version": BUNDLE_MANIFEST_SCHEMA_VERSION,
        "generated_at": built_at,
        "built_at": built_at,
        "build_id": build_id,
        "source_revision": _source_revision(base_dir),
        "source_fingerprint": source_fingerprint,
        "runtime_contracts": dict(RUNTIME_CONTRACT_VERSIONS),
        "catalog_files": sorted(path for path in packaged_resources if path.startswith("resources/catalog/")),
        "asset_files": sorted(
            path.removeprefix("resources/assets/")
            for path in packaged_resources
            if path.startswith("resources/assets/")
        ),
        "seed_files": seed_files,
        "seed_health": validate_snapshot_seed(seed_root),
        "seed_sha256": {bundled: _sha256(source) for bundled, source in zip(seed_files, seed_sources)},
        "source_files": source_files,
        "unlisted_resource_files": resource_report["unlisted_files"],
    }
    validate_bundle_manifest(manifest)
    return manifest


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "BUNDLE_MANIFEST_SCHEMA_VERSION",
    "RUNTIME_CONTRACT_VERSIONS",
    "build_bundle_manifest",
    "manifest_contains_forbidden_path",
    "validate_bundle_manifest",
    "validate_snapshot_seed",
]

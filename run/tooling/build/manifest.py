"""生成新布局发布包的可审计资源清单。"""

from __future__ import annotations

import hashlib
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
    if missing:
        raise ValueError("bundle manifest missing critical fields: " + ", ".join(missing))
    for forbidden in FORBIDDEN_BUNDLE_PATH_PARTS:
        if manifest_contains_forbidden_path(manifest, forbidden):
            raise ValueError(f"bundle manifest contains forbidden path: {forbidden}")


def build_bundle_manifest(base_dir: Path, *, verified_snapshot_root: Path | None = None) -> dict:
    resource_report = validate_resource_manifest(base_dir)
    packaged_resources = set(resource_report["packaged_files"])
    seed_root = (verified_snapshot_root or (base_dir / SEED_DIR)).resolve()
    seed_sources = iter_seed_files(seed_root)
    seed_files = [(SEED_DIR / path.relative_to(seed_root)).as_posix() for path in seed_sources]
    missing_seed_whitelist = sorted(set(seed_files) - packaged_resources)
    if missing_seed_whitelist:
        raise ValueError("v2 seed 未列入 resources manifest：" + ", ".join(missing_seed_whitelist[:5]))
    manifest = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "catalog_files": sorted(path for path in packaged_resources if path.startswith("resources/catalog/")),
        "asset_files": sorted(
            path.removeprefix("resources/assets/")
            for path in packaged_resources
            if path.startswith("resources/assets/")
        ),
        "seed_files": seed_files,
        "seed_health": validate_snapshot_seed(seed_root),
        "seed_sha256": {bundled: _sha256(source) for bundled, source in zip(seed_files, seed_sources)},
        "source_files": iter_source_files(base_dir),
        "unlisted_resource_files": resource_report["unlisted_files"],
    }
    validate_bundle_manifest(manifest)
    return manifest


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "build_bundle_manifest",
    "manifest_contains_forbidden_path",
    "validate_bundle_manifest",
    "validate_snapshot_seed",
]

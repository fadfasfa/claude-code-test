"""Only v4 seeds may initialize a new runtime; legacy migration is explicit/offline."""
from __future__ import annotations

import shutil
from pathlib import Path

from hextech.modules.data.generation import DataSnapshotClient
from hextech.modules.data.generation.release_store import checked_file, closure, closure_paths, catalog_paths, read_head, update_head, independent_roots
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import resource_path


def copy_release_closure(source_runtime, target_runtime, generation_id):
    source, target = independent_roots(source_runtime, target_runtime)
    view = DataSnapshotClient(source / "snapshots").open_generation(generation_id)
    closure(source, view.manifest)
    declared = set((*closure_paths(source, view.manifest), Path("snapshots/generations") / generation_id / "manifest.json"))
    source_head = read_head(source / "snapshots")
    for role in ("recognition_catalog", "previous_recognition_catalog"):
        if source_head[role]:
            declared.update(catalog_paths(source, source_head[role]))
    for file_relative in sorted(declared):
        path = checked_file(source, file_relative)
        dest = target / file_relative
        if dest.exists():
            from hextech.modules.data.catalog.versioned import sha256_file
            checked_file(target, file_relative, digest=sha256_file(path), size=path.stat().st_size)
            continue
        for parent in (dest.parent, *dest.parents):
            if parent.exists() and (parent.is_symlink() or getattr(parent.stat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("seed destination refuses reparse paths")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    return DataSnapshotClient(target / "snapshots").open_generation(generation_id)


def install_v4_seed(seed_runtime, target_runtime):
    source, target = independent_roots(seed_runtime, target_runtime)
    # Every supplied Build must prove its baseline, even when a newer target wins.
    source_head = read_head(source / "snapshots")
    supplied = DataSnapshotClient(source / "snapshots").open_generation(source_head["current_generation_id"])
    closure(source, supplied.manifest)
    for role in ("recognition_catalog", "previous_recognition_catalog"):
        if source_head[role]:
            catalog_paths(source, source_head[role])
    client = DataSnapshotClient(target / "snapshots")
    if (target / "snapshots/current.v4.json").exists():
        view = client.open_view()
        head = read_head(target / "snapshots")
        try:
            catalog_paths(target, head["recognition_catalog"])
        except ValueError:
            previous = head["previous_recognition_catalog"]
            try:
                catalog_paths(target, previous)
            except ValueError:
                catalog, _, _, _ = closure(target, view.manifest, validate_units=False)
                previous = {"schema_version": 2, "catalog_generation_id": catalog.generation_id,
                            "content_sha256": catalog.content_sha256, "manifest_sha256": catalog.manifest_sha256}
            update_head(target / "snapshots", recognition_catalog=previous)
        return {**view.status(), "source": "runtime_current"}
    if (target / "snapshots/current.v2.json").exists():
        return {"state": "failed", "reason_code": "migration_required", "generation_id": ""}
    view = supplied
    copied = copy_release_closure(source, target, view.manifest.generation_id)
    for role in ("recognition_catalog", "previous_recognition_catalog"):
        if source_head[role]:
            catalog_paths(target, source_head[role])
    atomic_write_json(target / "snapshots/current.v4.json", {"schema_version": 4,
        "current_generation_id": view.manifest.generation_id, "previous_generation_id": "",
        "recognition_catalog": source_head["recognition_catalog"],
        "previous_recognition_catalog": source_head["previous_recognition_catalog"]}, indent=2)
    return {**copied.status(), "source": "verified_v4_seed"}


def initialize_snapshot(publisher, seed_runtime=None):
    try:
        if publisher.current_path.exists():
            return {**DataSnapshotClient(publisher.root).open_view().status(), "source": "runtime_current"}
        if (publisher.root / "current.v2.json").exists():
            return {"state": "failed", "reason_code": "migration_required", "generation_id": ""}
        seed = Path(seed_runtime) if seed_runtime is not None else resource_path("seeds", "runtime.v4")
        return install_v4_seed(seed, publisher.root.parent)
    except (ValueError, OSError) as exc:
        return {"state": "failed", "reason_code": "v4_seed_unavailable", "generation_id": "", "error_type": type(exc).__name__}

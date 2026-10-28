"""Explicit offline conversion of exactly the valid current v3 closure to a new root.

This command never repairs the source, follows previous, or merges destination data.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from tooling.migration.v3_contracts import LegacySnapshotManifestV3
from hextech.modules.data.generation import DataSnapshotPublisher, DataSnapshotClient
from hextech.modules.data.generation.release_store import checked_file, closure, closure_paths, object_at, independent_roots
from tooling.migration.v3_validation import validate_generation_directory


def convert_current_v3(source_runtime, target_runtime):
    source, target = independent_roots(source_runtime, target_runtime, require_new_target=True)
    head = object_at(checked_file(source, "snapshots/current.v2.json"))
    if head.get("schema_version") != 2 or not isinstance(head.get("current_generation_id"), str):
        raise ValueError("migration requires a valid current v3 generation pointer")
    current = head["current_generation_id"]
    directory = source / "snapshots/generations" / current
    raw = object_at(checked_file(source, f"snapshots/generations/{current}/manifest.json"))
    if raw.get("schema_version") != 3:
        raise ValueError("only current snapshot schema 3 can be migrated")
    legacy = LegacySnapshotManifestV3.from_mapping(raw)
    if legacy.generation_id != current:
        raise ValueError("legacy generation identity mismatch")
    payloads = {}
    for ref in legacy.files:
        path = checked_file(directory, ref.relative_path, digest=ref.sha256, size=ref.size)
        payloads[ref.role] = json.loads(path.read_text(encoding="utf-8"))
    validate_generation_directory(directory, legacy, payloads=payloads)
    publisher = DataSnapshotPublisher(target / "snapshots")
    candidate = publisher._candidate(source_files=legacy.source_files, components=legacy.components,
        source_status=legacy.source_status, health=legacy.health, degraded_sources=legacy.degraded_sources)
    # Validate source units independently from legacy precomputed payloads.
    closure(source, candidate)
    declared = closure_paths(source, candidate)
    target.mkdir(parents=True, exist_ok=False)
    for file_relative in declared:
        path = checked_file(source, file_relative)
        destination = target / file_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    result = publisher.publish(None, source_files=candidate.source_files, components=candidate.components,
        source_status=candidate.source_status, health=candidate.health, degraded_sources=candidate.degraded_sources)
    return {"state": "ready", "generation_id": result.generation_id, "schema_version": 4,
            "source_generation_id": current, "target": str(target),
            "status": DataSnapshotClient(target / "snapshots").status()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-runtime", required=True, type=Path)
    parser.add_argument("--target-runtime", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(convert_current_v3(args.source_runtime, args.target_runtime), ensure_ascii=False))


if __name__ == "__main__":
    main()

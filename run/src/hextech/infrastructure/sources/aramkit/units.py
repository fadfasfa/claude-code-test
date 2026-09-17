"""已验证排行/单英雄的不可变发布单元，不切换任何 current。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import ItemOutcome, SourceHealth, SourceRunManifestV2, utc_now_iso
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.source_runs import (
    build_artifact_descriptor, build_source_pointer, load_source_run_manifest,
    publish_source_run, source_run_dir,
)
from .catalog_binding import CatalogBinding
from .service import _write_artifact, validate_scoped_stats_artifact
from .revisions import PARSER_REVISION, PROJECTION_REVISION


def _candidate_run_ids(digest: str):
    yield "unit-" + digest[:40]
    for attempt in range(1, 17):
        yield f"unit-{digest[:32]}-repair-{attempt:02d}"


def _reuse_unit(run_id: str, *, champion_id: str) -> dict[str, Any] | None:
    existing = load_source_run_manifest("aramkit", run_id)
    if existing is None:
        return None
    if (
        existing.metadata.get("parser_revision") != PARSER_REVISION
        or existing.metadata.get("projection_revision") != PROJECTION_REVISION
    ):
        raise ValueError("ARAMKit unit processing revision mismatch")
    pointer = build_source_pointer(existing).to_dict()
    if champion_id:
        validate_scoped_stats_artifact(pointer)
    else:
        path = source_run_dir("aramkit", run_id) / "rankings.json"
        if existing.artifact is None or hashlib.sha256(path.read_bytes()).hexdigest() != existing.artifact.sha256:
            raise ValueError("ranking unit hash mismatch")
    return pointer


def publish_unit(version: Mapping[str, Any], data: Any, *, binding: CatalogBinding,
                 champion_id: str = "", pointer_output: Path | None = None) -> dict[str, Any]:
    payload = {"version": dict(version), "catalog_id": binding.generation_id,
               "catalog_sha256": binding.content_sha256, "champion_id": champion_id, "data": data,
               "parser_revision": PARSER_REVISION, "projection_revision": PROJECTION_REVISION}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    run_id = ""
    for candidate in _candidate_run_ids(digest):
        directory = source_run_dir("aramkit", candidate)
        if not directory.exists():
            run_id = candidate
            break
        try:
            pointer = _reuse_unit(candidate, champion_id=champion_id)
        except (OSError, ValueError, RuntimeError):
            continue
        if pointer is not None:
            if pointer_output is not None:
                atomic_write_json(pointer_output, pointer, indent=2)
            return pointer
    if not run_id:
        raise ValueError("ARAMKit unit repair slots exhausted")
    if champion_id:
        path, count = _write_artifact(run_id, version, {champion_id: data})
        role, relative = "scoped_stats", "scoped_stats/manifest.json"
        item_ids = [champion_id]
    else:
        path = source_run_dir("aramkit", run_id) / "rankings.json"
        atomic_write_json(path, {"version": dict(version), "rows": data}, ensure_ascii=False, separators=(",", ":"))
        count, role, relative = len(data), "hero_rankings", "rankings.json"
        item_ids = [str(row["id"]) for row in data]
    artifact = build_artifact_descriptor(path, role=role, relative_path=relative,
                                         record_count=count, content_schema_version=1)
    stamp = utc_now_iso()
    manifest = SourceRunManifestV2(
        source="aramkit", run_id=run_id, catalog_generation_id=binding.generation_id,
        catalog_sha256=binding.content_sha256, health=SourceHealth.HEALTHY,
        started_at=stamp, completed_at=stamp, expected_items=len(item_ids),
        successful_items=len(item_ids), confirmed_empty_items=0, failed_items=0,
        artifact=artifact, outcomes=tuple(ItemOutcome(item, "success", role, record_count=1) for item in item_ids),
        metadata={"unit": role, "champion_id": champion_id, "version": dict(version),
                  "source_version": str(version["dataPath"]),
                  "parser_revision": PARSER_REVISION,
                  "projection_revision": PROJECTION_REVISION},
    )
    return publish_source_run(manifest, pointer_output=pointer_output)

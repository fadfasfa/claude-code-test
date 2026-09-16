"""v2 Catalog、source run、generation 与 staging 的保留策略。

清理只发生在 cohort commit 之后。current、previous、活动 promotion journal 及其
generation provenance 引用始终受保护；目录结构或 JSON 无法解析时宁可保留。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


SOURCE_NAMES = ("aramkit", "blitz", "apex", "mayhem")
MINIMUM_AGE = timedelta(days=30)
STAGING_MAX_AGE = timedelta(hours=24)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _unit_references(payload: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Retain explicit v3 unit roots even when a generation manifest is unavailable."""
    runs: set[str] = set()
    catalogs: set[str] = set()
    units = payload.get("units")
    if isinstance(units, Mapping):
        for pointer in units.values():
            if not isinstance(pointer, Mapping):
                continue
            source, run_id = str(pointer.get("source") or ""), str(pointer.get("run_id") or "")
            if source in SOURCE_NAMES and run_id:
                runs.add(f"{source}:{run_id}")
            catalog = str(pointer.get("catalog_generation_id") or "")
            if catalog:
                catalogs.add(catalog)
    components = payload.get("components")
    if payload.get("schema_version") == 3 and isinstance(components, Mapping):
        champions = components.get("champions")
        descriptors = [components.get("ranking"), *(champions.values() if isinstance(champions, Mapping) else ())]
        provenance = payload.get("source_files", [])
        for descriptor in descriptors:
            if not isinstance(descriptor, Mapping):
                continue
            run_id = str(descriptor.get("run_id") or "")
            matches = [item for item in provenance if isinstance(item, Mapping) and item.get("run_id") == run_id] if isinstance(provenance, list) else []
            if run_id:
                sources = {str(item.get("source")) for item in matches} or {"aramkit"}
                runs.update(f"{source}:{run_id}" for source in sources if source in SOURCE_NAMES)
            catalog = str(descriptor.get("catalog_id") or "")
            if catalog:
                catalogs.add(catalog)
    return runs, catalogs


def _generation_provenance(root: Path, generation_id: str) -> tuple[set[str], set[str], set[str]]:
    source_runs: set[str] = set()
    catalog_ids: set[str] = set()
    origin_generations: set[str] = set()
    if not generation_id:
        return source_runs, catalog_ids, origin_generations
    manifest = _read_object(root / "snapshots" / "generations" / generation_id / "manifest.json")
    unit_runs, unit_catalogs = _unit_references(manifest)
    source_runs.update(unit_runs)
    catalog_ids.update(unit_catalogs)
    for item in manifest.get("source_files", []):
        if not isinstance(item, Mapping):
            continue
        source = str(item.get("source") or "")
        run_id = str(item.get("run_id") or "")
        catalog_id = str(item.get("catalog_generation_id") or "")
        if source in SOURCE_NAMES and run_id:
            source_runs.add(f"{source}:{run_id}")
        if catalog_id:
            catalog_ids.add(catalog_id)
    source_status = manifest.get("source_status")
    if isinstance(source_status, Mapping):
        for status in source_status.values():
            if not isinstance(status, Mapping):
                continue
            origin_generation_id = str(status.get("origin_generation_id") or "")
            if origin_generation_id:
                origin_generations.add(origin_generation_id)
    return source_runs, catalog_ids, origin_generations


def _journal_references(root: Path) -> tuple[set[str], set[str], set[str]]:
    journal = _read_object(root / "state" / "data-service" / "promotion_journal.v1.json")
    source_runs: set[str] = set()
    catalog_ids: set[str] = set()
    generations: set[str] = set()
    for section_name in ("old_pointers", "target_pointers"):
        section = journal.get(section_name)
        if not isinstance(section, Mapping):
            continue
        catalog = section.get("catalog")
        if isinstance(catalog, Mapping):
            value = str(catalog.get("catalog_generation_id") or "")
            if value:
                catalog_ids.add(value)
        for source in SOURCE_NAMES:
            pointer = section.get(source)
            if isinstance(pointer, Mapping):
                run_id = str(pointer.get("run_id") or "")
                if run_id:
                    source_runs.add(f"{source}:{run_id}")
        generation = section.get("generation")
        if isinstance(generation, Mapping):
            recovery = generation.get("recovery_point")
            if isinstance(recovery, Mapping):
                runs, catalogs = _unit_references(recovery)
                source_runs.update(runs)
                catalog_ids.update(catalogs)
            for pointer in (generation.get("current"), generation.get("previous")):
                if not isinstance(pointer, Mapping):
                    continue
                generation_id = str(pointer.get("current_generation_id") or pointer.get("generation_id") or "")
                if generation_id:
                    generations.add(generation_id)
    return source_runs, catalog_ids, generations


def _recovery_references(root: Path) -> tuple[set[str], set[str], set[str]]:
    """保护单调恢复点直接引用；结构不完整时 generation provenance 仍兜底保留。"""

    point = _read_object(root / "state" / "data-service" / "cohort_recovery_point.v1.json")
    source_runs: set[str] = set()
    catalog_ids: set[str] = set()
    generations: set[str] = set()
    generation_id = str(point.get("generation_id") or "")
    unit_runs, unit_catalogs = _unit_references(point)
    source_runs.update(unit_runs)
    catalog_ids.update(unit_catalogs)
    if generation_id:
        generations.add(generation_id)
    pointers = point.get("pointers")
    if not isinstance(pointers, Mapping):
        return source_runs, catalog_ids, generations
    catalog = pointers.get("catalog")
    if isinstance(catalog, Mapping):
        catalog_id = str(catalog.get("catalog_generation_id") or "")
        if catalog_id:
            catalog_ids.add(catalog_id)
    for source in SOURCE_NAMES:
        pointer = pointers.get(source)
        if isinstance(pointer, Mapping):
            run_id = str(pointer.get("run_id") or "")
            if run_id:
                source_runs.add(f"{source}:{run_id}")
    generation = pointers.get("generation")
    if isinstance(generation, Mapping):
        for pointer in (generation.get("current"), generation.get("previous")):
            if isinstance(pointer, Mapping):
                referenced = str(pointer.get("current_generation_id") or pointer.get("generation_id") or "")
                if referenced:
                    generations.add(referenced)
    return source_runs, catalog_ids, generations


def _legacy_generation_ids(root: Path) -> set[str]:
    """永久保留切源前的 Hextech 统计代，作为显式回滚边界。"""

    generations_root = root / "snapshots" / "generations"
    if not generations_root.is_dir():
        return set()
    result: set[str] = set()
    for path in generations_root.iterdir():
        if not path.is_dir():
            continue
        manifest = _read_object(path / "manifest.json")
        source_files = manifest.get("source_files")
        if isinstance(source_files, list) and any(
            isinstance(item, Mapping)
            and item.get("source") == "hextech"
            and item.get("artifact_role") == "stats"
            for item in source_files
        ):
            result.add(path.name)
    return result


def protected_references(root: str | Path) -> dict[str, set[str]]:
    runtime_root = Path(root)
    source_runs: set[str] = set()
    catalog_ids: set[str] = set()
    generations: set[str] = set()

    catalog_pointer = _read_object(runtime_root / "catalog" / "current.v2.json")
    catalog_id = str(catalog_pointer.get("catalog_generation_id") or "")
    if catalog_id:
        catalog_ids.add(catalog_id)
    for source in SOURCE_NAMES:
        pointer = _read_object(runtime_root / "sources" / source / "current.v2.json")
        run_id = str(pointer.get("run_id") or "")
        if run_id:
            source_runs.add(f"{source}:{run_id}")

    candidates_dir = runtime_root / "state" / "data-service" / "candidates"
    if candidates_dir.is_dir():
        for path in candidates_dir.glob("*.v2.json"):
            pointer = _read_object(path)
            source = str(pointer.get("source") or path.name.removesuffix(".v2.json"))
            run_id = str(pointer.get("run_id") or "")
            catalog_id = str(pointer.get("catalog_generation_id") or "")
            if source in SOURCE_NAMES and run_id:
                source_runs.add(f"{source}:{run_id}")
            if catalog_id:
                catalog_ids.add(catalog_id)

    for filename, field in (("current.v2.json", "current_generation_id"), ("previous.v2.json", "generation_id")):
        pointer = _read_object(runtime_root / "snapshots" / filename)
        generation_id = str(pointer.get(field) or "")
        if generation_id:
            generations.add(generation_id)
    sidecar_status = _read_object(
        runtime_root / "state" / "game_overlay_sidecar_status.json"
    )
    active_catalog = str(sidecar_status.get("recognition_catalog_id") or sidecar_status.get("catalog_generation_id") or "")
    if active_catalog:
        catalog_ids.add(active_catalog)
    active_vision_generation = str(
        sidecar_status.get("vision_pool_origin_generation_id")
        or sidecar_status.get("vision_pool_generation_id")
        or ""
    )
    if active_vision_generation:
        generations.add(active_vision_generation)

    journal_runs, journal_catalogs, journal_generations = _journal_references(runtime_root)
    source_runs.update(journal_runs)
    catalog_ids.update(journal_catalogs)
    generations.update(journal_generations)
    recovery_runs, recovery_catalogs, recovery_generations = _recovery_references(runtime_root)
    source_runs.update(recovery_runs)
    catalog_ids.update(recovery_catalogs)
    generations.update(recovery_generations)
    generations.update(_legacy_generation_ids(runtime_root))
    pending_generations = list(generations)
    inspected_generations: set[str] = set()
    while pending_generations:
        generation_id = pending_generations.pop()
        if generation_id in inspected_generations:
            continue
        inspected_generations.add(generation_id)
        runs, catalogs, origins = _generation_provenance(runtime_root, generation_id)
        source_runs.update(runs)
        catalog_ids.update(catalogs)
        for origin in origins - generations:
            generations.add(origin)
            pending_generations.append(origin)
    return {"source_runs": source_runs, "catalog_generations": catalog_ids, "generations": generations}


def _select_source_runs(root: Path, source: str, protected: set[str], now: datetime) -> set[Path]:
    runs_root = root / "sources" / source / "runs"
    if not runs_root.is_dir():
        return set()
    successes: list[Path] = []
    failures: list[Path] = []
    keep: set[Path] = set()
    for path in runs_root.iterdir():
        if not path.is_dir():
            continue
        manifest = _read_object(path / "manifest.json")
        if not manifest:
            keep.add(path)
            continue
        if f"{source}:{path.name}" in protected or now - _mtime(path) <= MINIMUM_AGE:
            keep.add(path)
        if manifest.get("health") == "healthy" and manifest.get("publishable") is True:
            successes.append(path)
        else:
            failures.append(path)
    successes.sort(key=_mtime, reverse=True)
    failures.sort(key=_mtime, reverse=True)
    keep.update(successes[:3])
    keep.update(failures[:10])
    return keep


def _remove_unprotected_children(root: Path, keep_names: set[str], *, now: datetime, minimum_age: timedelta) -> int:
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.iterdir():
        if not path.is_dir() or path.name in keep_names:
            continue
        if now - _mtime(path) <= minimum_age:
            continue
        shutil.rmtree(path)
        removed += 1
    return removed


def apply_retention(root: str | Path, *, now: datetime | None = None) -> dict[str, int]:
    runtime_root = Path(root)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    protected = protected_references(runtime_root)
    result = {"source_runs": 0, "catalog_generations": 0, "generations": 0, "staging": 0}

    for source in SOURCE_NAMES:
        runs_root = runtime_root / "sources" / source / "runs"
        keep = _select_source_runs(runtime_root, source, protected["source_runs"], current)
        if runs_root.is_dir():
            for path in runs_root.iterdir():
                if path.is_dir() and path not in keep:
                    shutil.rmtree(path)
                    result["source_runs"] += 1

    result["catalog_generations"] = _remove_unprotected_children(
        runtime_root / "catalog" / "generations",
        protected["catalog_generations"],
        now=current,
        minimum_age=MINIMUM_AGE,
    )
    result["generations"] = _remove_unprotected_children(
        runtime_root / "snapshots" / "generations",
        protected["generations"],
        now=current,
        minimum_age=timedelta(0),
    )
    staging_roots = [
        runtime_root / "catalog" / "staging",
        runtime_root / "snapshots" / "staging",
        *(runtime_root / "sources" / source / "staging" for source in SOURCE_NAMES),
    ]
    for staging_root in staging_roots:
        result["staging"] += _remove_unprotected_children(
            staging_root,
            set(),
            now=current,
            minimum_age=STAGING_MAX_AGE,
        )
    return result


__all__ = ["apply_retention", "protected_references"]

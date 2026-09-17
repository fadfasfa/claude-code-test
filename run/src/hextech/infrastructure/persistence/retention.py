"""v2 Catalog、source run、generation 与 staging 的保留策略。

清理只发生在 cohort commit 之后。current、previous、活动 promotion journal 及其
generation provenance 引用始终受保护；目录结构或 JSON 无法解析时宁可保留。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


SOURCE_NAMES = ("aramkit", "blitz", "apex", "mayhem")
MINIMUM_AGE = timedelta(days=30)
STAGING_MAX_AGE = timedelta(hours=24)


class RetentionSafetyError(RuntimeError):
    """Retention cannot prove that every referenced immutable root is protected."""


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_reference(path: Path) -> dict[str, Any]:
    """Read an existing authority strictly; missing authorities remain optional."""

    if not path.exists():
        return {}
    if _path_has_reparse(path) or not path.is_file():
        raise RetentionSafetyError(f"unsafe reference path: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetentionSafetyError(f"unreadable reference: {path}") from exc
    if not isinstance(payload, dict):
        raise RetentionSafetyError(f"reference is not an object: {path}")
    return payload


def _path_has_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_tree(root: Path, path: Path) -> bool:
    """Reject links/junctions and non-regular entries before recursive deletion."""

    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        path.relative_to(root)
    except ValueError:
        return False
    current = path
    while True:
        if _path_has_reparse(current):
            return False
        if current == root:
            break
        current = current.parent
    pending = [path]
    while pending:
        current = pending.pop()
        if _path_has_reparse(current):
            return False
        try:
            info = current.lstat()
        except OSError:
            return False
        if stat.S_ISDIR(info.st_mode):
            try:
                pending.extend(current.iterdir())
            except OSError:
                return False
        elif not stat.S_ISREG(info.st_mode):
            return False
    return True


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
    manifest_path = root / "snapshots" / "generations" / generation_id / "manifest.json"
    if not manifest_path.exists():
        return source_runs, catalog_ids, origin_generations
    manifest = _read_reference(manifest_path)
    source_files = manifest.get("source_files")
    if not isinstance(source_files, list):
        raise RetentionSafetyError(f"generation manifest has invalid source_files: {generation_id}")
    unit_runs, unit_catalogs = _unit_references(manifest)
    source_runs.update(unit_runs)
    catalog_ids.update(unit_catalogs)
    for item in source_files:
        if not isinstance(item, Mapping):
            raise RetentionSafetyError(f"generation manifest has invalid provenance: {generation_id}")
        source = str(item.get("source") or "")
        run_id = str(item.get("run_id") or "")
        catalog_id = str(item.get("catalog_generation_id") or "")
        if source in SOURCE_NAMES and run_id:
            source_runs.add(f"{source}:{run_id}")
        if catalog_id:
            catalog_ids.add(catalog_id)
    source_status = manifest.get("source_status")
    if source_status is not None and not isinstance(source_status, Mapping):
        raise RetentionSafetyError(f"generation manifest has invalid source_status: {generation_id}")
    if isinstance(source_status, Mapping):
        for status in source_status.values():
            if not isinstance(status, Mapping):
                raise RetentionSafetyError(f"generation manifest has invalid source status: {generation_id}")
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


def _staging_references(root: Path) -> tuple[set[str], set[str]]:
    """Protect immutable runs emitted by a worker but not published yet."""

    source_runs: set[str] = set()
    catalog_ids: set[str] = set()
    staging = root / "snapshots" / "staging"
    if not staging.is_dir():
        return source_runs, catalog_ids
    if not _safe_tree(staging, staging):
        raise RetentionSafetyError(f"unsafe staging tree: {staging}")
    for path in staging.rglob("*.json"):
        if path.name not in {"pointer.json", "catalog.json"} and not path.name.endswith(".pointer.json"):
            continue
        payload = _read_reference(path)
        source = str(payload.get("source") or "")
        run_id = str(payload.get("run_id") or "")
        catalog_id = str(payload.get("catalog_generation_id") or "")
        if source in SOURCE_NAMES and run_id:
            source_runs.add(f"{source}:{run_id}")
        if catalog_id:
            catalog_ids.add(catalog_id)
    return source_runs, catalog_ids


def _validate_authorities(root: Path) -> None:
    authorities = (
        root / "catalog" / "current.v2.json",
        *(root / "sources" / source / "current.v2.json" for source in SOURCE_NAMES),
        root / "snapshots" / "current.v2.json",
        root / "snapshots" / "previous.v2.json",
        root / "state" / "data-service" / "promotion_journal.v1.json",
        root / "state" / "data-service" / "cohort_recovery_point.v1.json",
        root / "state" / "game_overlay_sidecar_status.json",
        root / "state" / "game_overlay_visibility.v1.json",
    )
    payloads = {path: _read_reference(path) for path in authorities}
    catalog_path = root / "catalog" / "current.v2.json"
    if catalog_path.exists() and not str(payloads[catalog_path].get("catalog_generation_id") or ""):
        raise RetentionSafetyError("catalog current has no generation identity")
    for source in SOURCE_NAMES:
        source_path = root / "sources" / source / "current.v2.json"
        if source_path.exists() and not str(payloads[source_path].get("run_id") or ""):
            raise RetentionSafetyError(f"{source} current has no run identity")
    current_path = root / "snapshots" / "current.v2.json"
    previous_path = root / "snapshots" / "previous.v2.json"
    current = payloads[current_path]
    previous = payloads[previous_path]
    if current_path.exists() and not str(current.get("current_generation_id") or ""):
        raise RetentionSafetyError("snapshot current has no generation identity")
    if previous_path.exists() and not str(previous.get("generation_id") or ""):
        raise RetentionSafetyError("snapshot previous has no generation identity")
    journal_path = root / "state" / "data-service" / "promotion_journal.v1.json"
    journal = payloads[journal_path]
    if journal_path.exists() and any(
        not isinstance(journal.get(key), Mapping) for key in ("old_pointers", "target_pointers")
    ):
        raise RetentionSafetyError("promotion journal has unknown pointer structure")
    recovery_path = root / "state" / "data-service" / "cohort_recovery_point.v1.json"
    recovery = payloads[recovery_path]
    if recovery_path.exists() and (
        not str(recovery.get("generation_id") or "")
        or not isinstance(recovery.get("pointers"), Mapping)
    ):
        raise RetentionSafetyError("recovery point has unknown pointer structure")
    candidates = root / "state" / "data-service" / "candidates"
    if candidates.exists():
        if not candidates.is_dir() or not _safe_tree(candidates, candidates):
            raise RetentionSafetyError("unsafe candidate pointer tree")
        for path in candidates.glob("*.v2.json"):
            payload = _read_reference(path)
            source = str(payload.get("source") or path.name.removesuffix(".v2.json"))
            if source not in SOURCE_NAMES or not str(payload.get("run_id") or ""):
                raise RetentionSafetyError(f"candidate pointer has unknown identity: {path}")


def _validate_protected_roots(root: Path, protected: Mapping[str, set[str]]) -> None:
    for generation_id in protected["generations"]:
        path = root / "snapshots" / "generations" / generation_id
        if not path.is_dir() or _path_has_reparse(path):
            raise RetentionSafetyError(f"protected generation is unavailable: {generation_id}")
        manifest = _read_reference(path / "manifest.json")
        if not isinstance(manifest.get("source_files"), list):
            raise RetentionSafetyError(f"protected generation manifest is incomplete: {generation_id}")
    for identity in protected["source_runs"]:
        source, separator, run_id = identity.partition(":")
        path = root / "sources" / source / "runs" / run_id
        if not separator or source not in SOURCE_NAMES or not path.is_dir() or _path_has_reparse(path):
            raise RetentionSafetyError(f"protected source run is unavailable: {identity}")
        if not _read_reference(path / "manifest.json"):
            raise RetentionSafetyError(f"protected source manifest is incomplete: {identity}")
    for catalog_id in protected["catalog_generations"]:
        path = root / "catalog" / "generations" / catalog_id
        if not path.is_dir() or _path_has_reparse(path):
            raise RetentionSafetyError(f"protected catalog is unavailable: {catalog_id}")


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
        source_catalog_id = str(pointer.get("catalog_generation_id") or "")
        if run_id:
            source_runs.add(f"{source}:{run_id}")
        if source_catalog_id:
            catalog_ids.add(source_catalog_id)

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
    host_status = _read_object(
        runtime_root / "state" / "game_overlay_visibility.v1.json"
    )
    active_stats_generation = str(
        host_status.get("stats_generation_id") or host_status.get("data_generation_id") or ""
    )
    if active_stats_generation:
        generations.add(active_stats_generation)

    journal_runs, journal_catalogs, journal_generations = _journal_references(runtime_root)
    source_runs.update(journal_runs)
    catalog_ids.update(journal_catalogs)
    generations.update(journal_generations)
    recovery_runs, recovery_catalogs, recovery_generations = _recovery_references(runtime_root)
    source_runs.update(recovery_runs)
    catalog_ids.update(recovery_catalogs)
    generations.update(recovery_generations)
    staging_runs, staging_catalogs = _staging_references(runtime_root)
    source_runs.update(staging_runs)
    catalog_ids.update(staging_catalogs)
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


def _planned_removals(
    root: Path,
    protected: Mapping[str, set[str]],
    *,
    now: datetime,
) -> list[tuple[str, Path]]:
    planned: list[tuple[str, Path]] = []
    for source in SOURCE_NAMES:
        runs_root = root / "sources" / source / "runs"
        keep = _select_source_runs(root, source, protected["source_runs"], now)
        if runs_root.is_dir():
            planned.extend(
                ("source_runs", path)
                for path in runs_root.iterdir()
                if path.is_dir() and path not in keep
            )
    policies = (
        ("catalog_generations", root / "catalog" / "generations", protected["catalog_generations"], MINIMUM_AGE),
        ("generations", root / "snapshots" / "generations", protected["generations"], timedelta(0)),
        ("staging", root / "catalog" / "staging", set(), STAGING_MAX_AGE),
        ("staging", root / "snapshots" / "staging", set(), STAGING_MAX_AGE),
        *(('staging', root / "sources" / source / "staging", set(), STAGING_MAX_AGE) for source in SOURCE_NAMES),
    )
    for category, directory, keep_names, minimum_age in policies:
        if not directory.is_dir():
            continue
        planned.extend(
            (category, path)
            for path in directory.iterdir()
            if path.is_dir() and path.name not in keep_names and now - _mtime(path) > minimum_age
        )
    return planned


def _empty_result(disposition: str, reason: str = "") -> dict[str, int | str]:
    return {
        "source_runs": 0,
        "catalog_generations": 0,
        "generations": 0,
        "staging": 0,
        "disposition": disposition,
        "reason": reason,
    }


def apply_retention(root: str | Path, *, now: datetime | None = None) -> dict[str, int | str]:
    runtime_root = Path(root)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        _validate_authorities(runtime_root)
        protected = protected_references(runtime_root)
        _validate_protected_roots(runtime_root, protected)
        planned = _planned_removals(runtime_root, protected, now=current)
        unsafe = next((path for _category, path in planned if not _safe_tree(runtime_root, path)), None)
        if unsafe is not None:
            raise RetentionSafetyError(f"unsafe removal tree: {unsafe}")
    except (OSError, RetentionSafetyError) as exc:
        return _empty_result("skipped_unsafe_state", str(exc))
    result = _empty_result("completed")
    for category, path in planned:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            result["disposition"] = "failed"
            result["reason"] = str(exc)
            return result
        result[category] = int(result[category]) + 1
    return result


def apply_cohort_retention(
    root: str | Path,
    *,
    workers_idle: bool,
    now: datetime | None = None,
) -> dict[str, int | str]:
    """Run cohort retention only after every acquisition worker has become idle."""

    if not workers_idle:
        return _empty_result("skipped_workers_active", "acquisition workers are not idle")
    return apply_retention(root, now=now)


apply_postcommit_retention = apply_cohort_retention


__all__ = [
    "RetentionSafetyError",
    "apply_cohort_retention",
    "apply_postcommit_retention",
    "apply_retention",
    "protected_references",
]

"""Validation and atomic publication of the single production release head."""
from __future__ import annotations

import hashlib
import json
import stat
import threading
from collections import OrderedDict
from pathlib import Path

from hextech.contracts import CatalogManifestV2, SourcePointerV2, SourceRunManifestV2
from hextech.modules.data.catalog.versioned import CatalogView, sha256_file, validate_catalog_files

from .validation import SnapshotValidationError


HEAD_LOCK = threading.RLock()
HEAD_FIELDS = {"schema_version", "current_generation_id", "previous_generation_id",
               "recognition_catalog", "previous_recognition_catalog"}
_CATALOG_CACHE = OrderedDict()
_CATALOG_CACHE_LOCK = threading.RLock()


def _signature(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise SnapshotValidationError("Catalog verification cache refuses reparse paths")
    if stat.S_ISDIR(info.st_mode):
        return info.st_mode, 0, 0, info.st_ino
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


def verified_catalog(root, expected_sha):
    """Reuse proven immutable Catalog hashes only while every file identity is unchanged."""
    root = Path(root)
    key = str(root.absolute()), expected_sha
    with _CATALOG_CACHE_LOCK:
        cached = _CATALOG_CACHE.get(key)
        if cached is not None:
            catalog, stamps = cached
            try:
                if all(_signature(path) == stamp for path, stamp in stamps.items()):
                    _CATALOG_CACHE.move_to_end(key)
                    return catalog
            except OSError:
                pass
            _CATALOG_CACHE.pop(key, None)
        manifest_path = checked_file(root, "manifest.json", digest=expected_sha)
        manifest = CatalogManifestV2.from_mapping(object_at(manifest_path))
        paths = {manifest_path}
        for descriptor in manifest.files:
            # validate_catalog_files performs the hash; this pass proves path/reparse safety.
            paths.add(checked_file(root, descriptor.relative_path, size=descriptor.size))
        assets_path = root / "augment_assets.v1.json"
        if assets_path.exists():
            for entry in object_at(assets_path)["entries"]:
                paths.add(checked_file(root, entry["relative_path"], size=entry["size"]))
        for path in tuple(paths):
            paths.update(path.parents)
        before = {path: _signature(path) for path in paths}
        try:
            validate_catalog_files(root, manifest)
        except (ValueError, RuntimeError, OSError) as exc:
            raise SnapshotValidationError("Catalog artifact validation failed") from exc
        stamps = {path: _signature(path) for path in paths}
        if before != stamps:
            raise SnapshotValidationError("Catalog changed during hash validation")
        catalog = CatalogView(root, manifest, expected_sha)
        _CATALOG_CACHE[key] = (catalog, stamps)
        while len(_CATALOG_CACHE) > 4:
            _CATALOG_CACHE.popitem(last=False)
        return catalog


def independent_roots(source, target, *, require_new_target=False):
    """Inspect lexical reparse ancestors before canonical containment checks."""
    source_input, target_input = Path(source).absolute(), Path(target).absolute()
    for value in (source_input, target_input):
        for ancestor in (value, *value.parents):
            try:
                info = ancestor.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("independent runtime roots refuse reparse paths")
    source_root, target_root = source_input.resolve(strict=True), target_input.resolve(strict=False)
    if (source_root == target_root or source_root in target_root.parents or target_root in source_root.parents
            or (require_new_target and target_root.exists())):
        raise ValueError("target must be new and independent of source")
    return source_root, target_root


def read_head(snapshot_root):
    payload = object_at(Path(snapshot_root) / "current.v4.json")
    if set(payload) != HEAD_FIELDS or payload.get("schema_version") != 4:
        raise SnapshotValidationError("invalid unified v4 release head")
    if any(not isinstance(payload[key], str) for key in ("current_generation_id", "previous_generation_id")):
        raise SnapshotValidationError("invalid release head generation identities")
    if any(not isinstance(payload[key], dict) for key in ("recognition_catalog", "previous_recognition_catalog")):
        raise SnapshotValidationError("invalid recognition head references")
    return payload


def update_head(snapshot_root, *, current_generation_id=None, previous_generation_id=None, recognition_catalog=None):
    """The one commit owner preserves the independently moving role references."""
    from hextech.modules.data.ports.atomic import atomic_write_json
    root = Path(snapshot_root)
    with HEAD_LOCK:
        head = read_head(root) if (root / "current.v4.json").exists() else {
            "schema_version": 4, "current_generation_id": "", "previous_generation_id": "",
            "recognition_catalog": {}, "previous_recognition_catalog": {}}
        if current_generation_id is not None:
            head["previous_generation_id"] = previous_generation_id if previous_generation_id is not None else head["current_generation_id"]
            head["current_generation_id"] = current_generation_id
        if recognition_catalog is not None and recognition_catalog != head["recognition_catalog"]:
            def identity(pointer):
                return tuple(pointer.get(k, "") for k in ("catalog_generation_id", "content_sha256", "manifest_sha256"))
            if identity(recognition_catalog) != identity(head["recognition_catalog"]):
                from hextech.modules.data.catalog.versioned import load_runtime_catalog_from_pointer
                previous = next((p for p in (head["recognition_catalog"], head["previous_recognition_catalog"])
                                 if p and load_runtime_catalog_from_pointer(p, runtime_root=root.parent) is not None), {})
                head["previous_recognition_catalog"] = previous
            head["recognition_catalog"] = dict(recognition_catalog)
        atomic_write_json(root / "current.v4.json", head, indent=2)
        return head


def object_at(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return value
    except (OSError, ValueError) as exc:
        raise SnapshotValidationError(f"invalid release object: {path}") from exc


def checked_file(root, relative, *, digest=None, size=None):
    root = Path(root).absolute()
    path = root / str(relative)
    if Path(str(relative)).is_absolute() or ".." in Path(str(relative)).parts:
        raise SnapshotValidationError("release path escapes root")
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise SnapshotValidationError("release refuses reparse paths")
        if root.resolve() not in path.resolve().parents or not path.is_file():
            raise SnapshotValidationError("release artifact missing or outside root")
        if size is not None and path.stat().st_size != size:
            raise SnapshotValidationError("release artifact size mismatch")
        if digest is not None and sha256_file(path) != digest:
            raise SnapshotValidationError("release artifact hash mismatch")
        return path
    except OSError as exc:
        raise SnapshotValidationError("release artifact inaccessible") from exc


def checked_object(root, relative, *, digest, size=None):
    """Parse exactly the bytes whose digest was checked, including lazy reads."""
    path = checked_file(root, relative)
    try:
        raw = path.read_bytes()
        if size is not None and len(raw) != size:
            raise ValueError("release artifact size mismatch")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("release artifact hash mismatch")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("expected release object")
        return path, payload
    except (OSError, ValueError) as exc:
        raise SnapshotValidationError(str(exc)) from exc


def closure(runtime, manifest, *, validate_units=True):
    """Validate exact source/Catalog/child closure; return immutable unit pointers."""
    runtime = Path(runtime)
    catalogs = {p.catalog_generation_id for p in manifest.source_files}
    if len(catalogs) != 1:
        raise SnapshotValidationError("release mixes Catalog identities")
    catalog_id = next(iter(catalogs))
    catalog_root = runtime / "catalog" / "generations" / catalog_id
    catalog_provenance = [p for p in manifest.source_files if p.source == "catalog"]
    if not catalog_provenance or len({p.manifest_sha256 for p in catalog_provenance}) != 1:
        raise SnapshotValidationError("release Catalog provenance absent or mixed")
    sha = catalog_provenance[0].manifest_sha256
    catalog = verified_catalog(catalog_root, sha)
    cm = catalog.manifest
    if cm.catalog_generation_id != catalog_id:
        raise SnapshotValidationError("release Catalog ID mismatch")
    expected = {(p.artifact_role, p.artifact_sha256, p.record_count, p.content_schema_version) for p in catalog_provenance}
    actual = {(f.role, f.sha256, f.record_count, f.content_schema_version) for f in cm.files}
    if expected != actual or len(expected) != len(catalog_provenance) or any(p.run_id != catalog_id for p in catalog_provenance):
        raise SnapshotValidationError("release Catalog descriptor closure mismatch")
    pointers = {}
    for ref in manifest.source_files:
        if ref.source == "catalog":
            continue
        run_root = runtime / "sources" / ref.source / "runs" / ref.run_id
        path = checked_file(run_root, "manifest.json", digest=ref.manifest_sha256)
        run = SourceRunManifestV2.from_mapping(object_at(path))
        art = run.artifact
        if (not run.publishable or art is None or run.source != ref.source or run.run_id != ref.run_id
                or run.catalog_generation_id != catalog_id or run.catalog_sha256 != cm.content_sha256
                or art.role != ref.artifact_role or art.sha256 != ref.artifact_sha256
                or art.record_count != ref.record_count or art.content_schema_version != ref.content_schema_version):
            raise SnapshotValidationError("release source provenance mismatch")
        artifact = (checked_file(run_root, art.relative_path, digest=art.sha256, size=art.size)
                    if validate_units or art.role == "hero_rankings" else run_root / art.relative_path)
        if validate_units and art.role == "scoped_stats":
            index = object_at(artifact)
            children = index.get("files")
            if (index.get("schema_version") != 1 or not isinstance(children, list)
                    or len(children) != index.get("champion_count") or not children):
                raise SnapshotValidationError("release scoped index invalid")
            seen = set()
            total = 0
            from hextech.modules.data.scoped_stats import _index_records
            for child in children:
                hero = str(child.get("champion_id") or "")
                if not hero or hero in seen:
                    raise SnapshotValidationError("release duplicate or absent scoped hero")
                seen.add(hero)
                target = checked_file(artifact.parent, child["relative_path"], digest=child["sha256"], size=child["size"])
                payload = object_at(target)
                if str(payload.get("champion", {}).get("id")) != hero or any(not payload.get("stages", {}).get(s) for s in ("1", "2", "3", "4")):
                    raise SnapshotValidationError("release scoped hero incomplete")
                all_records = _index_records(payload.get("all"), scope="all")
                if len(all_records) != child["record_count"]:
                    raise SnapshotValidationError("release scoped child count mismatch")
                for stage in ("1", "2", "3", "4"):
                    _index_records(payload["stages"][stage], scope=stage)
                total += int(child["record_count"])
            if total != art.record_count or total != index.get("record_count"):
                raise SnapshotValidationError("release scoped record count mismatch")
        pointers[(ref.source, ref.run_id, ref.artifact_role)] = SourcePointerV2(
            ref.source, ref.run_id, catalog_id, cm.content_sha256, ref.manifest_sha256, art,
            run.completed_at, run.completed_at).to_dict()
    ranking = manifest.components["ranking"]
    ranking_key = ("aramkit", str(ranking.get("run_id") or ""), "hero_rankings")
    if ranking_key not in pointers or ranking["catalog_id"] != catalog_id:
        raise SnapshotValidationError("release ranking is not bound to provenance")
    ranking_pointer = pointers[ranking_key]
    ranking_path = runtime / "sources/aramkit/runs" / ranking_key[1] / ranking_pointer["artifact"]["relative_path"]
    ranking_payload = object_at(ranking_path)
    ranking_version = str(ranking_payload.get("version", {}).get("dataPath") or "")
    ranking_ids = {str(row.get("id") or "") for row in ranking_payload.get("rows", [])}
    if (not ranking_version or ranking_version != ranking["source_version"]
            or ranking_ids != set(manifest.components["champions"])):
        raise SnapshotValidationError("release ranking version/cohort mismatch")
    if sum(ref.source == "aramkit" and ref.artifact_role == "hero_rankings" for ref in manifest.source_files) != 1:
        raise SnapshotValidationError("release must reference one ranking unit")
    for source in ("apex", "mayhem"):
        if sum(ref.source == source for ref in manifest.source_files) > 1:
            raise SnapshotValidationError("release duplicate optional source")
    heroes = {}
    for hero, component in manifest.components["champions"].items():
        if component["catalog_id"] != catalog_id:
            raise SnapshotValidationError("release hero Catalog mismatch")
        if component["complete"]:
            key = ("aramkit", str(component.get("run_id") or ""), "scoped_stats")
            if key not in pointers:
                raise SnapshotValidationError("release complete hero lacks exact unit")
            index_path = runtime / "sources" / "aramkit" / "runs" / key[1] / pointers[key]["artifact"]["relative_path"]
            if validate_units and str(hero) not in {str(c["champion_id"]) for c in object_at(index_path)["files"]}:
                raise SnapshotValidationError("release hero points to a different unit")
            run_version = object_at(runtime / "sources/aramkit/runs" / key[1] / "manifest.json").get("metadata", {}).get("version", {}).get("dataPath")
            if run_version != component["source_version"]:
                raise SnapshotValidationError("release hero source version mismatch")
            heroes[str(hero)] = pointers[key]
        elif component.get("run_id"):
            raise SnapshotValidationError("pending release hero cannot claim an adopted unit")
    referenced = {ranking_key, *(("aramkit", p["run_id"], "scoped_stats") for p in heroes.values())}
    adopted = {key for key in pointers if key[0] == "aramkit"}
    if referenced != adopted:
        raise SnapshotValidationError("release contains an unreferenced core unit")
    return catalog, pointers[ranking_key], heroes, {
        source: pointer for (source, _run, _role), pointer in pointers.items() if source in {"apex", "mayhem"}}


def fingerprint(source_files, components, status, health, degraded):
    semantic_status = {}
    for key, value in status.items():
        row = value.to_dict() if hasattr(value, "to_dict") else dict(value)
        row.pop("checked_at", None)
        row.pop("stale_age_seconds", None)
        semantic_status[key] = row
    references = sorted(source_files, key=lambda p: (p.source, p.run_id, p.artifact_role))
    body = {"sources": [p.to_dict() for p in references], "components": components,
            "source_status": semantic_status, "health": health, "degraded_sources": sorted(degraded)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def closure_paths(runtime, manifest):
    """Exact declared closure only; adjacent notes, logs and unknown assets stay private."""
    runtime = Path(runtime)
    catalog, _ranking, _heroes, _optional = closure(runtime, manifest)
    paths = {catalog.root / "manifest.json"}
    for descriptor in catalog.manifest.files:
        paths.add(catalog.root / descriptor.relative_path)
    assets = object_at(catalog.root / "augment_assets.v1.json")
    for entry in assets["entries"]:
        paths.add(checked_file(catalog.root, entry["relative_path"], digest=entry["sha256"], size=entry["size"]))
    for ref in manifest.source_files:
        if ref.source == "catalog":
            continue
        base = runtime / "sources" / ref.source / "runs" / ref.run_id
        manifest_path = checked_file(base, "manifest.json", digest=ref.manifest_sha256)
        paths.add(manifest_path)
        run = SourceRunManifestV2.from_mapping(object_at(manifest_path))
        artifact = checked_file(base, run.artifact.relative_path, digest=run.artifact.sha256, size=run.artifact.size)
        paths.add(artifact)
        if run.artifact.role == "scoped_stats":
            for child in object_at(artifact)["files"]:
                paths.add(checked_file(artifact.parent, child["relative_path"], digest=child["sha256"], size=child["size"]))
    return tuple(sorted(path.relative_to(runtime) for path in paths))


def catalog_paths(runtime, pointer):
    from hextech.modules.data.catalog.versioned import load_runtime_catalog_from_pointer
    runtime = Path(runtime)
    catalog = load_runtime_catalog_from_pointer(pointer, runtime_root=runtime)
    if catalog is None:
        raise SnapshotValidationError("recognition Catalog closure invalid")
    paths = {catalog.root / "manifest.json", *(catalog.root / d.relative_path for d in catalog.manifest.files)}
    for entry in object_at(catalog.root / "augment_assets.v1.json")["entries"]:
        paths.add(checked_file(catalog.root, entry["relative_path"], digest=entry["sha256"], size=entry["size"]))
    return tuple(sorted(path.relative_to(runtime) for path in paths))

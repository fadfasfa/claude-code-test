"""Conservative v4 release retention; legacy/source/raw assets are never swept."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hextech.contracts.release import ReleaseManifest
from hextech.modules.data.generation.release_store import HEAD_LOCK, read_head, object_at, checked_file, closure

MINIMUM_AGE = timedelta(days=30)

class RetentionSafetyError(ValueError):
    pass

def protected_references(root):
    root = Path(root)
    result = {"generations": set(), "catalog_generations": set(), "source_runs": set()}
    head_path = root / "snapshots/current.v4.json"
    if head_path.exists():
        head = read_head(root / "snapshots")
        result["generations"].update(str(head[k]) for k in ("current_generation_id", "previous_generation_id") if head[k])
        for role in ("recognition_catalog", "previous_recognition_catalog"):
            if head[role]:
                result["catalog_generations"].add(head[role]["catalog_generation_id"])
    for name in ("game_overlay_host_status.json", "game_overlay_sidecar_status.json", "overlay_runtime.json"):
        path = root / "state" / name
        if path.exists():
            payload = object_at(checked_file(root, path.relative_to(root)))
            for key in ("stats_generation_id", "generation_id", "vision_pool_origin_generation_id"):
                if payload.get(key):
                    result["generations"].add(str(payload[key]))
            for key in ("recognition_catalog_id", "active_recognition_catalog_id", "pending_recognition_catalog_id"):
                if payload.get(key):
                    result["catalog_generations"].add(str(payload[key]))
    for generation in tuple(result["generations"]):
        path = root / "snapshots/generations" / generation / "manifest.json"
        if not path.exists():
            raise RetentionSafetyError("active release manifest missing")
        manifest = ReleaseManifest.from_mapping(object_at(checked_file(root, path.relative_to(root))))
        for ref in manifest.source_files:
            result["catalog_generations"].add(ref.catalog_generation_id)
            if ref.source != "catalog":
                result["source_runs"].add(f"{ref.source}:{ref.run_id}")
    return result

def apply_retention(root, *, now=None):
    root = Path(root)
    result = {"disposition": "completed", "removed_generations": 0, "removed_source_runs": 0,
              "removed_catalogs": 0, "removed_staging": 0, "reason": "legacy_source_raw_preserved"}
    if not (root / "snapshots/current.v4.json").exists():
        return {**result, "disposition": "skipped_unverified", "reason": "no_v4_head"}
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with HEAD_LOCK:
        try:
            protected = protected_references(root)
            # Any broken live closure stops cleanup; no partial trust from a pointer name.
            head = read_head(root / "snapshots")
            for key in ("current_generation_id", "previous_generation_id"):
                if head[key]:
                    manifest = ReleaseManifest.from_mapping(object_at(root / "snapshots/generations" / head[key] / "manifest.json"))
                    closure(root, manifest)
            directory = root / "snapshots/generations"
            for candidate in directory.iterdir():
                if candidate.name in protected["generations"] or not candidate.is_dir() or candidate.is_symlink():
                    continue
                # Only our exact one-file schema4 directories are owned. Unknown and legacy trees remain untouched.
                if {p.name for p in candidate.iterdir()} != {"manifest.json"}:
                    continue
                path = checked_file(root, candidate.relative_to(root) / "manifest.json")
                try:
                    manifest = ReleaseManifest.from_mapping(object_at(path))
                    stamp = datetime.fromisoformat(manifest.created_at.replace("Z", "+00:00"))
                except (ValueError, KeyError, TypeError):
                    continue
                if manifest.generation_id != candidate.name or stamp > current_time - MINIMUM_AGE:
                    continue
                path.unlink()
                candidate.rmdir()
                result["removed_generations"] += 1
        except (ValueError, OSError) as exc:
            return {**result, "disposition": "skipped_unverified", "reason": type(exc).__name__}
    return result

def apply_cohort_retention(root, *, workers_idle, now=None):
    if not workers_idle:
        return {"disposition": "skipped_workers_active", "removed_generations": 0,
                "removed_source_runs": 0, "removed_catalogs": 0, "removed_staging": 0}
    return apply_retention(root, now=now)

apply_postcommit_retention = apply_cohort_retention

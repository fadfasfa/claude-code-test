"""Hash-bound incremental snapshot projection, independent of optional sources."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
from typing import Any

from hextech.contracts import SourcePointerV2, SourceProvenance, utc_now_iso
from hextech.infrastructure.persistence.production_pool_binding import bind_production_pool
from hextech.infrastructure.persistence.source_artifacts import validated_source_artifact
from hextech.infrastructure.sources.aramkit.service import validate_scoped_stats_artifact
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_champion_core_data
from hextech.modules.data.catalog.versioned import sha256_file
from hextech.modules.data.source_runs import source_run_dir
from hextech.modules.recommendation.hero_rankings import build_hero_rankings


@dataclass(frozen=True)
class IncrementalBuild:
    payloads: Mapping[str, Any]
    source_files: tuple[SourceProvenance, ...]
    components: Mapping[str, Any]
    source_status: Mapping[str, Any]


def _stamp(path: Path) -> tuple[int, int, int, int]:
    for candidate in (path, *path.parents):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"projection refuses reparse path: {candidate}")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"projection requires regular file: {path}")
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


@dataclass
class _Loaded:
    pointer: SourcePointerV2
    payload: dict[str, Any]
    paths: dict[Path, tuple[int, int, int, int]]
    provenance: SourceProvenance
    manifest: dict[str, Any]
    scoped_payloads: dict[str, Any] | None = None

    def verify_unchanged(self) -> None:
        for path, expected in self.paths.items():
            if _stamp(path) != expected:
                raise ValueError(f"immutable projection input changed: {path.name}")


class IncrementalProjection:
    """Retain verified immutable unit projections across per-champion publishes.

    File identity checks avoid rereading every historical champion body. This
    assumes task-owned immutable run directories, not adversarial kernel writes.
    """
    def __init__(self, catalog: Any, runtime_root: Path | None = None):
        self.runtime_root = runtime_root
        self.catalog = catalog
        self._cache: dict[str, _Loaded] = {}
        self._used: set[str] = set()
        self._catalog_paths = {}
        for descriptor in catalog.manifest.files:
            path = Path(catalog.root) / descriptor.relative_path
            before = _stamp(path)
            if sha256_file(path) != descriptor.sha256 or path.stat().st_size != descriptor.size:
                raise ValueError("Catalog file identity mismatch")
            if _stamp(path) != before:
                raise ValueError("Catalog changed during validation")
            self._catalog_paths[path] = before
        manifest_path = Path(catalog.root) / "manifest.v2.json"
        if manifest_path.exists():
            before = _stamp(manifest_path)
            if sha256_file(manifest_path) != catalog.manifest_sha256:
                raise ValueError("Catalog manifest identity mismatch")
            self._catalog_paths[manifest_path] = before
        self.champions = load_champion_core_data(catalog.root)
        self.augments = load_augment_manifest_entries(catalog.root)
        self._pool: dict[str, Any] = {}
        bind_production_pool(self._pool, catalog=catalog, stats_pointer={}, legacy_baseline=False)

    def _load(self, source: str, payload: Mapping[str, Any], role: str) -> _Loaded:
        pointer = SourcePointerV2.from_mapping(payload)
        if (pointer.source != source or pointer.artifact.role != role
                or pointer.catalog_generation_id != self.catalog.generation_id
                or pointer.catalog_sha256 != self.catalog.content_sha256):
            raise ValueError(f"{source} pointer Catalog/source/role mismatch")
        identity = pointer.to_dict()
        identity.pop("last_success_at", None)
        identity.pop("completed_at", None)
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self._used.add(fingerprint)
        cached = self._cache.get(fingerprint)
        if cached is not None:
            cached.verify_unchanged()
            return cached
        manifest_path = (self.runtime_root / "sources" / source / "runs" / pointer.run_id if self.runtime_root is not None else source_run_dir(source, pointer.run_id)) / "manifest.json"
        artifact_unresolved = (self.runtime_root / "sources" / source / "runs" / pointer.run_id if self.runtime_root is not None else source_run_dir(source, pointer.run_id)) / pointer.artifact.relative_path
        paths = {path: _stamp(path) for path in (manifest_path, artifact_unresolved)}
        if self.runtime_root is not None:
            from hextech.modules.data.generation.release_store import checked_file
            base = self.runtime_root / "sources" / source / "runs" / pointer.run_id
            checked_file(base, "manifest.json", digest=pointer.manifest_sha256)
            path = checked_file(base, pointer.artifact.relative_path, digest=pointer.artifact.sha256, size=pointer.artifact.size)
        else:
            path = validated_source_artifact(source, payload, expected_role=role)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{source} artifact must be an object")
        scoped_payloads = None
        if source == "aramkit" and role == "scoped_stats":
            for child in raw.get("files", []):
                relative = Path(str(child["relative_path"]))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("scoped child path escapes artifact")
                child_path = path.parent / relative
                paths[child_path] = _stamp(child_path)
            index = raw if self.runtime_root is not None else validate_scoped_stats_artifact(payload)
            scoped_payloads = {}
            for child in index["files"]:
                child_path = path.parent / child["relative_path"]
                scoped = json.loads(child_path.read_text(encoding="utf-8"))
                from hextech.modules.data.scoped_stats import _index_records
                _index_records(scoped["all"], scope="all")
                for stage in ("1", "2", "3", "4"):
                    _index_records(scoped["stages"][stage], scope=stage)
                scoped_payloads[str(child["champion_id"])] = {"champion": scoped["champion"],
                    "complete": bool(scoped["all"]) and all(scoped["stages"][stage] for stage in ("1", "2", "3", "4"))}
        provenance = SourceProvenance(source=source, run_id=pointer.run_id,
            catalog_generation_id=pointer.catalog_generation_id,
            artifact_role=role, artifact_sha256=pointer.artifact.sha256,
            record_count=pointer.artifact.record_count, manifest_sha256=pointer.manifest_sha256,
            content_schema_version=pointer.artifact.content_schema_version)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        loaded = _Loaded(pointer, raw, paths, provenance, manifest, scoped_payloads)
        loaded.verify_unchanged()
        self._cache[fingerprint] = loaded
        return loaded

    def _status(self, loaded: _Loaded) -> dict[str, Any]:
        pointer = loaded.pointer
        metadata = loaded.manifest.get("metadata", {})
        version = metadata.get("version", {})
        data_at = "" if pointer.source == "aramkit" else str(loaded.manifest["completed_at"])
        if isinstance(version, Mapping) and version.get("buildTimeUnixMs"):
            try:
                parsed = datetime.fromtimestamp(float(version["buildTimeUnixMs"]) / 1000, tz=timezone.utc)
                if 0 < parsed.timestamp() <= datetime.now(timezone.utc).timestamp():
                    data_at = parsed.isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OverflowError, OSError):
                pass
        candidate = metadata.get("data_at")
        if pointer.source != "aramkit" and isinstance(candidate, str):
            try:
                parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                if parsed.tzinfo is not None and parsed <= datetime.now(timezone.utc):
                    data_at = parsed.astimezone(timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        return {"catalog_id": self.catalog.generation_id, "data_at": data_at,
                "checked_at": utc_now_iso(), "freshness": "fresh", "data_status": "fresh",
                "run_id": pointer.run_id, "artifact_sha256": pointer.artifact.sha256,
                "manifest_sha256": pointer.manifest_sha256, "record_count": pointer.artifact.record_count}

    def validate_champion(self, champion_id: str, pointer: Mapping[str, Any]) -> None:
        """Validate/cache one staged hero without rebuilding a whole snapshot."""
        unit = self._load("aramkit", pointer, "scoped_stats")
        scoped = (unit.scoped_payloads or {}).get(champion_id, {})
        if str(scoped.get("champion", {}).get("id")) != champion_id or not scoped.get("complete"):
            raise ValueError("champion component is incomplete")

    def _ranking_cohort(
        self,
        ranking_pointer: Mapping[str, Any],
    ) -> tuple[_Loaded, str, list[dict[str, Any]]]:
        for path, expected in self._catalog_paths.items():
            if _stamp(path) != expected:
                raise ValueError("immutable Catalog changed")
        ranking = self._load("aramkit", ranking_pointer, "hero_rankings")
        version = ranking.payload.get("version")
        rows = ranking.payload.get("rows")
        if not isinstance(version, Mapping) or not version.get("dataPath") or not isinstance(rows, list) or not rows:
            raise ValueError("ranking artifact schema/version invalid")
        summaries = [
            {
                "id": row["id"],
                "source_rank": row["rank"],
                "source_tier": row["tier"],
                "sample_count": row["stats"]["sampleCount"],
                "win_rate": row["stats"]["winRate"],
                "pick_rate": row["stats"]["pickRate"],
            }
            for row in rows
        ]
        return ranking, str(version["dataPath"]), build_hero_rankings(summaries, self.champions)

    def ranked_champion_ids(self, ranking_pointer: Mapping[str, Any]) -> frozenset[str]:
        """Validate a ranking unit and expose the exact cohort accepted by projection."""

        _ranking, _source_version, champions = self._ranking_cohort(ranking_pointer)
        return frozenset(str(item["id"]) for item in champions)

    def build_references(self, ranking_pointer, champion_pointers, optional_pointers):
        """Production publishing validates units without constructing duplicate consumer payloads."""
        if set(optional_pointers) - {"apex", "mayhem"}:
            raise ValueError("retired optional source")
        self._used = set()
        ranking, version, champions = self._ranking_cohort(ranking_pointer)
        expected = {row["id"] for row in champions}
        if set(champion_pointers) - expected:
            raise ValueError("champion contribution absent from ranking cohort")
        components = {"ranking": {"source_version": version, "catalog_id": self.catalog.generation_id,
                                   "run_id": ranking.pointer.run_id}, "champions": {}}
        sources = [*self.catalog.provenance(), ranking.provenance]
        states = {"catalog": {"catalog_id": self.catalog.generation_id, "data_at": self.catalog.manifest.created_at,
                  "data_status": "fresh", "freshness": "fresh", "manifest_sha256": self.catalog.manifest_sha256},
                  "aramkit": self._status(ranking)}
        for hero in sorted(expected, key=int):
            pointer = champion_pointers.get(hero)
            component = {"source_version": version, "catalog_id": self.catalog.generation_id, "complete": False}
            if pointer:
                self.validate_champion(hero, pointer)
                unit = self._load("aramkit", pointer, "scoped_stats")
                component.update(complete=True, run_id=unit.pointer.run_id,
                    source_version=str(unit.manifest["metadata"]["version"]["dataPath"]))
                if unit.provenance not in sources:
                    sources.append(unit.provenance)
            components["champions"][hero] = component
        for source, role in (("apex", "synergy"), ("mayhem", "combos")):
            if source in optional_pointers:
                try:
                    unit = self._load(source, optional_pointers[source], role)
                    sources.append(unit.provenance)
                    states[source] = self._status(unit)
                except (OSError, ValueError, TypeError, KeyError):
                    states[source] = {"catalog_id": self.catalog.generation_id, "data_status": "unavailable",
                                      "data_reason": "optional_source_rejected"}
            else:
                states[source] = {"catalog_id": self.catalog.generation_id, "data_status": "pending",
                                  "data_reason": "optional_source_pending"}
        self._cache = {key: value for key, value in self._cache.items() if key in self._used}
        return IncrementalBuild({}, tuple(sources), components, states)

"""Hash-bound incremental snapshot projection, independent of optional sources."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
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
from hextech.infrastructure.sources.aramkit.projection import build_aramkit_payloads
from hextech.infrastructure.sources.aramkit.service import validate_scoped_stats_artifact
from hextech.infrastructure.sources.blitz.projection import build_blitz_details
from hextech.modules.acquisition.mayhem.merge import merge_mayhem_payloads
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries, load_champion_core_data
from hextech.modules.data.catalog.versioned import sha256_file
from hextech.modules.data.source_runs import source_run_dir
from hextech.modules.recommendation.hero_rankings import build_hero_rankings
from hextech.modules.recommendation.hints import (
    build_overlay_hint_cache, enrich_overlay_hint_cache_with_catalog,
    enrich_overlay_hint_cache_with_synergy,
)
from hextech.modules.recommendation.identities import build_augment_identity_payload


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
    details: dict[str, Any] | None = None
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
    def __init__(self, catalog: Any):
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
        manifest_path = source_run_dir(source, pointer.run_id) / "manifest.json"
        artifact_unresolved = source_run_dir(source, pointer.run_id) / pointer.artifact.relative_path
        paths = {path: _stamp(path) for path in (manifest_path, artifact_unresolved)}
        path = validated_source_artifact(source, payload, expected_role=role)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{source} artifact must be an object")
        details = None
        scoped_payloads = None
        if source == "aramkit" and role == "scoped_stats":
            for child in raw.get("files", []):
                relative = Path(str(child["relative_path"]))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("scoped child path escapes artifact")
                child_path = path.parent / relative
                paths[child_path] = _stamp(child_path)
            index = validate_scoped_stats_artifact(payload)
            scoped_payloads = {}
            for child in index["files"]:
                child_path = path.parent / child["relative_path"]
                scoped_payloads[str(child["champion_id"])] = json.loads(child_path.read_text(encoding="utf-8"))
            _, details = build_aramkit_payloads(payload, catalog=self.catalog)
        elif source == "blitz":
            details = build_blitz_details(payload, catalog=self.catalog)
        provenance = SourceProvenance(source=source, run_id=pointer.run_id,
            catalog_generation_id=pointer.catalog_generation_id,
            artifact_role=role, artifact_sha256=pointer.artifact.sha256,
            record_count=pointer.artifact.record_count, manifest_sha256=pointer.manifest_sha256,
            content_schema_version=pointer.artifact.content_schema_version)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        loaded = _Loaded(pointer, raw, paths, provenance, manifest, details, scoped_payloads)
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
        detail = next((value for value in (unit.details or {}).values()
                       if str(value.get("hero_id")) == champion_id), None)
        if (not detail or not detail.get("augments") or
                str(scoped.get("champion", {}).get("id")) != champion_id or
                any(not scoped.get("stages", {}).get(stage) for stage in ("1", "2", "3", "4"))):
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

    def build(self, ranking_pointer: Mapping[str, Any],
              champion_pointers: Mapping[str, Mapping[str, Any]],
              optional_pointers: Mapping[str, Mapping[str, Any]]) -> IncrementalBuild:
        self._used = set()
        ranking, source_version, champions = self._ranking_cohort(ranking_pointer)
        ranked_ids = {item["id"] for item in champions}
        if set(champion_pointers) - ranked_ids:
            raise ValueError("champion contribution absent from ranking cohort")
        component_base = {"source_version": source_version, "catalog_id": self.catalog.generation_id}
        components = {"ranking": {**component_base, "run_id": ranking.pointer.run_id}, "champions": {}}
        sources = [*self.catalog.provenance(), ranking.provenance]
        source_status: dict[str, Any] = {"aramkit": self._status(ranking), "catalog": {
            "catalog_id": self.catalog.generation_id, "data_at": self.catalog.manifest.created_at,
            "checked_at": utc_now_iso(), "freshness": "fresh", "data_status": "fresh",
            "manifest_sha256": self.catalog.manifest_sha256}}
        optional = {}
        for source, role in (("blitz", "augment_ranking"), ("apex", "synergy"), ("mayhem", "combos")):
            candidate = optional_pointers.get(source)
            if not candidate:
                source_status[source] = {"catalog_id": self.catalog.generation_id, "data_status": "pending",
                                         "data_reason": "optional_source_pending", "checked_at": utc_now_iso()}
                continue
            try:
                optional[source] = self._load(source, candidate, role)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                source_status[source] = {"catalog_id": self.catalog.generation_id, "data_status": "unavailable",
                    "data_reason": "optional_source_rejected", "coverage": {"reason": str(exc)},
                    "checked_at": utc_now_iso()}
                continue
            sources.append(optional[source].provenance)
            source_status[source] = self._status(optional[source])
        synergy = deepcopy(optional["apex"].payload) if "apex" in optional else {}
        if "mayhem" in optional:
            synergy = merge_mayhem_payloads(apex_payload=synergy, mayhem_payload=optional["mayhem"].payload,
                manifest_payload=self.augments, core_payload=self.champions)["merged_payload"]
        fallback = optional["blitz"].details if "blitz" in optional else {}
        details = {}
        complete_count = 0
        for champion in champions:
            hero_id, name = champion["id"], champion["name"]
            pointer = champion_pointers.get(hero_id)
            complete = False
            run_id = ""
            hero_source_version = source_version
            detail = deepcopy((fallback or {}).get(name))
            if pointer:
                unit = self._load("aramkit", pointer, "scoped_stats")
                if hero_id not in {str(item["champion_id"]) for item in unit.payload["files"]}:
                    raise ValueError("champion component hero mismatch")
                hero_source_version = str(unit.payload.get("data_path") or "")
                if not hero_source_version:
                    raise ValueError("champion component source version missing")
                detail = deepcopy((unit.details or {}).get(name))
                if not detail or str(detail.get("hero_id")) != hero_id or not detail.get("augments"):
                    raise ValueError("champion component is incomplete")
                scoped = (unit.scoped_payloads or {}).get(hero_id, {})
                if (str(scoped.get("champion", {}).get("id")) != hero_id
                        or any(not scoped.get("stages", {}).get(stage) for stage in ("1", "2", "3", "4"))):
                    raise ValueError("champion component stages are incomplete")
                complete = True
                complete_count += 1
                run_id = unit.pointer.run_id
                if unit.provenance not in sources:
                    sources.append(unit.provenance)
            if detail:
                detail["data_status"] = "data_stale" if complete and hero_source_version != source_version else "fresh"
                if not complete:
                    detail["data_reason"] = "blitz_ranking_only"
                detail["synergy"] = deepcopy(synergy.get(hero_id) or synergy.get(name) or {})
            else:
                detail = {"hero_id": hero_id, "augments": [], "data_status": "pending", "synergy": {}}
            details[name] = detail
            components["champions"][hero_id] = {**component_base, "source_version": hero_source_version,
                                                "complete": complete, **({"run_id": run_id} if run_id else {})}
        source_status["aramkit"]["coverage"] = {"complete": complete_count, "expected": len(champions)}
        hero_ids = {item["name"]: item["id"] for item in champions}
        hints = build_overlay_hint_cache(details, include_private_stats=True, source_tag="incremental-aramkit",
                                         synergy_by_name={}, champion_id_by_name=hero_ids)
        # Immutable content identity must not change because the same inputs were re-projected.
        hints["generated_at"] = datetime.fromisoformat(str(ranking.manifest["completed_at"]).replace("Z", "+00:00")).timestamp()
        identities = {"champions": {item["id"]: item["name"] for item in champions},
                      **build_augment_identity_payload(hints, self.augments)}
        enrich_overlay_hint_cache_with_catalog(hints, self.augments)
        hints["source"]["production_augment_pool"] = deepcopy(self._pool["source"]["production_augment_pool"])
        if any(isinstance(value, Mapping) and value.get("synergy_items") for value in synergy.values()):
            projected = deepcopy(hints)
            try:
                enrich_overlay_hint_cache_with_synergy(projected, synergy, previous_report=None)
            except ValueError as exc:
                # Optional enrichment cannot invalidate already complete stats.
                for source in ("apex", "mayhem"):
                    if source in optional:
                        source_status[source].update(data_status="unavailable", data_reason="optional_projection_rejected",
                                                     coverage={"reason": str(exc)})
                        sources.remove(optional[source].provenance)
                for detail in details.values():
                    detail["synergy"] = {}
            else:
                hints = projected
        self._cache = {key: value for key, value in self._cache.items() if key in self._used}
        return IncrementalBuild({"champions": champions, "champion_hextech": details,
                                 "overlay_hints": hints, "identities": identities},
                                tuple(sources), components, source_status)

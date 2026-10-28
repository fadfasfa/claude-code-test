"""Bounded, read-only query projection over release unit references."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from functools import wraps
import threading

from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool
from hextech.modules.acquisition.mayhem.merge import merge_mayhem_payloads
from hextech.modules.recommendation.hero_rankings import build_hero_rankings
from hextech.modules.recommendation.card_projection import project_augment_cards
from hextech.modules.recommendation.hints import normalize_augment_name, normalize_augment_id

from .release_store import closure, checked_file, checked_object
from .validation import SnapshotValidationError


def _locked(method):
    @wraps(method)
    def query(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return query


class ReleaseQuery:
    """Ranking and identity queries never open hero bodies; hero LRU is bounded."""
    def __init__(self, runtime, manifest):
        self._lock = threading.RLock()
        self.runtime, self.manifest = runtime, manifest
        self.catalog, self.ranking, self.heroes, self.optional = closure(runtime, manifest, validate_units=False)
        # Project only bytes verified against the fixed Catalog descriptors. A
        # separate unverified read after checking a file could ingest new bytes.
        descriptors = {row.relative_path: row for row in self.catalog.manifest.files}
        def catalog_object(name):
            row = descriptors[name]
            return checked_object(self.catalog.root, name, digest=row.sha256, size=row.size)[1]
        heroes = catalog_object("英雄目录.v1.json")
        aliases = {str(row.get("heroName", "")).strip(): list(row.get("aliases", []))
                   for row in heroes.get("aliases", []) if isinstance(row, dict) and isinstance(row.get("aliases", []), list)}
        self.champions_catalog = {}
        for hero, row in heroes.get("id_to_name", {}).items():
            if not isinstance(row, dict):
                continue
            fallback = heroes.get("id_to_detail", {}).get(str(hero).strip(), "")
            if isinstance(fallback, dict):
                fallback = fallback.get("heroName") or fallback.get("name") or ""
            name = str(row.get("heroName") or row.get("name") or fallback or "").strip()
            if name and str(hero).strip():
                self.champions_catalog[str(hero).strip()] = {
                    "id": str(hero).strip(), "hero_id": str(hero).strip(), "name": name,
                    "title": str(row.get("title") or "").strip(),
                    "en_name": str(row.get("enName") or row.get("en_name") or "").strip(),
                    "aliases": aliases.get(name, []),
                }
        self.augments = {str(row.get("cdragon_id")): row for row in catalog_object("海克斯资源目录.v1.json").get("entries", [])
                         if isinstance(row, dict)}
        self.pool = catalog_object("augment_identities.v2.json")
        validate_production_augment_pool(self.pool)
        self.pool = {**self.pool, "catalog_generation_id": self.catalog.generation_id,
                     "catalog_sha256": self.catalog.content_sha256, "catalog_manifest_sha256": self.catalog.manifest_sha256}
        self.allowed = set(self.pool["canonical_ids"])
        self._hero_cache = OrderedDict()
        self._ranking_cache = None
        self._synergy_cache = None
        self._hints_cache = None

    def unit(self, pointer):
        base = self.runtime / "sources" / pointer["source"] / "runs" / pointer["run_id"]
        checked_file(base, "manifest.json", digest=pointer["manifest_sha256"])
        artifact = pointer["artifact"]
        return checked_object(base, artifact["relative_path"], digest=artifact["sha256"], size=artifact["size"])

    def _ensure_ranking(self):
        if self._ranking_cache is None:
            _, payload = self.unit(self.ranking)
            rows = [{"id": row["id"], "source_rank": row["rank"], "source_tier": row["tier"],
                     "sample_count": row["stats"]["sampleCount"], "win_rate": row["stats"]["winRate"],
                     "pick_rate": row["stats"]["pickRate"]} for row in payload["rows"]]
            self._ranking_cache = build_hero_rankings(rows, self.champions_catalog)
        return self._ranking_cache

    @_locked
    def champions(self):
        return deepcopy(self._ensure_ranking())

    @_locked
    def champion(self, value):
        needle = str(value).strip().casefold()
        row = next((row for row in self._ensure_ranking() if needle in {row["id"].casefold(), row["name"].casefold()}), None)
        return deepcopy(row) if row is not None else None

    def _ensure_synergy(self):
        if self._synergy_cache is None:
            data = self.unit(self.optional["apex"])[1] if "apex" in self.optional else {}
            if "mayhem" in self.optional:
                data = merge_mayhem_payloads(apex_payload=data, mayhem_payload=self.unit(self.optional["mayhem"])[1],
                    manifest_payload=list(self.augments.values()), core_payload=self.champions_catalog)["merged_payload"]
            self._synergy_cache = data
        return self._synergy_cache

    @_locked
    def synergy(self):
        return deepcopy(self._ensure_synergy())

    @_locked
    def detail(self, value):
        champion = self.champion(value)
        if champion is None:
            return None
        hero, name = champion["id"], champion["name"]
        if hero in self._hero_cache:
            self._hero_cache.move_to_end(hero)
            return deepcopy(self._hero_cache[hero])
        pointer = self.heroes.get(hero)
        if pointer is None:
            return {"hero_id": hero, "hero_name": name, "augments": [], "data_status": "pending", "synergy": {}}
        index_path, index = self.unit(pointer)
        matches = [row for row in index["files"] if str(row["champion_id"]) == hero]
        if len(matches) != 1:
            raise SnapshotValidationError("hero unit index mismatch")
        child = matches[0]
        _, payload = checked_object(index_path.parent, child["relative_path"], digest=child["sha256"], size=child["size"])
        if str(payload.get("champion", {}).get("id")) != hero or any(not payload.get("stages", {}).get(s) for s in ("1", "2", "3", "4")):
            raise SnapshotValidationError("hero unit is incomplete")
        from hextech.modules.data.scoped_stats import _index_records
        records = _index_records(payload["all"], scope="all")
        if len(records) != child["record_count"]:
            raise SnapshotValidationError("hero unit child count mismatch")
        for stage in ("1", "2", "3", "4"):
            _index_records(payload["stages"][stage], scope=stage)
        cards = project_augment_cards(payload["all"], hero, name, self.augments, allowed_ids=self.allowed)
        synergy = self._ensure_synergy()
        data_status = "fresh" if self.manifest.components["champions"][hero]["source_version"] == self.manifest.components["ranking"]["source_version"] else "data_stale"
        detail = {"hero_id": hero, "hero_name": name, "source": "aramkit", "dataset": "all",
                  "comprehensive": cards, "augments": cards, "winrate_only": sorted(cards, key=lambda r: -r["winrate"]),
                  "data_status": data_status, "synergy": synergy.get(hero) or synergy.get(name) or {}}
        self._hero_cache[hero] = detail
        while len(self._hero_cache) > 2:
            self._hero_cache.popitem(last=False)
        return deepcopy(detail)

    def _ensure_hints(self):
        if self._hints_cache is None:
            # This projection contains names/identity/synergy only, never all-hero statistics.
            hints, aliases = {}, {}
            conflicts = set()
            from hextech.modules.recommendation.hints import enrich_overlay_hint_cache_with_synergy, _card_aliases
            aliases.update({identity: identity for identity in self.allowed})
            for row in self.pool["identities"]:
                identity, name = row["canonical_id"], row["name"]
                card_aliases = _card_aliases(row)
                hints[identity] = {"augment_id": identity, "name": name, "tier": row.get("tier", ""),
                                   "aliases": card_aliases, "synergies": []}
                for key in (name, normalize_augment_id(name), normalize_augment_name(name), *card_aliases):
                    if not key:
                        continue
                    if key in self.allowed:
                        continue  # a name can never shadow another canonical numeric ID
                    if key in aliases and aliases[key] != identity:
                        conflicts.add(key)
                    aliases[key] = identity
            for key in conflicts:
                aliases.pop(key, None)
            cache = {"schema_version": 1, "generated_at": __import__("time").time(),
                     "source": {"production_augment_pool": deepcopy(self.pool), "private_policy_stats_enabled": True},
                     "hints": hints, "name_index": aliases}
            if self.optional:
                enrich_overlay_hint_cache_with_synergy(cache, self._ensure_synergy(), previous_report=None)
            self._hints_cache = cache
        return self._hints_cache

    @_locked
    def hints(self):
        return deepcopy(self._ensure_hints())

    @_locked
    def resolve(self, value):
        cache = self._ensure_hints()
        raw = str(value or "").strip()
        identity = next((cache["name_index"].get(k) for k in (raw, normalize_augment_id(raw), normalize_augment_name(raw)) if cache["name_index"].get(k)), "")
        if identity not in self.allowed:
            return None
        row = cache["hints"][identity]
        return {"canonical_id": identity, "name": row["name"], "vision_id": identity, "stats_available": True}

    @_locked
    def identities(self):
        cache = self._ensure_hints()
        return {"schema_version": 2, "champions": {r["id"]: r["name"] for r in self.champions()},
                "augments": {k: v["name"] for k, v in cache["hints"].items()},
                "augment_aliases": deepcopy(cache["name_index"]), "catalog_augments": {}}

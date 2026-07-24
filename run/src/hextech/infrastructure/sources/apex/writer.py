"""Apex 抓取职责拆分模块。"""
from __future__ import annotations

from hextech.infrastructure.sources.apex.common import (
    ChampionInfo,
    Path,
    SynergyEntry,
    _atomic_write_json,
    _output_file_lock,
    build_champion_lookup,
    logger,
    normalize_name,
    normalize_slug,
)
class SynergyWriter:
    def __init__(self, core_info: dict[str, ChampionInfo]):
        self.core_info = core_info
        self.champion_lookup = build_champion_lookup(core_info)

    def build_payload(self, synergy_map: dict[str, list[SynergyEntry]]) -> dict:
        final_data = {}
        missing_synergy = []
        for champ_id, champ_info in self.core_info.items():
            synergies = self._find_synergies_for_champion(champ_info, synergy_map)
            if not synergies:
                missing_synergy.append(champ_info.name)
            final_data[champ_id] = {
                "id": champ_id,
                "name": champ_info.name,
                "title": champ_info.title,
                "en_name": champ_info.en_name,
                "aliases": champ_info.aliases,
                "synergies": [entry.to_display_string() for entry in synergies],
                "synergy_items": [
                    {
                        "augment_names": entry.augment_names,
                        "tier": entry.tier,
                        "rating": entry.rating,
                        "tag": entry.tag,
                        "author": entry.author,
                        "is_original": entry.is_original,
                        "content": entry.content,
                        "upvotes": entry.upvotes,
                        "downvotes": entry.downvotes,
                    }
                    for entry in synergies
                ],
            }
        if missing_synergy:
            logger.warning("部分英雄暂无联动：count=%s", len(missing_synergy))
        return final_data

    def write(self, output_path: Path, payload: dict) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = output_path.with_suffix(output_path.suffix + ".lock")
        with _output_file_lock(lock_path):
            _atomic_write_json(output_path, payload)

    def _lookup_synergies_by_key(self, key: str, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
        for candidate in (normalize_slug(key), normalize_name(key)):
            if candidate and candidate in synergy_map:
                return synergy_map[candidate]
        return []

    def _alias_belongs_to_champion(self, alias: str, champ_info: ChampionInfo) -> bool:
        for candidate in (normalize_name(alias), normalize_slug(alias)):
            if not candidate:
                continue
            matched = self.champion_lookup.get(candidate)
            if matched is not None and matched.id != champ_info.id:
                return False
        return True

    def _find_synergies_for_champion(self, champ_info: ChampionInfo, synergy_map: dict[str, list[SynergyEntry]]) -> list[SynergyEntry]:
        primary_keys = [champ_info.id, champ_info.slug, champ_info.en_name, champ_info.name, champ_info.title]
        for key in primary_keys:
            synergies = self._lookup_synergies_by_key(key, synergy_map)
            if synergies:
                return synergies

        keys = [alias for alias in champ_info.aliases if self._alias_belongs_to_champion(alias, champ_info)]
        for key in keys:
            synergies = self._lookup_synergies_by_key(key, synergy_map)
            if synergies:
                return synergies
        return []

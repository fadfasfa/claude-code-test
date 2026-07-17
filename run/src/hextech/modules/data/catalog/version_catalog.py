"""版本数据统一目录。

本模块读取 `resources/catalog` 的权威 JSON，并生成 Web 和模块层需要的只读投影。
它不触发远端刷新，也不写回资源文件。

调用方: catalog.aliases、display.web.api、overlay.context; 关键依赖: scraping._paths。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hextech.modules.data.ports.paths import STATIC_DATA_DIR


HERO_CATALOG_FILENAME = "英雄目录.v1.json"
AUGMENT_RESOURCE_CATALOG_FILENAME = "海克斯资源目录.v1.json"


def get_version_data_dir(config_dir: str | Path | None = None) -> Path:
    return Path(config_dir) if config_dir is not None else Path(STATIC_DATA_DIR)


def get_hero_catalog_path(config_dir: str | Path | None = None) -> Path:
    return get_version_data_dir(config_dir) / HERO_CATALOG_FILENAME


def get_augment_resource_catalog_path(config_dir: str | Path | None = None) -> Path:
    return get_version_data_dir(config_dir) / AUGMENT_RESOURCE_CATALOG_FILENAME


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _dict_payload(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list:
    return value if isinstance(value, list) else []


def load_hero_catalog(config_dir: str | Path | None = None) -> dict:
    return _dict_payload(_read_json(get_hero_catalog_path(config_dir)))


def load_augment_resource_catalog(config_dir: str | Path | None = None) -> dict:
    return _dict_payload(_read_json(get_augment_resource_catalog_path(config_dir)))


def load_champion_alias_records(config_dir: str | Path | None = None) -> list[dict]:
    """返回统一英雄目录中的别名记录。"""

    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    records = catalog.get("aliases")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]

    return []


def load_champion_alias_to_id(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    payload = catalog.get("alias_to_id")
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def load_champion_id_to_name(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    payload = catalog.get("id_to_detail")
    if isinstance(payload, dict):
        projected = {}
        for hero_id, hero_name in payload.items():
            normalized_id = str(hero_id).strip()
            if not normalized_id:
                continue
            if isinstance(hero_name, str):
                name = hero_name.strip()
            elif isinstance(hero_name, dict):
                name = str(hero_name.get("heroName") or hero_name.get("name") or "").strip()
            else:
                name = ""
            if name:
                projected[normalized_id] = name
        if projected:
            return projected
    return {}


def load_champion_id_to_detail(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    payload = catalog.get("id_to_name")
    if isinstance(payload, dict):
        projected = {}
        for hero_id, detail in payload.items():
            normalized_id = str(hero_id).strip()
            if not normalized_id or not isinstance(detail, dict):
                continue
            projected[normalized_id] = dict(detail)
        if projected:
            return projected
    return {}


def load_champion_core_data(config_dir: str | Path | None = None) -> dict:
    """从统一英雄目录生成 Web 核心数据投影。"""

    version_dir = get_version_data_dir(config_dir)
    name_by_id = load_champion_id_to_name(version_dir)
    detail_by_id = load_champion_id_to_detail(version_dir)
    aliases_by_name = {
        str(item.get("heroName", "")).strip(): list(item.get("aliases", []))
        for item in load_champion_alias_records(version_dir)
        if str(item.get("heroName", "")).strip() and isinstance(item.get("aliases", []), list)
    }

    core_data = {}
    for hero_id, detail in detail_by_id.items():
        if not isinstance(detail, dict):
            continue
        normalized_id = str(hero_id).strip()
        hero_name = str(detail.get("heroName") or detail.get("name") or name_by_id.get(normalized_id, "") or "").strip()
        title = str(detail.get("title") or "").strip()
        en_name = str(detail.get("enName") or detail.get("en_name") or "").strip()
        if not normalized_id or not hero_name:
            continue
        core_data[normalized_id] = {
            "id": normalized_id,
            "hero_id": normalized_id,
            "name": hero_name,
            "title": title,
            "en_name": en_name,
            "aliases": aliases_by_name.get(hero_name, []),
        }
    if core_data:
        return core_data

    return core_data


def load_augment_manifest_entries(config_dir: str | Path | None = None) -> list[dict]:
    """返回统一海克斯目录中的 entry 列表。"""

    version_dir = get_version_data_dir(config_dir)
    catalog = load_augment_resource_catalog(version_dir)
    entries = catalog.get("entries")
    if isinstance(entries, list):
        return [item for item in entries if isinstance(item, dict)]

    return []


def load_augment_name_to_icon_map(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_augment_resource_catalog(version_dir)
    payload = catalog.get("name_to_icon")
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def load_augment_tier_map(config_dir: str | Path | None = None) -> dict:
    entries = load_augment_manifest_entries(config_dir)
    return {
        str(item.get("name", "")).strip(): str(item.get("tier", "")).strip()
        for item in entries
        if str(item.get("name", "")).strip() and str(item.get("tier", "")).strip()
    }


def load_apexlol_slug_map(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_augment_resource_catalog(version_dir)
    payload = catalog.get("apexlol_slug_map")
    if isinstance(payload, dict):
        return dict(payload)
    return _dict_payload(_read_json(version_dir / "Augment_Apexlol_Map.json"))


def catalog_payload(filename: str, config_dir: str | Path | None = None) -> Any:
    """按公开 catalog 文件名返回稳定投影。"""

    name = Path(str(filename or "")).name
    if name == "Augment_Icon_Manifest.json":
        return load_augment_manifest_entries(config_dir)
    if name == "Augment_Apexlol_Map.json":
        return load_apexlol_slug_map(config_dir)
    if name == "Champion_Alias_Index.json":
        return load_champion_alias_records(config_dir)
    if name == "Champion_Core_Data.json":
        return load_champion_core_data(config_dir)
    return None


def catalog_index_payload(filename: str, config_dir: str | Path | None = None) -> Any:
    """按索引文件名返回从统一目录派生的稳定投影。"""

    name = Path(str(filename or "")).name
    if name == "Champion_Alias_Index.json":
        return load_champion_alias_records(config_dir)
    if name == "augment.name-to-icon.v1.json":
        return load_augment_name_to_icon_map(config_dir)
    if name == "champion.alias-to-id.v1.json":
        return load_champion_alias_to_id(config_dir)
    if name == "champion.id-to-detail.v1.json":
        return load_champion_id_to_detail(config_dir)
    if name == "champion.id-to-name.v1.json":
        return load_champion_id_to_name(config_dir)
    return None


__all__ = [
    "AUGMENT_RESOURCE_CATALOG_FILENAME",
    "HERO_CATALOG_FILENAME",
    "get_augment_resource_catalog_path",
    "get_hero_catalog_path",
    "get_version_data_dir",
    "catalog_index_payload",
    "catalog_payload",
    "load_apexlol_slug_map",
    "load_augment_manifest_entries",
    "load_augment_name_to_icon_map",
    "load_augment_resource_catalog",
    "load_augment_tier_map",
    "load_champion_alias_records",
    "load_champion_alias_to_id",
    "load_champion_core_data",
    "load_champion_id_to_detail",
    "load_champion_id_to_name",
    "load_hero_catalog",
]

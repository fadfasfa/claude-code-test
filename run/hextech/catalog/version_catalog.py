from __future__ import annotations

"""版本数据统一目录。

本模块把 `resources/版本数据` 下的中文权威 JSON 投影成旧读取点需要的结构。
它只做只读解析和兼容投影，不触发远端刷新，也不写回资源文件。
"""

import json
from pathlib import Path
from typing import Any

from hextech.scraping._paths import STATIC_DATA_DIR


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
    """返回旧 `Champion_Alias_Index.json` 的记录列表。"""

    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    records = catalog.get("aliases")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]

    legacy = _read_json(version_dir / "Champion_Alias_Index.json")
    return [item for item in _list_payload(legacy) if isinstance(item, dict)]


def load_champion_alias_to_id(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    payload = catalog.get("alias_to_id")
    if isinstance(payload, dict):
        return dict(payload)
    return _dict_payload(_read_json(version_dir / "champion.alias-to-id.v1.json"))


def load_champion_id_to_name(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    payload = catalog.get("id_to_name")
    if isinstance(payload, dict):
        return dict(payload)
    return _dict_payload(_read_json(version_dir / "champion.id-to-name.v1.json"))


def load_champion_id_to_detail(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_hero_catalog(version_dir)
    payload = catalog.get("id_to_detail")
    if isinstance(payload, dict):
        return dict(payload)
    return _dict_payload(_read_json(version_dir / "champion.id-to-detail.v1.json"))


def load_champion_core_data(config_dir: str | Path | None = None) -> dict:
    """返回旧 `Champion_Core_Data.json` 结构，供前端兼容 URL 使用。"""

    version_dir = get_version_data_dir(config_dir)
    legacy = _read_json(version_dir / "Champion_Core_Data.json")
    if isinstance(legacy, dict) and legacy:
        return dict(legacy)

    detail_by_id = load_champion_id_to_name(version_dir)
    name_by_id = load_champion_id_to_detail(version_dir)
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
            "name": hero_name,
            "title": title,
            "en_name": en_name,
            "aliases": aliases_by_name.get(hero_name, []),
        }
    return core_data


def load_augment_manifest_entries(config_dir: str | Path | None = None) -> list[dict]:
    """返回旧 `Augment_Icon_Manifest.json` 的 entry 列表。"""

    version_dir = get_version_data_dir(config_dir)
    catalog = load_augment_resource_catalog(version_dir)
    entries = catalog.get("entries")
    if isinstance(entries, list):
        return [item for item in entries if isinstance(item, dict)]

    legacy = _read_json(version_dir / "Augment_Icon_Manifest.json")
    return [item for item in _list_payload(legacy) if isinstance(item, dict)]


def load_augment_name_to_icon_map(config_dir: str | Path | None = None) -> dict:
    version_dir = get_version_data_dir(config_dir)
    catalog = load_augment_resource_catalog(version_dir)
    payload = catalog.get("name_to_icon")
    if isinstance(payload, dict):
        return dict(payload)
    return _dict_payload(_read_json(version_dir / "augment.name-to-icon.v1.json"))


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


def legacy_static_payload(filename: str, config_dir: str | Path | None = None) -> Any:
    """为旧 `/data/static/<filename>` 兼容路由返回投影结果。"""

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


def legacy_index_payload(filename: str, config_dir: str | Path | None = None) -> Any:
    """为旧 `/data/indexes/<filename>` 兼容路由返回投影结果。"""

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
    "legacy_index_payload",
    "legacy_static_payload",
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

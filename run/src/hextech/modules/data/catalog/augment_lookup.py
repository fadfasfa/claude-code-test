"""海克斯资源目录的只读查询投影。

本模块只读取已提交的 ``resources/catalog``，不做 freshness 判断、网络访问或
资源写回。interfaces 和 application services 通过这里消费稳定目录；远端同步仍
由 infrastructure 负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hextech.modules.acquisition.common.icons import normalize_augment_name
from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries


def _build_lookup(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for item in entries:
        name = str(item.get("name") or "").strip()
        filename = str(item.get("filename") or "").strip().lower()
        if not name:
            continue
        lookup[name] = item
        lookup[normalize_augment_name(name)] = item
        if filename:
            lookup[filename] = item
            lookup[Path(filename).stem] = item
    return lookup


def load_augment_icon_manifest(config_dir: str | Path | None = None) -> list[dict[str, Any]]:
    return load_augment_manifest_entries(config_dir)


def load_augment_catalog_lookup_read_only(config_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    return _build_lookup(load_augment_manifest_entries(config_dir))


def build_augment_catalog_lookup(
    config_dir: str | Path | None = None,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """返回稳定目录投影；刷新参数仅保留调用形状，不触发 I/O 副作用。"""

    del force_refresh
    return load_augment_catalog_lookup_read_only(config_dir)


def find_augment_catalog_entry(
    lookup_name: str,
    config_dir: str | Path | None = None,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    del force_refresh
    if not str(lookup_name or "").strip():
        return None
    lookup = load_augment_catalog_lookup_read_only(config_dir)
    clean_name = str(lookup_name).strip()
    return lookup.get(clean_name) or lookup.get(normalize_augment_name(clean_name))


__all__ = [
    "build_augment_catalog_lookup",
    "find_augment_catalog_entry",
    "load_augment_catalog_lookup_read_only",
    "load_augment_icon_manifest",
]

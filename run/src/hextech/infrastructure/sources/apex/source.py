"""Apex 英雄 slug 目录和确定性详情 URL。"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote, urlparse


class ApexSlugMapError(ValueError):
    pass


def build_champion_slug_map(core_data: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    slug_map: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    for champion_id, item in core_data.items():
        normalized_id = str(champion_id).strip()
        slug = str(item.get("en_name") or item.get("enName") or item.get("slug") or "").strip()
        if not normalized_id or not slug:
            raise ApexSlugMapError(f"英雄缺少稳定 Apex slug：id={normalized_id}")
        folded = slug.casefold()
        for previous_id, previous_slug in slug_map.items():
            if previous_slug.casefold() == folded:
                duplicates.setdefault(folded, [previous_id]).append(normalized_id)
        slug_map[normalized_id] = slug
    if duplicates:
        raise ApexSlugMapError(f"Apex slug 重复：{duplicates}")
    return slug_map


def champion_detail_url(base_url: str, slug: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ApexSlugMapError("Apex base URL 必须是 https")
    language_root = parsed.path.rstrip("/") or "/zh"
    return f"{parsed.scheme}://{parsed.netloc}{language_root}/champions/{quote(str(slug).strip(), safe='-')}"


__all__ = ["ApexSlugMapError", "build_champion_slug_map", "champion_detail_url"]

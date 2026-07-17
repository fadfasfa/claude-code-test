"""Hextech 英雄目录与确定性抓取目标构建。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class ChampionCatalogMismatch(ValueError):
    pass


def build_expected_champions(
    core_data: Mapping[str, Mapping[str, Any]],
    remote_stats: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """以本地英雄目录为全集，把远端摘要只作为每个英雄的输入数据。"""

    expected_ids = {str(key) for key, item in core_data.items() if str(key).isdigit() and isinstance(item, Mapping)}
    stats_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in remote_stats:
        champion_id = str(item.get("championId") or "").strip()
        if not champion_id:
            continue
        if champion_id in stats_by_id:
            duplicate_ids.add(champion_id)
        stats_by_id[champion_id] = dict(item)

    missing = sorted(expected_ids - stats_by_id.keys(), key=int)
    unknown = sorted(stats_by_id.keys() - expected_ids, key=lambda value: int(value) if value.isdigit() else 10**9)
    if duplicate_ids or missing or unknown:
        raise ChampionCatalogMismatch(
            f"英雄摘要与目录不一致：missing={missing[:10]} unknown={unknown[:10]} duplicates={sorted(duplicate_ids)[:10]}"
        )

    return [stats_by_id[champion_id] for champion_id in sorted(expected_ids, key=int)]


__all__ = ["ChampionCatalogMismatch", "build_expected_champions"]

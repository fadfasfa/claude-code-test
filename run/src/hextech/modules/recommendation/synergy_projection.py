"""把 generation 内联动投影到最终 Overlay hint 身份集。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def _projection_key(name_index: Mapping[str, Any], raw_name: str) -> str:
    from .hints import normalize_augment_id, normalize_augment_name

    for key in (raw_name, normalize_augment_name(raw_name), normalize_augment_id(raw_name)):
        augment_id = str(name_index.get(key) or "").strip()
        if augment_id:
            return augment_id
    return ""


def project_generation_synergy(
    cache_payload: dict[str, Any],
    synergy_payload: Mapping[str, Any],
    *,
    previous_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """投影当前 generation 联动并执行 99% 覆盖与 last-good 回退门禁。"""

    from .hints import _clean_text, _normalize_synergy_item

    hints = cache_payload.get("hints")
    name_index = cache_payload.get("name_index")
    if not isinstance(hints, dict) or not isinstance(name_index, Mapping):
        raise ValueError("Overlay hint cache 缺少可投影的身份索引")

    hero_count = 0
    item_count = 0
    unique_names: set[str] = set()
    resolvable_names: set[str] = set()
    projected_names: set[str] = set()
    unresolved_names: set[str] = set()
    touched_hints: set[str] = set()
    catalog_only_hints: set[str] = set()
    seen_items: dict[str, set[str]] = {}

    for hero_id, hero_payload in synergy_payload.items():
        if not isinstance(hero_payload, Mapping):
            continue
        items = hero_payload.get("synergy_items")
        if not isinstance(items, list):
            continue
        hero_name = _clean_text(hero_payload.get("name") or hero_payload.get("title"))
        normalized_items = 0
        for item in items:
            if not isinstance(item, Mapping):
                continue
            normalized = _normalize_synergy_item(item, hero_id=str(hero_id or ""), hero_name=hero_name)
            if not normalized:
                continue
            normalized_items += 1
            item_count += 1
            signature = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for value in normalized["augment_names"]:
                raw_name = _clean_text(value)
                if not raw_name:
                    continue
                unique_names.add(raw_name)
                augment_id = _projection_key(name_index, raw_name)
                if not augment_id:
                    unresolved_names.add(raw_name[:80])
                    continue
                resolvable_names.add(raw_name)
                hint = hints.get(augment_id)
                if not isinstance(hint, dict):
                    unresolved_names.add(raw_name[:80])
                    continue
                signatures = seen_items.setdefault(augment_id, set())
                if signature not in signatures:
                    hint.setdefault("synergies", []).append(dict(normalized))
                    signatures.add(signature)
                projected_names.add(raw_name)
                touched_hints.add(augment_id)
                if not any(
                    isinstance(hint.get(key), Mapping) and bool(hint.get(key))
                    for key in ("stats_by_champion_id", "stats_by_champion_name")
                ):
                    catalog_only_hints.add(augment_id)
        if normalized_items:
            hero_count += 1

    resolvable_count = len(resolvable_names)
    projected_count = len(projected_names)
    coverage = projected_count / resolvable_count if resolvable_count else 0.0
    report = {
        "schema_version": 1,
        "hero_count": hero_count,
        "item_count": item_count,
        "unique_augment_name_count": len(unique_names),
        "catalog_resolvable_name_count": resolvable_count,
        "projected_name_count": projected_count,
        "projection_coverage": round(coverage, 6),
        "hints_with_synergy_count": len(touched_hints),
        "catalog_only_hints_with_synergy_count": len(catalog_only_hints),
        "unresolved_name_count": len(unresolved_names),
        "unresolved_name_sample": sorted(unresolved_names)[:20],
    }
    source = cache_payload.setdefault("source", {})
    if isinstance(source, dict):
        source["synergy_projection"] = report

    if item_count <= 0:
        raise ValueError("Overlay 联动投影输入为空")
    if resolvable_count <= 0 or not touched_hints:
        raise ValueError("Overlay 联动投影没有可关联 Catalog 的海克斯")
    if coverage < 0.99:
        raise ValueError(f"Overlay 联动投影覆盖不足：{coverage:.2%}")
    if isinstance(previous_report, Mapping):
        try:
            previous_coverage = float(previous_report.get("projection_coverage") or 0.0)
        except (TypeError, ValueError):
            previous_coverage = 0.0
        if previous_coverage > 0.0 and coverage < previous_coverage - 0.05:
            raise ValueError(f"Overlay 联动投影较 last-good 回退：{previous_coverage:.2%} -> {coverage:.2%}")
    return report


def load_previous_synergy_projection_report() -> Mapping[str, Any] | None:
    """读取 last-good 投影摘要；首次安装或旧 generation 返回 None。"""

    try:
        from hextech.modules.data.generation import DataSnapshotClient

        previous_hints = DataSnapshotClient().open_view().get_overlay_hints()
        previous_source = previous_hints.get("source") if isinstance(previous_hints, Mapping) else None
        candidate = previous_source.get("synergy_projection") if isinstance(previous_source, Mapping) else None
        return candidate if isinstance(candidate, Mapping) else None
    except Exception:
        return None


__all__ = ["load_previous_synergy_projection_report", "project_generation_synergy"]

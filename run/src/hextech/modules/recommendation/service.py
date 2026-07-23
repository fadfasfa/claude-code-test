"""将固定代统计、游戏上下文和 Vision 结果组合为统一推荐模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hextech.contracts import (
    GameContext,
    GenerationId,
    HealthState,
    RecommendationModel,
    VisionSelection,
    VisionSlotState,
)
from hextech.modules.data.ports import SnapshotViewPort


@dataclass(frozen=True)
class RecommendationPolicy:
    sort_champions_by_win_rate: bool = True
    private_stats_enabled: bool = True


class RecommendationService:
    def build(
        self,
        context: GameContext,
        snapshot: SnapshotViewPort,
        *,
        vision: VisionSelection | None = None,
        policy: RecommendationPolicy = RecommendationPolicy(),
    ) -> RecommendationModel:
        status = snapshot.status()
        generation_id = GenerationId(str(status.get("generation_id") or ""))
        source_order = {
            str(item.get("id") or ""): index
            for index, item in enumerate(snapshot.get_champions())
            if isinstance(item, dict) and item.get("id")
        }
        roles: list[tuple[str, str, int]] = []
        if context.local_champion_id:
            roles.append((str(context.local_champion_id), "self", 0))
        roles.extend((str(value), "teammate", index + 1) for index, value in enumerate(context.teammate_champion_ids))
        roles.extend((str(value), "bench", index + 100) for index, value in enumerate(context.bench_champion_ids))

        candidates: list[dict[str, object]] = []
        seen: set[str] = set()
        for champion_id, role, stable_index in roles:
            if champion_id in seen:
                continue
            champion = snapshot.get_champion(champion_id)
            if champion is None:
                continue
            seen.add(champion_id)
            candidates.append(
                {
                    **champion,
                    "selection_role": role,
                    "stable_index": source_order.get(champion_id, stable_index),
                }
            )
        if policy.sort_champions_by_win_rate:
            candidates.sort(key=lambda item: (-_win_rate(item), _stable_index(item)))
        for candidate in candidates:
            candidate.pop("stable_index", None)

        slots: list[dict[str, object]] = []
        if vision is not None:
            for slot in vision.slots:
                row: dict[str, Any] = {
                    "slot": slot.index,
                    "state": slot.state.value,
                    "augment_id": str(slot.augment_id or ""),
                    "name": slot.name,
                    "confidence": slot.confidence,
                    "generation_id": str(generation_id),
                    "vision_id": str(slot.visual_variant_id or slot.augment_id or ""),
                    "canonical_id": "",
                    # 旧 overlay/session 调用方仍读取此字段；v3 的权威字段为
                    # canonical_id，保留别名只为平滑读取，不再参与新逻辑。
                    "canonical_augment_id": "",
                    "champion_id": str(context.local_champion_id or ""),
                    "data_status": "",
                    "data_reason": "",
                    "status_code": "",
                    "stats": {},
                }
                private_stats_enabled = policy.private_stats_enabled
                lookup_keys = tuple(
                    dict.fromkeys(
                        value
                        for value in (
                            str(slot.visual_variant_id or "").strip(),
                            str(slot.augment_id or "").strip(),
                            str(slot.name or "").strip(),
                        )
                        if value
                    )
                )
                if slot.state is not VisionSlotState.READY:
                    row["data_status"] = "unavailable"
                    row["data_reason"] = "recognition_missing"
                    row["status_code"] = "RECOGNITION_MISSING"
                elif str(status.get("state") or "") == "unavailable":
                    row["data_status"] = "unavailable"
                    row["data_reason"] = "snapshot_unavailable"
                    row["status_code"] = "SNAPSHOT_UNAVAILABLE"
                elif not context.local_champion_id:
                    row["data_status"] = "unavailable"
                    row["data_reason"] = "context_missing"
                    row["status_code"] = "CONTEXT_MISSING"
                elif not private_stats_enabled:
                    row["data_status"] = "disabled"
                    row["status_code"] = "PRIVACY_OFF"
                elif lookup_keys:
                    identity = None
                    resolved_key = ""
                    for key in lookup_keys:
                        resolved = snapshot.resolve_augment(key)
                        if resolved:
                            identity = resolved
                            resolved_key = key
                            break
                    if identity:
                        row["name"] = str(identity.get("name") or row["name"])
                        # tier 属于视觉版本。仅靠卡名 fallback 时不能从同名 catalog
                        # 随机借用一个版本；有视觉 ID 或 Vision 已给出 tier 才展示。
                        row["tier"] = str(
                            slot.tier
                            or (
                                identity.get("tier")
                                if resolved_key != str(slot.name or "").strip()
                                else ""
                            )
                            or ""
                        )
                        row["canonical_id"] = str(identity.get("canonical_id") or "")
                        row["canonical_augment_id"] = row["canonical_id"]
                    stats = (
                        snapshot.get_combo_stats(context.local_champion_id, identity.get("canonical_id"))
                        if identity and private_stats_enabled
                        else None
                    )
                    row["stats"] = stats or {}
                    if stats is None:
                        if not identity:
                            row["data_status"] = "unavailable"
                            row["data_reason"] = "identity_unresolved"
                            row["status_code"] = "IDENTITY_UNRESOLVED"
                        elif _snapshot_has_source_stat(snapshot, identity):
                            row["data_status"] = "missing"
                            row["data_reason"] = "champion_stat_missing"
                            row["status_code"] = "CHAMPION_STAT_MISSING"
                        else:
                            row["data_status"] = "missing"
                            row["data_reason"] = "source_stat_missing"
                            row["status_code"] = "SOURCE_STAT_MISSING"
                    else:
                        row["data_status"] = "degraded" if status.get("state") == "degraded" else "ready"
                        row["status_code"] = "GENERATION_DEGRADED" if status.get("state") == "degraded" else "READY"
                else:
                    row["data_status"] = "unavailable"
                    row["data_reason"] = "identity_unresolved"
                    row["status_code"] = "IDENTITY_UNRESOLVED"
                slots.append(row)

        health = _max_health(
            context.health,
            HealthState.DEGRADED if status.get("state") == "degraded" else HealthState.READY,
        )
        return RecommendationModel(
            generation_id=generation_id,
            session_id=context.session_id,
            observed_at=time.time(),
            champion_candidates=tuple(candidates),
            augment_slots=tuple(slots),
            health=health,
            error_code=str(
                context.error_code
                if context.health is HealthState.UNAVAILABLE and context.error_code
                else status.get("reason") or context.error_code
            ),
        )


def _win_rate(item: dict[str, object]) -> float:
    for key in ("英雄胜率", "胜率", "win_rate"):
        value = item.get(key)
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            continue
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _stable_index(item: dict[str, object]) -> int:
    value = item.get("stable_index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _snapshot_has_source_stat(snapshot: SnapshotViewPort, identity: dict[str, Any]) -> bool:
    """区分来源完全没有该统计与仅当前英雄没有该组合。"""

    try:
        hints = snapshot.get_overlay_hints()
    except Exception:
        return False
    hint_map = hints.get("hints") if isinstance(hints, dict) else {}
    canonical_id = str(identity.get("canonical_id") or "")
    hint = hint_map.get(canonical_id) if isinstance(hint_map, dict) else None
    if not isinstance(hint, dict):
        return False
    stats_by_champion = hint.get("stats_by_champion_id")
    return bool(isinstance(stats_by_champion, dict) and stats_by_champion)


def _max_health(*states: HealthState) -> HealthState:
    severity = {HealthState.READY: 0, HealthState.DEGRADED: 1, HealthState.UNAVAILABLE: 2}
    return max(states, key=severity.__getitem__)

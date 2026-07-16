"""将固定代统计、游戏上下文和 Vision 结果组合为统一推荐模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from hextech.contracts import GameContext, GenerationId, HealthState, RecommendationModel, VisionSelection
from hextech.data_core.ports import SnapshotViewPort


@dataclass(frozen=True)
class RecommendationPolicy:
    sort_champions_by_win_rate: bool = True


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
            champion = snapshot.get_champion(champion_id)  # type: ignore[arg-type]
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
                    "status_code": "DETECTION_FAILED" if slot.state.value == "failed" else "",
                }
                if not status.get("private_stats_enabled") and slot.state.value == "ready":
                    row["status_code"] = "PRIVACY_OFF"
                if slot.augment_id and context.local_champion_id:
                    identity = snapshot.resolve_augment(slot.augment_id)
                    if identity:
                        row["name"] = str(identity.get("name") or row["name"])
                        row["tier"] = str(identity.get("tier") or "")
                        row["canonical_augment_id"] = str(identity.get("canonical_id") or "")
                    stats = snapshot.get_combo_stats(context.local_champion_id, slot.augment_id) if identity else None
                    row["stats"] = stats or {}
                    if stats is None and not row["status_code"]:
                        row["status_code"] = "SOURCE_STATS_MISSING" if identity else "IDENTITY_UNRESOLVED"
                    elif stats is not None and not row["status_code"]:
                        row["status_code"] = "GENERATION_DEGRADED" if status.get("state") == "degraded" else "READY"
                slots.append(row)

        health = HealthState.DEGRADED if status.get("state") == "degraded" else context.health
        return RecommendationModel(
            generation_id=generation_id,
            session_id=context.session_id,
            observed_at=time.time(),
            champion_candidates=tuple(candidates),
            augment_slots=tuple(slots),
            health=health,
            error_code=str(status.get("reason") or context.error_code),
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

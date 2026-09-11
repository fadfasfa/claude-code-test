"""把固定 Stage 的 ARAMKit 统计投影到现有 Overlay 推荐行。

投影只覆盖已稳定识别的行：Stage 记录优先、同英雄 ``all`` 次之；两者都没有时
保留既有 Blitz tier。它不改变 Vision DTO、slot revision 或 Web API。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from hextech.contracts import GameSessionState, HealthState
from hextech.modules.data.scoped_stats import ScopedStatsView
from hextech.modules.game_context.stage_context import StageContextV1


def _source_state(snapshot_status: Mapping[str, Any], source: str) -> dict[str, str]:
    """只读取目标来源状态；聚合 generation 或其他来源失败不得污染本行。"""

    source_status = snapshot_status.get("source_status")
    value = source_status.get(source) if isinstance(source_status, Mapping) else None
    if isinstance(value, Mapping):
        freshness = str(value.get("freshness") or "unknown")
        data_status = str(value.get("data_status") or "unknown")
        stale = freshness != "fresh" or data_status == "data_stale"
        return {
            "freshness": freshness,
            "data_status": data_status,
            "run_id": str(value.get("run_id") or ""),
            "data_reason": str(value.get("data_reason") or "") if stale else "",
            "data_at": str(value.get("data_at") or ""),
            "stale": "true" if stale else "false",
        }
    return {
        "freshness": "unknown",
        "data_status": "unknown",
        "run_id": "",
        "data_reason": "source_status_missing",
        "data_at": "",
        "stale": "true",
    }


def _annotate_blitz_fallback(
    row: dict[str, Any],
    *,
    fallback_reason: str,
    requested_stage: int | None,
    snapshot_status: Mapping[str, Any],
    stats_generation_id: str,
) -> dict[str, Any]:
    source_state = _source_state(snapshot_status, "blitz")
    stats = row.get("stats")
    if not isinstance(stats, Mapping) or not str(stats.get("source_tier") or "").strip():
        row["stats_fallback_reason"] = fallback_reason
        row["requested_stage"] = requested_stage
        return row
    row["stats"] = {
        **dict(stats),
        "stats_source": "blitz",
        "stats_scope": str(stats.get("stats_scope") or "global_tier"),
        "requested_stage": requested_stage,
        "fallback_reason": fallback_reason,
        "stats_generation_id": stats_generation_id,
        "source_run_id": source_state["run_id"],
        "source_freshness": source_state["freshness"],
        "data_reason": source_state["data_reason"],
        "source_data_at": source_state["data_at"],
    }
    row["stats_source"] = "blitz"
    row["stats_scope"] = str(row["stats"].get("stats_scope") or "global_tier")
    row["stats_fallback_reason"] = fallback_reason
    row["requested_stage"] = requested_stage
    row["stats_generation_id"] = stats_generation_id
    row["source_run_id"] = source_state["run_id"]
    row["source_freshness"] = source_state["freshness"]
    row["source_data_at"] = source_state["data_at"]
    if source_state["data_reason"]:
        row["data_reason"] = source_state["data_reason"]
    if source_state["stale"] == "true":
        # stale Blitz 只能保留来源诊断，排名/tier 不得进入公开 DTO 或 Canvas。
        row["stats"] = {
            key: value
            for key, value in row["stats"].items()
            if key
            not in {
                "source_tier",
                "champion_tier",
                "rank",
                "source_rank",
                "score",
            }
        }
        row["data_status"] = "stale"
        row["status_code"] = "STATS_STALE"
    return row


def apply_scoped_stage_stats(
    state: GameSessionState,
    *,
    stage_context: StageContextV1,
    scoped_view: ScopedStatsView | None,
    scope_status: str,
    scope_reason: str,
    snapshot_status: Mapping[str, Any] | None = None,
) -> GameSessionState:
    """返回替换过统计行的新会话状态；原 DTO 和 Vision revision 保持不变。"""

    recommendation = state.recommendation
    if recommendation is None:
        return state
    status = snapshot_status if isinstance(snapshot_status, Mapping) else {}
    projected: list[dict[str, object]] = []
    any_degraded = False
    for raw in recommendation.augment_slots:
        row: dict[str, Any] = dict(raw)
        if str(row.get("state") or "") != "ready" or str(row.get("status_code") or "") == "PRIVACY_OFF":
            projected.append(row)
            continue
        if scope_status == "preparing":
            row.update(
                {
                    "data_status": "unavailable",
                    "data_reason": "stats_preparing",
                    "status_code": "STATS_PREPARING",
                    "stats": {},
                    "stats_source": "",
                    "stats_scope": "preparing",
                    "requested_stage": stage_context.stage,
                }
            )
            projected.append(row)
            continue

        canonical_id = str(row.get("canonical_id") or row.get("canonical_augment_id") or "").strip()
        selection = scoped_view.select(canonical_id, stage_context.stage) if scoped_view and canonical_id else None
        if selection is not None and selection.record is not None:
            stats_payload = selection.to_stats_dict()
            source_state = _source_state(status, "aramkit")
            stats_generation_id = str(scoped_view.generation_id or state.generation_id or "")
            stats_payload.update(
                {
                    "stats_generation_id": stats_generation_id,
                    "source_run_id": source_state["run_id"] or scoped_view.run_id,
                    "source_freshness": source_state["freshness"],
                    "data_reason": source_state["data_reason"],
                    "source_data_at": source_state["data_at"],
                    "data_path": scoped_view.data_path,
                }
            )
            row.update(
                {
                    "data_status": "stale" if source_state["stale"] == "true" else "ready",
                    "data_reason": source_state["data_reason"],
                    # 过期但已通过同 Catalog、hash 和字段校验的 ARAMKit 仍是
                    # 可核验的主统计：只降低健康度并在阶段栏提示，不清空百分比。
                    # STATS_STALE 仅留给没有可安全展示统计的 fallback。
                    "status_code": "GENERATION_DEGRADED" if source_state["stale"] == "true" else "READY",
                    "stats": stats_payload,
                    "stats_source": "aramkit",
                    "stats_scope": selection.stats_scope,
                    "stats_generation_id": stats_generation_id,
                    "stats_fallback_reason": selection.fallback_reason,
                    "requested_stage": stage_context.stage,
                    "source_freshness": source_state["freshness"],
                    "source_run_id": source_state["run_id"] or scoped_view.run_id,
                    "source_data_at": source_state["data_at"],
                }
            )
            any_degraded = any_degraded or source_state["stale"] == "true"
            projected.append(row)
            continue

        fallback_reason = (
            selection.fallback_reason
            if selection is not None
            else scope_reason or "aramkit_scoped_stats_unavailable"
        )
        projected.append(
            _annotate_blitz_fallback(
                row,
                fallback_reason=fallback_reason,
                requested_stage=stage_context.stage,
                snapshot_status=status,
                stats_generation_id=str(state.generation_id or ""),
            )
        )

    health = HealthState.DEGRADED if any_degraded else recommendation.health
    updated_recommendation = replace(
        recommendation,
        augment_slots=tuple(projected),
        health=health,
    )
    return replace(
        state,
        recommendation=updated_recommendation,
        health=HealthState.DEGRADED if any_degraded else state.health,
    )


__all__ = ["apply_scoped_stage_stats"]

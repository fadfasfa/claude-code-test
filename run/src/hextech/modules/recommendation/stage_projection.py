"""把固定 Stage 的 ARAMKit 统计投影到现有 Overlay 推荐行。

投影只覆盖已稳定识别的行：Stage 记录优先、同英雄 ``all`` 次之；两者都没有时
明确报告缺失，不消费历史 Blitz tier。它不改变 Vision DTO、slot revision 或 Web API。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
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
        check_status = str(value.get("check_status") or "unknown")
        evidence_bound = value.get("check_evidence_bound") is True
        current = check_status == "up_to_date" and evidence_bound
        stale = freshness != "fresh" or data_status != "fresh" or not current
        reason = str(value.get("data_reason") or "") if stale else ""
        if stale and not reason:
            reason = {
                "changed": "upstream_revision_changed",
                "failed": "upstream_check_failed",
            }.get(check_status, "upstream_check_unknown")
        return {
            "freshness": freshness,
            "data_status": data_status,
            "run_id": str(value.get("run_id") or ""),
            "data_reason": reason,
            "data_at": str(value.get("data_at") or ""),
            "check_status": check_status,
            "upstream_revision": str(value.get("upstream_revision") or ""),
            "applied_revision": str(value.get("applied_revision") or ""),
            "check_evidence_bound": "true" if evidence_bound else "false",
            "stale": "true" if stale else "false",
        }
    return {
        "freshness": "unknown",
        "data_status": "unknown",
        "run_id": "",
        "data_reason": "source_status_missing",
        "data_at": "",
        "check_status": "unknown",
        "upstream_revision": "",
        "applied_revision": "",
        "check_evidence_bound": "false",
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
        # 来源 DTO 已经通过同 Catalog 和字段校验；年龄只降健康度，不删除旧排名。
        row["data_status"] = "stale"
        row["status_code"] = "GENERATION_DEGRADED"
    return row


def _scoped_source_state(view: ScopedStatsView, status: Mapping[str, Any]) -> dict[str, str]:
    """把 ranking 的检查证据严格绑定到当前英雄的 immutable component。"""

    state = _source_state(status, "aramkit")
    components = status.get("components")
    ranking = components.get("ranking") if isinstance(components, Mapping) else None
    champions = components.get("champions") if isinstance(components, Mapping) else None
    component = champions.get(str(view.champion_id)) if isinstance(champions, Mapping) else None
    reason = ""
    if not isinstance(component, Mapping) or component.get("complete") is not True:
        reason = "component_check_evidence_missing"
    elif str(component.get("run_id") or "") != view.run_id:
        reason = "component_run_mismatch"
    elif not isinstance(ranking, Mapping) or (
        str(component.get("catalog_id") or "") != str(ranking.get("catalog_id") or "")
    ):
        reason = "component_catalog_incompatible"
    elif (
        state["check_status"] != "up_to_date"
        or state["check_evidence_bound"] != "true"
        or not state["applied_revision"]
        or state["applied_revision"] != state["upstream_revision"]
        or str(component.get("source_version") or "") != state["applied_revision"]
    ):
        reason = (
            "component_revision_outdated"
            if state["check_status"] == "up_to_date"
            else state["data_reason"] or "upstream_check_unknown"
        )
    if reason:
        state = {
            **state,
            "freshness": "last_good" if state["freshness"] == "fresh" else state["freshness"],
            "data_status": "data_stale",
            "data_reason": reason,
            "stale": "true",
        }
    else:
        state = {
            **state,
            "freshness": "fresh",
            "data_status": "fresh",
            "data_reason": "",
            "stale": "false",
        }
    return {**state, "run_id": view.run_id, "data_at": view.data_at}


def apply_scoped_stage_stats(
    state: GameSessionState,
    *,
    stage_context: StageContextV1,
    scoped_view: ScopedStatsView | None,
    scope_status: str,
    scope_reason: str,
    snapshot_status: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> GameSessionState:
    """返回替换过统计行的新会话状态；原 DTO 和 Vision revision 保持不变。"""

    recommendation = state.recommendation
    if recommendation is None:
        return state
    status = snapshot_status if isinstance(snapshot_status, Mapping) else {}
    projected: list[dict[str, object]] = []
    any_degraded = False
    del now  # 保留测试/调用兼容；数据生成年龄不再形成隐式 stale 门。
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
            source_state = _scoped_source_state(scoped_view, status)
            stats_generation_id = str(scoped_view.generation_id or state.generation_id or "")
            stats_payload.update(
                {
                    "stats_generation_id": stats_generation_id,
                    "source_run_id": scoped_view.run_id,
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
                    # 可核验的主统计：只降低诊断健康度，不清空百分比或显示年龄。
                    # STATS_STALE 仅留给没有可安全展示统计的 fallback。
                    "status_code": "GENERATION_DEGRADED" if source_state["stale"] == "true" else "READY",
                    "stats": stats_payload,
                    "stats_source": "aramkit",
                    "stats_scope": selection.stats_scope,
                    "stats_generation_id": stats_generation_id,
                    "stats_fallback_reason": selection.fallback_reason,
                    "requested_stage": stage_context.stage,
                    "source_freshness": source_state["freshness"],
                    "source_run_id": scoped_view.run_id,
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
        row.update(stats_fallback_reason=fallback_reason, requested_stage=stage_context.stage)
        # An old snapshot may still contain tier-only Blitz rows. Keep identity, not its tier.
        stats = row.get("stats")
        if isinstance(stats, Mapping) and (
            stats.get("stats_source") == "blitz"
            or (stats.get("source_tier") and stats.get("winrate", stats.get("win_rate")) is None)
        ):
            row.update(stats={}, stats_source="", stats_scope="", data_status="missing",
                       data_reason="champion_stat_missing", status_code="CHAMPION_STAT_MISSING")
        projected.append(row)
        any_degraded = any_degraded or projected[-1].get("status_code") == "GENERATION_DEGRADED"

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

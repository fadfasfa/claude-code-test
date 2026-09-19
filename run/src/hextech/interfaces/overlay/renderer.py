# ruff: noqa: F401
"""LoL 原生风格的游戏内统计窗和英雄联动列。

本模块是纯展示层：不读文件、不启动进程、不依赖 ``display`` 或 Web。输入是已经
加载到内存的 event/hint/context，输出只通过 Canvas-like 接口绘制。

调用方: overlay.__main__、overlay.host、collect_runtime_diagnostics; 关键依赖: overlay.hints、overlay.vision.layout。
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, TypedDict

from hextech.contracts import GameSessionState

from hextech.modules.recommendation.hints import normalize_augment_id, normalize_augment_name
from hextech.modules.vision.layout import pick_card_panels

from .canvas_renderer import (
    CanvasLike,
    DataNoticeModel,
    OverlayLayout,
    OverlayRenderModel,
    StatPanelModel,
    StatStatusCode,
    SynergyPanelModel,
    SynergyStatusCode,
    SynergyTextLayout,
    _clean_text,
    _format_percent,
    draw_overlay_frame,
    resolve_overlay_layout,
)
from .data_notice import build_data_notice as _data_notice
from .data_notice import stats_stale_text as _stats_stale_text
def _query_hint(slot: Mapping[str, Any], hint_cache: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(hint_cache, Mapping):
        return {}
    hints = hint_cache.get("hints")
    name_index = hint_cache.get("name_index")
    if not isinstance(hints, Mapping):
        return {}
    augment_id = _clean_text(slot.get("augment_id"))
    slot_name = _clean_text(slot.get("name"))
    hint = None
    for candidate in (augment_id, normalize_augment_id(augment_id), normalize_augment_name(augment_id)):
        if candidate:
            hint = hints.get(candidate)
            if isinstance(hint, Mapping):
                break
    if not isinstance(hint, Mapping) and isinstance(name_index, Mapping):
        indexed_id = ""
        for candidate in (
            augment_id,
            normalize_augment_id(augment_id),
            normalize_augment_name(augment_id),
            slot_name,
            normalize_augment_id(slot_name),
            normalize_augment_name(slot_name),
        ):
            if not candidate:
                continue
            indexed_id = _clean_text(name_index.get(candidate))
            if indexed_id:
                break
        if indexed_id:
            hint = hints.get(indexed_id) or hints.get(normalize_augment_id(indexed_id))
    if not isinstance(hint, Mapping) and slot_name:
        hint = next(
            (
                candidate
                for candidate in hints.values()
                if isinstance(candidate, Mapping) and _clean_text(candidate.get("name")) == slot_name
            ),
            None,
        )
    return dict(hint) if isinstance(hint, Mapping) else {}


def _format_stats_entry(stats: Mapping[str, Any]) -> str:
    parts: list[str] = []
    winrate = _format_percent(stats.get("winrate"))
    pickrate = _format_percent(stats.get("pickrate"))
    if winrate:
        parts.append(f"胜率 {winrate}")
    if pickrate:
        parts.append(f"出场 {pickrate}")
    return " · ".join(parts)


def _current_champion_stats(hint: Mapping[str, Any], context: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not (isinstance(context, Mapping) and context.get("ok")):
        return None
    champion_id = _clean_text(context.get("champion_id"))
    champion_name = _clean_text(context.get("champion_name"))
    by_id = hint.get("stats_by_champion_id")
    if champion_id and isinstance(by_id, Mapping):
        stats = by_id.get(champion_id)
        if isinstance(stats, Mapping):
            return stats
    by_name = hint.get("stats_by_champion_name")
    if champion_name and isinstance(by_name, Mapping):
        stats = by_name.get(champion_name)
        if isinstance(stats, Mapping):
            return stats
    return None


def _context_status_code(context: Mapping[str, Any] | None) -> StatStatusCode | None:
    if isinstance(context, Mapping) and context.get("ok"):
        return None
    error = _clean_text(context.get("error"), limit=48) if isinstance(context, Mapping) else "context_missing"
    return "CONTEXT_EXPIRED" if error == "context_expired" else "CONTEXT_MISSING"


def _context_missing_status_text(context: Mapping[str, Any] | None) -> str:
    """把当前英雄缺失原因压成短文案，避免所有状态都显示成同一个“等待英雄”。"""

    error = _clean_text(context.get("error"), limit=48) if isinstance(context, Mapping) else "context_missing"
    source = _clean_text(context.get("source"), limit=48) if isinstance(context, Mapping) else ""
    key = source if error in {"", "context_missing"} and source else error
    return {
        "context_expired": "等待当前英雄",
        "context_unmapped_champion": "英雄未映射",
        "lcu-unavailable": "等待 LCU",
        "lcu-error": "LCU 不可用",
        "lcu-auth": "LCU 认证失效",
        "lcu-no-session": "等待选人",
        "lcu-no-champion": "等待锁定英雄",
        "live-client-data": "等待当前英雄",
    }.get(key, "等待当前英雄")


def _data_reason_display(reason: object) -> tuple[str, StatStatusCode, str, str, str] | None:
    """v3 槽位原因优先于推测；只有真实来源缺口才显示公开来源文案。"""

    normalized = _clean_text(reason, limit=48).lower()
    mapping: dict[str, tuple[str, StatStatusCode, str, str, str]] = {
        "recognition_missing": ("未识别到海克斯", "RECOGNITION_MISSING", "", "", "识别未完成"),
        "identity_unresolved": ("无法关联统计 ID", "IDENTITY_UNRESOLVED", "", "", "无法关联统计 ID"),
        "source_stat_missing": ("公开来源未提供此海克斯统计", "SOURCE_STAT_MISSING", "", "", "公开来源未提供此海克斯统计"),
        "champion_stat_missing": ("该英雄暂无此海克斯样本", "CHAMPION_STAT_MISSING", "", "", "该英雄暂无此海克斯样本"),
        "context_missing": ("等待当前英雄", "CONTEXT_MISSING", "", "", "等待当前英雄"),
        "snapshot_unavailable": ("统计数据准备中", "SNAPSHOT_UNAVAILABLE", "", "", "数据准备中"),
    }
    return mapping.get(normalized)


def _effective_context(
    context: Mapping[str, Any] | None,
    recent_context: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """短暂 context 缺失时沿用调用方提供的最近有效英雄，避免渲染状态闪烁。"""

    if isinstance(context, Mapping) and context.get("ok"):
        return context
    if isinstance(context, Mapping) and _clean_text(context.get("error"), limit=48) == "context_expired":
        return context
    if isinstance(recent_context, Mapping) and recent_context.get("ok"):
        return recent_context
    return context


def _ranking_stats_text(stats: Mapping[str, Any]) -> str:
    source_tier = _clean_text(stats.get("source_tier"), limit=8)
    if not source_tier:
        return ""
    champion_tier = _clean_text(stats.get("champion_tier"), limit=8)
    return (
        f"该英雄 T{champion_tier} · 全局 T{source_tier}"
        if champion_tier
        else f"全局 T{source_tier}"
    )


def _scoped_stage_stats_display(stats: Mapping[str, Any]) -> dict[str, Any] | None:
    """格式化 ARAMKit Stage/all，并把范围与低样本拆成独立视觉语义。"""

    if _clean_text(stats.get("stats_source"), limit=24) != "aramkit":
        return None
    stats_scope = _clean_text(stats.get("stats_scope"), limit=24)
    fallback_reason = _clean_text(stats.get("fallback_reason"), limit=80)
    raw_sample_count = stats.get("sample_count")
    sample_count: int | None = None
    if isinstance(raw_sample_count, int) and not isinstance(raw_sample_count, bool):
        sample_count = raw_sample_count if raw_sample_count >= 0 else None
    elif isinstance(raw_sample_count, str) and raw_sample_count.isascii() and raw_sample_count.isdigit():
        sample_count = int(raw_sample_count)
    winrate = _format_percent(stats.get("winrate", stats.get("win_rate")))
    pickrate = _format_percent(stats.get("pickrate", stats.get("pick_rate")))
    if not (winrate and pickrate):
        return {
            "text": "统计字段不完整",
            "winrate_text": "",
            "pickrate_text": "",
            "stats_tone": "aggregate" if stats_scope == "all" else "default",
            "low_sample_outline": False,
            "sample_count": sample_count,
            "stats_scope": stats_scope,
            "fallback_reason": fallback_reason,
        }
    low_sample = sample_count is not None and sample_count < 1000
    aggregate = stats_scope == "all"
    text = (
        f"胜率 {winrate} · 出场数 {sample_count}"
        if sample_count is not None and sample_count < 100
        else f"胜率 {winrate} · 出场 {pickrate}"
    )
    return {
        "text": text,
        "winrate_text": winrate,
        "pickrate_text": pickrate,
        "stats_tone": "aggregate" if aggregate else "low_sample" if low_sample else "default",
        "low_sample_outline": aggregate and low_sample,
        "sample_count": sample_count,
        "stats_scope": stats_scope,
        "fallback_reason": fallback_reason,
    }


def _stage_indicator(
    stats_scope: Mapping[str, Any] | None,
    data_notice: DataNoticeModel | None = None,
) -> dict[str, Any] | None:
    if not isinstance(stats_scope, Mapping):
        return None
    raw_stage = stats_scope.get("stage")
    if not isinstance(raw_stage, int) or isinstance(raw_stage, bool):
        return None
    stage = raw_stage
    if stage not in {1, 2, 3, 4}:
        return None
    result: dict[str, Any] = {"stage": stage, "label": f"阶段 {stage}"}
    if data_notice is not None:
        result["data_notice"] = dict(data_notice)
    return result


def _stats_display(
    hint: Mapping[str, Any],
    hint_cache: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> tuple[str, StatStatusCode, str, str, str]:
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache, Mapping) else None
    snapshot_state = str(snapshot_status.get("state") or "") if isinstance(snapshot_status, Mapping) else ""
    if snapshot_state == "unavailable":
        return "统计数据准备中", "SNAPSHOT_UNAVAILABLE", "", "", "数据准备中"
    source = hint_cache.get("source") if isinstance(hint_cache, Mapping) else None
    if not (isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True):
        return "已开启隐私模式", "PRIVACY_OFF", "", "", "统计关闭"
    context_status = _context_status_code(context)
    if context_status == "CONTEXT_EXPIRED":
        return "等待当前英雄", "CONTEXT_EXPIRED", "", "", _context_missing_status_text(context)
    if context_status == "CONTEXT_MISSING":
        return "等待当前英雄", "CONTEXT_MISSING", "", "", _context_missing_status_text(context)
    stats = _current_champion_stats(hint, context)
    if not isinstance(stats, Mapping):
        has_source_stats = any(
            isinstance(hint.get(key), Mapping) and bool(hint.get(key))
            for key in ("stats_by_champion_id", "stats_by_champion_name")
        )
        if has_source_stats:
            return "该英雄暂无此海克斯样本", "CHAMPION_STAT_MISSING", "", "", "该英雄暂无此海克斯样本"
        return "公开来源未提供此海克斯统计", "SOURCE_STAT_MISSING", "", "", "公开来源未提供此海克斯统计"
    winrate = _format_percent(stats.get("winrate"))
    pickrate = _format_percent(stats.get("pickrate"))
    text = _format_stats_entry(stats)
    if not (winrate and pickrate):
        ranking_text = _ranking_stats_text(stats)
        if ranking_text:
            degraded = _snapshot_sources_degraded(snapshot_status, ("blitz",))
            return (
                ranking_text,
                "GENERATION_DEGRADED" if degraded else "READY",
                "",
                "",
                "",
            )
        return text or "统计字段不完整", "NO_STATS", "", "", "统计不完整"
    if _snapshot_sources_degraded(snapshot_status, ("aramkit", "hextech")):
        # ARAMKit 的记录已通过同 Catalog、hash 和字段校验；过期只改变健康度，
        # 不得把仍可核验的百分比清空。真正没有可用统计的 fallback 仍走
        # STATS_STALE（例如 stale Blitz 无 ranking 可展示）。
        return text, "GENERATION_DEGRADED", winrate, pickrate, ""
    return text, "READY", winrate, pickrate, ""


def _synergy_rating_rank(value: Any) -> int:
    normalized = _clean_text(value, limit=12).upper().replace(" ", "")
    priority = {
        "SSS": 8,
        "SS": 7,
        "S+": 6,
        "S": 5,
        "A": 4,
        "B": 3,
        "C": 2,
        "D": 1,
    }
    return priority.get(normalized, 0)


def _matched_synergy(hint: Mapping[str, Any], context: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not (isinstance(context, Mapping) and context.get("ok")):
        return None
    champion_id = _clean_text(context.get("champion_id"))
    champion_name = _clean_text(context.get("champion_name"))
    if not champion_id and not champion_name:
        return None
    synergies = hint.get("synergies")
    if not isinstance(synergies, list):
        return None
    best: Mapping[str, Any] | None = None
    best_rank = -1
    for item in synergies:
        if not isinstance(item, Mapping):
            continue
        hero_id = _clean_text(item.get("hero_id"))
        hero_name = _clean_text(item.get("hero_name"))
        if (champion_id and hero_id == champion_id) or (champion_name and hero_name == champion_name):
            rank = _synergy_rating_rank(item.get("rating"))
            if rank > best_rank:
                best = item
                best_rank = rank
    return best


def _synergy_status(
    *,
    matched: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    snapshot_status: Mapping[str, Any],
    event_generation_id: str = "",
    snapshot_generation_id: str = "",
) -> SynergyStatusCode:
    snapshot_state = str(snapshot_status.get("state") or "unavailable")
    if snapshot_state == "unavailable":
        return "SOURCE_UNAVAILABLE"
    if event_generation_id and snapshot_generation_id and event_generation_id != snapshot_generation_id:
        return "GENERATION_MISMATCH"
    if not (isinstance(context, Mapping) and context.get("ok")):
        return "CONTEXT_MISSING"
    if not isinstance(matched, Mapping):
        source_status = snapshot_status.get("source_status")
        if isinstance(source_status, Mapping):
            states = [source_status.get(name) for name in ("apex", "mayhem")]
            present = [value for value in states if isinstance(value, Mapping)]
            if present and all(value.get("data_status") == "confirmed_empty" for value in present):
                return "CONFIRMED_EMPTY"
            if present and all(value.get("data_status") in {
                "pending", "unavailable", "failed", "missing", "confirmed_empty",
            } for value in present):
                return "SOURCE_UNAVAILABLE"
        return "NO_MATCH"
    if _snapshot_sources_degraded(snapshot_status, ("apex", "mayhem")):
        return "SYNERGY_DEGRADED"
    return "READY"


def _synergy_source_expiry(snapshot_status: Mapping[str, Any] | None) -> tuple[str, str]:
    """从 apex/mayhem source_status 提取过期诊断码与最旧 data_at。"""

    source_status = snapshot_status.get("source_status") if isinstance(snapshot_status, Mapping) else None
    if not isinstance(source_status, Mapping):
        return ("", "")
    reason = ""
    data_ats: list[str] = []
    for source in ("apex", "mayhem"):
        value = source_status.get(source)
        if not isinstance(value, Mapping):
            continue
        if str(value.get("data_reason") or "") == "source_data_expired":
            reason = "source_data_expired"
        data_at = str(value.get("data_at") or "")
        if data_at:
            data_ats.append(data_at)
    return (reason, min(data_ats) if data_ats else "")


def _stats_source_expiry(
    snapshot_status: Mapping[str, Any] | None,
    sources: Sequence[str],
) -> tuple[str, str]:
    """提取目标统计来源的过期原因和最旧 data_at。"""

    source_status = snapshot_status.get("source_status") if isinstance(snapshot_status, Mapping) else None
    if not isinstance(source_status, Mapping):
        return ("", "")
    reason = ""
    data_ats: list[str] = []
    for source in sources:
        value = source_status.get(source)
        if not isinstance(value, Mapping):
            continue
        stale = (
            str(value.get("freshness") or "unknown") != "fresh"
            or str(value.get("data_status") or "unknown") == "data_stale"
        )
        if not stale:
            continue
        if str(value.get("data_reason") or "") == "source_data_expired":
            reason = "source_data_expired"
        data_at = str(value.get("data_at") or "")
        if data_at:
            data_ats.append(data_at)
    return (reason, min(data_ats) if data_ats else "")


def _synergy_stale_text(reason: str, data_at: str, *, now: datetime | None = None) -> str:
    """联动降级文案：数据过期时给出真实年龄；其余降级仍显示"上一代"。

    年龄在渲染时从 data_at 现算，避免使用发布时刻冻结的年龄失真；
    data_at 不可解析时回退通用文案，不虚构时间。
    """

    if reason == "source_data_expired" and data_at:
        try:
            parsed = datetime.fromisoformat(data_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            current = now if now is not None else datetime.now(timezone.utc)
            hours = int(max((current - parsed).total_seconds(), 0) // 3600)
            if hours >= 48:
                return f"联动数据为 {hours // 24} 天前"
            if hours >= 1:
                return f"联动数据为 {hours} 小时前"
    return "联动数据为上一代"


def _snapshot_sources_degraded(
    snapshot_status: Mapping[str, Any] | None,
    sources: Sequence[str],
) -> bool:
    """优先读取逐来源 freshness；旧 snapshot 才兼容聚合 degraded。"""

    if not isinstance(snapshot_status, Mapping):
        return False
    source_status = snapshot_status.get("source_status")
    if isinstance(source_status, Mapping) and any(source in source_status for source in sources):
        for source in sources:
            value = source_status.get(source)
            if not isinstance(value, Mapping):
                continue
            if str(value.get("freshness") or "unknown") != "fresh":
                return True
            if str(value.get("data_status") or "unknown") == "data_stale":
                return True
        return False
    return str(snapshot_status.get("state") or "") == "degraded"


def build_render_model(
    snapshot: Mapping[str, Any],
    *,
    hint_cache: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
    recent_context: Mapping[str, Any] | None = None,
) -> OverlayRenderModel:
    """把共享数据收口为稳定三统计窗和仅命中联动的展示模型。"""

    context = _effective_context(context, recent_context)
    slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
    event_source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache, Mapping) else {}
    event_generation_id = _clean_text(event_source.get("generation_id"))
    snapshot_generation_id = _clean_text(snapshot_status.get("generation_id")) if isinstance(snapshot_status, Mapping) else ""
    synergy_stale_reason, synergy_data_at = _synergy_source_expiry(
        snapshot_status if isinstance(snapshot_status, Mapping) else None
    )
    stats: list[StatPanelModel] = []
    synergies: list[SynergyPanelModel] = []
    for index in range(3):
        slot = slots[index] if index < len(slots) and isinstance(slots[index], Mapping) else {}
        state = _clean_text(slot.get("state"), limit=32)
        slot_name = _clean_text(slot.get("name"), limit=60)
        ready = state == "ready" and bool(slot_name or _clean_text(slot.get("augment_id")))
        hint = _query_hint(slot, hint_cache) if ready else {}
        name = _clean_text(hint.get("name") or slot_name, limit=60)
        tier = _clean_text(hint.get("tier") or slot.get("tier"), limit=24)
        explicit_display = _data_reason_display(slot.get("data_reason"))
        if explicit_display is not None:
            stats_text, status_code, winrate_text, pickrate_text, status_text = explicit_display
        elif ready:
            if not hint:
                stats_text, status_code = "海克斯身份未解析", "IDENTITY_UNRESOLVED"
                winrate_text, pickrate_text, status_text = "", "", "身份未解析"
            else:
                stats_text, status_code, winrate_text, pickrate_text, status_text = _stats_display(
                    hint,
                    hint_cache,
                    context,
                )
        elif state == "failed":
            stats_text, status_code = "未识别到海克斯", "RECOGNITION_MISSING"
            winrate_text, pickrate_text, status_text = "", "", "识别未完成"
        else:
            stats_text, status_code = "识别中…", "DETECTING"
            winrate_text, pickrate_text, status_text = "", "", "识别中…"
            if slot.get("diagnostic") == "evidence_starved":
                stats_text = status_text = "识别未确认"
        has_current_stats = status_code in {"READY", "GENERATION_DEGRADED"}
        matched = _matched_synergy(hint, context) if ready else None
        synergy_status = _synergy_status(
            matched=matched,
            context=context,
            snapshot_status=snapshot_status if isinstance(snapshot_status, Mapping) else {},
            event_generation_id=event_generation_id,
            snapshot_generation_id=snapshot_generation_id,
        )
        stats.append(
            {
                "slot": index,
                "state": ("matched" if has_current_stats else "missing_stats") if ready else (
                    "failed" if state == "failed" else "detecting"
                ),
                "name": name,
                "tier": tier,
                "stats_text": stats_text,
                "status_code": status_code,
                "winrate_text": winrate_text,
                "pickrate_text": pickrate_text,
                "status_text": status_text,
                "synergy_status": synergy_status,
                "hint_id": _clean_text(hint.get("augment_id"), limit=60),
                "stats_tone": "default",
                "low_sample_outline": False,
            }
        )
        if not ready:
            continue
        if not isinstance(matched, Mapping):
            continue
        synergies.append(
            {
                "slot": index,
                "augment_name": name,
                "tier": tier,
                "hero_name": _clean_text(matched.get("hero_name"), limit=40),
                "rating": _clean_text(matched.get("rating"), limit=12),
                "tag": _clean_text(matched.get("tag"), limit=24),
                # 原文不在 renderer 热路径清洗或截断。后台数据准备器会按实际
                # viewport/mode 附加 display_summary；直接调用者仍能看到完整原文。
                "content": deepcopy(matched.get("content")),
                "data_status": synergy_status,
                "status_text": _synergy_stale_text(synergy_stale_reason, synergy_data_at)
                if synergy_status == "SYNERGY_DEGRADED"
                else "",
                **(
                    {"display_summary": deepcopy(dict(matched["display_summary"]))}
                    if isinstance(matched.get("display_summary"), Mapping)
                    else {}
                ),
            }
        )
    model: OverlayRenderModel = {"stats": stats, "synergies": synergies}
    notice = _data_notice(snapshot_status if isinstance(snapshot_status, Mapping) else None)
    if notice is not None:
        model["data_notice"] = notice
    return model
def build_render_model_from_session(
    state: GameSessionState,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    stats_scope: Mapping[str, Any] | None = None,
) -> OverlayRenderModel:
    """把核心会话模型适配为 Tk 绘制模型；业务状态不得在此重新推导。"""

    recommendation = state.recommendation
    rows = list(recommendation.augment_slots) if recommendation is not None else []
    context_mapping: dict[str, Any] = {}
    if state.context is not None:
        context_mapping = {
            "ok": bool(state.context.local_champion_id),
            "champion_id": str(state.context.local_champion_id or ""),
            "error": state.context.error_code,
            "source": state.context.source,
        }
    stats: list[StatPanelModel] = []
    synergies: list[SynergyPanelModel] = []
    labels: dict[str, tuple[str, str]] = {
        "RECOGNITION_MISSING": ("未识别到海克斯", "识别未完成"),
        "SNAPSHOT_UNAVAILABLE": ("统计数据准备中", "数据准备中"),
        "STATS_PREPARING": ("统计准备中", "统计准备中"),
        "STATS_STALE": ("统计数据暂非最新", "统计数据暂非最新"),
        "PRIVACY_OFF": ("已开启隐私模式", "统计关闭"),
        "SOURCE_STAT_MISSING": ("公开来源未提供此海克斯统计", "公开来源未提供此海克斯统计"),
        "CHAMPION_STAT_MISSING": ("该英雄暂无此海克斯样本", "该英雄暂无此海克斯样本"),
        "IDENTITY_UNRESOLVED": ("无法关联统计 ID", "无法关联统计 ID"),
        "CONTEXT_MISSING": ("等待当前英雄", "等待英雄上下文"),
        "CONTEXT_EXPIRED": ("等待当前英雄", "等待当前英雄"),
    }
    for index in range(3):
        row = rows[index] if index < len(rows) and isinstance(rows[index], Mapping) else {}
        slot_state = _clean_text(row.get("state"), limit=32) or "detecting"
        status_code = _clean_text(row.get("status_code"), limit=48)
        if not recommendation:
            if state.context is None or state.context.local_champion_id is None:
                context_error = state.context.error_code if state.context is not None else state.error_code
                status_code = "CONTEXT_EXPIRED" if context_error == "context_expired" else "CONTEXT_MISSING"
            else:
                status_code = "SNAPSHOT_UNAVAILABLE"
        elif slot_state == "failed":
            status_code = "RECOGNITION_MISSING"
        elif slot_state != "ready":
            status_code = "DETECTING"
        elif not status_code:
            explicit = _data_reason_display(row.get("data_reason"))
            status_code = explicit[1] if explicit is not None else "SOURCE_STAT_MISSING"
        stats_payload = row.get("stats") if isinstance(row.get("stats"), Mapping) else {}
        normalized_stats = {
            "winrate": stats_payload.get(
                "winrate", stats_payload.get("win_rate", stats_payload.get("海克斯胜率"))
            ),
            "pickrate": stats_payload.get(
                "pickrate", stats_payload.get("pick_rate", stats_payload.get("海克斯出场率"))
            ),
            "source_tier": stats_payload.get("source_tier"),
            "champion_tier": stats_payload.get("champion_tier"),
            "stats_source": stats_payload.get("stats_source"),
            "stats_scope": stats_payload.get("stats_scope"),
            "scope_label": stats_payload.get("scope_label"),
            "sample_count": stats_payload.get("sample_count"),
            "sample_quality": stats_payload.get("sample_quality"),
            "source_freshness": stats_payload.get(
                "source_freshness", row.get("source_freshness")
            ),
            "data_reason": stats_payload.get("data_reason", row.get("data_reason")),
            "source_data_at": stats_payload.get(
                "source_data_at", row.get("source_data_at")
            ),
            "fallback_reason": stats_payload.get(
                "fallback_reason", row.get("stats_fallback_reason")
            ),
        }
        scoped_display = _scoped_stage_stats_display(normalized_stats)
        winrate_text = _format_percent(normalized_stats["winrate"])
        pickrate_text = _format_percent(normalized_stats["pickrate"])
        stats_tone = "default"
        low_sample_outline = False
        sample_count: int | None = None
        stats_scope_value = _clean_text(normalized_stats.get("stats_scope"), limit=24)
        fallback_reason = _clean_text(normalized_stats.get("fallback_reason"), limit=80)
        if status_code == "STATS_STALE" and (
            (winrate_text and pickrate_text) or _ranking_stats_text(normalized_stats)
        ):
            # 仍有经过上游校验的旧值时保留展示，时效状态留在 DTO/诊断。
            status_code = "GENERATION_DEGRADED"
        if status_code in {"READY", "GENERATION_DEGRADED"}:
            if scoped_display is not None:
                stats_text = str(scoped_display["text"])
                winrate_text = str(scoped_display["winrate_text"])
                pickrate_text = str(scoped_display["pickrate_text"])
                stats_tone = str(scoped_display["stats_tone"])
                low_sample_outline = bool(scoped_display["low_sample_outline"])
                sample_count = scoped_display["sample_count"]
                stats_scope_value = str(scoped_display["stats_scope"])
                fallback_reason = str(scoped_display["fallback_reason"])
            else:
                stats_text = _format_stats_entry(normalized_stats)
                if not winrate_text and not pickrate_text:
                    stats_text = _ranking_stats_text(normalized_stats) or stats_text
            status_text = ""
        elif status_code == "STATS_STALE":
            stats_text = status_text = "统计暂不可用"
            winrate_text = pickrate_text = ""
        elif status_code in {"DETECTING", "STATS_PREPARING"}:
            stats_text, status_text = labels.get(status_code, ("识别中…", "识别中…"))
            if (status_code == "DETECTING" and state.vision is not None
                and index < len(state.vision.slots) and state.vision.slots[index].error_code == "evidence_starved"):
                stats_text = status_text = "识别未确认"
            winrate_text = pickrate_text = ""
        else:
            stats_text, status_text = labels.get(status_code, ("暂无统计", "暂无统计"))
            winrate_text = pickrate_text = ""
        name = _clean_text(row.get("name"), limit=60)
        tier = _clean_text(row.get("tier"), limit=24)
        ready = slot_state == "ready"
        stats.append(
            {
                "slot": index,
                "state": ("matched" if status_code in {"READY", "GENERATION_DEGRADED"} else "missing_stats")
                if ready
                else ("failed" if slot_state == "failed" else "detecting"),
                "name": name,
                "tier": tier,
                "stats_text": stats_text,
                "status_code": status_code,  # type: ignore[typeddict-item]
                "winrate_text": winrate_text,
                "pickrate_text": pickrate_text,
                "status_text": status_text,
                "synergy_status": "SOURCE_UNAVAILABLE",
                "stats_tone": stats_tone,  # type: ignore[typeddict-item]
                "low_sample_outline": low_sample_outline,
                "sample_count": sample_count,
                "stats_scope": stats_scope_value,
                "fallback_reason": fallback_reason,
            }
        )
        hint = _query_hint(row, hint_cache) if ready else {}
        stats[-1]["hint_id"] = _clean_text(hint.get("augment_id"), limit=60)
        matched = _matched_synergy(hint, context_mapping)
        stats[-1]["synergy_status"] = _synergy_status(
            matched=matched,
            context=context_mapping,
            snapshot_status={
                "state": "unavailable" if recommendation is None else "ready",
                "source_status": {
                    "apex": {
                        "freshness": "last_good"
                        if str(row.get("synergy_data_status") or "") == "degraded"
                        else "fresh"
                    }
                },
            },
            event_generation_id=str(state.generation_id),
            snapshot_generation_id=str(recommendation.generation_id) if recommendation is not None else "",
        )  # type: ignore[typeddict-item]
        if isinstance(matched, Mapping):
            synergy_status = stats[-1]["synergy_status"]
            synergies.append(
                {
                    "slot": index,
                    "augment_name": name,
                    "tier": tier,
                    "hero_name": _clean_text(matched.get("hero_name"), limit=40),
                    "rating": _clean_text(matched.get("rating"), limit=12),
                    "tag": _clean_text(matched.get("tag"), limit=24),
                    "content": deepcopy(matched.get("content")),
                    "data_status": synergy_status,
                    "status_text": _synergy_stale_text(
                        str(row.get("synergy_data_reason") or ""),
                        str(row.get("synergy_data_at") or ""),
                    )
                    if synergy_status == "SYNERGY_DEGRADED"
                    else "",
                    **(
                        {"display_summary": deepcopy(dict(matched["display_summary"]))}
                        if isinstance(matched.get("display_summary"), Mapping)
                        else {}
                    ),
                }
            )
    model: OverlayRenderModel = {"stats": stats, "synergies": synergies}
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache, Mapping) else None
    notice = _data_notice(
        snapshot_status if isinstance(snapshot_status, Mapping) else None,
        rows=[row for row in rows if isinstance(row, Mapping)],
        stats_scope=stats_scope,
    )
    if notice is not None:
        model["data_notice"] = notice
    indicator = _stage_indicator(stats_scope, notice)
    if indicator is not None:
        model["stage_indicator"] = indicator  # type: ignore[typeddict-item]
    return model

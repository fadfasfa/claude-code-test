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
from typing import Any, Literal, Protocol, TypedDict

from hextech.contracts import GameSessionState

from hextech.modules.recommendation.hints import normalize_augment_id, normalize_augment_name
from hextech.modules.vision.layout import pick_card_panels

from .canvas_renderer import (
    CanvasLike,
    OverlayLayout,
    OverlayRenderModel,
    StatPanelModel,
    StatStatusCode,
    SynergyPanelModel,
    SynergyTextLayout,
    _clean_text,
    _format_percent,
    draw_overlay_frame,
    resolve_overlay_layout,
)
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


def _stats_display(
    hint: Mapping[str, Any],
    hint_cache: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> tuple[str, StatStatusCode, str, str, str]:
    snapshot_status = hint_cache.get("snapshot") if isinstance(hint_cache, Mapping) else None
    snapshot_state = str(snapshot_status.get("state") or "") if isinstance(snapshot_status, Mapping) else ""
    if snapshot_state == "unavailable":
        return "统计数据准备中", "DATA_NOT_READY", "", "", "数据准备中"
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
        return "源站暂无该组合统计", "SOURCE_STATS_MISSING", "", "", "源站暂无统计"
    winrate = _format_percent(stats.get("winrate"))
    pickrate = _format_percent(stats.get("pickrate"))
    text = _format_stats_entry(stats)
    if not (winrate and pickrate):
        return text or "统计字段不完整", "NO_STATS", "", "", "统计不完整"
    if snapshot_state == "degraded":
        # 代际信息保留在状态字段中，避免把诊断前缀挤进卡片内的单行统计。
        return text, "GENERATION_DEGRADED", winrate, pickrate, "上一代数据"
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
    snapshot_state: str,
    event_generation_id: str = "",
    snapshot_generation_id: str = "",
) -> str:
    if snapshot_state == "unavailable":
        return "SOURCE_UNAVAILABLE"
    if event_generation_id and snapshot_generation_id and event_generation_id != snapshot_generation_id:
        return "GENERATION_MISMATCH"
    if not (isinstance(context, Mapping) and context.get("ok")):
        return "CONTEXT_MISSING"
    return "READY" if isinstance(matched, Mapping) else "NO_MATCH"


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
    snapshot_state = str(snapshot_status.get("state") or "unavailable") if isinstance(snapshot_status, Mapping) else "unavailable"
    event_generation_id = _clean_text(event_source.get("generation_id"))
    snapshot_generation_id = _clean_text(snapshot_status.get("generation_id")) if isinstance(snapshot_status, Mapping) else ""
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
        if ready:
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
            stats_text, status_code = "识别失败/重试", "DETECTION_FAILED"
            winrate_text, pickrate_text, status_text = "", "", "识别失败/重试"
        else:
            stats_text, status_code = "识别中…", "DETECTING"
            winrate_text, pickrate_text, status_text = "", "", "识别中…"
        has_current_stats = status_code in {"READY", "GENERATION_DEGRADED"}
        matched = _matched_synergy(hint, context) if ready else None
        synergy_status = _synergy_status(
            matched=matched,
            context=context,
            snapshot_state=snapshot_state,
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
                "content": _clean_text(matched.get("content"), limit=180),
            }
        )
    return {"stats": stats, "synergies": synergies}
def build_render_model_from_session(
    state: GameSessionState,
    *,
    hint_cache: Mapping[str, Any] | None = None,
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
        "DETECTION_FAILED": ("识别失败/重试", "识别失败/重试"),
        "DATA_NOT_READY": ("统计数据准备中", "数据准备中"),
        "PRIVACY_OFF": ("已开启隐私模式", "统计关闭"),
        "SOURCE_STATS_MISSING": ("源站暂无该组合统计", "源站暂无统计"),
        "IDENTITY_UNRESOLVED": ("海克斯身份未解析", "身份未解析"),
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
                status_code = "DATA_NOT_READY"
        elif slot_state == "failed":
            status_code = "DETECTION_FAILED"
        elif slot_state != "ready":
            status_code = "DETECTING"
        elif not status_code:
            status_code = "SOURCE_STATS_MISSING"
        stats_payload = row.get("stats") if isinstance(row.get("stats"), Mapping) else {}
        normalized_stats = {
            "winrate": stats_payload.get(
                "winrate", stats_payload.get("win_rate", stats_payload.get("海克斯胜率"))
            ),
            "pickrate": stats_payload.get(
                "pickrate", stats_payload.get("pick_rate", stats_payload.get("海克斯出场率"))
            ),
        }
        winrate_text = _format_percent(normalized_stats["winrate"])
        pickrate_text = _format_percent(normalized_stats["pickrate"])
        if status_code in {"READY", "GENERATION_DEGRADED"}:
            stats_text = _format_stats_entry(normalized_stats)
            status_text = "上一代数据" if status_code == "GENERATION_DEGRADED" else ""
        elif status_code == "DETECTING":
            stats_text, status_text = "识别中…", "识别中…"
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
            }
        )
        hint = _query_hint(row, hint_cache) if ready else {}
        matched = _matched_synergy(hint, context_mapping)
        stats[-1]["synergy_status"] = _synergy_status(
            matched=matched,
            context=context_mapping,
            snapshot_state="unavailable" if recommendation is None else "ready",
            event_generation_id=str(state.generation_id),
            snapshot_generation_id=str(recommendation.generation_id) if recommendation is not None else "",
        )  # type: ignore[typeddict-item]
        if isinstance(matched, Mapping):
            synergies.append(
                {
                    "slot": index,
                    "augment_name": name,
                    "tier": tier,
                    "hero_name": _clean_text(matched.get("hero_name"), limit=40),
                    "rating": _clean_text(matched.get("rating"), limit=12),
                    "tag": _clean_text(matched.get("tag"), limit=24),
                    "content": _clean_text(matched.get("content"), limit=180),
                }
            )
    return {"stats": stats, "synergies": synergies}

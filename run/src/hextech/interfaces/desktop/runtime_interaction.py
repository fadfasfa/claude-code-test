"""本地桌面候选投影；无浏览器、Web预热或terminal入口。"""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Any
from hextech.modules.game_context.client import DEFAULT_CONTEXT_TTL_SECONDS
from hextech.interfaces.desktop.runtime_services import _append_unique_champion_id, _clean_champion_id, _candidate_groups_to_id_set, poll_lcu_live_ids
if TYPE_CHECKING:
    from hextech.interfaces.desktop.app import HextechUI



def normalize_candidate_groups(candidate_groups) -> dict[str, Any]:
    """兼容旧 set/list 输入，并把候选分组收口到稳定 schema。"""

    role_keys = {
        "local_champion_id",
        "teammate_champion_ids",
        "context_phase",
        "context_connection_state",
        "context_error_code",
    }
    if isinstance(candidate_groups, dict):
        selected = candidate_groups.get("selected_champion_ids") or candidate_groups.get("selected") or []
        bench = candidate_groups.get("bench_champion_ids") or candidate_groups.get("bench") or []
    else:
        selected = []
        bench = candidate_groups or []
    selected_ids: list[str] = []
    bench_ids: list[str] = []
    for value in selected:
        _append_unique_champion_id(selected_ids, value)
    for value in bench:
        _append_unique_champion_id(bench_ids, value)
    selected_set = set(selected_ids)
    normalized: dict[str, Any] = {
        "selected_champion_ids": selected_ids,
        "bench_champion_ids": [champion_id for champion_id in bench_ids if champion_id not in selected_set],
    }
    if isinstance(candidate_groups, dict) and role_keys.intersection(candidate_groups):
        local_id = _clean_champion_id(candidate_groups.get("local_champion_id"))
        teammate_ids: list[str] = []
        for value in candidate_groups.get("teammate_champion_ids", []):
            _append_unique_champion_id(teammate_ids, value)
        normalized.update(
            {
                "local_champion_id": local_id,
                "teammate_champion_ids": [value for value in teammate_ids if value != local_id],
                "context_phase": str(candidate_groups.get("context_phase") or ""),
                "context_connection_state": str(candidate_groups.get("context_connection_state") or ""),
                "context_error_code": str(candidate_groups.get("context_error_code") or ""),
            }
        )
    return normalized


def _preserve_recent_selection_roles(ui: "HextechUI", normalized_groups: dict) -> dict:
    """champ-select 结束瞬间短暂保留己方/队友角色标识，避免"已选"徽章闪断。

    `ClientContextProvider.not_in_champ_select()` 在 LCU 404（锁定完成、进入
    加载画面等自然过渡）时立即清空 local_champion_id/teammate_champion_ids；
    这与模块自身文档"短暂断连只保留最近一次未过期上下文"的承诺相悖，桌面
    是唯一没有补偿这个瞬断的消费方（overlay 渲染层已有等价的 recent_context
    兜底）。只在角色信息缺失、英雄池仍包含缓存的本地英雄 ID、且未超过与
    ClientContextProvider 一致的 TTL 时补回缓存值；真正换局或超时后如实清空。
    """

    local_id = str(normalized_groups.get("local_champion_id") or "")
    now = time.time()
    if local_id:
        ui._last_known_selection_roles = {
            "local_champion_id": local_id,
            "teammate_champion_ids": list(normalized_groups.get("teammate_champion_ids") or []),
            "at": now,
        }
        return normalized_groups

    cached = getattr(ui, "_last_known_selection_roles", None)
    if not cached or now - float(cached.get("at", 0.0)) > DEFAULT_CONTEXT_TTL_SECONDS:
        return normalized_groups

    cached_local_id = str(cached.get("local_champion_id") or "")
    known_ids = {
        *normalized_groups.get("selected_champion_ids", []),
        *normalized_groups.get("bench_champion_ids", []),
    }
    if not cached_local_id or cached_local_id not in known_ids:
        return normalized_groups

    return {
        **normalized_groups,
        "local_champion_id": cached_local_id,
        "teammate_champion_ids": [
            value for value in cached.get("teammate_champion_ids", []) if value in known_ids
        ],
    }


def _apply_candidate_update(ui: "HextechUI", candidate_groups, *, source: str, payload: dict | None = None) -> None:
    normalized_groups = normalize_candidate_groups(candidate_groups)
    normalized_groups = _preserve_recent_selection_roles(ui, normalized_groups)
    available_ids = _candidate_groups_to_id_set(normalized_groups)
    if available_ids != ui.current_hero_ids or normalized_groups != getattr(ui, "current_candidate_groups", {}):
        ui.current_hero_ids = available_ids.copy()
        ui.current_candidate_groups = normalized_groups
        def apply_current_candidates():
            if not getattr(ui, "_closing", False) and ui.current_candidate_groups == normalized_groups:
                ui.update_ui(normalized_groups)
        ui._run_on_ui_thread(apply_current_candidates)


def _sync_candidate_ids(ui: "HextechUI", candidate_groups, *, source: str, payload: dict | None = None) -> None:
    if candidate_groups is None:
        return
    _apply_candidate_update(ui, candidate_groups, source=source, payload=payload)


def _fallback_live_state(ui: "HextechUI") -> dict[str, list[str]] | None:
    return poll_lcu_live_ids(ui)

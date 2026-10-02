"""Finite statistics evidence bound to the consumed display, not the newest context."""
from collections.abc import Mapping
from typing import Any


def freeze_draw_statistics(prepared: Any, model: Mapping[str, Any]) -> dict[str, Any]:
    state = prepared.state
    recommendation = getattr(state, "recommendation", None)
    rows = list(recommendation.augment_slots) if recommendation is not None else []
    original = prepared.model.get("stats") or []
    drawn = model.get("stats") or []
    result = []
    for index, item in enumerate(drawn[:3]):
        bound = index < len(original) and item == original[index] and index < len(rows)
        row = rows[index] if bound else {}
        stats = (row.get("stats") or {}) if item.get("status_code") in {"READY", "GENERATION_DEGRADED"} else {}
        result.append({"slot": index, "prepared_binding_valid": bound,
            "canonical_id": row.get("canonical_id") or row.get("canonical_augment_id") or "",
            "win_rate": stats.get("win_rate", stats.get("winrate")),
            "pick_rate": stats.get("pick_rate", stats.get("pickrate")),
            "stats_text": str(item.get("stats_text") or "")[:160],
            "sample_count": stats.get("sample_count"), "requested_stage": row.get("requested_stage"),
            "source_run_id": row.get("source_run_id"), "stats_generation_id": row.get("stats_generation_id"),
            "stats_scope": row.get("stats_scope")})
    return {"context": dict(getattr(prepared, "context", None) or {}), "scope": dict(prepared.scope),
            "selection": dict(prepared.event.get("source") or {}).get("selection_epoch"),
            "session_id": dict(prepared.event.get("source") or {}).get("session_id"),
            "game_instance_id": dict(prepared.event.get("source") or {}).get("game_instance_id"),
            "slot_generations": [slot.get("slot_generation") for slot in (prepared.event.get("slots") or [])[:3]],
            "preparation_requested_at": (getattr(prepared, "input_timing", None) or {}).get("preparation_requested_at"),
            "rows": result}

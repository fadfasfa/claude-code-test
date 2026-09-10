"""一个Tracker内的同帧场景/身份分阶段；不复制Tracker或重复登记观察。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import math

from hextech.modules.vision.events import build_overlay_event


def frame_key(raw: Mapping[str, Any]) -> tuple:
    source, timing = raw.get("source") or {}, raw.get("timing") or {}
    return (str(source.get("session_id") or ""), int(source.get("window_hwnd") or 0),
            int(source.get("frame_id") or 0), float(timing.get("captured_at") or 0))


@dataclass(frozen=True)
class SceneFrameTicket:
    version: int
    key: tuple
    epoch: int
    pending_only: bool | None
    event: Mapping[str, Any]


def begin_frame(tracker: Any, raw: Mapping[str, Any]) -> SceneFrameTicket | None:
    key = frame_key(raw)
    if not key[0] or key[1] <= 0 or key[2] <= 0 or not math.isfinite(key[3]) or key[3] <= 0:
        raise ValueError("scene_frame_identity_missing")
    previous = getattr(tracker, "_last_scene_frame_key", None)
    if previous and previous[:2] == key[:2] and (key[2] <= previous[2] or key[3] < previous[3]):
        return None
    tracker._last_scene_frame_key = key
    source = raw.get("source") or {}
    if tracker.scene_active and not tracker.body_shard_latched and source.get("selection_click") and source.get("transition_kind") == "reroll":
        from .slot_reducer import begin_explicit_transition
        targets = set(source.get("cursor_over_slots") or [])
        if source.get("transition_source") == "async_mouse_down" and isinstance(source.get("transition_slot"), int):
            targets.add(source["transition_slot"])
        for index in targets:
            if 0 <= index < len(tracker.slots):
                begin_explicit_transition(tracker.slots[index], observed_at=key[3], frame_id=key[2],
                                          reason="slot_click_async" if source.get("transition_source") == "async_mouse_down" else "slot_click")
    tracker._phase_version = getattr(tracker, "_phase_version", 0) + 1
    tracker._defer_slot_reduction = True
    tracker._deferred_slot_mode = None
    try:
        event = tracker.update(raw)
    finally:
        tracker._defer_slot_reduction = False
    event["timing"] = dict(raw.get("timing") or {})
    event["source"].update(frame_id=key[2], session_id=key[0], window_hwnd=key[1], frame_phase="scene")
    ticket = SceneFrameTicket(tracker._phase_version, key, tracker.epoch, tracker._deferred_slot_mode, event)
    tracker._frame_ticket = ticket
    return ticket


def finish_frame(tracker: Any, ticket: SceneFrameTicket, raw: Mapping[str, Any]) -> dict[str, Any] | None:
    if (getattr(tracker, "_frame_ticket", None) is not ticket
        or getattr(tracker, "_phase_version", 0) != ticket.version
        or tracker.epoch != ticket.epoch or frame_key(raw) != ticket.key):
        return None
    tracker._frame_ticket = None
    source = raw.get("source") or {}
    if ticket.pending_only is None and ticket.event.get("source", {}).get("scene_state") in {"absent", "paused"}:
        return dict(ticket.event)
    # 后到的硬否定高于早期场景判断。这里只处理否决，不重新推进普通场景计数。
    negative = tracker._negative_scene_event(raw)
    if negative is not None:
        negative["timing"] = dict(raw.get("timing") or {})
        return negative
    if source.get("reason") == "body_shard_only":
        tracker.body_shard_latched = True
        tracker.scene_active = False
        for slot in tracker.slots:
            slot.clear()
        return tracker._body_shard_event(source)
    if ticket.pending_only is None:
        return dict(ticket.event)
    timing = raw.get("timing") or {}
    observed_at = float(timing.get("recognition_completed_at") or timing.get("captured_at"))
    slots, _, raw_slots, _ = tracker._reduce_slots(raw, source, observed_at=observed_at,
                                                 pending_only=ticket.pending_only)
    count = sum(s.get("state") == "ready" for s in slots)
    event = build_overlay_event(slots, source_tag="vision-sidecar", selection_type="hextech",
                                active=bool(tracker.scene_active and count))
    event["source"].update(dict(ticket.event.get("source") or {}))
    event["source"].update(selection_revision=tracker.selection_revision, ready_slots=count,
                            content_ready=count == 3, slot_states=[s.get("state", "detecting") for s in slots],
                            frame_phase="identity", matching_timing=dict(source.get("matching_timing") or {}))
    event["source"]["gate_state"] = "visible_ready" if count == 3 else "visible_partial" if count else "detecting"
    if event["source"].get("reason") in {"", "slots_detecting"}:
        event["source"]["reason"] = "" if count else "slots_detecting"
    event["timing"] = dict(timing)
    event["_raw_slots"] = [dict(s) for s in raw_slots]
    event["_acceptance_rules"] = [s.get("acceptance_rule", "") for s in slots]
    return event

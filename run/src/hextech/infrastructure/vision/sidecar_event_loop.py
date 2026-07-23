"""Vision sidecar event_loop 职责模块。"""
from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import (
    Any,
    DEFAULT_EXIT_UNSTABLE_FRAMES,
    DEFAULT_LOOP_HEARTBEAT_SECONDS,
    LayoutTransform,
    Mapping,
    Sequence,
    SLOT_COUNT,
    apply_transform,
    build_overlay_event,
    cursor_in_client_boxes,
    pick_card_panels,
    time,
)
from hextech.infrastructure.vision.sidecar_scene_geometry import _selection_button_source_fields

def _cursor_over_card_panels(
    client_rect: tuple[int, int, int, int],
    frame_size: tuple[int, int],
    source: Mapping[str, Any],
) -> bool:
    raw_transform = source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {}
    try:
        transform = LayoutTransform(
            dx_ratio=float(raw_transform.get("dx_ratio") or 0.0),
            dy_ratio=float(raw_transform.get("dy_ratio") or 0.0),
            scale=float(raw_transform.get("scale") or 1.0),
        )
    except (TypeError, ValueError):
        transform = LayoutTransform()
    panel_defs = pick_card_panels(frame_size)
    boxes = [apply_transform(box, frame_size, transform) for box in panel_defs]
    return cursor_in_client_boxes(client_rect, boxes)


def _slot_ready_for_display(slot: Mapping[str, Any]) -> bool:
    """统一正式显示判定，避免检测与生命周期对 ready 槽位语义分叉。"""

    return bool(slot.get("state") == "ready" and (slot.get("augment_id") or slot.get("name")))


def _ready_slot_count(slots: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for slot in list(slots)[:SLOT_COUNT] if _slot_ready_for_display(slot))


def _scene_active_from_slots(slots: Sequence[Mapping[str, Any]]) -> bool:
    """三张卡全部 ready 才具备正式显示条件。"""

    return len(list(slots)[:SLOT_COUNT]) == SLOT_COUNT and _ready_slot_count(slots) == SLOT_COUNT


def _slot_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    signature: list[str] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            continue
        candidates = slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else []
        top_name = ""
        if candidates and isinstance(candidates[0], Mapping):
            top_name = str(candidates[0].get("augment_id") or candidates[0].get("name") or "")
        signature.append(f"{slot.get('state') or 'empty'}:{slot.get('augment_id') or top_name}")
    return tuple(signature)


def _loop_event_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if bool(event_payload.get("active")):
        return ("active", *_slot_signature(event_payload))
    if source.get("selection_window_active") is True:
        return (
            "selection",
            str(source.get("gate_state") or ""),
            str(_event_ready_slots(event_payload)),
            *_slot_signature(event_payload),
        )
    return ("inactive", str(source.get("reason") or "inactive"))


def _inactive_reason(event_payload: Mapping[str, Any]) -> str:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    return str(source.get("reason") or "")


def _event_ready_slots(event_payload: Mapping[str, Any]) -> int:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    try:
        return int(source.get("ready_slots"))
    except (TypeError, ValueError):
        slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
        return _ready_slot_count([slot for slot in slots if isinstance(slot, Mapping)])


def _event_selection_button_fields(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_box = source.get("button_box")
    button_box = list(raw_box) if isinstance(raw_box, Sequence) and not isinstance(raw_box, (str, bytes)) else []
    try:
        blue_ratio = max(0.0, float(source.get("button_blue_ratio") or 0.0))
    except (TypeError, ValueError):
        blue_ratio = 0.0
    return {
        "selection_button_present": bool(source.get("selection_button_present")),
        "selection_window_active": bool(source.get("selection_window_active")),
        "button_blue_ratio": round(blue_ratio, 6),
        "button_box": button_box,
    }


def _event_content_ready(event_payload: Mapping[str, Any]) -> bool:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if isinstance(source.get("content_ready"), bool):
        return bool(source.get("content_ready"))
    slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    return _scene_active_from_slots([slot for slot in slots if isinstance(slot, Mapping)])


def _active_gate_state(event_payload: Mapping[str, Any]) -> tuple[str, str, str]:
    ready_slots = _event_ready_slots(event_payload)
    if _event_content_ready(event_payload):
        return "visible_ready", "", ""
    if ready_slots:
        return "partial_ready", "partial_ready", "partial_ready"
    return "detecting", "selection_scene_not_detected", "selection_scene_not_detected"


def _copy_active_lifecycle_event(event_payload: Mapping[str, Any], *, stable_frames: int) -> dict[str, Any]:
    event = dict(event_payload)
    source = dict(event.get("source") if isinstance(event.get("source"), Mapping) else {})
    gate_state, reason, unstable_reason = _active_gate_state(event)
    event["active"] = True
    event["source"] = source
    source["selection_window_active"] = True
    source["stable_frames"] = int(stable_frames)
    source["content_ready"] = _event_content_ready(event)
    source["ready_slots"] = _event_ready_slots(event)
    source["gate_state"] = gate_state
    source["reason"] = reason
    source["unstable_reason"] = unstable_reason
    return event


def _copy_inactive_detection(
    event_payload: Mapping[str, Any],
    *,
    reason: str,
    gate_state: str,
    stable_frames: int = 0,
) -> dict[str, Any]:
    event = dict(event_payload)
    source = dict(event.get("source") if isinstance(event.get("source"), Mapping) else {})
    event["active"] = False
    event["source"] = source
    source["stable_frames"] = int(stable_frames)
    source["gate_state"] = gate_state
    source["reason"] = reason
    source["unstable_reason"] = reason
    return event


def _build_loop_inactive_event(reason: str, *, poll_mode: str = "high") -> dict[str, Any]:
    event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
    event["source"].update(
        {
            "reason": reason,
            "gate_state": "inactive",
            "ready_slots": 0,
            "stable_frames": 0,
            "unstable_reason": reason,
            "poll_mode": poll_mode,
            **_selection_button_source_fields(present=False),
        }
    )
    return event


def should_write_loop_event(
    event_payload: Mapping[str, Any],
    *,
    last_signature: tuple[str, ...] | None,
    last_write_at: float,
    now: float,
    heartbeat_seconds: float = DEFAULT_LOOP_HEARTBEAT_SECONDS,
) -> bool:
    """判断 loop 是否需要写事件，避免每帧刷新运行态文件。"""

    signature = _loop_event_signature(event_payload)
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if bool(event_payload.get("active")) or source.get("selection_window_active") is True:
        return signature != last_signature or (now - float(last_write_at or 0.0)) >= float(heartbeat_seconds)
    return (
        last_signature is None
        or signature != last_signature
        or (bool(last_signature) and last_signature[0] in {"active", "selection"})
    )


def should_defer_unstable_event(
    last_signature: tuple[str, ...] | None,
    unstable_streak: int,
    *,
    selection_window_active: bool | None = None,
    reason: str = "",
    exit_unstable_frames: int = DEFAULT_EXIT_UNSTABLE_FRAMES,
) -> bool:
    """吸收短抖动；blocking/body 等硬阻断仍立即结束选择生命周期。"""

    if not last_signature or last_signature[0] != "active":
        return False
    if reason in {"blocking_modal_present", "body_shard_only", "game_not_foreground", "game_window_missing"}:
        return False
    return int(unstable_streak) < max(1, int(exit_unstable_frames))


def remaining_frame_sleep_seconds(frame_interval_ms: int, *, elapsed_seconds: float) -> float:
    """把 interval 解释为帧起点周期，识别耗时不得与固定休眠重复累加。"""

    target_seconds = max(0.05, int(frame_interval_ms) / 1000.0)
    return max(0.0, target_seconds - max(0.0, float(elapsed_seconds)))


def loop_event_needs_fast_poll(event_payload: Mapping[str, Any]) -> bool:
    """判断本帧是否应进入 warm-path fast poll。"""

    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    if bool(event_payload.get("active")) or source.get("selection_window_active") is True:
        return True
    if str(source.get("scene_state") or "") in {"candidate", "active"}:
        return True
    try:
        return int(source.get("ready_slots") or 0) > 0
    except (TypeError, ValueError):
        return False


def resolve_loop_poll_mode(
    event_payload: Mapping[str, Any],
    *,
    fast_until: float,
    now: float | None = None,
) -> str:
    """把当前事件和保持窗口收口成 scan/fast 两档。"""

    if loop_event_needs_fast_poll(event_payload):
        return "fast"
    current = time.monotonic() if now is None else float(now)
    return "fast" if current < float(fast_until) else "scan"


def stabilize_detections(events: Sequence[Mapping[str, Any]], *, required_frames: int = 2) -> dict[str, Any]:
    """要求连续多帧槽位 ID 一致，避免动画早期误输出。"""

    required = max(1, int(required_frames))
    recent = list(events)[-required:]
    if recent:
        latest_reason = _inactive_reason(recent[-1])
        if latest_reason in {"blocking_modal_present", "body_shard_only"}:
            return _copy_inactive_detection(
                recent[-1],
                reason=latest_reason,
                gate_state="blocked",
                stable_frames=1,
            )
    if len(recent) < required:
        if recent:
            return _copy_inactive_detection(
                recent[-1],
                reason="warming_up",
                gate_state="unstable",
                stable_frames=len(recent),
            )
        return _build_loop_inactive_event("warming_up")
    first_signature = _slot_signature(recent[0])
    if (
        bool(first_signature)
        and _event_content_ready(recent[0])
        and all(bool(event.get("active")) for event in recent)
        and all(_event_content_ready(event) for event in recent)
        and all(_slot_signature(event) == first_signature for event in recent)
    ):
        return _copy_active_lifecycle_event(recent[-1], stable_frames=len(recent))
    if all(not bool(event.get("active")) for event in recent):
        first_reason = _inactive_reason(recent[0])
        if first_reason and all(_inactive_reason(event) == first_reason for event in recent):
            gate_state = "partial_ready" if first_reason == "partial_ready" else "inactive"
            return _copy_inactive_detection(
                recent[-1],
                reason=first_reason,
                gate_state=gate_state,
                stable_frames=len(recent),
            )
    return _copy_inactive_detection(
        recent[-1],
        reason="unstable",
        gate_state="unstable",
    )



__all__ = [name for name in globals() if not name.startswith("__")]

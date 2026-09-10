"""Vision runner 的无副作用事件与槽状态辅助函数。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping, cast

from hextech.modules.data.ports.paths import get_var_dir
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.infrastructure.vision.held_scene import CaptureBinding, HeldSceneEvidence
from hextech.infrastructure.vision.scene_recovery import (
    SceneRecoveryReference,
    advance_scene_recovery,
    consume_scene_recovery_capture,
    recovery_confirmation_allows_held,
    scene_recovery_source_fields,
)


def held_request_for_frame(held: HeldSceneEvidence | None, tracker: SelectionTracker,
                           binding: CaptureBinding, frame_size: tuple[int, int], cursor_probe: Any):
    if held is None:
        return None
    cursor = cursor_probe(binding.client_rect, frame_size, {"layout_transform": {
        "dx_ratio": held.transform.dx_ratio, "dy_ratio": held.transform.dy_ratio, "scale": held.transform.scale}})
    return held.for_frame(tracker, binding, tuple(cursor))


def next_held_scene_evidence(previous: HeldSceneEvidence | None, raw_event: Mapping[str, Any],
                             event: Mapping[str, Any], tracker: SelectionTracker, binding: CaptureBinding,
                             *, allow_confirmed: bool = True):
    """保留显示不等于保留取证资格；失败/无按钮grace后必须重新正常确认。"""
    source = mutable_string_key_mapping(raw_event.get("source"))
    if source.get("scene_present") is True:
        return HeldSceneEvidence.from_confirmed(raw_event, tracker, binding) if allow_confirmed else None
    resolved = mutable_string_key_mapping(event.get("source"))
    if (previous is not None and source.get("hold_evidence_state") == "collecting"
        and resolved.get("scene_temporal_state") == "button_hold"):
        return previous.for_frame(tracker, binding)
    return None


def recovery_capture_for_frame(reference: SceneRecoveryReference | None, tracker: SelectionTracker,
                               game_instance_id: str, hwnd: int, rect: tuple[int, int, int, int],
                               dpi_scale: float, *, now: float):
    expected_epoch = tracker.epoch + int(not tracker.scene_active and tracker.scene_frames == 0)
    binding = CaptureBinding(game_instance_id, int(hwnd), tuple(rect), expected_epoch, dpi_scale)
    reference, force_full = consume_scene_recovery_capture(reference, binding, now=now)
    return binding, reference, force_full


def advance_recovery_event(reference: SceneRecoveryReference | None,
                           held: HeldSceneEvidence | None, raw_event: Mapping[str, Any],
                           event: dict[str, Any], tracker: SelectionTracker,
                           binding: CaptureBinding, *, now: float, full_capture_used: bool):
    previous = reference
    reference = advance_scene_recovery(
        reference, held, raw_event, event, tracker, binding, now=now,
    )
    allow_held = recovery_confirmation_allows_held(
        previous, reference, raw_event, tracker, binding, now=now,
    )
    source = mutable_string_key_mapping(event.get("source"))
    source.update(scene_recovery_source_fields(
        reference, full_capture_used=full_capture_used,
        confirmed=bool(previous is not None and allow_held
                       and mutable_string_key_mapping(raw_event.get("source")).get("scene_present") is True)))
    event["source"] = source
    cutoff = max(
        previous.evidence_not_before if previous else 0.0,
        reference.evidence_not_before if reference else 0.0,
    )
    return reference, allow_held, cutoff


def captured_frame_source(frame: Any, binding: CaptureBinding, frame_id: int, dpi_scale: float) -> dict[str, Any]:
    """捕获身份和有效范围统一进入观察事件，不改统计或槽身份。"""
    return {"session_id": binding.game_instance_id, "window_hwnd": binding.window_hwnd,
            "game_instance_id": binding.game_instance_id,
            "monitor_device": str(frame.info.get("hextech_monitor_device") or ""),
            "client_rect": list(binding.client_rect), "capture_size": list(frame.size),
            "dpi_scale": dpi_scale, "frame_id": frame_id,
            "capture_mode": str(frame.info.get("hextech_capture_mode") or "full"),
            "capture_roi_origin": list(frame.info.get("hextech_roi_origin") or (0, 0)),
            "capture_roi_size": list(frame.info.get("hextech_roi_size") or frame.size)}


def roi_dump_signature(event: Mapping[str, Any]) -> tuple[str, ...]:
    source = mutable_string_key_mapping(event.get("source"))
    raw_slots = event.get("_raw_slots")
    top_ids = []
    for slot in (raw_slots if isinstance(raw_slots, list) else [])[:3]:
        candidates = slot.get("top_candidates") if isinstance(slot, Mapping) else None
        top = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], Mapping) else {}
        top_ids.append(str(top.get("augment_id") or top.get("name") or ""))
    return (str(source.get("scene_state") or ""), str(source.get("reason") or ""),
            str(source.get("ready_slots") or 0), str(source.get("scoreboard_key_down") or False), *top_ids)


def captured_window_still_current(frame: Any, *, hwnd: int, client_rect: tuple[int, int, int, int],
                                  game_instance_id: str) -> bool:
    """生产截图之后复核同一前台客户区；无捕获元数据的离线 fixture 不探测 OS。"""
    if not any(str(key).startswith("hextech_") for key in frame.info):
        return True
    from hextech.modules.vision.window import _window_client_rect, game_window_identity
    from hextech.infrastructure.vision.sidecar_capture import _is_lol_game_foreground
    if (not game_instance_id or tuple(frame.info.get("hextech_client_rect") or ()) != tuple(client_rect)
        or not _is_lol_game_foreground(hwnd)
        or _window_client_rect(hwnd, allow_window_fallback=False) != tuple(client_rect)):
        return False
    observed = game_window_identity(hwnd)
    return bool(observed.get("game_instance_id") == game_instance_id
                and not game_window_mode_pause_reason(observed))


def sanitize_bootstrap_error_message(value: object) -> str:
    """bootstrap 仅保留可诊断错误，不暴露本机用户目录。"""

    text = str(value or "").strip()
    home = str(Path.home())
    if home:
        text = text.replace(home, "<home>")
        text = text.replace(home.replace("\\", "/"), "<home>")
    return re.sub(r"(?i)\b[a-z]:[\\/][^\s；;,]+", "<path>", text)


def mutable_string_key_mapping(value: object) -> dict[str, Any]:
    """把外部事件字典收窄为可写 source。"""

    if not isinstance(value, Mapping):
        return {}
    source = cast(Mapping[object, Any], value)
    return {str(key): item for key, item in source.items()}


def stable_slot_fingerprints(tracker: SelectionTracker) -> list[set[str]]:
    """只允许已 READY 的槽按感知指纹跳过矩阵投影。"""

    return [
        set(track.baseline_fingerprints) if track.stable_slot is not None else set()
        for track in tracker.slots
    ]


def attach_completed_ocr_evidence(
    raw_event: dict[str, Any],
    runtime: Any,
    tracker: SelectionTracker,
    *,
    session_id: str,
    selection_epoch: int,
    observed_at: float,
    minimum_captured_at: float = 0.0,
) -> list[str]:
    """drain 只附加私有原帧证据；Tracker 在本 tick 合并后统一发布。"""

    source = mutable_string_key_mapping(raw_event.get("source"))
    scene_open = bool(
        raw_event.get("selection_type") == "hextech"
        and (source.get("scene_present") or (tracker.scene_active and source.get("selection_button_present")
             and (source.get("card_residue") or any(source.get("name_residue") or []))))
        and str(source.get("scene_kind") or "hextech") == "hextech"
        and not source.get("blocking_modal")
        and not source.get("selection_confirmed")
    )
    completed = runtime.drain_completed_evidence(
        session_id=session_id,
        selection_epoch=selection_epoch,
        slot_generations=[max(1, slot.slot_generation) for slot in tracker.slots],
        observed_at=observed_at,
        scene_open=scene_open,
    )
    if minimum_captured_at > 0.0:
        completed = [item for item in completed if _completed_captured_at(item) >= minimum_captured_at]
    outcomes: list[str] = []
    if completed:
        raw_event["_completed_ocr_evidence"] = completed
        raw_event["_completed_ocr_outcomes"] = outcomes
    return outcomes


def _completed_captured_at(item: object) -> float:
    if not isinstance(item, Mapping):
        return 0.0
    evidence = item.get("evidence")
    if not isinstance(evidence, Mapping):
        return 0.0
    try:
        return float(evidence.get("captured_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def current_snapshot_generation_id() -> str:
    """只读 current 小指针；Sidecar 不重新加载 Stats payload。"""

    try:
        payload = json.loads((get_var_dir() / "snapshots" / "current.v2.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(payload.get("current_generation_id") or "") if isinstance(payload, Mapping) else ""


def game_window_mode_payload(identity: Mapping[str, object]) -> dict[str, object]:
    """把只读 game.cfg 探针投影为事件可公开的有限对象。"""

    return {
        "status": str(identity.get("game_window_mode_status") or "supported"),
        "mode": str(identity.get("game_window_mode") or "borderless"),
        "reason": str(identity.get("game_window_mode_reason") or ""),
        "source": str(identity.get("game_window_mode_source") or "game_cfg"),
        "observed_at": float(identity.get("game_window_mode_observed_at") or time.time()),
    }


def game_window_mode_pause_reason(identity: Mapping[str, object]) -> str:
    """返回受支持模式的空值，或 fail-closed pause 原因。"""

    status = str(identity.get("game_window_mode_status") or "supported")
    if status == "supported":
        return ""
    return "unsupported_fullscreen_mode" if status == "unsupported" else "game_window_mode_unknown"


__all__ = [
    "current_snapshot_generation_id",
    "game_window_mode_pause_reason",
    "game_window_mode_payload",
    "mutable_string_key_mapping",
    "sanitize_bootstrap_error_message",
    "stable_slot_fingerprints",
    "attach_completed_ocr_evidence",
]

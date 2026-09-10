"""生产单帧先场景反馈、再身份归约；窗口观察只采一次并绑定同帧。"""
from __future__ import annotations

import time
from typing import Any

from .runner_helpers import captured_frame_source, attach_completed_ocr_evidence, stable_slot_fingerprints


def process_captured_frame(frame, templates, *, sidecar, tracker, ocr, binding, frame_id: int,
                          capture_started_at: float, captured_at: float, preset: str, min_confidence: float,
                          held_scene, mouse_observer, left_mouse_was_down: bool, minimum_captured_at: float,
                          publish_scene) -> tuple[dict, dict, bool]:
    metadata = captured_frame_source(frame, binding, frame_id, binding.dpi_scale)
    ticket = None
    scene_called = False
    transition: dict[str, Any] | None = None
    left_down = sidecar.is_left_mouse_button_down()

    def attach(raw):
        nonlocal transition
        source = dict(raw.get("source") or {})
        source.update(metadata)
        if transition is None:
            covered = sidecar._cursor_over_card_slots(binding.client_rect, frame.size, source)
            transition = {"cursor_over_slots": covered, "cursor_over_cards": bool(covered),
                          "selection_click": bool(left_down and not left_mouse_was_down and covered)}
            if transition["selection_click"]:
                transition.update(transition_source="frame_mouse_down", transition_kind="card", transition_slot=int(covered[0]))
            if mouse_observer is not None:
                edge = mouse_observer.consume_slot_event(client_rect=binding.client_rect, frame_size=frame.size,
                    source={**source, **transition}, window_hwnd=binding.window_hwnd,
                    game_instance_id=binding.game_instance_id, selection_epoch=tracker.epoch)
                if edge is not None:
                    transition.update(edge, selection_click=True)
        source.update(transition)
        raw["source"] = source
        raw["timing"] = {"observation_kind": "recognition", "capture_status": "captured",
            "capture_started_at": capture_started_at, "captured_at": captured_at,
            "recognition_completed_at": time.time()}
        raw["_negative_minimum_captured_at"] = minimum_captured_at

    def on_scene(light):
        nonlocal ticket, scene_called
        scene_called = True
        attach(light)
        ticket = tracker.begin_frame(light)
        if ticket is not None and ticket.event.get("source", {}).get("selection_window_active"):
            # 轻量反馈不进入完整识别统计分母，也不等待任何磁盘诊断。
            feedback = dict(ticket.event)
            feedback["source"] = {**metadata, **dict(ticket.event.get("source") or {})}
            feedback["timing"] = {**light["timing"], "observation_kind": "scene_feedback"}
            publish_scene(feedback)

    raw = sidecar.detect_overlay_choices(frame, templates, preset_name=preset, min_confidence=min_confidence,
        stable_fingerprints=stable_slot_fingerprints(tracker), ocr_shadow=ocr, held_scene=held_scene,
        capture_binding=binding, on_scene=on_scene)
    attach(raw)
    if raw["source"].get("reason") == "capture_roi_invalid":
        raw["timing"].update(observation_kind="capture_failure", capture_status="invalid_roi")
    outcomes = attach_completed_ocr_evidence(raw, ocr, tracker, session_id=binding.game_instance_id,
        selection_epoch=binding.selection_epoch, observed_at=raw["timing"]["recognition_completed_at"],
        minimum_captured_at=minimum_captured_at)
    if ticket is not None:
        result = tracker.finish_frame(ticket, raw)
        if result is None:
            result = tracker.pause("frame_admission_expired")
    elif scene_called:
        result = tracker.pause("duplicate_scene_frame")
    else:
        result = tracker.update(raw)
    ocr.record_completed_outcomes(outcomes)
    return raw, result, left_down

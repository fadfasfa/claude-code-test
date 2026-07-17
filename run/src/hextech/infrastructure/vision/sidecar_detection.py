"""Vision sidecar detection 职责模块。"""
# ruff: noqa: F403, F405

from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import *
from hextech.infrastructure.vision.sidecar_scene_geometry import *
from hextech.infrastructure.vision.sidecar_fingerprints import *
from hextech.infrastructure.vision.sidecar_matching import *
from hextech.infrastructure.vision.sidecar_event_loop import *

def _slots_have_body_shard_keywords(slots: Sequence[Mapping[str, Any]]) -> bool:
    matched = 0
    for slot in list(slots)[:SLOT_COUNT]:
        text = " ".join(
            str(slot.get(key) or "").lower()
            for key in ("augment_id", "name", "summary", "diagnostic")
        )
        candidates = slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else []
        for candidate in candidates[:1]:
            if isinstance(candidate, Mapping):
                text += " " + " ".join(
                    str(candidate.get(key) or "").lower()
                    for key in ("augment_id", "name")
                )
        if any(keyword in text for keyword in BODY_SHARD_KEYWORDS):
            matched += 1
    return matched == SLOT_COUNT


def _relative_crop_array(image: Image.Image, region: tuple[float, float, float, float]) -> np.ndarray:
    width, height = image.size
    left, top, right, bottom = region
    box = (
        max(0, min(width - 1, int(round(left * width)))),
        max(0, min(height - 1, int(round(top * height)))),
        max(1, min(width, int(round(right * width)))),
        max(1, min(height, int(round(bottom * height)))),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return np.empty((0, 0, 3), dtype=np.uint8)
    return np.asarray(image.crop(box).convert("RGB"), dtype=np.uint8)


def _blocking_modal_present(image: Image.Image) -> bool:
    """识别 LoL 中央阻塞弹窗；弹窗存在时仍可诊断识别，但 overlay 不显示。"""

    panel = _relative_crop_array(image, BLOCKING_MODAL_PANEL_REGION)
    button = _relative_crop_array(image, BLOCKING_MODAL_BUTTON_REGION)
    if panel.size == 0 or button.size == 0:
        return False

    panel_red = panel[:, :, 0]
    panel_green = panel[:, :, 1]
    panel_blue = panel[:, :, 2]
    dark_ratio = float(np.mean((panel_red < 55) & (panel_green < 60) & (panel_blue < 70)))

    button_red = button[:, :, 0]
    button_green = button[:, :, 1]
    button_blue = button[:, :, 2]
    gold_ratio = float(
        np.mean(
            (button_red > 80)
            & (button_green > 45)
            & (button_green < 165)
            & (button_blue < 95)
            & (button_red > button_blue + 25)
        )
    )
    return dark_ratio >= BLOCKING_MODAL_MIN_DARK_RATIO and gold_ratio >= BLOCKING_MODAL_MIN_BUTTON_GOLD_RATIO


def _slot_ready_for_display(slot: Mapping[str, Any]) -> bool:
    return bool(slot.get("state") == "ready" and (slot.get("augment_id") or slot.get("name")))


def _ready_slot_count(slots: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for slot in list(slots)[:SLOT_COUNT] if _slot_ready_for_display(slot))


def _scene_active_from_slots(slots: Sequence[Mapping[str, Any]]) -> bool:
    """三张卡全部 ready 才具备正式显示条件。"""

    return len(list(slots)[:SLOT_COUNT]) == SLOT_COUNT and _ready_slot_count(slots) == SLOT_COUNT


def detect_overlay_choices(
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    calibration_path: str | Path | None = None,
) -> dict[str, Any]:
    """执行单帧 V2 观察；跨帧生命周期由 ``SelectionTracker`` 负责。"""

    _ = calibration_path  # 保留旧调用签名；V2 不再读取或写入持久化 anchor。
    started_at = time.perf_counter()
    image = frame.convert("RGB")
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    scene = detect_selection_scene(image, layout_id=preset.name)
    transform_payload = {
        "dx_ratio": round(scene.transform.dx_ratio, 6),
        "dy_ratio": round(scene.transform.dy_ratio, 6),
        "scale": round(scene.transform.scale, 6),
    }
    if not scene.present or scene.button_box is None:
        residual_name_boxes = tuple(
            apply_transform(box, image.size, scene.transform) for box in preset.name_slots
        )
        name_residue = [
            _name_crop_has_residue(image.crop(box)) for box in residual_name_boxes
        ]
        event = _build_loop_inactive_event("selection_scene_not_detected")
        event["source"].update(
            {
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "preset": preset.name,
                "capture_size": [int(image.size[0]), int(image.size[1])],
                "calibration": "layout_v2",
                "scene_present": False,
                "scene_state": "absent",
                "scene_kind": "absent",
                "scene_score": scene.score,
                "layout_id": scene.layout_id,
                "layout_transform": transform_payload,
                "panel_scores": list(scene.panel_scores),
                "name_residue": name_residue,
                "card_residue": bool(scene.card_residue or any(name_residue)),
                "scene_reject_reason": scene.reason or "selection_scene_not_detected",
                "content_ready": False,
                **_selection_button_source_fields(
                    present=scene.button_box is not None,
                    window_active=False,
                    button_box=scene.button_box,
                    blue_ratio=scene.button_blue_ratio,
                ),
            }
        )
        event["_raw_slots"] = []
        return event

    slot_boxes = tuple(apply_transform(box, image.size, scene.transform) for box in preset.slots)
    name_boxes = tuple(apply_transform(box, image.size, scene.transform) for box in preset.name_slots)
    name_crops = [image.crop(box) for box in name_boxes]
    name_masks = [_name_text_mask(crop) for crop in name_crops]
    body_shard_scores = _body_shard_name_scores(name_crops, name_masks=name_masks)
    name_residue = [
        _name_crop_has_residue(crop, name_mask=name_masks[index])
        for index, crop in enumerate(name_crops)
    ]
    common_source = {
        "latency_ms": 0.0,
        "preset": preset.name,
        "capture_size": [int(image.size[0]), int(image.size[1])],
        "calibration": "layout_v2",
        "scene_present": True,
        "scene_state": "candidate",
        "scene_kind": "body_shard" if _body_shard_scene_present(body_shard_scores) else "hextech",
        "scene_score": scene.score,
        "layout_id": scene.layout_id,
        "layout_transform": transform_payload,
        "panel_scores": list(scene.panel_scores),
        "name_residue": name_residue,
        "card_residue": bool(scene.card_residue or any(name_residue)),
        "body_shard_scores": list(body_shard_scores),
    }

    if _body_shard_scene_present(body_shard_scores):
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                **common_source,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "gate_state": "blocked",
                "ready_slots": 0,
                "content_ready": False,
                "stable_frames": 0,
                "unstable_reason": "body_shard_only",
                "poll_mode": "high",
                "reason": "body_shard_only",
                "body_shard_latched": True,
                **_selection_button_source_fields(
                    present=True,
                    window_active=False,
                    button_box=scene.button_box,
                    blue_ratio=scene.button_blue_ratio,
                ),
            }
        )
        event["_raw_slots"] = []
        return event

    slots = [
        _detect_slot(
            image,
            box,
            index,
            template_index,
            name_box=name_boxes[index] if index < len(name_boxes) else None,
            name_mask=name_masks[index] if index < len(name_masks) else None,
            min_confidence=min_confidence,
        )
        for index, box in enumerate(slot_boxes)
    ]
    if _slots_have_body_shard_keywords(slots):
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                **common_source,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
                "gate_state": "blocked",
                "ready_slots": _ready_slot_count(slots),
                "content_ready": False,
                "stable_frames": 0,
                "unstable_reason": "body_shard_only",
                "poll_mode": "high",
                "reason": "body_shard_only",
                "scene_kind": "body_shard",
                "body_shard_latched": True,
                **_selection_button_source_fields(
                    present=True,
                    window_active=False,
                    button_box=scene.button_box,
                    blue_ratio=scene.button_blue_ratio,
                ),
            }
        )
        event["_raw_slots"] = slots
        return event

    blocking_modal = _blocking_modal_present(image)
    ready_slots = _ready_slot_count(slots)
    content_ready = _scene_active_from_slots(slots)
    active = content_ready and not blocking_modal
    if active and content_ready:
        reason = ""
        gate_state = "visible_ready"
        unstable_reason = ""
    elif blocking_modal:
        reason = "blocking_modal_present"
        gate_state = "blocked"
        unstable_reason = reason
    elif ready_slots:
        reason = "partial_ready"
        gate_state = "partial_ready"
        unstable_reason = reason
    else:
        reason = "selection_scene_not_detected"
        gate_state = "detecting"
        unstable_reason = reason
    event = build_overlay_event(
        slots,
        source_tag="vision-sidecar",
        selection_type="hextech",
        active=active,
    )
    event["source"].update(
        {
            **common_source,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "gate_state": gate_state,
            "ready_slots": ready_slots,
            "content_ready": content_ready,
            "stable_frames": 0,
            "slot_states": [str(slot.get("state") or "") for slot in slots[:SLOT_COUNT]],
            "blocking_modal": blocking_modal,
            "unstable_reason": unstable_reason,
            "poll_mode": "high",
            "reason": reason,
            **_selection_button_source_fields(
                present=True,
                window_active=not blocking_modal,
                button_box=scene.button_box,
                blue_ratio=scene.button_blue_ratio,
            ),
        }
    )
    event["_raw_slots"] = slots
    return event



__all__ = [name for name in globals() if not name.startswith("__")]

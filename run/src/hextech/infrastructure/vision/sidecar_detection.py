"""Vision sidecar detection 职责模块。"""
from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import (
    BLOCKING_MODAL_BUTTON_REGION,
    BLOCKING_MODAL_MIN_BUTTON_GOLD_RATIO,
    BLOCKING_MODAL_MIN_DARK_RATIO,
    BLOCKING_MODAL_PANEL_REGION,
    BODY_SHARD_KEYWORDS,
    DEFAULT_MIN_CONFIDENCE,
    Any,
    Image,
    Mapping,
    Path,
    SLOT_COUNT,
    Sequence,
    TemplateEntry,
    apply_transform,
    build_overlay_event,
    detect_selection_scene,
    np,
    time,
)
from hextech.infrastructure.vision.sidecar_event_loop import (
    _build_loop_inactive_event,
    _ready_slot_count,
    _scene_active_from_slots,
)
from hextech.infrastructure.vision.sidecar_fingerprints import (
    _body_shard_name_scores,
    _body_shard_scene_present,
    _name_crop_has_residue,
    _name_text_mask,
)
from hextech.infrastructure.vision.sidecar_batch import _detect_slots
from hextech.infrastructure.vision.sidecar_scene_geometry import _selection_button_source_fields, resolve_roi_preset
from hextech.infrastructure.vision.capture_geometry import capture_regions_valid, required_capture_bounds
from hextech.infrastructure.vision.held_scene import CaptureBinding, HeldSceneEvidence
from hextech.infrastructure.vision.slot_evidence import slot_evidence_fingerprint

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


def detect_overlay_choices(
    frame: Image.Image,
    template_index: Sequence[TemplateEntry],
    *,
    preset_name: str = "auto",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    calibration_path: str | Path | None = None,
    stable_fingerprints: Sequence[set[str]] | None = None,
    ocr_shadow: Any | None = None,
    held_scene: HeldSceneEvidence | None = None,
    capture_binding: CaptureBinding | None = None,
    on_scene: Any | None = None,
) -> dict[str, Any]:
    """执行单帧 V2 观察；跨帧生命周期由 ``SelectionTracker`` 负责。"""

    _ = calibration_path  # 保留旧调用签名；V2 不再读取或写入持久化 anchor。
    started_at = time.perf_counter()
    image = frame if frame.mode == "RGB" else frame.convert("RGB")
    preset = resolve_roi_preset(*image.size, preset=preset_name)
    if not capture_regions_valid(frame, (required_capture_bounds(image.size, preset_name),)):
        event = _build_loop_inactive_event("capture_roi_invalid")
        event["_raw_slots"] = []
        return event
    scene_started_at = time.perf_counter()
    scene = detect_selection_scene(image, layout_id=preset.name)
    scene_ms = (time.perf_counter() - scene_started_at) * 1000.0
    transform_payload = {
        "dx_ratio": round(scene.transform.dx_ratio, 6),
        "dy_ratio": round(scene.transform.dy_ratio, 6),
        "scale": round(scene.transform.scale, 6),
    }
    holding = bool(not scene.present and scene.button_box is not None and held_scene is not None
                   and held_scene.matches(capture_binding, image.size))
    if (not scene.present or scene.button_box is None) and not holding:
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

    transform = held_scene.transform if holding and held_scene is not None else scene.transform
    transform_payload = {"dx_ratio": transform.dx_ratio, "dy_ratio": transform.dy_ratio, "scale": transform.scale}
    slot_boxes = tuple(apply_transform(box, image.size, transform) for box in preset.slots)
    name_boxes = tuple(apply_transform(box, image.size, transform) for box in preset.name_slots)
    if not capture_regions_valid(frame, (*slot_boxes, *name_boxes)):
        event = _build_loop_inactive_event("capture_roi_invalid")
        event["_raw_slots"] = []
        return event
    name_crops = [image.crop(box) for box in name_boxes]
    name_mask_started_at = time.perf_counter()
    name_masks = [_name_text_mask(crop) for crop in name_crops]
    name_mask_ms = (time.perf_counter() - name_mask_started_at) * 1000.0
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
        "scene_present": not holding,
        "scene_state": "absent" if holding else "candidate",
        "scene_kind": "body_shard" if _body_shard_scene_present(body_shard_scores) else "hextech",
        "scene_score": scene.score,
        "layout_id": scene.layout_id,
        "layout_transform": transform_payload,
        "panel_scores": list(scene.panel_scores),
        "name_residue": name_residue,
        "card_residue": bool(scene.card_residue or any(name_residue)),
        "body_shard_scores": list(body_shard_scores),
    }
    blocking_modal = _blocking_modal_present(image)
    if holding:
        common_source["scene_reject_reason"] = scene.reason or "selection_scene_not_detected"
        common_source["hold_evidence_state"] = "collecting"
        if blocking_modal:
            event = _build_loop_inactive_event("blocking_modal_present")
            event["source"].update(common_source, blocking_modal=True)
            event["_raw_slots"] = []
            return event

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

    eligible = tuple(bool(held_scene.eligible_slots[i] and name_residue[i]) for i in range(SLOT_COUNT)) if holding else None
    batch_options = {"eligible_slots": eligible} if eligible is not None else {}
    # 模板排序前先提交当前帧OCR，完成结果仍由同一个Tracker验证绑定和独立帧。
    fingerprints = [slot_evidence_fingerprint(name_crops[i], image.crop(slot_boxes[i])) for i in range(SLOT_COUNT)]
    early_slots = [{"slot": i, "evidence_fingerprint": fingerprints[i]} for i in range(SLOT_COUNT)]
    ocr_submit_ms = 0.0
    if not blocking_modal and ocr_shadow is not None:
        ocr_started_at = time.perf_counter()
        ocr_shadow.observe(name_crops, early_slots, **batch_options)
        ocr_submit_ms = (time.perf_counter()-ocr_started_at)*1000
    if on_scene is not None:
        light = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        light["source"].update(common_source, reason="blocking_modal_present" if blocking_modal else "slots_detecting",
            blocking_modal=blocking_modal, **_selection_button_source_fields(present=scene.button_box is not None,
            window_active=bool(scene.present and not blocking_modal), button_box=scene.button_box,
            blue_ratio=scene.button_blue_ratio))
        light["_raw_slots"] = early_slots
        on_scene(light)
    slots, matching_timing = _detect_slots(
        image,
        slot_boxes,
        template_index,
        name_boxes=name_boxes,
        name_masks=name_masks,
        min_confidence=min_confidence,
        stable_fingerprints=stable_fingerprints,
        name_crops=name_crops,
        **batch_options,
    )
    for slot, early in zip(slots, early_slots, strict=True):
        for key in ("ocr_shadow", "ocr_production"):
            if key in early:
                slot[key] = early[key]
    matching_timing["scene_ms"] = round(scene_ms, 3)
    matching_timing["name_mask_ms"] = round(name_mask_ms, 3)
    matching_timing["ocr_submit_ms"] = round(ocr_submit_ms, 3)
    common_source["matching_timing"] = matching_timing
    common_source["compute_profile"] = "float32_batched"
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

    ready_slots = _ready_slot_count(slots)
    content_ready = _scene_active_from_slots(slots)
    active = ready_slots > 0 and not blocking_modal and not holding
    if content_ready and not blocking_modal:
        reason = ""
        gate_state = "visible_ready"
        unstable_reason = ""
    elif blocking_modal:
        reason = "blocking_modal_present"
        gate_state = "blocked"
        unstable_reason = reason
    elif active:
        reason = ""
        gate_state = "visible_partial"
        unstable_reason = ""
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
                window_active=not blocking_modal and not holding,
                button_box=scene.button_box,
                blue_ratio=scene.button_blue_ratio,
            ),
        }
    )
    event["_raw_slots"] = slots
    return event



__all__ = [name for name in globals() if not name.startswith("__")]

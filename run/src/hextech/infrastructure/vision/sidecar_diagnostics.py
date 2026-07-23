"""Vision sidecar diagnostics 职责模块。"""
from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import (
    BODY_SHARD_STRONG_CONFIDENCE,
    BODY_SHARD_SUPPORT_CONFIDENCE,
    BODY_SHARD_VERY_STRONG_CONFIDENCE,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_MARGIN,
    DEFAULT_TEXT_MIN_CONFIDENCE,
    FLAT_CROP_STD_THRESHOLD,
    ICON_SHORTLIST_MAX_DELTA,
    ICON_SHORTLIST_MIN_CONFIDENCE,
    Any,
    Image,
    LayoutTransform,
    Mapping,
    OVERLAY_VISION_TRACE_FILE,
    OVERLAY_VISION_TRACE_HISTORY_FILE,
    Path,
    ROI_DIAGNOSTIC_LIMIT,
    SCENE_SLOT_MIN_CONFIDENCE,
    SLOT_COUNT,
    Sequence,
    V2_BUTTON_SEARCH_REGION,
    VISION_TRACE_HISTORY_LIMIT,
    VISION_TRACE_REFRESH_SECONDS,
    VISION_TRACE_SCHEMA_VERSION,
    _LAST_VISION_TRACE_SIGNATURES,
    _LAST_VISION_TRACE_WRITES,
    apply_transform,
    atomic_write_json,
    json,
    shutil,
    time,
)
from hextech.infrastructure.vision.sidecar_event_loop import _event_ready_slots, _slot_signature
from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset

def _write_roi_diagnostic_dump(
    dump_root: str | Path,
    frame: Image.Image,
    event_payload: Mapping[str, Any],
) -> Path:
    """只保存按钮、图标和卡名 ROI；禁止写入完整游戏帧。"""

    root = Path(dump_root) / "overlay_roi_v2"
    stamp = f"roi-{time.strftime('%Y%m%d-%H%M%S')}-{int((time.time() % 1) * 1000):03d}"
    target = root / stamp
    target.mkdir(parents=True, exist_ok=False)
    image = frame.convert("RGB")
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    preset = resolve_roi_preset(*image.size, preset=str(source.get("preset") or "auto"))
    transform_source = source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {}
    transform = LayoutTransform(
        dx_ratio=float(transform_source.get("dx_ratio") or 0.0),
        dy_ratio=float(transform_source.get("dy_ratio") or 0.0),
        scale=float(transform_source.get("scale") or 1.0),
    )
    raw_button_box = source.get("button_box")
    if isinstance(raw_button_box, list) and len(raw_button_box) == 4:
        button_box = tuple(int(value) for value in raw_button_box)
    else:
        button_box = apply_transform(V2_BUTTON_SEARCH_REGION, image.size, LayoutTransform())
    image.crop(button_box).save(target / "button.png")
    for index, box in enumerate(preset.slots):
        image.crop(apply_transform(box, image.size, transform)).save(target / f"icon_{index}.png")
    for index, box in enumerate(preset.name_slots):
        image.crop(apply_transform(box, image.size, transform)).save(target / f"name_{index}.png")
    atomic_write_json(
        target / "report.json",
        build_vision_trace_payload(event_payload),
        ensure_ascii=False,
        indent=2,
    )

    directories = sorted((path for path in root.glob("roi-*") if path.is_dir()), key=lambda path: path.name)
    for expired in directories[:-ROI_DIAGNOSTIC_LIMIT]:
        shutil.rmtree(expired, ignore_errors=True)
    return target


def build_vision_trace_payload(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    """生成最近一次识别链路诊断；正式 overlay 不读取该文件。"""

    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_slots = event_payload.get("_raw_slots") if isinstance(event_payload.get("_raw_slots"), list) else (
        event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    )
    rendered_slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    acceptance_rules = event_payload.get("_acceptance_rules") if isinstance(event_payload.get("_acceptance_rules"), list) else []
    slots: list[dict[str, Any]] = []
    for index, slot in enumerate(raw_slots[:SLOT_COUNT]):
        if not isinstance(slot, Mapping):
            continue
        rendered_slot = rendered_slots[index] if index < len(rendered_slots) and isinstance(rendered_slots[index], Mapping) else {}
        slots.append(
            {
                "slot": int(slot.get("slot") if slot.get("slot") is not None else index),
                "state": str(slot.get("state") or ""),
                "augment_id": str(slot.get("augment_id") or ""),
                "name": str(slot.get("name") or ""),
                "confidence": slot.get("confidence"),
                "diagnostic": str(slot.get("diagnostic") or ""),
                "acceptance_rule": str(
                    (acceptance_rules[index] if index < len(acceptance_rules) else "")
                    or rendered_slot.get("acceptance_rule")
                    or slot.get("acceptance_rule")
                    or ""
                ),
                "top_candidates": slot.get("top_candidates") if isinstance(slot.get("top_candidates"), list) else [],
                "channels": slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {},
            }
        )
    return {
        "schema_version": VISION_TRACE_SCHEMA_VERSION,
        "generated_at": time.time(),
        "active": bool(event_payload.get("active")),
        "selection_type": str(event_payload.get("selection_type") or ""),
        "source": {
            "reason": str(source.get("reason") or ""),
            "gate_state": str(source.get("gate_state") or ""),
            "ready_slots": source.get("ready_slots"),
            "content_ready": source.get("content_ready"),
            "selection_button_present": source.get("selection_button_present"),
            "selection_window_active": source.get("selection_window_active"),
            "button_blue_ratio": source.get("button_blue_ratio"),
            "button_box": source.get("button_box") if isinstance(source.get("button_box"), list) else [],
            "blocking_modal": source.get("blocking_modal"),
            "calibration": source.get("calibration"),
            "preset": source.get("preset"),
            "capture_size": source.get("capture_size"),
            "latency_ms": source.get("latency_ms"),
            "scene_state": source.get("scene_state"),
            "scene_kind": source.get("scene_kind"),
            "scene_score": source.get("scene_score"),
            "layout_id": source.get("layout_id"),
            "layout_transform": source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {},
            "selection_epoch": source.get("selection_epoch"),
            "scoreboard_key_down": source.get("scoreboard_key_down"),
            "body_shard_scores": source.get("body_shard_scores")
            if isinstance(source.get("body_shard_scores"), list)
            else [],
            "body_shard_latched": bool(source.get("body_shard_latched")),
            "cursor_over_cards": bool(source.get("cursor_over_cards")),
            "card_residue": bool(source.get("card_residue")),
            "name_residue": source.get("name_residue") if isinstance(source.get("name_residue"), list) else [],
            "hover_occluded": bool(source.get("hover_occluded")),
            "slot_states": source.get("slot_states") if isinstance(source.get("slot_states"), list) else [],
        },
        "thresholds": {
            "min_confidence": DEFAULT_MIN_CONFIDENCE,
            "scene_slot_min_confidence": SCENE_SLOT_MIN_CONFIDENCE,
            "text_min_confidence": DEFAULT_TEXT_MIN_CONFIDENCE,
            "min_margin": DEFAULT_MIN_MARGIN,
            "flat_crop_std": FLAT_CROP_STD_THRESHOLD,
            "dual_font_confidence": 0.70,
            "weak_text_confidence": 0.68,
            "weak_text_margin": 0.01,
            "body_shard_strong_confidence": BODY_SHARD_STRONG_CONFIDENCE,
            "body_shard_very_strong_confidence": BODY_SHARD_VERY_STRONG_CONFIDENCE,
            "body_shard_support_confidence": BODY_SHARD_SUPPORT_CONFIDENCE,
            "icon_shortlist_min_confidence": ICON_SHORTLIST_MIN_CONFIDENCE,
            "icon_shortlist_max_delta": ICON_SHORTLIST_MAX_DELTA,
        },
        "slots": slots,
    }


def _public_event_payload(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    """移除只供进程内 trace 使用的私有字段。"""

    payload = dict(event_payload)
    payload.pop("_raw_slots", None)
    payload.pop("_acceptance_rules", None)
    return payload


def _vision_trace_signature(event_payload: Mapping[str, Any]) -> tuple[str, ...]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_slots = event_payload.get("_raw_slots") if isinstance(event_payload.get("_raw_slots"), list) else []
    selection_active = source.get("selection_window_active") is True
    raw_signature: list[str] = []
    for slot in raw_slots[:SLOT_COUNT]:
        if not isinstance(slot, Mapping):
            raw_signature.append("")
            continue
        if not selection_active:
            continue
        channels = slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {}
        for channel_name in ("text", "text_alt", "icon", "text_narrowed", "text_alt_narrowed"):
            channel = channels.get(channel_name) if isinstance(channels.get(channel_name), Mapping) else {}
            candidates = channel.get("top_candidates") if isinstance(channel.get("top_candidates"), list) else []
            top = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
            raw_signature.append(f"{channel_name}:{top.get('augment_id') or top.get('name') or ''}")
    acceptance_rules = event_payload.get("_acceptance_rules") if isinstance(event_payload.get("_acceptance_rules"), list) else []
    return (
        "active" if event_payload.get("active") else "inactive",
        str(source.get("reason") or ""),
        str(source.get("gate_state") or ""),
        str(source.get("ready_slots") or ""),
        str(source.get("selection_window_active") or ""),
        str(source.get("scene_kind") or ""),
        "body_shard_latched" if source.get("body_shard_latched") else "",
        "cursor_over_cards" if source.get("cursor_over_cards") else "",
        "hover_occluded" if source.get("hover_occluded") else "",
        *_slot_signature(event_payload),
        *(str(rule or "") for rule in acceptance_rules[:SLOT_COUNT]),
        *raw_signature,
    )


def _vision_trace_path_for_event(event_path: str | Path | None = None) -> Path:
    if event_path is None:
        return OVERLAY_VISION_TRACE_FILE
    return Path(event_path).with_name(OVERLAY_VISION_TRACE_FILE.name)


def _vision_trace_history_path(trace_path: str | Path | None = None) -> Path:
    target = Path(trace_path) if trace_path is not None else OVERLAY_VISION_TRACE_FILE
    return target.with_name(OVERLAY_VISION_TRACE_HISTORY_FILE.name)


def _vision_trace_history_entry(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    raw_box = source.get("button_box")
    button_box = list(raw_box) if isinstance(raw_box, list) else []
    capture_size = source.get("capture_size")
    center_y_ratio = None
    if (
        len(button_box) == 4
        and isinstance(capture_size, Sequence)
        and not isinstance(capture_size, (str, bytes))
        and len(capture_size) == 2
    ):
        try:
            capture_height = float(capture_size[1])
            if capture_height > 0:
                center_y_ratio = round((float(button_box[1]) + float(button_box[3])) / 2.0 / capture_height, 6)
        except (TypeError, ValueError):
            center_y_ratio = None
    ready_slots = _event_ready_slots(event_payload)
    blocking_modal = bool(source.get("blocking_modal"))
    visible = bool(
        event_payload.get("active")
        and str(event_payload.get("selection_type") or "") == "hextech"
        and ready_slots >= 1
        and not blocking_modal
        and not bool(source.get("scoreboard_key_down"))
    )
    rendered_slots = event_payload.get("slots") if isinstance(event_payload.get("slots"), list) else []
    raw_slots = event_payload.get("_raw_slots") if isinstance(event_payload.get("_raw_slots"), list) else []
    slot_summaries: list[dict[str, Any]] = []
    for index in range(SLOT_COUNT):
        rendered = rendered_slots[index] if index < len(rendered_slots) and isinstance(rendered_slots[index], Mapping) else {}
        raw = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
        candidates = rendered.get("top_candidates")
        if not isinstance(candidates, list):
            candidates = raw.get("top_candidates") if isinstance(raw.get("top_candidates"), list) else []
        slot_summaries.append(
            {
                "slot": index,
                "state": str(rendered.get("state") or "detecting"),
                "augment_id": str(rendered.get("augment_id") or ""),
                "name": str(rendered.get("name") or ""),
                "confidence": rendered.get("confidence"),
                "rejection_reason": str(
                    rendered.get("rejection_reason")
                    or rendered.get("diagnostic")
                    or raw.get("diagnostic")
                    or raw.get("reason")
                    or ""
                ),
                "top_candidates": [dict(item) for item in candidates[:3] if isinstance(item, Mapping)],
            }
        )
    return {
        "generated_at": time.time(),
        "session_id": str(source.get("session_id") or ""),
        "window_hwnd": int(source.get("window_hwnd") or 0),
        "client_rect": list(source.get("client_rect")) if isinstance(source.get("client_rect"), list) else [],
        "capture_size": list(capture_size) if isinstance(capture_size, list) else [],
        "dpi_scale": float(source.get("dpi_scale") or 0.0),
        "active": bool(event_payload.get("active")),
        "visible": visible,
        "selection_type": str(event_payload.get("selection_type") or ""),
        "reason": str(source.get("reason") or ""),
        "gate_state": str(source.get("gate_state") or ""),
        "calibration": str(source.get("calibration") or ""),
        "scene_state": str(source.get("scene_state") or ""),
        "scene_kind": str(source.get("scene_kind") or ""),
        "scene_score": float(source.get("scene_score") or 0.0),
        "layout_id": str(source.get("layout_id") or ""),
        "selection_epoch": int(source.get("selection_epoch") or 0),
        "scoreboard_key_down": bool(source.get("scoreboard_key_down")),
        "body_shard_scores": list(source.get("body_shard_scores"))
        if isinstance(source.get("body_shard_scores"), list)
        else [],
        "body_shard_latched": bool(source.get("body_shard_latched")),
        "cursor_over_cards": bool(source.get("cursor_over_cards")),
        "card_residue": bool(source.get("card_residue")),
        "hover_occluded": bool(source.get("hover_occluded")),
        "ready_slots": ready_slots,
        "slot_states": list(source.get("slot_states")) if isinstance(source.get("slot_states"), list) else [],
        "stable_frames": int(source.get("stable_frames") or 0),
        "selection_button_present": bool(source.get("selection_button_present")),
        "selection_window_active": bool(source.get("selection_window_active")),
        "button_blue_ratio": float(source.get("button_blue_ratio") or 0.0),
        "button_center_y_ratio": center_y_ratio,
        "button_box": button_box,
        "slot_signature": list(_slot_signature(event_payload)),
        "slots": slot_summaries,
    }


def _append_vision_trace_history(
    event_payload: Mapping[str, Any],
    path: str | Path,
) -> Path:
    target = Path(path)
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    entries = existing.get("entries") if isinstance(existing, Mapping) else None
    history = [dict(item) for item in entries if isinstance(item, Mapping)] if isinstance(entries, list) else []
    history.append(_vision_trace_history_entry(event_payload))
    payload = {
        "schema_version": VISION_TRACE_SCHEMA_VERSION,
        "updated_at": time.time(),
        "entries": history[-VISION_TRACE_HISTORY_LIMIT:],
    }
    atomic_write_json(target, payload, ensure_ascii=False, indent=2)
    return target


def write_vision_trace_if_changed(
    event_payload: Mapping[str, Any],
    path: str | Path | None = None,
    *,
    history_path: str | Path | None = None,
) -> Path | None:
    """状态变化写历史；分数抖动仅按 1Hz 刷新最新 trace。"""

    signature = _vision_trace_signature(event_payload)
    target = Path(path) if path is not None else OVERLAY_VISION_TRACE_FILE
    signature_key = str(target.resolve())
    now = time.monotonic()
    state_changed = signature != _LAST_VISION_TRACE_SIGNATURES.get(signature_key)
    if (
        not state_changed
        and target.exists()
        and now - _LAST_VISION_TRACE_WRITES.get(signature_key, 0.0) < VISION_TRACE_REFRESH_SECONDS
    ):
        return None
    atomic_write_json(target, build_vision_trace_payload(event_payload), ensure_ascii=False, indent=2)
    if state_changed:
        _append_vision_trace_history(
            event_payload,
            Path(history_path) if history_path is not None else _vision_trace_history_path(target),
        )
    _LAST_VISION_TRACE_SIGNATURES[signature_key] = signature
    _LAST_VISION_TRACE_WRITES[signature_key] = now
    return target



__all__ = [name for name in globals() if not name.startswith("__")]

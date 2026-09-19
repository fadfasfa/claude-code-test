"""选择区域诊断缓存：只消费现有捕获，独立于识别 epoch 与人工样本库。"""
from __future__ import annotations

import math
import time
from copy import deepcopy
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import uuid4

from PIL import Image, ImageFilter, ImageStat

from .selection_capture_persistence import (
    CACHE_BYTE_LIMIT as CACHE_BYTE_LIMIT,
    CACHE_GROUP_LIMIT as CACHE_GROUP_LIMIT,
    CACHE_OWNER as CACHE_OWNER,
    persist_selection_capture as persist_selection_capture,
    selection_cache_status as selection_cache_status,
)
from .sidecar_common import LayoutTransform, apply_transform
from .sidecar_scene_geometry import resolve_roi_preset
from hextech.modules.vision.layout import pick_card_panels, BUTTON_SEARCH_REGION
from hextech.modules.session.selection_diagnostics import selection_cache_root as selection_cache_root


CACHE_INTERVAL = .25
CACHE_SECONDS = 2.0
MAX_COALESCED_FRAMES = 12
SLOW_CONFIRMATION_SECONDS = .9

@dataclass(frozen=True)
class SelectionFrame:
    image: Any  # RoiImage; imported by collector without a module import cycle.
    metadata: dict[str, Any]
    sampled_at: float
    clarity: float


@dataclass(frozen=True)
class SelectionCaptureDraft:
    diagnostic_id: str
    frames: tuple[SelectionFrame, ...]
    first_id: int
    clear_id: int
    last_id: int
    terminal: dict[str, Any]
    manual: bool = False
    retention_floor: str = ""

    @property
    def slot_key(self) -> str:
        return self.diagnostic_id

    @property
    def fingerprint(self) -> str:
        return self.diagnostic_id

    @property
    def retention_class(self) -> str:
        classified = _retention_class(self)
        floor = self.retention_floor if self.retention_floor in {"weak", "success", "anomaly", "manual"} else "weak"
        return max((classified, floor), key=_retention_priority)

    @property
    def priority(self) -> int:
        return _retention_priority(self.retention_class)


def _retention_priority(retention_class: str) -> int:
    return {"manual": 400, "anomaly": 300, "success": 200, "weak": 100}[retention_class]


def _slots(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            key: deepcopy(slot[key])
            for key in (
                "slot", "state", "temporal_state", "augment_id", "name",
                "rejection_reason", "slot_generation",
            )
            if key in slot
        }
        for slot in event.get("slots", [])[:3]
        if isinstance(slot, Mapping)
    ]


def _event_generations(event: Mapping[str, Any]) -> tuple[int, ...]:
    return tuple(
        int(slot.get("slot_generation"))
        for slot in event.get("slots", [])[:3]
        if isinstance(slot, Mapping) and isinstance(slot.get("slot_generation"), int)
        and not isinstance(slot.get("slot_generation"), bool) and int(slot.get("slot_generation")) > 0
    )


def _trigger_reasons(source: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    scores = source.get("panel_scores") if isinstance(source.get("panel_scores"), (list, tuple)) else []
    if sum(isinstance(score, (int, float)) and not isinstance(score, bool) and score >= .35
           for score in scores[:3]) >= 2:
        reasons.append("panel_structure")
    if source.get("selection_button_present") is True:
        reasons.append("selection_button")
    if source.get("scene_present") is True:
        reasons.append("scene_present")
    if source.get("scene_state") in {"candidate", "active"}:
        reasons.append("scene_admitted")
    if source.get("scene_kind") == "body_shard":
        reasons.append("body_shard")
    if source.get("reason") == "scene_type_conflict":
        reasons.append("scene_type_conflict")
    return reasons


def _raw_scene_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    timing = event.get("timing") if isinstance(event.get("timing"), Mapping) else {}
    scores = source.get("panel_scores") if isinstance(source.get("panel_scores"), (list, tuple)) else []
    return {
        "frame_id": source.get("frame_id"),
        "captured_at": timing.get("captured_at"),
        "trigger_reasons": _trigger_reasons(source),
        "scene_state": source.get("scene_state"),
        "scene_kind": source.get("scene_kind"),
        "scene_present": source.get("scene_present"),
        "panel_scores": [float(score) for score in scores[:3]
                         if isinstance(score, (int, float)) and not isinstance(score, bool)],
        "button_evidence": {
            "present": source.get("selection_button_present"),
            "box": deepcopy(source.get("button_box")) if isinstance(source.get("button_box"), (list, tuple)) else [],
        },
        "reason": source.get("reason"),
        "blocking_modal": source.get("blocking_modal"),
        "card_residue": source.get("card_residue"),
        "slots": _slots(event),
    }


def _rejection_stage(source: Mapping[str, Any], slots: list[dict[str, Any]]) -> str:
    explicit = source.get("rejection_stage") or source.get("reject_stage")
    if explicit:
        return str(explicit)
    if any(slot.get("rejection_reason") for slot in slots):
        return "slot_admission"
    if source.get("scene_state") in {"absent", "blocked"} or source.get("reason") in {
        "scene_type_conflict", "blocking_modal_present", "body_shard_only",
    }:
        return "scene_admission"
    return ""


def _final_classification(event: Mapping[str, Any], *, full_ready_elapsed: float | None = None,
                          generation_binding: str = "unavailable") -> dict[str, Any]:
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    timing = event.get("timing") if isinstance(event.get("timing"), Mapping) else {}
    slots = _slots(event)
    result = {
        "frame_id": source.get("frame_id"),
        "captured_at": timing.get("captured_at"),
        "scene_state": source.get("scene_state"),
        "scene_kind": source.get("scene_kind"),
        "scene_present": source.get("scene_present"),
        "reason": source.get("reason"),
        "rejection_stage": _rejection_stage(source, slots),
        "generation_binding": generation_binding,
        "slots": slots,
        "recognition_completed_at": timing.get("recognition_completed_at"),
    }
    if full_ready_elapsed is not None:
        result["full_ready_elapsed_ms"] = round(full_ready_elapsed * 1000.0, 3)
    return result


def _metadata(event: Mapping[str, Any]) -> dict[str, Any]:
    source = event.get("source") or {}
    timing = event.get("timing") or {}
    fields = ("session_id", "game_instance_id", "selection_epoch", "frame_id", "scene_state",
              "scene_kind", "scene_present", "selection_button_present", "reason", "build_id",
              "sidecar_instance_id", "layout_id", "capture_size", "button_box")
    result = {key: source[key] for key in fields if key in source}
    result["captured_at"] = timing.get("captured_at")
    result["slots"] = _slots(event)
    return result


def _suspected(source: Mapping[str, Any]) -> bool:
    # Raw scene evidence is consumed before tracker admission, including conflict/zero epoch.
    scores = source.get("panel_scores") or []
    structural = sum(isinstance(score, (int, float)) and score >= .35 for score in scores[:3]) >= 2
    return bool(structural or source.get("selection_button_present") or source.get("scene_present")
                or source.get("scene_state") in {"candidate", "active"}
                or source.get("scene_kind") == "body_shard"
                or source.get("reason") == "scene_type_conflict")


def _allows_periodic_snapshot(metadata: Mapping[str, Any]) -> bool:
    raw = metadata.get("raw_scene_evidence") if isinstance(metadata.get("raw_scene_evidence"), Mapping) else {}
    reasons = set(raw.get("trigger_reasons") or [])
    final = metadata.get("final_classification") if isinstance(metadata.get("final_classification"), Mapping) else {}
    return bool(reasons & {"scene_present", "scene_admitted", "body_shard"}
                or any(slot.get("state") == "ready" for slot in final.get("slots", [])
                       if isinstance(slot, Mapping)))


def _retention_class(draft: SelectionCaptureDraft) -> str:
    return classify_selection_evidence([frame.metadata for frame in draft.frames], draft.terminal, manual=draft.manual)


def classify_selection_evidence(frames: list[Mapping[str, Any]], terminal: Mapping[str, Any],
                                *, manual: bool = False) -> str:
    """A normal reroll boundary is evidence, not a recognition failure by itself."""
    if manual:
        return "manual"
    terminal_reason = str(terminal.get("reason") or terminal.get("capture_reason") or "")
    anomalous_reasons = {
        "scene_type_conflict", "evidence_starved",
        "reroll_unconfirmed", "slow_confirmation", "capture_binding_changed",
    }
    success = terminal_reason in {"selection_completed", "reroll_completed"}
    for frame in frames:
        raw = frame.get("raw_scene_evidence") if isinstance(frame.get("raw_scene_evidence"), Mapping) else {}
        final = frame.get("final_classification") if isinstance(frame.get("final_classification"), Mapping) else {}
        final_reason = str(final.get("reason") or "")
        if final.get("generation_binding") == "mismatch":
            return "anomaly"
        if terminal_reason in anomalous_reasons or final_reason in anomalous_reasons:
            return "anomaly"
        final_slots = [slot for slot in final.get("slots", []) if isinstance(slot, Mapping)]
        final_rejected_scene = final.get("scene_state") in {"absent", "blocked"}
        raw_scene_evidence = (
            (raw.get("button_evidence") or {}).get("present") is True
            or raw.get("scene_present") is True
            or raw.get("scene_state") in {"candidate", "active"}
        )
        false_ready = (raw.get("scene_state") in {"absent", "blocked"}
                       and any(slot.get("state") == "ready" for slot in final_slots))
        if raw_scene_evidence and final_rejected_scene or false_ready:
            return "anomaly"
        elapsed = final.get("full_ready_elapsed_ms")
        if isinstance(elapsed, (int, float)) and elapsed >= SLOW_CONFIRMATION_SECONDS * 1000:
            return "anomaly"
        success = success or any(slot.get("state") == "ready" for slot in final_slots)
    # Pending rerolls may later confirm normally. Their first provisional snapshot
    # must not latch an irreversible anomaly floor before any failure is observed.
    return "success" if success else "weak"


def _frame_anomaly_category(frame: SelectionFrame) -> str:
    raw = frame.metadata.get("raw_scene_evidence") if isinstance(
        frame.metadata.get("raw_scene_evidence"), Mapping) else {}
    final = frame.metadata.get("final_classification") if isinstance(
        frame.metadata.get("final_classification"), Mapping) else {}
    reason = str(final.get("reason") or "")
    if final.get("generation_binding") == "mismatch":
        return "generation_binding_mismatch"
    if reason in {
        "scene_type_conflict", "evidence_starved", "slot_generation_changed", "reroll_completed",
        "reroll_unconfirmed", "capture_binding_changed", "blocking_modal_present", "body_shard_only",
    }:
        return reason
    final_slots = [slot for slot in final.get("slots", []) if isinstance(slot, Mapping)]
    if raw.get("scene_state") in {"absent", "blocked"} and any(
        slot.get("state") == "ready" for slot in final_slots
    ):
        return "false_ready"
    rejected_scene_evidence = (
        (raw.get("button_evidence") or {}).get("present") is True
        or raw.get("scene_present") is True
    )
    if rejected_scene_evidence and final.get("scene_state") in {"absent", "blocked"}:
        return "scene_evidence_rejected"
    elapsed = final.get("full_ready_elapsed_ms")
    if isinstance(elapsed, (int, float)) and elapsed >= SLOW_CONFIRMATION_SECONDS * 1000:
        return "slow_confirmation"
    return ""


def _frame_state_signature(frame: SelectionFrame) -> tuple[Any, ...]:
    final = frame.metadata.get("final_classification") if isinstance(
        frame.metadata.get("final_classification"), Mapping) else {}
    slots = final.get("slots") if isinstance(final.get("slots"), list) else []
    return (
        str(final.get("scene_state") or ""),
        tuple(str(slot.get("state") or "") for slot in slots if isinstance(slot, Mapping)),
        _slot_generation_signature(frame),
        _frame_anomaly_category(frame),
    )


def _frame_identity(frame: SelectionFrame) -> tuple[Any, ...]:
    metadata = frame.metadata
    return (
        metadata.get("game_instance_id") or metadata.get("session_id"),
        metadata.get("frame_id"), metadata.get("captured_at"), frame.sampled_at,
    )


def _slot_generation_signature(frame: SelectionFrame) -> tuple[int, ...]:
    final = frame.metadata.get("final_classification") if isinstance(
        frame.metadata.get("final_classification"), Mapping) else {}
    slots = final.get("slots") if isinstance(final.get("slots"), list) else frame.metadata.get("slots")
    if not isinstance(slots, list):
        return ()
    result = tuple(
        int(slot.get("slot_generation"))
        for slot in slots
        if isinstance(slot, Mapping) and isinstance(slot.get("slot_generation"), int)
        and not isinstance(slot.get("slot_generation"), bool) and int(slot.get("slot_generation")) > 0
    )
    return result if result else ()


def _bounded_frames(frames: tuple[SelectionFrame, ...] | list[SelectionFrame]) -> list[SelectionFrame]:
    merged = {_frame_identity(frame): frame for frame in frames}
    ordered = sorted(merged.values(), key=lambda frame: frame.sampled_at)
    if len(ordered) <= MAX_COALESCED_FRAMES:
        return ordered
    first = ordered[0]
    last = ordered[-1]
    clear = max(ordered, key=lambda frame: frame.clarity)
    selected = {_frame_identity(frame) for frame in (first, clear, last)}
    anomaly_representatives: dict[str, SelectionFrame] = {}
    state_transitions: dict[tuple[tuple[Any, ...], tuple[Any, ...]], SelectionFrame] = {}
    previous_state: tuple[Any, ...] | None = None
    for frame in ordered:
        category = _frame_anomaly_category(frame)
        if category and category not in anomaly_representatives:
            anomaly_representatives[category] = frame
        state = _frame_state_signature(frame)
        if previous_state is not None and state != previous_state:
            state_transitions.setdefault((previous_state, state), frame)
        previous_state = state
    for frame in anomaly_representatives.values():
        if len(selected) >= MAX_COALESCED_FRAMES:
            break
        selected.add(_frame_identity(frame))
    generation_transitions: list[SelectionFrame] = []
    previous_generation: tuple[int, ...] = ()
    for frame in ordered:
        generation = _slot_generation_signature(frame)
        if previous_generation and generation and generation != previous_generation:
            generation_transitions.append(frame)
        if generation:
            previous_generation = generation
    for frame in reversed(generation_transitions):
        if len(selected) >= MAX_COALESCED_FRAMES:
            break
        selected.add(_frame_identity(frame))
    for frame in state_transitions.values():
        if len(selected) >= MAX_COALESCED_FRAMES:
            break
        selected.add(_frame_identity(frame))
    for frame in reversed(ordered):
        if len(selected) >= MAX_COALESCED_FRAMES:
            break
        selected.add(_frame_identity(frame))
    return [frame for frame in ordered if _frame_identity(frame) in selected]


def coalesce_selection_drafts(previous: SelectionCaptureDraft, latest: SelectionCaptureDraft) -> SelectionCaptureDraft:
    """Keep the first/transition evidence while replacing the queued latest view of one group."""
    if previous.diagnostic_id != latest.diagnostic_id:
        raise ValueError("selection_cache_coalesce_identity_mismatch")
    merged: dict[tuple[Any, ...], SelectionFrame] = {_frame_identity(frame): frame for frame in previous.frames}
    merged.update({_frame_identity(frame): frame for frame in latest.frames})
    ordered = _bounded_frames(list(merged.values()))
    first = ordered[0]
    last = ordered[-1]
    clear = max(ordered, key=lambda frame: frame.clarity)
    return SelectionCaptureDraft(
        diagnostic_id=latest.diagnostic_id,
        frames=tuple(ordered),
        first_id=ordered.index(first),
        clear_id=ordered.index(clear),
        last_id=ordered.index(last),
        terminal=deepcopy(latest.terminal),
        manual=latest.manual or previous.manual,
        retention_floor=max((previous.retention_class, latest.retention_class), key=_retention_priority),
    )


def _selection_crop(frame: Image.Image, source: Mapping[str, Any]):
    preset = resolve_roi_preset(*frame.size, preset=str(source.get("preset") or "auto"))
    raw = source.get("layout_transform") or {}
    transform = LayoutTransform(dx_ratio=float(raw.get("dx_ratio") or 0),
                                dy_ratio=float(raw.get("dy_ratio") or 0),
                                scale=float(raw.get("scale") or 1))
    boxes = [apply_transform(box, frame.size, transform)
             for box in (*pick_card_panels(frame.size), *preset.name_slots, *preset.slots)]
    boxes.append(apply_transform(BUTTON_SEARCH_REGION, frame.size, LayoutTransform()))
    box = (max(0, min(b[0] for b in boxes)), max(0, min(b[1] for b in boxes)),
           min(frame.width, max(b[2] for b in boxes)), min(frame.height, max(b[3] for b in boxes)))
    # Sparse client canvases contain black padding; never claim it as captured pixels.
    origin, size = frame.info.get("hextech_roi_origin"), frame.info.get("hextech_roi_size")
    if origin is not None and size is not None:
        box = (max(box[0], origin[0]), max(box[1], origin[1]),
               min(box[2], origin[0]+size[0]), min(box[3], origin[1]+size[1]))
    if box[0] >= box[2] or box[1] >= box[3] or box == (0, 0, *frame.size):
        raise ValueError("selection_roi_unavailable")
    return frame.crop(box).convert("RGB"), box


def _slot_roi_geometry(frame: Image.Image, source: Mapping[str, Any], selection_box: tuple) -> list[dict]:
    raw = source.get("layout_transform")
    valid_transform = (isinstance(raw, Mapping) and all(key in raw and isinstance(raw[key], (int, float))
        and not isinstance(raw[key], bool) and math.isfinite(raw[key]) for key in ("dx_ratio", "dy_ratio", "scale"))
        and raw["scale"] > 0)
    if not valid_transform:
        return [{"slot": index, **{kind: {"box": [], "valid": False, "reason": "layout_transform_unavailable"}
                                   for kind in ("name", "icon")}} for index in range(3)]
    preset = resolve_roi_preset(*frame.size, preset=str(source.get("preset") or "auto"))
    transform = LayoutTransform(**{key: raw[key] for key in ("dx_ratio", "dy_ratio", "scale")})
    slots = []
    for index in range(3):
        slot: dict[str, Any] = {"slot": index}
        for kind, definitions in (("name", preset.name_slots), ("icon", preset.slots)):
            box = apply_transform(definitions[index], frame.size, transform)
            left, top, right, bottom = definitions[index]
            cx, cy = (left+right)/2+transform.dx_ratio, (top+bottom)/2+transform.dy_ratio
            hw, hh = (right-left)*transform.scale/2, (bottom-top)*transform.scale/2
            unbounded = [round(value*size) for value, size in zip((cx-hw, cy-hh, cx+hw, cy+hh),
                                                                 (frame.width, frame.height)*2)]
            valid = (selection_box[0] <= box[0] < box[2] <= selection_box[2]
                     and selection_box[1] <= box[1] < box[3] <= selection_box[3] and list(box) == unbounded)
            slot[kind] = {"box": list(box), "requested_box": unbounded, "valid": valid}
            if not valid:
                slot[kind]["reason"] = "slot_roi_outside_capture"
        slots.append(slot)
    return slots


class SelectionCaptureBuffer:
    """4fps/2秒环 + 首帧/最清晰帧/最新帧；每个疑似选择独立诊断 id。"""

    def __init__(self, writer, *, clock=time.monotonic, pool_id: str = "", build_id: str = ""):
        self.writer, self.clock = writer, clock
        self.frames: deque[SelectionFrame] = deque(maxlen=8)
        self.recent_frames: deque[SelectionFrame] = deque(maxlen=8)
        self.first = self.clear = self.last = None
        self.diagnostic_id = ""
        self._last_sample = float("-inf")
        self._last_seen = 0.0
        self._binding = None
        self._latest_event: dict[str, Any] = {}
        self._recent: SelectionCaptureDraft | None = None
        self._last_snapshot = float("-inf")
        self._generations: tuple = ()
        self._group_origin = ""
        self._group_started_at: float | None = None
        self._full_ready_elapsed: float | None = None
        self._anomaly_landmarks: dict[str, SelectionFrame] = {}
        self._transition_landmarks: dict[tuple[tuple[Any, ...], tuple[Any, ...]], SelectionFrame] = {}
        self._last_frame_state: tuple[Any, ...] | None = None
        self.last_error = ""
        self.last_manual_id = ""
        self.pool_id, self.build_id = pool_id, build_id

    def capture(self, frame, raw_event) -> None:
        source = raw_event.get("source") or {}
        now = self.clock()
        suspected = _suspected(source)
        binding = (source.get("game_instance_id") or source.get("session_id"),
                   source.get("window_hwnd"), source.get("capture_size"))
        if self._binding is not None and binding != self._binding:
            if self.diagnostic_id:
                self.finish({"source": {"reason": "capture_binding_changed"}})
            self.recent_frames.clear()
        self._binding = binding
        if suspected:
            self._last_seen = now
        if suspected and not self.diagnostic_id:
            self.diagnostic_id = uuid4().hex
            self._group_started_at = now
        if now-self._last_sample < CACHE_INTERVAL:
            return
        try:
            image, box = _selection_crop(frame, source)
            from .failure_evidence import RoiImage
            gray = image.convert("L").resize((160, 90))
            clarity = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0])
            metadata = deepcopy({**_metadata(raw_event), "selection_box": list(box), "frame_size": list(frame.size),
                "coordinate_space": "physical_client", "pool_id": self.pool_id, "build_id": self.build_id,
                "layout_transform": source.get("layout_transform"), "slot_rois": _slot_roi_geometry(frame, source, box),
                "raw_scene_evidence": _raw_scene_evidence(raw_event), "final_classification": None})
            sample = SelectionFrame(RoiImage.from_image(image), metadata, now, clarity)
        except (ValueError, TypeError, OSError) as exc:
            self.last_error = str(exc)
            return
        self._last_sample = now
        while self.recent_frames and now-self.recent_frames[0].sampled_at >= CACHE_SECONDS:
            self.recent_frames.popleft()
        self.recent_frames.append(sample)
        if not self.diagnostic_id:
            return
        while self.frames and now-self.frames[0].sampled_at >= CACHE_SECONDS:
            self.frames.popleft()
        self.frames.append(sample)
        self.first = self.first or sample
        if self.clear is None or suspected and sample.clarity > self.clear.clarity:
            self.clear = sample
        self.last = sample

    def observe_result(self, event) -> None:
        if not self.diagnostic_id:
            return
        self._latest_event = _metadata(event)
        captured = self.last.metadata if self.last is not None else {}
        frame_id = captured.get("frame_id")
        game_id = captured.get("game_instance_id") or captured.get("session_id")
        captured_at = captured.get("captured_at")
        generations = _event_generations(event)
        captured_generations = tuple(
            int(slot.get("slot_generation"))
            for slot in captured.get("slots", [])
            if isinstance(slot, Mapping) and isinstance(slot.get("slot_generation"), int)
            and not isinstance(slot.get("slot_generation"), bool) and int(slot.get("slot_generation")) > 0
        )
        same_frame = (isinstance(frame_id, int) and not isinstance(frame_id, bool) and frame_id > 0
                      and frame_id == self._latest_event.get("frame_id") and bool(game_id)
                      and game_id == (self._latest_event.get("game_instance_id") or self._latest_event.get("session_id"))
                      and isinstance(captured_at, (int, float))
                      and captured_at == self._latest_event.get("captured_at"))
        if same_frame:
            # Final tracker classification is adjacent to, never merged into, the pre-admission observation.
            elapsed = None
            generation_matches = not captured_generations or captured_generations == generations
            generation_binding = "unavailable" if not captured_generations else (
                "matched" if captured_generations == generations else "mismatch"
            )
            rendered_slots = _slots(event)
            full_ready = len(rendered_slots) == 3 and all(slot.get("state") == "ready" for slot in rendered_slots)
            if self.first is not None and len(generations) == 3 and generation_matches and full_ready:
                if self._full_ready_elapsed is None:
                    started_at = self._group_started_at if self._group_started_at is not None else self.first.sampled_at
                    self._full_ready_elapsed = max(0.0, self.clock() - started_at)
                elapsed = self._full_ready_elapsed
            captured["final_classification"] = _final_classification(
                event, full_ready_elapsed=elapsed, generation_binding=generation_binding,
            )
            if self.last is not None:
                self._remember_landmark(self.last)
        source = event.get("source") or {}
        reason = str(source.get("reason") or "")
        changed = any(isinstance(before, int) and isinstance(after, int) and before > 0 and after > before
                      for before, after in zip(self._generations, generations))
        if changed and self.last is not None:
            # Close a diagnostic group at a real observed reroll boundary. Later READY snapshots
            # must not overwrite the transition's last frame or retroactively invent an epoch.
            generation_matches = not captured_generations or captured_generations == generations
            current = self.last if same_frame and len(generations) == 3 and generation_matches else None
            self.finish({"source": {**source, "reason": "slot_generation_changed"}, "slots": event.get("slots", [])})
            self.diagnostic_id = uuid4().hex
            self._group_origin = "slot_generation_changed"
            self._group_started_at = self.clock()
            if current is not None:
                self.frames.append(current)
                self.first = self.clear = self.last = current
                self._last_sample = current.sampled_at
                final = current.metadata.get("final_classification") if isinstance(
                    current.metadata.get("final_classification"), Mapping) else {}
                if any(slot.get("state") == "ready" for slot in final.get("slots", [])
                       if isinstance(slot, Mapping)):
                    rendered_slots = [slot for slot in final.get("slots", []) if isinstance(slot, Mapping)]
                    if len(rendered_slots) == 3 and all(slot.get("state") == "ready" for slot in rendered_slots):
                        self._full_ready_elapsed = 0.0
                        final["full_ready_elapsed_ms"] = 0.0
                self._remember_landmark(current)
        self._generations = generations
        if reason in {"selection_completed", "scene_loss_confirmed", "gameflow_ended", "reroll_completed"}:
            self.finish(event)
        elif self.clock()-self._last_seen >= CACHE_SECONDS:
            self.finish({"source": {**source, "reason": reason or "suspected_selection_ended"}})
        elif self._last_snapshot == float("-inf") or (
            self.clock()-self._last_snapshot >= CACHE_SECONDS
            and self.last is not None
            and _allows_periodic_snapshot(self.last.metadata)
        ):
            # A weak-only trigger gets one recoverable snapshot, not a perpetual two-second rewrite.
            submitted = self._submit(manual=False, terminal=self._terminal(
                {**self._latest_event, "capture_reason": "initial_snapshot" if self._last_snapshot == float("-inf")
                 else "rolling_snapshot"}))
            if submitted:
                self._last_snapshot = self.clock()

    def _terminal(self, terminal: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(terminal))
        if self._group_origin:
            result["group_origin"] = self._group_origin
        return result

    def _remember_landmark(self, frame: SelectionFrame) -> None:
        category = _frame_anomaly_category(frame)
        if (category and category not in self._anomaly_landmarks
                and len(self._anomaly_landmarks) < MAX_COALESCED_FRAMES):
            self._anomaly_landmarks[category] = frame
        state = _frame_state_signature(frame)
        if self._last_frame_state is not None and state != self._last_frame_state:
            transition = (self._last_frame_state, state)
            if (transition not in self._transition_landmarks
                    and len(self._transition_landmarks) < MAX_COALESCED_FRAMES):
                self._transition_landmarks[transition] = frame
        self._last_frame_state = state

    def _draft(self, *, manual: bool, terminal: dict) -> SelectionCaptureDraft | None:
        if self.first is None or self.last is None or self.clear is None:
            return None
        frames = _bounded_frames((
            self.first, self.clear, *self._anomaly_landmarks.values(),
            *self._transition_landmarks.values(), *self.frames, self.last,
        ))
        return SelectionCaptureDraft(uuid4().hex if manual else self.diagnostic_id, tuple(frames),
                                     frames.index(self.first), frames.index(self.clear), frames.index(self.last),
                                     self._terminal(terminal), manual)

    def _submit(self, *, manual: bool, terminal: dict) -> bool:
        draft = self._draft(manual=manual, terminal=terminal)
        if draft is None:
            return False
        # Freeze metadata: the recognition thread can enrich a frame after queue submission.
        frozen = SelectionCaptureDraft(draft.diagnostic_id,
            tuple(SelectionFrame(f.image, deepcopy(f.metadata), f.sampled_at, f.clarity) for f in draft.frames),
            draft.first_id, draft.clear_id, draft.last_id, deepcopy(draft.terminal), draft.manual,
            draft.retention_floor)
        self._recent = frozen
        if manual:
            self.last_manual_id = frozen.diagnostic_id
        return self.writer.submit(frozen)

    def finish(self, event) -> None:
        self._submit(manual=False, terminal=_metadata(event))
        self.frames.clear()
        self.first = self.clear = self.last = None
        self.diagnostic_id = ""
        self._generations = ()
        self._group_origin = ""
        self._group_started_at = None
        self._full_ready_elapsed = None
        self._anomaly_landmarks.clear()
        self._transition_landmarks.clear()
        self._last_frame_state = None
        self._last_snapshot = float("-inf")

    def save_recent(self) -> bool:
        if self.diagnostic_id:
            ok = self._submit(manual=True, terminal={**self._latest_event, "capture_reason": "manual_save"})
        elif self.recent_frames:
            recent = tuple(self.recent_frames)
            self.last_manual_id = uuid4().hex
            draft = SelectionCaptureDraft(self.last_manual_id, recent, 0,
                max(range(len(recent)), key=lambda i: recent[i].clarity), len(recent)-1,
                {"capture_reason": "manual_save_without_scene_admission"}, True)
            ok = self.writer.submit(draft)
        elif self._recent is not None:
            from dataclasses import replace
            self.last_manual_id = uuid4().hex
            ok = self.writer.submit(replace(self._recent, diagnostic_id=self.last_manual_id, manual=True))
        else:
            ok = False
        self.last_error = "" if ok else "recent_selection_buffer_unavailable_or_queue_full"
        return ok

    def status(self):
        return {"diagnostic_id": self.diagnostic_id, "buffer_frames": len(self.frames),
                "recent_available": bool(self.first or self.recent_frames or self._recent), "last_error": self.last_error,
                "recent_frames": len(self.recent_frames),
                "last_manual_id": self.last_manual_id}

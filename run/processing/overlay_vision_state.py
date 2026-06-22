"""Overlay V2 场景与逐槽状态机。

状态机只处理已经完成单帧视觉分析的字典，不访问窗口、截图或磁盘。场景和槽位
分别稳定，避免任一槽抖动时让整个 overlay 反复显隐。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from processing.overlay_event_channel import build_overlay_event
from processing.overlay_vision_matcher import SlotCandidate, candidate_from_slot, unknown_slot


SCENE_ENTER_FRAMES = 2
SCENE_EXIT_FRAMES = 2
SLOT_COUNT = 3


@dataclass
class _SlotTrack:
    candidate_identity: str = ""
    candidate_frames: int = 0
    stable_slot: dict[str, Any] | None = None
    weak_miss_frames: int = 0

    def clear(self) -> None:
        self.candidate_identity = ""
        self.candidate_frames = 0
        self.stable_slot = None
        self.weak_miss_frames = 0


@dataclass
class SelectionTracker:
    """维护单个 sidecar 进程内的选择 epoch。"""

    scene_frames: int = 0
    absent_frames: int = 0
    scene_active: bool = False
    epoch: int = 0
    scene_enter_frames: int = SCENE_ENTER_FRAMES
    scene_exit_frames: int = SCENE_EXIT_FRAMES
    body_shard_latched: bool = False
    body_shard_absent_frames: int = 0
    slots: list[_SlotTrack] = field(default_factory=lambda: [_SlotTrack() for _ in range(SLOT_COUNT)])

    def reset(self) -> None:
        self.scene_frames = 0
        self.absent_frames = 0
        self.scene_active = False
        self.body_shard_latched = False
        self.body_shard_absent_frames = 0
        for slot in self.slots:
            slot.clear()

    def _body_shard_event(self, source: Mapping[str, Any]) -> dict[str, Any]:
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="body_shard", active=False)
        event["source"].update(
            {
                "reason": "body_shard_only",
                "gate_state": "blocked",
                "scene_state": "blocked",
                "scene_kind": "body_shard",
                "scene_score": float(source.get("scene_score") or 0.0),
                "selection_epoch": self.epoch,
                "selection_window_active": False,
                "scoreboard_key_down": False,
                "ready_slots": 0,
                "content_ready": False,
                "slot_states": ["detecting"] * SLOT_COUNT,
                "stable_frames": 0,
                "body_shard_scores": list(source.get("body_shard_scores"))
                if isinstance(source.get("body_shard_scores"), list)
                else [],
                "body_shard_latched": True,
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "card_residue": bool(source.get("card_residue")),
                "hover_occluded": False,
            }
        )
        return event

    def block(self, reason: str, *, scoreboard_key_down: bool = False) -> dict[str, Any]:
        self.reset()
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update(
            {
                "reason": reason,
                "gate_state": "blocked",
                "scene_state": "blocked",
                "scene_score": 0.0,
                "selection_epoch": self.epoch,
                "selection_window_active": False,
                "scoreboard_key_down": bool(scoreboard_key_down),
                "ready_slots": 0,
                "content_ready": False,
                "slot_states": ["detecting"] * SLOT_COUNT,
                "stable_frames": 0,
            }
        )
        return event

    def _update_slot(self, index: int, raw_slot: Mapping[str, Any]) -> dict[str, Any]:
        track = self.slots[index]
        candidate = candidate_from_slot(raw_slot)
        if candidate is None:
            track.candidate_identity = ""
            track.candidate_frames = 0
            track.weak_miss_frames += 1
            if track.weak_miss_frames >= 2:
                track.stable_slot = None
            return dict(track.stable_slot) if track.stable_slot is not None else unknown_slot(index)

        identity = candidate.identity
        if track.stable_slot is not None:
            stable_identity = str(track.stable_slot.get("augment_id") or track.stable_slot.get("name") or "")
            if identity != stable_identity and candidate.required_frames <= 2:
                # 强新候选通常表示单槽重随；旧结果立即撤下，但新结果仍需稳定确认。
                track.stable_slot = None
        track.weak_miss_frames = 0
        if identity == track.candidate_identity:
            track.candidate_frames += 1
        else:
            track.candidate_identity = identity
            track.candidate_frames = 1
        if track.candidate_frames >= candidate.required_frames:
            track.stable_slot = candidate.ready_slot()
        return dict(track.stable_slot) if track.stable_slot is not None else unknown_slot(index)

    def _residue_event(self, source: Mapping[str, Any], *, hover_occluded: bool) -> dict[str, Any]:
        rendered_slots = [
            dict(track.stable_slot) if track.stable_slot is not None else unknown_slot(index)
            for index, track in enumerate(self.slots)
        ]
        ready_slots = sum(slot.get("state") == "ready" for slot in rendered_slots)
        event = build_overlay_event(
            rendered_slots,
            source_tag="vision-sidecar",
            selection_type="hextech",
            active=bool(self.scene_active and ready_slots >= 1),
        )
        event["source"].update(
            {
                "reason": "hover_occluded" if hover_occluded else "scene_residue_hold",
                "gate_state": "visible_partial" if ready_slots < SLOT_COUNT else "visible_ready",
                "scene_state": "active",
                "scene_kind": "hextech",
                "scene_score": float(source.get("scene_score") or 0.0),
                "layout_id": str(source.get("layout_id") or ""),
                "layout_transform": source.get("layout_transform")
                if isinstance(source.get("layout_transform"), Mapping)
                else {},
                "selection_epoch": self.epoch,
                "selection_window_active": True,
                "scoreboard_key_down": False,
                "ready_slots": ready_slots,
                "content_ready": ready_slots == SLOT_COUNT,
                "slot_states": [str(slot.get("state") or "detecting") for slot in rendered_slots],
                "stable_frames": self.scene_frames,
                "blocking_modal": False,
                "poll_mode": "high",
                "selection_button_present": bool(source.get("selection_button_present")),
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "card_residue": True,
                "hover_occluded": hover_occluded,
                "panel_scores": list(source.get("panel_scores"))
                if isinstance(source.get("panel_scores"), list)
                else [],
                "name_residue": list(source.get("name_residue"))
                if isinstance(source.get("name_residue"), list)
                else [],
                "body_shard_scores": list(source.get("body_shard_scores"))
                if isinstance(source.get("body_shard_scores"), list)
                else [],
                "body_shard_latched": False,
            }
        )
        return event

    def update(self, raw_event: Mapping[str, Any]) -> dict[str, Any]:
        source = raw_event.get("source") if isinstance(raw_event.get("source"), Mapping) else {}
        reason = str(source.get("reason") or "")
        if reason == "body_shard_only":
            if not self.body_shard_latched:
                self.epoch += 1
            self.scene_frames = 0
            self.absent_frames = 0
            self.scene_active = False
            self.body_shard_latched = True
            self.body_shard_absent_frames = 0
            for slot in self.slots:
                slot.clear()
            return self._body_shard_event(source)
        if reason in {"blocking_modal_present", "scoreboard_key_down"}:
            return self.block(reason, scoreboard_key_down=reason == "scoreboard_key_down")

        scene_present = bool(source.get("scene_present") or source.get("selection_window_active"))
        if self.body_shard_latched:
            if scene_present:
                self.body_shard_absent_frames = 0
                return self._body_shard_event(source)
            self.body_shard_absent_frames += 1
            if self.body_shard_absent_frames < max(1, int(self.scene_exit_frames)):
                return self._body_shard_event(source)
            self.reset()

        name_residue = source.get("name_residue") if isinstance(source.get("name_residue"), list) else []
        hover_occluded = bool(
            self.scene_active
            and not scene_present
            and source.get("cursor_over_cards")
            and source.get("card_residue")
        )
        scene_residue_hold = bool(
            self.scene_active
            and not scene_present
            and source.get("card_residue")
            and sum(bool(value) for value in name_residue[:SLOT_COUNT]) >= 2
        )
        if hover_occluded or scene_residue_hold:
            self.absent_frames = 0
            return self._residue_event(source, hover_occluded=hover_occluded)

        if scene_present:
            self.absent_frames = 0
            if self.scene_frames == 0 and not self.scene_active:
                self.epoch += 1
                for slot in self.slots:
                    slot.clear()
            self.scene_frames += 1
            if not self.scene_active and self.scene_frames >= max(1, int(self.scene_enter_frames)):
                self.scene_active = True
        else:
            self.scene_frames = 0
            self.absent_frames += 1
            if self.absent_frames >= max(1, int(self.scene_exit_frames)):
                self.reset()
            if not self.scene_active:
                event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
                event["source"].update(
                    {
                        "reason": reason or "selection_scene_not_detected",
                        "gate_state": "inactive",
                        "scene_state": "absent",
                        "scene_score": float(source.get("scene_score") or 0.0),
                        "layout_id": str(source.get("layout_id") or ""),
                        "selection_epoch": self.epoch,
                        "selection_window_active": False,
                        "scoreboard_key_down": False,
                        "ready_slots": 0,
                        "content_ready": False,
                        "slot_states": ["detecting"] * SLOT_COUNT,
                        "stable_frames": self.absent_frames,
                        "scene_kind": str(source.get("scene_kind") or "absent"),
                        "body_shard_scores": list(source.get("body_shard_scores"))
                        if isinstance(source.get("body_shard_scores"), list)
                        else [],
                        "body_shard_latched": False,
                        "cursor_over_cards": bool(source.get("cursor_over_cards")),
                        "card_residue": bool(source.get("card_residue")),
                        "name_residue": list(source.get("name_residue"))
                        if isinstance(source.get("name_residue"), list)
                        else [],
                        "hover_occluded": False,
                    }
                )
                return event

        raw_slots = raw_event.get("_raw_slots") if isinstance(raw_event.get("_raw_slots"), list) else []
        rendered_slots = [
            self._update_slot(index, raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {})
            for index in range(SLOT_COUNT)
        ]
        ready_slots = sum(slot.get("state") == "ready" for slot in rendered_slots)
        event = build_overlay_event(
            rendered_slots,
            source_tag="vision-sidecar",
            selection_type="hextech",
            active=bool(self.scene_active and ready_slots >= 1),
        )
        scene_state = "active" if self.scene_active else "candidate"
        event["source"].update(
            {
                "reason": "" if ready_slots else "slots_detecting",
                "gate_state": "visible_partial" if 0 < ready_slots < SLOT_COUNT else (
                    "visible_ready" if ready_slots == SLOT_COUNT else "detecting"
                ),
                "scene_state": scene_state,
                "scene_kind": str(source.get("scene_kind") or "hextech"),
                "scene_score": float(source.get("scene_score") or 0.0),
                "layout_id": str(source.get("layout_id") or ""),
                "layout_transform": source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {},
                "selection_epoch": self.epoch,
                "selection_window_active": bool(self.scene_active),
                "scoreboard_key_down": False,
                "ready_slots": ready_slots,
                "content_ready": ready_slots == SLOT_COUNT,
                "slot_states": [str(slot.get("state") or "detecting") for slot in rendered_slots],
                "stable_frames": self.scene_frames,
                "blocking_modal": False,
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "card_residue": bool(source.get("card_residue")),
                "hover_occluded": False,
                "body_shard_scores": list(source.get("body_shard_scores"))
                if isinstance(source.get("body_shard_scores"), list)
                else [],
                "body_shard_latched": False,
                "name_residue": list(source.get("name_residue"))
                if isinstance(source.get("name_residue"), list)
                else [],
                "poll_mode": "high",
                "generated_from_tracker_at": time.time(),
                "selection_button_present": bool(source.get("selection_button_present")),
                "button_blue_ratio": float(source.get("button_blue_ratio") or 0.0),
                "button_box": list(source.get("button_box")) if isinstance(source.get("button_box"), list) else [],
                "panel_scores": list(source.get("panel_scores")) if isinstance(source.get("panel_scores"), list) else [],
                "preset": str(source.get("preset") or ""),
                "capture_size": list(source.get("capture_size")) if isinstance(source.get("capture_size"), list) else [],
                "latency_ms": source.get("latency_ms"),
                "calibration": str(source.get("calibration") or "layout_v2"),
            }
        )
        event["_raw_slots"] = [dict(slot) for slot in raw_slots if isinstance(slot, Mapping)]
        event["_acceptance_rules"] = [str(slot.get("acceptance_rule") or "") for slot in rendered_slots]
        return event

"""Overlay V2 场景与逐槽状态机。

状态机只处理已经完成单帧视觉分析的字典，不访问窗口、截图或磁盘。场景和槽位
分别稳定，避免任一槽抖动时让整个 overlay 反复显隐。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from hextech.overlay.events import build_overlay_event
from hextech.overlay.vision.matcher import SlotCandidate, candidate_from_slot, unknown_slot


SCENE_ENTER_FRAMES = 2  # 场景连续出现 N 帧后判定为"进入"
SCENE_EXIT_FRAMES = 2   # 场景连续消失 N 帧后判定为"退出"
SLOT_COUNT = 3          # 海克斯三选一槽位数
RESIDUE_HOLD_FRAMES = 2  # 普通残影只短暂沿用，避免选择结束后长时间残留


@dataclass
class _SlotTrack:
    """单槽位跟踪器：维护候选连续性和稳定输出。"""

    candidate_identity: str = ""       # 当前帧候选标识（augment_id 或 name）
    candidate_frames: int = 0          # 同一候选连续出现帧数
    stable_slot: dict[str, Any] | None = None  # 已稳定确认的槽位输出
    weak_miss_frames: int = 0          # 候选丢失帧数（连续丢失 ≥ 2 帧则清空稳定输出）

    def clear(self) -> None:
        self.candidate_identity = ""
        self.candidate_frames = 0
        self.stable_slot = None
        self.weak_miss_frames = 0


@dataclass
class SelectionTracker:
    """维护单个 sidecar 进程内的选择 epoch。

    状态机分两层：
    - 场景层：scene_active 控制 overlay 显隐，有进入/退出帧数防抖
    - 槽位层：每个 _SlotTrack 独立累积候选帧数，互不干扰
    """

    scene_frames: int = 0              # 场景连续出现帧数
    absent_frames: int = 0             # 场景连续消失帧数
    scene_active: bool = False         # 场景已激活（≥ SCENE_ENTER_FRAMES）
    epoch: int = 0                     # 选择窗口编号，每次新窗口递增
    scene_enter_frames: int = SCENE_ENTER_FRAMES
    scene_exit_frames: int = SCENE_EXIT_FRAMES
    body_shard_latched: bool = False   # 锻体碎片场景锁定中
    body_shard_absent_frames: int = 0  # 锻体场景消失帧计数（退出防抖）
    residue_hold_frames: int = 0       # 非真实场景下沿用上一帧的连续帧数
    slots: list[_SlotTrack] = field(default_factory=lambda: [_SlotTrack() for _ in range(SLOT_COUNT)])

    def reset(self) -> None:
        self.scene_frames = 0
        self.absent_frames = 0
        self.scene_active = False
        self.body_shard_latched = False
        self.body_shard_absent_frames = 0
        self.residue_hold_frames = 0
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
        """逐槽处理单帧识别结果，累积候选帧数直到达到 required_frames 阈值。

        候选判定三态：
        - candidate 为 None（识别失败）→ 增加 weak_miss，连续 ≥2 帧清空稳定输出
        - candidate 与当前稳定输出不同 → 强候选（≤2 帧）立即撤旧等新确认；弱候选保留旧值并标记 stale_hold
        - candidate 与当前追踪一致 → 累加帧数，达到 required_frames 后锁定为稳定输出
        """
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
        stale_hold_identity = ""
        if track.stable_slot is not None:
            stable_identity = str(track.stable_slot.get("augment_id") or track.stable_slot.get("name") or "")
            if identity != stable_identity:
                if candidate.required_frames <= 2:
                    # 强新候选通常表示单槽重随；旧结果立即撤下，但新结果仍需稳定确认。
                    track.stable_slot = None
                else:
                    # 弱/慢候选更替期间保留旧稳定槽位，但把 hold 原因写进诊断。
                    stale_hold_identity = identity
        track.weak_miss_frames = 0
        if identity == track.candidate_identity:
            track.candidate_frames += 1
        else:
            track.candidate_identity = identity
            track.candidate_frames = 1
        if track.candidate_frames >= candidate.required_frames:
            track.stable_slot = candidate.ready_slot()
            stale_hold_identity = ""
        if track.stable_slot is None:
            return unknown_slot(index)
        rendered_slot = dict(track.stable_slot)
        if stale_hold_identity:
            base_rule = str(rendered_slot.get("acceptance_rule") or "").strip()
            rendered_slot["acceptance_rule"] = f"{base_rule}|stale_hold:{stale_hold_identity}" if base_rule else (
                f"stale_hold:{stale_hold_identity}"
            )
        return rendered_slot

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
            active=bool(self.scene_active and ready_slots == SLOT_COUNT),
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
        """处理一帧视觉事件，返回 overlay 就绪事件。

        状态转换：
        - body_shard_only → 锁定锻体模式，清空槽位，输出 body_shard 事件
        - blocking_modal_present / scoreboard_key_down → 重置并阻塞
        - body_shard_latched + 场景消失 → 防抖退出锻体模式
        - hover_occluded（鼠标遮挡）→ 只要仍能确认卡片残留就沿用上次槽位
        - scene_residue_hold（普通残留保持）→ 短暂沿用上次槽位后退出
        - 场景出现 → 累积帧数，达到阈值后 scene_active=True
        - 场景消失 → 累积 absent_frames，达到阈值后 reset
        """
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
            self.residue_hold_frames += 1
            if hover_occluded:
                self.absent_frames = 0
                return self._residue_event(source, hover_occluded=True)
            if self.residue_hold_frames <= max(1, int(RESIDUE_HOLD_FRAMES)):
                self.absent_frames = 0
                return self._residue_event(source, hover_occluded=False)
            return self.block("scene_residue_expired")

        if scene_present:
            self.residue_hold_frames = 0
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
            self.residue_hold_frames = 0
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
            active=bool(self.scene_active and ready_slots == SLOT_COUNT),
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

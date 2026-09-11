"""Overlay V2 场景与逐槽状态机。"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from hextech.modules.vision.events import build_overlay_event
from hextech.infrastructure.vision.matcher import unknown_slot
from hextech.infrastructure.vision.state_models import SlotTrack
from hextech.infrastructure.vision.scene_negative import SceneNegativeState, evaluate_scene_negative
SCENE_ENTER_FRAMES = 2  # 场景连续出现 N 帧后判定为"进入"
SCENE_EXIT_FRAMES = 2   # 场景连续消失 N 帧后判定为"退出"
SLOT_COUNT = 3          # 海克斯三选一槽位数
RESIDUE_HOLD_FRAMES = 2  # 普通残影只短暂沿用，避免选择结束后长时间残留
PARTIAL_SCENE_GRACE_SECONDS = 0.75
READY_SCENE_GRACE_SECONDS = 0.75
EMPTY_SCENE_GRACE_SECONDS = 0.75
_MATCHING_TIMING_KEYS = (
    "fingerprint_ms",
    "icon_projection_ms",
    "name_projection_ms",
    "recall_top_k_ms",
    "decision_ms",
    "total_ms",
    "scene_ms", "name_mask_ms", "evidence_fingerprint_ms", "icon_feature_ms", "name_feature_ms", "ocr_submit_ms",
)


def _matching_timing(source: Mapping[str, Any]) -> dict[str, float]:
    """事件重建时只传递固定耗时字段，避免 source 内部对象泄漏到诊断。"""

    raw = source.get("matching_timing") if isinstance(source.get("matching_timing"), Mapping) else {}
    return {
        key: round(float(raw[key]), 3)
        for key in _MATCHING_TIMING_KEYS
        if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool)
    }


def _attach_matching_timing(event: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    event_source = event.get("source")
    if isinstance(event_source, dict):
        event_source["matching_timing"] = _matching_timing(source)
        if source.get("hold_evidence_state") in {"collecting", "blocked"}:
            event_source["hold_evidence_state"] = source["hold_evidence_state"]
    return event


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
    selection_revision: int = 0        # 同一窗口内刷新卡片时递增，防止新卡沿用旧统计
    scene_enter_frames: int = SCENE_ENTER_FRAMES
    scene_exit_frames: int = SCENE_EXIT_FRAMES
    body_shard_latched: bool = False   # 锻体碎片场景锁定中
    body_shard_absent_frames: int = 0  # 锻体场景消失帧计数（退出防抖）
    residue_hold_frames: int = 0       # 非真实场景下沿用上一帧的连续帧数
    selection_click_armed: bool = False  # 场景内卡片点击后，场景消失即确认为本轮选择完成
    scene_lost_at: float = 0.0          # 有稳定槽后场景门丢失的真实观察时间
    _revision_changed: bool = False
    slots: list[SlotTrack] = field(default_factory=lambda: [SlotTrack() for _ in range(SLOT_COUNT)])
    _scene_negative_state: SceneNegativeState = field(default_factory=SceneNegativeState)

    def reset(self) -> None:
        self._scene_negative_state = SceneNegativeState()
        self._phase_version = getattr(self, "_phase_version", 0) + 1
        self._frame_ticket = None
        self.scene_frames = 0
        self.absent_frames = 0
        self.scene_active = False
        self.body_shard_latched = False
        self.body_shard_absent_frames = 0
        self.residue_hold_frames = 0
        self.selection_click_armed = False
        self.scene_lost_at = 0.0
        self.selection_revision = 0
        self._revision_changed = False
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
                "selection_revision": self.selection_revision,
                "selection_window_active": False,
                "scene_present": False,
                "selection_button_present": bool(source.get("selection_button_present")),
                "selection_click": bool(source.get("selection_click")),
                "transition_source": str(source.get("transition_source") or ""),
                "transition_kind": str(source.get("transition_kind") or ""),
                "transition_slot": source.get("transition_slot"),
                "scoreboard_key_down": False,
                "ready_slots": 0,
                "content_ready": False,
                "slot_states": ["detecting"] * SLOT_COUNT,
                "stable_frames": 0,
                "body_shard_scores": list(source.get("body_shard_scores"))
                if isinstance(source.get("body_shard_scores"), list)
                else [],
                "body_shard_latched": True,
                "matching_timing": _matching_timing(source),
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "cursor_over_slots": list(source.get("cursor_over_slots"))
                if isinstance(source.get("cursor_over_slots"), list)
                else [],
                "card_residue": bool(source.get("card_residue")),
                "name_residue": list(source.get("name_residue"))
                if isinstance(source.get("name_residue"), list)
                else [],
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
                "selection_revision": self.selection_revision,
                "selection_window_active": False,
                "scoreboard_key_down": bool(scoreboard_key_down),
                "ready_slots": 0,
                "content_ready": False,
                "slot_states": ["detecting"] * SLOT_COUNT,
                "stable_frames": 0,
            }
        )
        return event

    def _selection_completed_event(
        self,
        source: Mapping[str, Any],
        *,
        reason: str = "selection_completed",
    ) -> dict[str, Any]:
        """结束当前 epoch，避免完成选择后继续把空槽解释为 detecting。"""

        completed_epoch = self.epoch
        completed_revision = self.selection_revision
        completed_fragment = self.body_shard_latched
        self.reset()
        completed_type = "body_shard" if completed_fragment else "hextech"
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type=completed_type, active=False)
        event["source"].update(
            {
                "reason": reason,
                "gate_state": "inactive",
                "scene_state": "absent",
                "scene_kind": completed_type,
                "scene_score": float(source.get("scene_score") or 0.0),
                "selection_epoch": completed_epoch,
                "selection_revision": completed_revision,
                "selection_confirmed": reason == "selection_completed",
                "selection_window_active": False,
                "scene_present": False,
                "selection_button_present": bool(source.get("selection_button_present")),
                "selection_click": bool(source.get("selection_click")),
                "scoreboard_key_down": False,
                "ready_slots": 0,
                "content_ready": False,
                "slot_states": [],
                "stable_frames": 0,
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "cursor_over_slots": list(source.get("cursor_over_slots"))
                if isinstance(source.get("cursor_over_slots"), list)
                else [],
                "card_residue": bool(source.get("card_residue")),
                "name_residue": list(source.get("name_residue"))
                if isinstance(source.get("name_residue"), list)
                else [],
                "hover_occluded": False,
                "scene_temporal_state": "ended",
                "matching_timing": _matching_timing(source),
            }
        )
        return _attach_matching_timing(event, source)

    def complete(self, reason: str, *, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """以保留刚结束 epoch/revision 的终止事件清空当前选择窗口。"""

        return self._selection_completed_event(source or {}, reason=reason)

    def pause(
        self,
        reason: str,
        *,
        source: Mapping[str, Any] | None = None,
        scoreboard_key_down: bool = False,
    ) -> dict[str, Any]:
        """短暂不可捕获时隐藏窗口，但保留同一局的识别证据。

        Alt-Tab、计分板和窗口短暂最小化都不是选择结束信号。此事件必须让 Host
        隐藏 overlay，却不能调用 ``reset()``；返回同一 game instance 后，下一次
        有效观察会继续原 epoch、revision 与已稳定槽位。
        """

        self._phase_version = getattr(self, "_phase_version", 0) + 1
        self._frame_ticket = None
        raw_source = source if isinstance(source, Mapping) else {}
        if self.body_shard_latched:
            self.scene_lost_at = 0.0
            event = self._body_shard_event(raw_source)
            event["source"].update(reason=reason, transient_pause=True, scene_temporal_state="transient_pause")
            return event
        rendered_slots = [
            dict(track.stable_slot) if track.stable_slot is not None else unknown_slot(index)
            for index, track in enumerate(self.slots)
        ]
        ready_slots = sum(slot.get("state") == "ready" for slot in rendered_slots)
        event = build_overlay_event(
            rendered_slots,
            source_tag="vision-sidecar",
            selection_type="hextech",
            active=False,
        )
        event["source"].update(
            {
                "reason": reason,
                "gate_state": "transient_pause",
                "scene_state": "paused",
                "scene_kind": "hextech",
                "scene_score": float(raw_source.get("scene_score") or 0.0),
                "selection_epoch": self.epoch,
                "selection_revision": self.selection_revision,
                "selection_window_active": bool(self.scene_active),
                "scene_present": False,
                "scene_temporal_state": "transient_pause",
                "transient_pause": True,
                "paused_reason": reason,
                "selection_button_present": bool(raw_source.get("selection_button_present")),
                "selection_click": False,
                "scoreboard_key_down": bool(scoreboard_key_down),
                "ready_slots": ready_slots,
                "content_ready": ready_slots == SLOT_COUNT,
                "slot_states": [str(slot.get("state") or "detecting") for slot in rendered_slots],
                "stable_frames": self.scene_frames,
                "cursor_over_cards": False,
                "cursor_over_slots": [],
                "card_residue": bool(raw_source.get("card_residue")),
                "name_residue": list(raw_source.get("name_residue"))
                if isinstance(raw_source.get("name_residue"), list)
                else [],
                "hover_occluded": False,
                "matching_timing": _matching_timing(raw_source),
            }
        )
        return _attach_matching_timing(event, raw_source)

    def _residue_event(
        self,
        source: Mapping[str, Any],
        *,
        hover_occluded: bool,
        reason: str | None = None,
        grace_seconds: float = 0.0,
        grace_elapsed_seconds: float = 0.0,
        temporal_state: str | None = None,
        rendered_slots: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        resolved_slots = rendered_slots or [
            dict(track.stable_slot) if track.stable_slot is not None else unknown_slot(index)
            for index, track in enumerate(self.slots)
        ]
        ready_slots = sum(slot.get("state") == "ready" for slot in resolved_slots)
        event = build_overlay_event(
            resolved_slots,
            source_tag="vision-sidecar",
            selection_type="hextech",
            active=bool(self.scene_active and ready_slots > 0),
        )
        event["source"].update(
            {
                "reason": reason or ("hover_occluded" if hover_occluded else "scene_residue_hold"),
                "gate_state": (
                    "visible_ready"
                    if ready_slots == SLOT_COUNT
                    else "visible_partial"
                    if ready_slots > 0
                    else "detecting"
                ),
                "scene_state": "active",
                "scene_kind": "hextech",
                "scene_score": float(source.get("scene_score") or 0.0),
                "layout_id": str(source.get("layout_id") or ""),
                "button_box": list(source.get("button_box") or []),
                "layout_transform": source.get("layout_transform")
                if isinstance(source.get("layout_transform"), Mapping)
                else {},
                "selection_epoch": self.epoch,
                "selection_revision": self.selection_revision,
                "selection_window_active": True,
                "scene_present": bool(source.get("scene_present")),
                "selection_click": bool(source.get("selection_click")),
                "transition_source": str(source.get("transition_source") or ""),
                "transition_kind": str(source.get("transition_kind") or ""),
                "transition_slot": source.get("transition_slot"),
                "scoreboard_key_down": False,
                "ready_slots": ready_slots,
                "content_ready": ready_slots == SLOT_COUNT,
                "slot_states": [str(slot.get("state") or "detecting") for slot in resolved_slots],
                "stable_frames": self.scene_frames,
                "blocking_modal": False,
                "poll_mode": "high",
                "selection_button_present": bool(source.get("selection_button_present")),
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "cursor_over_slots": list(source.get("cursor_over_slots"))
                if isinstance(source.get("cursor_over_slots"), list)
                else [],
                "card_residue": bool(source.get("card_residue")),
                "name_residue": list(source.get("name_residue"))
                if isinstance(source.get("name_residue"), list)
                else [],
                "hover_occluded": hover_occluded,
                "scene_temporal_state": temporal_state or (
                    "grace_hold" if grace_seconds > 0.0 else "stable"
                ),
                "scene_grace_seconds": round(max(0.0, grace_seconds), 3),
                "scene_grace_elapsed_seconds": round(max(0.0, grace_elapsed_seconds), 3),
                "panel_scores": list(source.get("panel_scores"))
                if isinstance(source.get("panel_scores"), list)
                else [],
                "body_shard_scores": list(source.get("body_shard_scores"))
                if isinstance(source.get("body_shard_scores"), list)
                else [],
                "body_shard_latched": False,
            }
        )
        return _attach_matching_timing(event, source)

    def _reduce_slots(
        self, raw_event: Mapping[str, Any], source: Mapping[str, Any], *,
        observed_at: float, pending_only: bool = False,
    ) -> tuple[list[dict[str, Any]], set[int], list[Any], int]:
        if getattr(self, "_defer_slot_reduction", False):
            self._deferred_slot_mode = pending_only
            return ([dict(track.stable_slot) if track.stable_slot is not None else
                     {**unknown_slot(i), "slot_generation": max(1, track.slot_generation)}
                     for i, track in enumerate(self.slots)], set(), [], int(source.get("frame_id") or 0))
        from .slot_frame import reduce_slots
        return reduce_slots(self, raw_event, source, observed_at=observed_at, pending_only=pending_only)

    def begin_frame(self, raw_event):
        from .frame_admission import begin_frame
        return begin_frame(self, raw_event)

    def finish_frame(self, ticket, raw_event):
        from .frame_admission import finish_frame
        return finish_frame(self, ticket, raw_event)

    def _negative_scene_event(self, raw_event):
        source = raw_event.get("source") or {}
        if not (source.get("scene_present") or source.get("selection_window_active")
                or source.get("selection_button_present") or source.get("card_residue")
                or any(source.get("name_residue") or [])):
            return None
        if source.get("selection_confirmed") or (source.get("selection_click") and source.get("transition_kind") == "card"):
            return None
        timing = raw_event.get("timing") or {}
        decision = evaluate_scene_negative(self._scene_negative_state, raw_event, self,
            observed_at=float(timing.get("recognition_completed_at") or timing.get("captured_at") or time.time()),
            minimum_captured_at=float(raw_event.get("_negative_minimum_captured_at") or 0))
        if decision.kind == "body_shard":
            self.body_shard_latched = True
            self.scene_active = False
            self.scene_lost_at = 0.0
            for slot in self.slots:
                slot.clear()
            return self._body_shard_event(source)
        if decision.kind == "conflict":
            event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
            event["source"].update(dict(source), reason="scene_type_conflict", scene_state="blocked",
                scene_kind="unknown", selection_window_active=False, ready_slots=0, content_ready=False,
                selection_epoch=self.epoch, selection_revision=self.selection_revision)
            return event
        return None

    def update(self, raw_event: Mapping[str, Any]) -> dict[str, Any]:
        """处理一帧视觉事件，返回 overlay 就绪事件。

        状态转换：
        - body_shard_only → 锁定锻体模式，清空槽位，输出 body_shard 事件
        - blocking_modal_present → 重置并阻塞；scoreboard_key_down → 非破坏性暂停
        - body_shard_latched + 场景消失 → 防抖退出锻体模式
        - hover_occluded（鼠标遮挡）→ 只要仍能确认卡片残留就沿用上次槽位
        - scene_residue_hold（普通残留保持）→ 按真实时间短暂保持后再退出
        - 场景出现 → 累积帧数，达到阈值后 scene_active=True
        - 场景消失 → 有残留时使用分级时间宽限；无残留才走短帧防抖
        """
        source = raw_event.get("source") if isinstance(raw_event.get("source"), Mapping) else {}
        timing = raw_event.get("timing") if isinstance(raw_event.get("timing"), Mapping) else {}
        try:
            observed_at = float(timing.get("recognition_completed_at") or timing.get("captured_at") or 0.0)
        except (TypeError, ValueError):
            observed_at = 0.0
        if observed_at <= 0.0:
            observed_at = time.monotonic()
        reason = str(source.get("reason") or "")
        if reason == "capture_roi_invalid":
            return self.pause(reason, source=source)
        if reason == "body_shard_only":
            if not self.body_shard_latched:
                self.epoch += 1
                self.selection_revision = 1
            self.scene_frames = 0
            self.absent_frames = 0
            self.scene_active = False
            self.body_shard_latched = True
            self.body_shard_absent_frames = 0
            self.scene_lost_at = 0.0
            for slot in self.slots:
                slot.clear()
            return self._body_shard_event(source)
        if reason == "scoreboard_key_down":
            return self.pause(reason, source=source, scoreboard_key_down=True)
        if reason == "blocking_modal_present":
            if self.body_shard_latched:
                self.scene_lost_at = 0.0
                return self._body_shard_event(source)
            return self.block(reason)
        if not self.body_shard_latched:
            negative = self._negative_scene_event(raw_event)
            if negative is not None:
                return negative

        name_residue = source.get("name_residue") if isinstance(source.get("name_residue"), list) else []
        raw_scene_present = bool(source.get("scene_present") or source.get("selection_window_active"))
        transition_kind = str(source.get("transition_kind") or "")
        card_click = bool(
            self.scene_active
            and source.get("selection_click")
            and transition_kind == "card"
        )
        if card_click:
            return self._selection_completed_event(source)
        if (
            raw_scene_present
            and source.get("selection_click")
            and source.get("cursor_over_cards")
            and transition_kind != "reroll"
        ):
            self.selection_click_armed = True
        scene_button_hold = bool(
            self.scene_active
            and not raw_scene_present
            and source.get("selection_button_present")
            and (
                source.get("card_residue")
                or any(bool(value) for value in name_residue[:SLOT_COUNT])
            )
            and not source.get("selection_click")
            and not source.get("selection_confirmed")
        )
        scene_present = bool(raw_scene_present or scene_button_hold)
        if self.body_shard_latched:
            if source.get("selection_confirmed") or (source.get("selection_click") and transition_kind == "card"):
                return self._selection_completed_event(source)
            if scene_present or source.get("selection_button_present") or source.get("card_residue") or any(name_residue):
                self.body_shard_absent_frames = 0
                self.scene_lost_at = 0.0
                return self._body_shard_event(source)
            if self.scene_lost_at <= 0.0:
                self.scene_lost_at = observed_at
            if observed_at - self.scene_lost_at < EMPTY_SCENE_GRACE_SECONDS:
                return self._body_shard_event(source)
            return self._selection_completed_event(source, reason="scene_loss_confirmed")

        if source.get("selection_confirmed") and self.scene_active:
            return self._selection_completed_event(source)
        if scene_button_hold:
            self.scene_lost_at = 0.0
            self.absent_frames = 0
            self.residue_hold_frames += 1
            held_slots, _, _, _ = self._reduce_slots(
                raw_event,
                source,
                observed_at=observed_at,
                pending_only=True,
            )
            return self._residue_event(
                source,
                hover_occluded=bool(source.get("cursor_over_cards")),
                reason="scene_button_hold",
                temporal_state="button_hold",
                rendered_slots=held_slots,
            )
        hover_occluded = bool(
            self.scene_active
            and not scene_present
            and source.get("cursor_over_cards")
            and any(track.stable_slot is not None for track in self.slots)
            and not source.get("selection_confirmed")
        )
        scene_residue_hold = bool(
            self.scene_active
            and not scene_present
            and (
                source.get("card_residue")
                or any(bool(value) for value in name_residue[:SLOT_COUNT])
                or source.get("cursor_over_cards")
            )
        )
        ready_slot_count = sum(track.stable_slot is not None for track in self.slots)
        if self.scene_active and not scene_present and (ready_slot_count > 0 or scene_residue_hold):
            if self.selection_click_armed:
                return self._selection_completed_event(source)
            if self.scene_lost_at <= 0.0:
                self.scene_lost_at = observed_at
            grace_seconds = (
                READY_SCENE_GRACE_SECONDS
                if ready_slot_count == SLOT_COUNT
                else PARTIAL_SCENE_GRACE_SECONDS
                if ready_slot_count > 0
                else EMPTY_SCENE_GRACE_SECONDS
            )
            grace_elapsed = max(0.0, observed_at - self.scene_lost_at)
            if grace_elapsed <= grace_seconds:
                self.absent_frames = 0
                self.residue_hold_frames += 1
                grace_slots, _, _, _ = self._reduce_slots(
                    raw_event,
                    source,
                    observed_at=observed_at,
                    pending_only=True,
                )
                return self._residue_event(
                    source,
                    hover_occluded=hover_occluded,
                    reason="hover_occluded" if hover_occluded else "scene_grace_hold",
                    grace_seconds=grace_seconds,
                    grace_elapsed_seconds=grace_elapsed,
                    rendered_slots=grace_slots,
                )
            return self._selection_completed_event(source, reason="scene_loss_confirmed")
        if scene_present:
            self.scene_lost_at = 0.0
            self.residue_hold_frames = 0
            self.absent_frames = 0
            if self.scene_frames == 0 and not self.scene_active:
                self.epoch += 1
                self.selection_revision = 1
                for slot in self.slots:
                    slot.clear()
            self.scene_frames += 1
            if not self.scene_active and self.scene_frames >= max(1, int(self.scene_enter_frames)):
                self.scene_active = True
        else:
            if self.scene_active and (self.selection_click_armed or source.get("selection_confirmed")):
                return self._selection_completed_event(source)
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
                        "selection_revision": self.selection_revision,
                        "selection_window_active": False,
                        "scene_present": False,
                        "selection_button_present": bool(source.get("selection_button_present")),
                        "selection_click": bool(source.get("selection_click")),
                        "scoreboard_key_down": False,
                        "ready_slots": 0,
                        "content_ready": False,
                        "slot_states": [],
                        "stable_frames": self.absent_frames,
                        "scene_kind": str(source.get("scene_kind") or "absent"),
                        "body_shard_scores": list(source.get("body_shard_scores"))
                        if isinstance(source.get("body_shard_scores"), list)
                        else [],
                        "body_shard_latched": False,
                        "cursor_over_cards": bool(source.get("cursor_over_cards")),
                        "cursor_over_slots": list(source.get("cursor_over_slots"))
                        if isinstance(source.get("cursor_over_slots"), list)
                        else [],
                        "card_residue": bool(source.get("card_residue")),
                        "name_residue": list(source.get("name_residue"))
                        if isinstance(source.get("name_residue"), list)
                        else [],
                        "hover_occluded": False,
                        "matching_timing": _matching_timing(source),
                    }
                )
                return _attach_matching_timing(event, source)

        rendered_slots, cursor_over_slots, raw_slots, frame_id = self._reduce_slots(
            raw_event,
            source,
            observed_at=observed_at,
        )
        ready_slots = sum(slot.get("state") == "ready" for slot in rendered_slots)
        event = build_overlay_event(
            rendered_slots,
            source_tag="vision-sidecar",
            selection_type="hextech",
            active=bool(self.scene_active and ready_slots > 0),
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
                "selection_revision": self.selection_revision,
                "selection_window_active": bool(self.scene_active),
                "scene_present": bool(scene_present),
                "scene_temporal_state": "stable",
                "selection_click": bool(source.get("selection_click")),
                "transition_source": str(source.get("transition_source") or ""),
                "transition_kind": str(source.get("transition_kind") or ""),
                "transition_slot": source.get("transition_slot"),
                "mouse_event_sequence": int(source.get("mouse_event_sequence") or 0),
                "mouse_event_observed_at": float(source.get("mouse_event_observed_at") or 0.0),
                "scoreboard_key_down": False,
                "ready_slots": ready_slots,
                "content_ready": ready_slots == SLOT_COUNT,
                "slot_states": [str(slot.get("state") or "detecting") for slot in rendered_slots],
                "stable_frames": self.scene_frames,
                "blocking_modal": False,
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "cursor_over_slots": sorted(cursor_over_slots),
                "card_residue": bool(source.get("card_residue")),
                "hover_occluded": any(
                    self.slots[index].stable_slot is not None for index in cursor_over_slots
                ),
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
                "frame_id": frame_id,
                "latency_ms": source.get("latency_ms"),
                "calibration": str(source.get("calibration") or "layout_v2"),
                "matching_timing": _matching_timing(source),
            }
        )
        event["_raw_slots"] = [dict(slot) for slot in raw_slots if isinstance(slot, Mapping)]
        event["_acceptance_rules"] = [str(slot.get("acceptance_rule") or "") for slot in rendered_slots]
        return _attach_matching_timing(event, source)

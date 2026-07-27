"""Overlay V2 场景与逐槽状态机。

状态机只处理已经完成单帧视觉分析的字典，不访问窗口、截图或磁盘。场景和槽位
分别稳定，避免任一槽抖动时让整个 overlay 反复显隐。

调用方: overlay.vision.sidecar、tests.test_overlay_vision_state、dev_checks; 关键依赖: overlay.events、overlay.vision.matcher。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from hextech.modules.vision.events import build_overlay_event
from hextech.modules.recommendation.hints import normalize_augment_id
from hextech.infrastructure.vision.matcher import (
    SlotCandidate,
    arbitrate_slot_candidates,
    unknown_slot,
)


SCENE_ENTER_FRAMES = 2  # 场景连续出现 N 帧后判定为"进入"
SCENE_EXIT_FRAMES = 2   # 场景连续消失 N 帧后判定为"退出"
SLOT_COUNT = 3          # 海克斯三选一槽位数
RESIDUE_HOLD_FRAMES = 2  # 普通残影只短暂沿用，避免选择结束后长时间残留
STRONG_WINDOW_SIZE = 3
STRONG_REQUIRED_HITS = 2
MEDIUM_WINDOW_SIZE = 5
MEDIUM_REQUIRED_HITS = 3
EVIDENCE_MAX_AGE_SECONDS = 6.0
EVIDENCE_STARVED_OBSERVATIONS = 5
EVIDENCE_STARVED_SECONDS = 2.0
PARTIAL_SCENE_GRACE_SECONDS = 0.75
READY_SCENE_GRACE_SECONDS = 0.75
EMPTY_SCENE_GRACE_SECONDS = 0.75


@dataclass(frozen=True)
class _CandidateEvidence:
    """单次原始观察；miss 也占据滑动窗口，避免跳过空帧累计旧证据。"""

    observed_at: float
    candidate: SlotCandidate | None


@dataclass
class _SlotTrack:
    """单槽位跟踪器：维护最近候选证据和已确认输出。"""

    candidate_identity: str = ""       # 最近一帧有效候选标识
    candidate_frames: int = 0          # 最近窗口内当前候选命中数，保留供诊断读取
    stable_slot: dict[str, Any] | None = None  # 已稳定确认的槽位输出
    weak_miss_frames: int = 0          # 连续 miss 数，仅用于诊断，不撤下同 epoch 已稳定结果
    observations: list[_CandidateEvidence] = field(default_factory=list)
    raw_observation_count: int = 0
    pending_started_at: float = 0.0

    def clear(self) -> None:
        self.candidate_identity = ""
        self.candidate_frames = 0
        self.stable_slot = None
        self.weak_miss_frames = 0
        self.observations.clear()
        self.raw_observation_count = 0
        self.pending_started_at = 0.0


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
    slots: list[_SlotTrack] = field(default_factory=lambda: [_SlotTrack() for _ in range(SLOT_COUNT)])

    def reset(self) -> None:
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
        self.reset()
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update(
            {
                "reason": reason,
                "gate_state": "inactive",
                "scene_state": "absent",
                "scene_kind": "hextech",
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
            }
        )
        return event

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

        raw_source = source if isinstance(source, Mapping) else {}
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
            }
        )
        return event

    @staticmethod
    def _max_identity_hits(observations: list[_CandidateEvidence]) -> int:
        counts: dict[str, int] = {}
        for item in observations:
            if item.candidate is not None:
                counts[item.candidate.identity] = counts.get(item.candidate.identity, 0) + 1
        return max(counts.values(), default=0)

    @staticmethod
    def _pending_temporal_state(track: _SlotTrack, observed_at: float) -> str:
        starved = bool(
            track.raw_observation_count >= EVIDENCE_STARVED_OBSERVATIONS
            and track.pending_started_at > 0.0
            and observed_at - track.pending_started_at >= EVIDENCE_STARVED_SECONDS
            and SelectionTracker._max_identity_hits(track.observations[-MEDIUM_WINDOW_SIZE:]) < 2
        )
        return "evidence_starved" if starved else "evidence_pending"

    def _update_slot(
        self,
        index: int,
        raw_slot: Mapping[str, Any],
        *,
        observed_at: float,
        candidate: SlotCandidate | None,
        rejection_reason: str = "",
    ) -> dict[str, Any]:
        """按真实时间和 M-of-N 证据窗口确认单槽候选。

        miss 只淘汰过期证据，不会把仍在窗口内的有效观察全部清零；候选不稳定也
        始终保持 detecting。只有进程、截图或模板等硬故障由上层运行态报告 failed。
        """

        track = self.slots[index]
        track.raw_observation_count += 1
        if track.pending_started_at <= 0.0 and track.stable_slot is None:
            track.pending_started_at = observed_at
        track.observations = [
            item
            for item in track.observations
            if observed_at - item.observed_at <= EVIDENCE_MAX_AGE_SECONDS
        ][-MEDIUM_WINDOW_SIZE:]
        # M-of-N 的 N 是原始观察数，不是“有候选的帧数”。因此 miss 必须作为空
        # 观察占据窗口；否则 ``medium, miss, miss, miss, medium, medium`` 会把已
        # 经离开最近五帧的旧 medium 错误累计成 3/5 ready。
        track.observations.append(_CandidateEvidence(observed_at=observed_at, candidate=candidate))
        track.observations = track.observations[-MEDIUM_WINDOW_SIZE:]

        if candidate is None:
            track.candidate_identity = ""
            track.candidate_frames = 0
            track.weak_miss_frames += 1
            if track.stable_slot is not None:
                return dict(track.stable_slot)
            temporal_state = self._pending_temporal_state(track, observed_at)
            pending = unknown_slot(index, diagnostic=temporal_state)
            pending.update(
                {
                    "temporal_state": temporal_state,
                    "candidate_identity": "",
                    "evidence_hits": 0,
                    "evidence_window": len(track.observations),
                    "required_hits": MEDIUM_REQUIRED_HITS,
                    "rejection_reason": str(
                        rejection_reason
                        or raw_slot.get("diagnostic")
                        or raw_slot.get("reason")
                        or temporal_state
                    ),
                    "observed_at": observed_at,
                }
            )
            return pending

        track.weak_miss_frames = 0
        track.candidate_identity = candidate.identity

        strong_window = track.observations[-STRONG_WINDOW_SIZE:]
        medium_window = track.observations[-MEDIUM_WINDOW_SIZE:]
        strong_hits = sum(
            item.candidate is not None
            and item.candidate.identity == candidate.identity
            and item.candidate.evidence_grade == "strong"
            for item in strong_window
        )
        medium_hits = sum(
            item.candidate is not None and item.candidate.identity == candidate.identity
            for item in medium_window
        )
        conflicting_strong_in_short = any(
            item.candidate is not None
            and item.candidate.evidence_grade == "strong"
            and item.candidate.identity != candidate.identity
            for item in strong_window
        )
        conflicting_strong = any(
            item.candidate is not None
            and item.candidate.evidence_grade == "strong"
            and item.candidate.identity != candidate.identity
            for item in medium_window
        )
        confirmed_as = ""
        if (
            candidate.evidence_grade == "strong"
            and strong_hits >= STRONG_REQUIRED_HITS
            and not conflicting_strong_in_short
        ):
            confirmed_as = "strong"
        elif medium_hits >= MEDIUM_REQUIRED_HITS and not conflicting_strong:
            confirmed_as = "medium"

        track.candidate_frames = strong_hits if candidate.evidence_grade == "strong" else medium_hits
        if track.stable_slot is not None:
            stable_identity = str(
                track.stable_slot.get("recognition_key")
                or normalize_augment_id(track.stable_slot.get("name"))
                or track.stable_slot.get("augment_id")
                or ""
            )
            stable_variant = str(track.stable_slot.get("visual_variant_id") or track.stable_slot.get("augment_id") or "")
            candidate_variant = str(candidate.visual_variant_id or candidate.augment_id or "")
            if candidate.identity == stable_identity:
                if candidate_variant and candidate.evidence_grade == "strong" and candidate_variant != stable_variant:
                    enriched = candidate.ready_slot()
                    enriched.update(
                        {
                            "temporal_state": "confirmed",
                            "evidence_hits": track.candidate_frames,
                            "evidence_window": len(medium_window),
                            "observed_at": observed_at,
                        }
                    )
                    track.stable_slot = enriched
                return dict(track.stable_slot)
            # 已稳定结果只能被不同身份的 strong 证据替换。medium 仍可累计为
            # 诊断，但不能用重复的双字体误匹配撤下已展示的正确卡名。
            if confirmed_as != "strong":
                return dict(track.stable_slot)

        if confirmed_as:
            replacing_stable = track.stable_slot is not None
            stable_slot = candidate.ready_slot()
            required_hits = STRONG_REQUIRED_HITS if confirmed_as == "strong" else MEDIUM_REQUIRED_HITS
            stable_slot.update(
                {
                    "required_frames": required_hits,
                    "observed_frames": track.candidate_frames,
                    "replacement_reason": "replacement_confirmed" if replacing_stable else f"initial_{confirmed_as}",
                    "temporal_state": "confirmed",
                    "evidence_hits": track.candidate_frames,
                    "evidence_window": len(strong_window if confirmed_as == "strong" else medium_window),
                    "observed_at": observed_at,
                }
            )
            track.stable_slot = stable_slot
            track.pending_started_at = 0.0
            if replacing_stable:
                self._revision_changed = True

        if track.stable_slot is None:
            temporal_state = self._pending_temporal_state(track, observed_at)
            pending = unknown_slot(index, diagnostic=temporal_state)
            pending.update(
                {
                    "temporal_state": temporal_state,
                    "candidate_identity": candidate.identity,
                    "confidence": candidate.confidence,
                    "top_candidates": [dict(item) for item in candidate.top_candidates],
                    "evidence_grade": candidate.evidence_grade,
                    "evidence_hits": track.candidate_frames,
                    "evidence_window": len(strong_window if candidate.evidence_grade == "strong" else medium_window),
                    "required_hits": STRONG_REQUIRED_HITS
                    if candidate.evidence_grade == "strong"
                    else MEDIUM_REQUIRED_HITS,
                    "rejection_reason": str(raw_slot.get("diagnostic") or candidate.diagnostic),
                    "observed_at": observed_at,
                }
            )
            return pending
        return dict(track.stable_slot)

    def _residue_event(
        self,
        source: Mapping[str, Any],
        *,
        hover_occluded: bool,
        reason: str | None = None,
        grace_seconds: float = 0.0,
        grace_elapsed_seconds: float = 0.0,
    ) -> dict[str, Any]:
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
                "reason": reason or ("hover_occluded" if hover_occluded else "scene_residue_hold"),
                "gate_state": "visible_partial" if ready_slots < SLOT_COUNT else "visible_ready",
                "scene_state": "active",
                "scene_kind": "hextech",
                "scene_score": float(source.get("scene_score") or 0.0),
                "layout_id": str(source.get("layout_id") or ""),
                "layout_transform": source.get("layout_transform")
                if isinstance(source.get("layout_transform"), Mapping)
                else {},
                "selection_epoch": self.epoch,
                "selection_revision": self.selection_revision,
                "selection_window_active": True,
                "scene_present": bool(source.get("scene_present")),
                "selection_click": bool(source.get("selection_click")),
                "scoreboard_key_down": False,
                "ready_slots": ready_slots,
                "content_ready": ready_slots == SLOT_COUNT,
                "slot_states": [str(slot.get("state") or "detecting") for slot in rendered_slots],
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
                "scene_temporal_state": "grace_hold" if grace_seconds > 0.0 else "stable",
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
        return event

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
        if reason == "body_shard_only":
            if not self.body_shard_latched:
                self.epoch += 1
                self.selection_revision = 1
            self.scene_frames = 0
            self.absent_frames = 0
            self.scene_active = False
            self.body_shard_latched = True
            self.body_shard_absent_frames = 0
            for slot in self.slots:
                slot.clear()
            return self._body_shard_event(source)
        if reason == "scoreboard_key_down":
            return self.pause(reason, source=source, scoreboard_key_down=True)
        if reason == "blocking_modal_present":
            return self.block(reason)

        scene_present = bool(source.get("scene_present") or source.get("selection_window_active"))
        if scene_present and source.get("selection_click") and source.get("cursor_over_cards"):
            self.selection_click_armed = True
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
        if source.get("selection_confirmed") and self.scene_active and not scene_present:
            return self._selection_completed_event(source)
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
                return self._residue_event(
                    source,
                    hover_occluded=hover_occluded,
                    reason="hover_occluded" if hover_occluded else "scene_grace_hold",
                    grace_seconds=grace_seconds,
                    grace_elapsed_seconds=grace_elapsed,
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
                    }
                )
                return event

        raw_slots = raw_event.get("_raw_slots") if isinstance(raw_event.get("_raw_slots"), list) else []
        cursor_over_slots = {
            int(value)
            for value in (
                source.get("cursor_over_slots")
                if isinstance(source.get("cursor_over_slots"), list)
                else []
            )
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < SLOT_COUNT
        }
        candidates, rejection_reasons = arbitrate_slot_candidates(
            raw_slots,
            [track.stable_slot for track in self.slots],
            cursor_over_slots=cursor_over_slots,
            slot_count=SLOT_COUNT,
        )
        rendered_slots: list[dict[str, Any]] = []
        for index in range(SLOT_COUNT):
            if index in cursor_over_slots:
                # 遮挡槽冻结自己的追踪状态；未遮挡槽仍在同一帧继续识别。
                stable = self.slots[index].stable_slot
                rendered_slots.append(dict(stable) if stable is not None else unknown_slot(index))
                continue
            raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
            rendered_slots.append(
                self._update_slot(
                    index,
                    raw_slot,
                    observed_at=observed_at,
                    candidate=candidates[index],
                    rejection_reason=rejection_reasons[index],
                )
            )
        if self._revision_changed:
            self.selection_revision = max(1, self.selection_revision + 1)
            self._revision_changed = False
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
                "selection_revision": self.selection_revision,
                "selection_window_active": bool(self.scene_active),
                "scene_present": bool(scene_present),
                "scene_temporal_state": "stable",
                "selection_click": bool(source.get("selection_click")),
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
                "latency_ms": source.get("latency_ms"),
                "calibration": str(source.get("calibration") or "layout_v2"),
            }
        )
        event["_raw_slots"] = [dict(slot) for slot in raw_slots if isinstance(slot, Mapping)]
        event["_acceptance_rules"] = [str(slot.get("acceptance_rule") or "") for slot in rendered_slots]
        return event

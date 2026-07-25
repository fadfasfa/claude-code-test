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
    candidate_from_slot,
    strong_evidence_identities,
    unknown_slot,
)


SCENE_ENTER_FRAMES = 2  # 场景连续出现 N 帧后判定为"进入"
SCENE_EXIT_FRAMES = 2   # 场景连续消失 N 帧后判定为"退出"
SLOT_COUNT = 3          # 海克斯三选一槽位数
RESIDUE_HOLD_FRAMES = 2  # 普通残影只短暂沿用，避免选择结束后长时间残留
HOVER_HOLD_FRAMES = 30  # 未点击 hover 需给玩家足够阅读时间；真实点击仍立即完成
SLOT_DETECTION_TIMEOUT_SECONDS = 3.0


@dataclass
class _SlotTrack:
    """单槽位跟踪器：维护候选连续性和稳定输出。"""

    candidate_identity: str = ""       # 当前帧候选标识（augment_id 或 name）
    candidate_frames: int = 0          # 同一候选连续出现帧数
    stable_slot: dict[str, Any] | None = None  # 已稳定确认的槽位输出
    weak_miss_frames: int = 0          # 候选丢失帧数，仅用于诊断，不撤下同 epoch 已稳定结果
    detecting_since: float = 0.0       # 当前选择 epoch 内首次无稳定候选的时间

    def clear(self) -> None:
        self.candidate_identity = ""
        self.candidate_frames = 0
        self.stable_slot = None
        self.weak_miss_frames = 0
        self.detecting_since = 0.0


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

    def _selection_completed_event(self, source: Mapping[str, Any]) -> dict[str, Any]:
        """结束当前 epoch，避免完成选择后继续把空槽解释为 detecting。"""

        completed_epoch = self.epoch
        completed_revision = self.selection_revision
        self.reset()
        event = build_overlay_event([], source_tag="vision-sidecar", selection_type="hextech", active=False)
        event["source"].update(
            {
                "reason": "selection_completed",
                "gate_state": "inactive",
                "scene_state": "absent",
                "scene_kind": "hextech",
                "scene_score": float(source.get("scene_score") or 0.0),
                "selection_epoch": completed_epoch,
                "selection_revision": completed_revision,
                "selection_confirmed": True,
                "selection_window_active": False,
                "scoreboard_key_down": False,
                "ready_slots": 0,
                "content_ready": False,
                "slot_states": [],
                "stable_frames": 0,
                "cursor_over_cards": bool(source.get("cursor_over_cards")),
                "hover_occluded": False,
            }
        )
        return event

    def _update_slot(self, index: int, raw_slot: Mapping[str, Any]) -> dict[str, Any]:
        """逐槽处理单帧识别结果，累积候选帧数直到达到 required_frames 阈值。

        候选判定三态：
        - candidate 为 None（识别失败）→ 保留同 epoch 已稳定输出
        - candidate 与当前稳定输出不同 → 旧结果继续显示，替代候选稳定后再原子替换
        - candidate 与当前追踪一致 → 累加帧数，达到 required_frames 后锁定为稳定输出
        """
        track = self.slots[index]
        now = time.monotonic()
        candidate = candidate_from_slot(raw_slot)

        def failed_slot() -> dict[str, Any]:
            raw_candidates = raw_slot.get("top_candidates") if isinstance(raw_slot.get("top_candidates"), list) else []
            top_candidates = (
                [dict(item) for item in candidate.top_candidates]
                if candidate is not None
                else [dict(item) for item in raw_candidates[:3] if isinstance(item, Mapping)]
            )
            confidence = candidate.confidence if candidate is not None else (
                top_candidates[0].get("confidence") if top_candidates else None
            )
            return {
                **unknown_slot(index, diagnostic="detection_timeout"),
                "state": "failed",
                "summary": "识别失败/重试",
                "confidence": confidence,
                "top_candidates": top_candidates,
                "candidate_identity": candidate.identity if candidate is not None else "",
                "rejection_reason": str(
                    raw_slot.get("diagnostic")
                    or raw_slot.get("reason")
                    or (candidate.diagnostic if candidate is not None else "no_stable_candidate")
                ),
                "elapsed_seconds": round(now - track.detecting_since, 3),
            }

        if candidate is None:
            if track.detecting_since <= 0.0:
                track.detecting_since = now
            track.candidate_identity = ""
            track.candidate_frames = 0
            track.weak_miss_frames += 1
            if track.stable_slot is not None:
                return dict(track.stable_slot)
            if now - track.detecting_since >= SLOT_DETECTION_TIMEOUT_SECONDS:
                return failed_slot()
            return unknown_slot(index)

        identity = candidate.identity
        if track.stable_slot is not None:
            stable_identity = str(
                track.stable_slot.get("recognition_key")
                or normalize_augment_id(track.stable_slot.get("name"))
                or track.stable_slot.get("augment_id")
                or ""
            )
            stable_variant = str(track.stable_slot.get("visual_variant_id") or track.stable_slot.get("augment_id") or "")
            candidate_variant = str(candidate.visual_variant_id or candidate.augment_id or "")
            same_variant = not (stable_variant and candidate_variant and stable_variant != candidate_variant)
            if identity == stable_identity and same_variant:
                if candidate_variant and not stable_variant:
                    # 卡名已经稳定后，后续强图标证据可以补齐视觉版本和 tier；
                    # 这不是候选刷新，不递增 revision，也不撤下名称 fallback 统计。
                    track.stable_slot = candidate.ready_slot()
                track.candidate_identity = identity
                track.candidate_frames = max(track.candidate_frames, candidate.required_frames)
                track.detecting_since = 0.0
                track.weak_miss_frames = 0
                return dict(track.stable_slot)
            # 不同候选只有连续达到自己的稳定帧数后才替换。单帧鼠标遮挡或动画噪声
            # 不能撤下已经展示的结果，否则三个槽会在真机里反复变空。
            if (
                candidate.evidence_grade == "medium"
                and stable_identity in strong_evidence_identities(raw_slot)
            ):
                track.candidate_identity = ""
                track.candidate_frames = 0
                return dict(track.stable_slot)
        track.weak_miss_frames = 0
        if identity == track.candidate_identity:
            track.candidate_frames += 1
        else:
            track.candidate_identity = identity
            track.candidate_frames = 1
        replacing_stable = track.stable_slot is not None
        required_frames = max(3, candidate.required_frames) if replacing_stable else candidate.required_frames
        if track.candidate_frames >= required_frames:
            stable_slot = candidate.ready_slot()
            stable_slot["required_frames"] = required_frames
            stable_slot["observed_frames"] = track.candidate_frames
            stable_slot["replacement_reason"] = (
                "replacement_confirmed" if replacing_stable else f"initial_{candidate.evidence_grade}"
            )
            track.stable_slot = stable_slot
            track.detecting_since = 0.0
            if replacing_stable:
                self._revision_changed = True
        if track.stable_slot is None:
            if track.detecting_since <= 0.0:
                track.detecting_since = now
            if now - track.detecting_since >= SLOT_DETECTION_TIMEOUT_SECONDS:
                return failed_slot()
            return unknown_slot(index)
        return dict(track.stable_slot)

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
                "selection_revision": self.selection_revision,
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
                "card_residue": bool(source.get("card_residue")),
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
                self.selection_revision = 1
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
            and source.get("card_residue")
            and sum(bool(value) for value in name_residue[:SLOT_COUNT]) >= 2
        )
        if source.get("selection_confirmed") and self.scene_active and not scene_present:
            return self._selection_completed_event(source)
        if hover_occluded or scene_residue_hold:
            self.residue_hold_frames += 1
            if hover_occluded:
                if self.selection_click_armed or self.residue_hold_frames > max(1, int(HOVER_HOLD_FRAMES)):
                    return self._selection_completed_event(source)
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
        rendered_slots: list[dict[str, Any]] = []
        for index in range(SLOT_COUNT):
            if index in cursor_over_slots:
                # 遮挡槽冻结自己的追踪状态；未遮挡槽仍在同一帧继续识别。
                stable = self.slots[index].stable_slot
                rendered_slots.append(dict(stable) if stable is not None else unknown_slot(index))
                continue
            raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
            rendered_slots.append(self._update_slot(index, raw_slot))
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

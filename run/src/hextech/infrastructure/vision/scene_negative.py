"""OCR 提供的场景否定证据。

碎片名称不是海克斯身份候选。本模块只用严格完整名称和完整原帧绑定，
把一个可信槽归约为场景冲突、两个不同可信槽归约为锻体碎片场景。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import unicodedata
from typing import Any, Literal, Mapping, Sequence


SCENE_NEGATIVE_MIN_CONFIDENCE = 0.95
SCENE_NEGATIVE_MAX_AGE_SECONDS = 6.0
SCENE_NEGATIVE_SEEN_CAPACITY = 128

# 只收录由仓内真机 ROI 或绑定的 r10 timeline 逐字核实过的完整名称。
# 新名称必须带同等级证据补入；不得改成后缀、包含或模糊匹配。
BODY_SHARD_OCR_NAMES = (
    "坚不可摧碎片",
    "攻击速度碎片",
    "穿甲碎片",
    "魔法抗性碎片",
    "技能急速碎片",
    "迅捷碎片",
)


def normalize_scene_negative_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


_NORMALIZED_BODY_SHARD_NAMES = {
    normalize_scene_negative_text(name): name for name in BODY_SHARD_OCR_NAMES
}


def classify_scene_negative(
    raw_text: object,
    confidence: object,
    *,
    minimum_confidence: float = SCENE_NEGATIVE_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """严格完整名称分类；结果独立于正面海克斯词表。"""

    normalized = normalize_scene_negative_text(raw_text)
    matched_name = _NORMALIZED_BODY_SHARD_NAMES.get(normalized, "")
    try:
        score = float(confidence or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    score_valid = math.isfinite(score)
    admitted = bool(
        matched_name
        and score_valid
        and score >= float(minimum_confidence)
    )
    reason = ""
    if not matched_name:
        reason = "scene_negative_not_exact"
    elif not score_valid or score < float(minimum_confidence):
        reason = "scene_negative_confidence_below_threshold"
    return {
        "schema_version": 1,
        "state": "admitted" if admitted else "rejected",
        "reason": reason,
        "scene_kind": "body_shard" if admitted else "",
        "name": matched_name if admitted else "",
        "normalized_text": normalized,
        "confidence": score if score_valid else 0.0,
        "match_rule": "exact" if admitted else "",
    }


@dataclass(frozen=True)
class TrustedSceneNegative:
    session_id: str
    selection_epoch: int
    slot_index: int
    slot_generation: int
    captured_frame_id: int
    captured_at: float
    rgb_sha256: str
    perceptual_fingerprint: str
    name: str
    confidence: float

    @property
    def key(self) -> tuple[str, int, int, int, int, str]:
        return (
            self.session_id,
            self.selection_epoch,
            self.slot_index,
            self.slot_generation,
            self.captured_frame_id,
            self.rgb_sha256,
        )


@dataclass
class SceneNegativeState:
    session_id: str = ""
    selection_epoch: int = 0
    by_slot: dict[int, TrustedSceneNegative] = field(default_factory=dict)
    _seen_order: deque[tuple[str, int, int, int, int, str]] = field(default_factory=deque)
    _seen: set[tuple[str, int, int, int, int, str]] = field(default_factory=set)

    def clear(self) -> None:
        self.session_id = ""
        self.selection_epoch = 0
        self.by_slot.clear()
        self._seen_order.clear()
        self._seen.clear()

    def bind(self, session_id: str, selection_epoch: int) -> None:
        if self.session_id == session_id and self.selection_epoch == selection_epoch:
            return
        self.clear()
        self.session_id = session_id
        self.selection_epoch = selection_epoch

    def remember(self, evidence: TrustedSceneNegative) -> bool:
        if evidence.key in self._seen:
            return False
        self._seen.add(evidence.key)
        self._seen_order.append(evidence.key)
        while len(self._seen_order) > SCENE_NEGATIVE_SEEN_CAPACITY:
            self._seen.discard(self._seen_order.popleft())
        current = self.by_slot.get(evidence.slot_index)
        if current is None or (
            evidence.captured_at,
            evidence.captured_frame_id,
        ) >= (
            current.captured_at,
            current.captured_frame_id,
        ):
            self.by_slot[evidence.slot_index] = evidence
        return True


SceneNegativeKind = Literal["none", "conflict", "body_shard"]


@dataclass(frozen=True)
class SceneNegativeDecision:
    kind: SceneNegativeKind
    trusted_slots: tuple[int, ...] = ()
    matched_names: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()

    @property
    def blocks_identity(self) -> bool:
        return self.kind != "none"


def clear_scene_negative(state: SceneNegativeState) -> None:
    state.clear()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _slot_generations(tracker: Any) -> tuple[int, ...]:
    return tuple(max(1, int(getattr(slot, "slot_generation", 0))) for slot in tracker.slots)


def _observation_matches(track: Any, evidence: TrustedSceneNegative) -> bool:
    return any(
        int(getattr(observation, "frame_id", 0)) == evidence.captured_frame_id
        and str(getattr(observation, "fingerprint", "") or "")
        == evidence.perceptual_fingerprint
        and str(getattr(observation, "rgb_sha256", "") or "") == evidence.rgb_sha256
        for observation in getattr(track, "observations", ())
    )


def _current_slot_matches(
    raw_slots: Sequence[object],
    evidence: TrustedSceneNegative,
    *,
    current_frame_id: int,
) -> bool:
    if evidence.captured_frame_id != current_frame_id:
        return False
    if not (0 <= evidence.slot_index < len(raw_slots)):
        return False
    raw_slot = _mapping(raw_slots[evidence.slot_index])
    if str(raw_slot.get("evidence_fingerprint") or "") != evidence.perceptual_fingerprint:
        return False
    shadow = _mapping(raw_slot.get("ocr_shadow"))
    shadow_sha = str(shadow.get("input_sha256") or "")
    return not shadow_sha or shadow_sha == evidence.rgb_sha256


def _parse_trusted(
    raw: object,
    *,
    session_id: str,
    selection_epoch: int,
    slot_generations: Sequence[int],
    observed_at: float,
    minimum_captured_at: float,
) -> tuple[TrustedSceneNegative | None, str]:
    evidence = _mapping(raw)
    negative = _mapping(evidence.get("scene_negative"))
    if (
        str(negative.get("state") or "") != "admitted"
        or str(negative.get("scene_kind") or "") != "body_shard"
        or str(negative.get("match_rule") or "") != "exact"
    ):
        return None, "not_scene_negative"
    normalized = normalize_scene_negative_text(negative.get("name"))
    name = _NORMALIZED_BODY_SHARD_NAMES.get(normalized, "")
    try:
        confidence = float(negative.get("confidence") or 0.0)
        slot_index = int(evidence.get("slot_index", -1))
        generation = int(evidence.get("slot_generation") or 0)
        frame_id = int(evidence.get("captured_frame_id") or 0)
        captured_at = float(evidence.get("captured_at") or 0.0)
        evidence_epoch = int(evidence.get("selection_epoch") or 0)
    except (TypeError, ValueError):
        return None, "rejected_malformed"
    if (
        not name
        or not math.isfinite(confidence)
        or confidence < SCENE_NEGATIVE_MIN_CONFIDENCE
    ):
        return None, "rejected_negative_gate"
    if (
        str(evidence.get("session_id") or "") != session_id
        or evidence_epoch != selection_epoch
        or not 0 <= slot_index < len(slot_generations)
        or generation != int(slot_generations[slot_index])
    ):
        return None, "rejected_context"
    if not math.isfinite(captured_at) or not math.isfinite(observed_at):
        return None, "rejected_clock"
    if captured_at > observed_at:
        return None, "rejected_future"
    if captured_at <= 0.0 or observed_at - captured_at > SCENE_NEGATIVE_MAX_AGE_SECONDS:
        return None, "rejected_late"
    if minimum_captured_at > 0.0 and captured_at < minimum_captured_at:
        return None, "rejected_cutoff"
    rgb_sha256 = str(evidence.get("rgb_sha256") or "")
    fingerprint = str(evidence.get("perceptual_fingerprint") or "")
    if frame_id <= 0 or len(rgb_sha256) != 64 or not fingerprint:
        return None, "rejected_frame_binding"
    return (
        TrustedSceneNegative(
            session_id=session_id,
            selection_epoch=selection_epoch,
            slot_index=slot_index,
            slot_generation=generation,
            captured_frame_id=frame_id,
            captured_at=captured_at,
            rgb_sha256=rgb_sha256,
            perceptual_fingerprint=fingerprint,
            name=name,
            confidence=confidence,
        ),
        "",
    )


def evaluate_scene_negative(
    state: SceneNegativeState,
    raw_event: Mapping[str, Any],
    tracker: Any,
    *,
    observed_at: float,
    minimum_captured_at: float = 0.0,
) -> SceneNegativeDecision:
    """合并当前缓存与异步完成结果；同一原帧证据只计一次。"""

    source = _mapping(raw_event.get("source"))
    session_id = str(source.get("session_id") or "")
    try:
        selection_epoch = int(getattr(tracker, "epoch", 0))
        current_frame_id = int(source.get("frame_id") or 0)
        observed = float(observed_at)
        cutoff = float(minimum_captured_at or 0.0)
    except (TypeError, ValueError):
        return SceneNegativeDecision("none", outcomes=("rejected_malformed",))
    if not session_id or selection_epoch <= 0 or not math.isfinite(observed):
        return SceneNegativeDecision("none", outcomes=("rejected_context",))

    state.bind(session_id, selection_epoch)
    generations = _slot_generations(tracker)
    transitioned: set[int] = set()
    # 明确重随使对应旧negative失效；真实generation仍由唯一slot reducer推进。
    if source.get("selection_click") and source.get("transition_kind") == "reroll":
        transitioned = set(source.get("cursor_over_slots") or [])
        if source.get("transition_source") == "async_mouse_down" and isinstance(source.get("transition_slot"), int):
            transitioned.add(source["transition_slot"])
        for index in transitioned:
            state.by_slot.pop(index, None)
    raw_slots_value = raw_event.get("_raw_slots")
    raw_slots: Sequence[object] = raw_slots_value if isinstance(raw_slots_value, list) else ()

    # 单槽否定只在时效、代际和恢复 cutoff 内保留；不会永久锁住 conflict。
    state.by_slot = {
        index: evidence
        for index, evidence in state.by_slot.items()
        if 0 <= index < len(generations)
        and evidence.slot_generation == generations[index]
        and math.isfinite(evidence.captured_at)
        and evidence.captured_at <= observed
        and observed - evidence.captured_at <= SCENE_NEGATIVE_MAX_AGE_SECONDS
        and (cutoff <= 0.0 or evidence.captured_at >= cutoff)
    }

    inputs: list[tuple[object, bool]] = []
    for raw_slot in raw_slots:
        slot = _mapping(raw_slot)
        if isinstance(slot.get("ocr_production"), Mapping):
            inputs.append((slot["ocr_production"], True))
    completed_value = raw_event.get("_completed_ocr_evidence")
    if isinstance(completed_value, list):
        for item in completed_value:
            wrapped = _mapping(item)
            if isinstance(wrapped.get("evidence"), Mapping):
                inputs.append((wrapped["evidence"], False))

    outcomes: list[str] = []
    for raw, immediate in inputs:
        evidence, outcome = _parse_trusted(
            raw,
            session_id=session_id,
            selection_epoch=selection_epoch,
            slot_generations=generations,
            observed_at=observed,
            minimum_captured_at=cutoff,
        )
        if evidence is None:
            # 普通正面 OCR 完成不是拒绝统计，避免把每个 hextech 结果写成异常。
            if outcome != "not_scene_negative":
                outcomes.append(outcome)
            continue
        if evidence.slot_index in transitioned:
            outcomes.append("rejected_transition_frame")
            continue
        track = tracker.slots[evidence.slot_index]
        current_match = _current_slot_matches(
            raw_slots,
            evidence,
            current_frame_id=current_frame_id,
        )
        if immediate:
            binding_valid = current_match
        else:
            # 两阶段 finish 可能在当前 frame 尚未入 observation 前拿到完成结果；
            # 当前 raw slot 可完整证明绑定，旧 frame 则必须存在于近期 observation。
            binding_valid = current_match or _observation_matches(track, evidence)
        if not binding_valid:
            outcomes.append("rejected_frame_binding")
            continue
        if state.remember(evidence):
            outcomes.append("accepted")
        else:
            outcomes.append("duplicate")

    trusted = tuple(state.by_slot[index] for index in sorted(state.by_slot))
    trusted_slots = tuple(item.slot_index for item in trusted)
    names = tuple(item.name for item in trusted)
    kind: SceneNegativeKind = (
        "body_shard" if len(trusted_slots) >= 2 else "conflict" if trusted_slots else "none"
    )
    return SceneNegativeDecision(kind, trusted_slots, names, tuple(outcomes))


__all__ = [
    "BODY_SHARD_OCR_NAMES",
    "SCENE_NEGATIVE_MAX_AGE_SECONDS",
    "SCENE_NEGATIVE_MIN_CONFIDENCE",
    "SceneNegativeDecision",
    "SceneNegativeState",
    "TrustedSceneNegative",
    "classify_scene_negative",
    "clear_scene_negative",
    "evaluate_scene_negative",
    "normalize_scene_negative_text",
]

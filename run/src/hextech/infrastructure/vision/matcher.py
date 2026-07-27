"""Overlay V2 槽位候选判定策略。

本模块只消费 sidecar 已计算的 icon/SimHei/SimSun 通道分数，把单帧结果收口成
可供状态机累计的候选。它不截图、不加载模板，也不直接决定窗口显隐。

调用方: overlay.vision.state、dev_checks; 关键依赖: 见 imports。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hextech.modules.recommendation.hints import normalize_augment_id


STRONG_TEXT_MARGIN = 0.025
# 双字体一致只产生候选；是否对外 ready 统一由时序仲裁器决定。
DUAL_FONT_CONFIDENCE = 0.70
SHORTLIST_LIMIT = 3
SHORTLIST_CONFIDENCE = 0.78
SHORTLIST_CHANNEL_GAP = 0.08
SHORTLIST_COMBINED_GAP = 0.04
VARIANT_ICON_CONFIDENCE = 0.80
VARIANT_ICON_MARGIN = 0.015
OBSERVED_NAME_CONFIDENCE = 0.92
OBSERVED_NAME_MARGIN = 0.08


@dataclass(frozen=True)
class SlotCandidate:
    """单帧槽位候选结果，不可变以安全在状态机帧间传递。"""

    slot: int            # 槽位编号 0/1/2
    augment_id: str      # 增强 ID（优先）或名称
    name: str            # 增强中文名
    tier: str            # 等级：白银/黄金/棱彩
    summary: str         # 效果简述
    confidence: float    # 匹配置信度 0-1
    rule: str            # 判定规则标签（如 dual_font / strong_text / temporal_text）
    required_frames: int # 兼容字段：时序仲裁所需命中数，不表示必须连续
    evidence_grade: str  # strong / medium；weak 不会生成候选
    diagnostic: str      # 诊断标签（如 v2_dual_font）
    top_candidates: tuple[dict[str, Any], ...]  # 候选 Top-N（最多 3 个）
    channels: dict[str, Any]  # 各通道原始得分
    recognition_key: str = ""  # 跨帧只跟踪卡名，不跟踪同名视觉版本
    visual_variant_id: str = ""  # 图标证据充分时解析出的具体 CDragon 版本

    @property
    def identity(self) -> str:
        return self.recognition_key or normalize_augment_id(self.name) or self.augment_id

    def ready_slot(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "state": "ready",
            "augment_id": self.augment_id,
            "recognition_key": self.recognition_key,
            "visual_variant_id": self.visual_variant_id,
            "name": self.name,
            "tier": self.tier,
            "summary": self.summary,
            "confidence": self.confidence,
            "diagnostic": self.diagnostic,
            "top_candidates": [dict(item) for item in self.top_candidates],
            "channels": dict(self.channels),
            "acceptance_rule": self.rule,
            "evidence_grade": self.evidence_grade,
            "required_frames": self.required_frames,
        }


def _channel(slot: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    channels = slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {}
    value = channels.get(name) if isinstance(channels, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _top(channel: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = channel.get("top_candidates") if isinstance(channel.get("top_candidates"), Sequence) else []
    if isinstance(candidates, (str, bytes)) or not candidates:
        return {}
    value = candidates[0]
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _identity(candidate: Mapping[str, Any]) -> str:
    return str(
        candidate.get("recognition_key")
        or normalize_augment_id(candidate.get("name"))
        or candidate.get("augment_id")
        or ""
    ).strip()


def _shortlist_consensus(
    text: Mapping[str, Any],
    alternate: Mapping[str, Any],
) -> tuple[Mapping[str, Any], float] | None:
    """返回两字体短名单中唯一且足够接近各自 Top-1 的共同身份。"""

    def eligible(channel: Mapping[str, Any]) -> dict[str, tuple[Mapping[str, Any], float]]:
        raw = channel.get("top_candidates")
        candidates = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else []
        top_confidence = _number(candidates[0].get("confidence")) if candidates and isinstance(candidates[0], Mapping) else 0.0
        result: dict[str, tuple[Mapping[str, Any], float]] = {}
        for item in list(candidates)[:SHORTLIST_LIMIT]:
            if not isinstance(item, Mapping):
                continue
            identity = _identity(item)
            confidence = _number(item.get("confidence"))
            if (
                identity
                and confidence >= SHORTLIST_CONFIDENCE
                and top_confidence - confidence <= SHORTLIST_CHANNEL_GAP
            ):
                result.setdefault(identity, (item, confidence))
        return result

    primary = eligible(text)
    secondary = eligible(alternate)
    ranked: list[tuple[float, str, Mapping[str, Any]]] = []
    for identity in primary.keys() & secondary.keys():
        candidate, primary_confidence = primary[identity]
        _, alternate_confidence = secondary[identity]
        ranked.append(((primary_confidence + alternate_confidence) / 2.0, identity, candidate))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < SHORTLIST_COMBINED_GAP:
        return None
    return ranked[0][2], ranked[0][0]


def _visual_variant(
    slot: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> tuple[str, str]:
    """卡名唯一时直接取版本；同名多版本必须由强图标证据消歧。"""

    try:
        variant_count = max(1, int(candidate.get("name_variant_count") or 1))
    except (TypeError, ValueError):
        variant_count = 1
    if variant_count <= 1:
        return (
            str(candidate.get("visual_variant_id") or candidate.get("augment_id") or "").strip(),
            str(candidate.get("tier") or "").strip(),
        )
    icon = _channel(slot, "icon")
    icon_top = _top(icon)
    if (
        _identity(icon_top) == _identity(candidate)
        and _number(icon_top.get("confidence")) >= VARIANT_ICON_CONFIDENCE
        and _number(icon.get("margin")) >= VARIANT_ICON_MARGIN
    ):
        return (
            str(icon_top.get("visual_variant_id") or icon_top.get("augment_id") or "").strip(),
            str(icon_top.get("tier") or "").strip(),
        )
    return "", ""


def _candidate_from_top(
    slot: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
    confidence: float,
    rule: str,
    required_frames: int,
    evidence_grade: str,
) -> SlotCandidate | None:
    identity = _identity(candidate)
    if not identity:
        return None
    raw_top = evidence.get("top_candidates") if isinstance(evidence.get("top_candidates"), Sequence) else []
    top_candidates = tuple(dict(item) for item in list(raw_top)[:3] if isinstance(item, Mapping))
    channels = slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {}
    visual_variant_id, tier = _visual_variant(slot, candidate)
    # 文本身份与视觉版本分离：visual_variant_id 仍必须由强图标证据消歧，但
    # 候选自带的规范 augment_id 不应随之丢弃——否则 text_icon_disagree 路径
    # 接受的卡在 session report 里 vision_id 恒空（2026-07-26 真机 3/10 张），
    # 识别→数据的诊断链断裂。
    augment_id = str(candidate.get("augment_id") or "").strip() or visual_variant_id
    return SlotCandidate(
        slot=int(slot.get("slot") or 0),
        augment_id=augment_id,
        name=str(candidate.get("name") or identity).strip(),
        tier=tier,
        summary=str(candidate.get("summary") or "").strip(),
        confidence=confidence,
        rule=rule,
        required_frames=required_frames,
        evidence_grade=evidence_grade,
        diagnostic=f"v2_{rule}",
        top_candidates=top_candidates,
        channels={str(key): value for key, value in channels.items()},
        recognition_key=identity,
        visual_variant_id=visual_variant_id,
    )


def candidate_from_slot(slot: Mapping[str, Any]) -> SlotCandidate | None:
    """把单帧槽位分数转换为时序仲裁可消费的 observation 候选。

    决策树优先级：
    1. 脱敏真机卡名指纹 → strong observation
    2. 双字体同结果且有可靠图标 → strong observation
    3. 双字体同结果且无可靠图标冲突 → medium observation
    4. 单字体、优势字体、图标短名单与其余弱文字只保留诊断，不产生候选

    本函数不决定最终 ready/failed；公开状态只由 SelectionTracker 裁决。
    """

    text = _channel(slot, "text")
    text_alt = _channel(slot, "text_alt")
    icon = _channel(slot, "icon")
    observed_name = _channel(slot, "observed_name")
    text_top = _top(text)
    alt_top = _top(text_alt)
    icon_top = _top(icon)
    observed_top = _top(observed_name)
    text_identity = _identity(text_top)
    alt_identity = _identity(alt_top)
    observed_identity = _identity(observed_top)
    if not text_identity and not alt_identity and not observed_identity:
        return None

    text_confidence = _number(text_top.get("confidence"))
    text_margin = _number(text.get("margin"))
    alt_confidence = _number(alt_top.get("confidence"))
    alt_margin = _number(text_alt.get("margin"))
    icon_confidence = _number(icon_top.get("confidence"))
    icon_margin = _number(icon.get("margin"))
    observed_confidence = _number(observed_top.get("confidence"))
    observed_margin = _number(observed_name.get("margin"))

    if (
        observed_identity
        and observed_confidence >= OBSERVED_NAME_CONFIDENCE
        and observed_margin >= OBSERVED_NAME_MARGIN
    ):
        return _candidate_from_top(
            slot,
            observed_top,
            evidence=observed_name,
            confidence=observed_confidence,
            rule="observed_name",
            required_frames=2,
            evidence_grade="strong",
        )

    # 双字体一致仍分证据等级：没有图标/视觉版本佐证的重复文字属于相关证据，
    # 不能因为连续两帧就按 strong 提交。图标与双字体冲突时降为 medium，交给
    # 3/5 时序确认；图标通道不再绝对否决连续一致的两路文字证据。
    if (
        text_identity
        and text_identity == alt_identity
        and text_confidence >= DUAL_FONT_CONFIDENCE
        and alt_confidence >= DUAL_FONT_CONFIDENCE
    ):
        icon_support = bool(
            _identity(icon_top) == text_identity
            and icon_confidence >= VARIANT_ICON_CONFIDENCE
            and icon_margin >= VARIANT_ICON_MARGIN
        )
        strong_dual = bool(
            text_confidence >= 0.90
            and alt_confidence >= 0.90
            and text_margin >= STRONG_TEXT_MARGIN
            and alt_margin >= STRONG_TEXT_MARGIN
            and icon_support
        )
        return _candidate_from_top(
            slot,
            text_top,
            evidence=text,
            confidence=max(text_confidence, alt_confidence),
            rule="dual_font",
            required_frames=2 if strong_dual else 3,
            evidence_grade="strong" if strong_dual else "medium",
        )

    # 两字体 Top-1 分歧时，只接受双方 Top-3 内唯一、各自距 Top-1 足够近的
    # 共同身份。它仍是相关的 medium 证据，必须经过 3/5 帧，不能升级成单帧猜测。
    shortlist = _shortlist_consensus(text, text_alt)
    if shortlist is not None:
        shared_candidate, shared_confidence = shortlist
        return _candidate_from_top(
            slot,
            shared_candidate,
            evidence=text,
            confidence=shared_confidence,
            rule="dual_font_shortlist",
            required_frames=3,
            evidence_grade="medium",
        )

    # 单字体、优势字体与 shortlist 仍保留在 channels 供诊断，但不能独立授权
    # ready。真机证据表明重复观察只会复制同一系统性误匹配，并不会增加独立信息。
    return None


def arbitrate_slot_candidates(
    raw_slots: Sequence[Any],
    stable_slots: Sequence[Mapping[str, Any] | None],
    *,
    cursor_over_slots: set[int],
    slot_count: int,
) -> tuple[list[SlotCandidate | None], list[str]]:
    """跨槽抑制同身份过渡帧，只让唯一 strong 继续进入时序窗口。"""

    candidates: list[SlotCandidate | None] = []
    for index in range(slot_count):
        raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
        candidates.append(None if index in cursor_over_slots else candidate_from_slot(raw_slot))

    by_identity: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        if candidate is not None:
            by_identity.setdefault(candidate.identity, []).append(index)

    rejection_reasons = [""] * slot_count
    for identity, indexes in by_identity.items():
        if len(indexes) <= 1:
            continue
        strong_indexes = [
            index
            for index in indexes
            if candidates[index] is not None and candidates[index].evidence_grade == "strong"
        ]
        kept_index = strong_indexes[0] if len(strong_indexes) == 1 else None
        for index in indexes:
            if index == kept_index:
                continue
            candidates[index] = None
            rejection_reasons[index] = f"cross_slot_identity_conflict:{identity}"

    stable_owners: dict[str, set[int]] = {}
    for index, stable in enumerate(stable_slots):
        if stable is None:
            continue
        identity = str(
            stable.get("recognition_key")
            or normalize_augment_id(stable.get("name"))
            or stable.get("augment_id")
            or ""
        )
        if identity:
            stable_owners.setdefault(identity, set()).add(index)
    for index, candidate in enumerate(candidates):
        if candidate is None:
            continue
        owners = stable_owners.get(candidate.identity, set())
        if owners and index not in owners:
            candidates[index] = None
            rejection_reasons[index] = f"cross_slot_stable_identity_conflict:{candidate.identity}"
    return candidates, rejection_reasons


def strong_evidence_identities(slot: Mapping[str, Any]) -> set[str]:
    """返回本帧真正具有跨通道/真机样本佐证的身份，供替换滞回使用。"""

    identities: set[str] = set()
    observed = _channel(slot, "observed_name")
    observed_top = _top(observed)
    if (
        _identity(observed_top)
        and _number(observed_top.get("confidence")) >= OBSERVED_NAME_CONFIDENCE
        and _number(observed.get("margin")) >= OBSERVED_NAME_MARGIN
    ):
        identities.add(_identity(observed_top))

    text = _channel(slot, "text")
    alternate = _channel(slot, "text_alt")
    icon = _channel(slot, "icon")
    text_top = _top(text)
    alternate_top = _top(alternate)
    icon_top = _top(icon)
    identity = _identity(text_top)
    if (
        identity
        and identity == _identity(alternate_top) == _identity(icon_top)
        and _number(text_top.get("confidence")) >= 0.90
        and _number(alternate_top.get("confidence")) >= 0.90
        and _number(text.get("margin")) >= STRONG_TEXT_MARGIN
        and _number(alternate.get("margin")) >= STRONG_TEXT_MARGIN
        and _number(icon_top.get("confidence")) >= VARIANT_ICON_CONFIDENCE
        and _number(icon.get("margin")) >= VARIANT_ICON_MARGIN
    ):
        identities.add(identity)
    return identities


def unknown_slot(slot_index: int, *, diagnostic: str = "v2_detecting") -> dict[str, Any]:
    return {
        "slot": int(slot_index),
        "state": "detecting",
        "augment_id": "",
        "name": "",
        "tier": "",
        "summary": "识别中",
        "confidence": None,
        "diagnostic": diagnostic,
        "top_candidates": [],
        "channels": {},
    }

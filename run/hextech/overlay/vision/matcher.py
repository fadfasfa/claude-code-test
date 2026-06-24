"""Overlay V2 槽位候选判定策略。

本模块只消费 sidecar 已计算的 icon/SimHei/SimSun 通道分数，把单帧结果收口成
可供状态机累计的候选。它不截图、不加载模板，也不直接决定窗口显隐。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# 文字通道阈值：SimHei 单字体 >= 0.74 且 margin 够大 → 强匹配，2 帧稳定
STRONG_TEXT_CONFIDENCE = 0.74
STRONG_TEXT_MARGIN = 0.025
# 双字体（SimHei + SimSun）同结果 ≥ 0.70 → 直接确认为双字体匹配
DUAL_FONT_CONFIDENCE = 0.70
# 弱文字通道阈值：≥ 0.68 且无文字冲突 → 时间确认，3 帧稳定
WEAK_TEXT_CONFIDENCE = 0.68
WEAK_TEXT_MARGIN = 0.01
# 图标通道高冲突阈值：图标 top1 与文字 top1 不同且图标置信度 ≥ 0.90 → 拒绝弱候选
HIGH_CONFLICT_ICON_CONFIDENCE = 0.90
HIGH_CONFLICT_ICON_MARGIN = 0.03
# 图标短名单阈值：由图标通道缩窄搜索空间后的文字通道准入条件
SHORTLIST_DUAL_FONT_CONFIDENCE = 0.66
SHORTLIST_TEXT_CONFIDENCE = 0.68
SHORTLIST_TEXT_MARGIN = 0.02


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
    required_frames: int # 该规则要求连续出现多少帧才算稳定
    diagnostic: str      # 诊断标签（如 v2_dual_font）
    top_candidates: tuple[dict[str, Any], ...]  # 候选 Top-N（最多 3 个）
    channels: dict[str, Any]  # 各通道原始得分

    @property
    def identity(self) -> str:
        return self.augment_id or self.name

    def ready_slot(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "state": "ready",
            "augment_id": self.augment_id,
            "name": self.name,
            "tier": self.tier,
            "summary": self.summary,
            "confidence": self.confidence,
            "diagnostic": self.diagnostic,
            "top_candidates": [dict(item) for item in self.top_candidates],
            "channels": dict(self.channels),
            "acceptance_rule": self.rule,
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
    return str(candidate.get("augment_id") or candidate.get("name") or "").strip()


def _candidate_from_top(
    slot: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
    confidence: float,
    rule: str,
    required_frames: int,
) -> SlotCandidate | None:
    identity = _identity(candidate)
    if not identity:
        return None
    raw_top = evidence.get("top_candidates") if isinstance(evidence.get("top_candidates"), Sequence) else []
    top_candidates = tuple(dict(item) for item in list(raw_top)[:3] if isinstance(item, Mapping))
    channels = slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {}
    return SlotCandidate(
        slot=int(slot.get("slot") or 0),
        augment_id=str(candidate.get("augment_id") or "").strip(),
        name=str(candidate.get("name") or identity).strip(),
        tier=str(candidate.get("tier") or "").strip(),
        summary=str(candidate.get("summary") or "").strip(),
        confidence=confidence,
        rule=rule,
        required_frames=required_frames,
        diagnostic=f"v2_{rule}",
        top_candidates=top_candidates,
        channels=dict(channels),
    )


def candidate_from_slot(slot: Mapping[str, Any]) -> SlotCandidate | None:
    """把单帧槽位分数转换为强、双字体或弱候选。

    决策树优先级：
    1. 双字体同结果（dual_font）→ 2 帧稳定
    2. 单字体强匹配（strong_text）→ 2 帧稳定
    3. 图标短名单 + 双字体（icon_shortlist_dual_font）→ 2 帧稳定
    4. 图标短名单 + 单字体（icon_shortlist_temporal）→ 3 帧稳定
    5. 弱文字时间确认（temporal_text）→ 3 帧稳定
    """

    text = _channel(slot, "text")
    text_alt = _channel(slot, "text_alt")
    icon = _channel(slot, "icon")
    icon_shortlist = _channel(slot, "icon_shortlist")
    text_narrowed = _channel(slot, "text_narrowed")
    text_alt_narrowed = _channel(slot, "text_alt_narrowed")
    text_top = _top(text)
    alt_top = _top(text_alt)
    icon_top = _top(icon)
    narrowed_top = _top(text_narrowed)
    narrowed_alt_top = _top(text_alt_narrowed)
    text_identity = _identity(text_top)
    alt_identity = _identity(alt_top)
    if not text_identity and not alt_identity:
        return None

    text_confidence = _number(text_top.get("confidence"))
    text_margin = _number(text.get("margin"))
    alt_confidence = _number(alt_top.get("confidence"))
    alt_margin = _number(text_alt.get("margin"))
    icon_confidence = _number(icon_top.get("confidence"))
    icon_margin = _number(icon.get("margin"))

    def has_high_icon_conflict(identity: str) -> bool:
        return bool(
            identity
            and _identity(icon_top)
            and _identity(icon_top) != identity
            and icon_confidence >= HIGH_CONFLICT_ICON_CONFIDENCE
            and icon_margin >= HIGH_CONFLICT_ICON_MARGIN
        )

    text_conflict = bool(
        text_identity
        and alt_identity
        and text_identity != alt_identity
        and text_confidence >= STRONG_TEXT_CONFIDENCE
        and alt_confidence >= STRONG_TEXT_CONFIDENCE
        and (text_margin >= STRONG_TEXT_MARGIN or text_confidence >= 0.92)
        and (alt_margin >= STRONG_TEXT_MARGIN or alt_confidence >= 0.92)
    )
    narrowed_identity = _identity(narrowed_top)
    narrowed_alt_identity = _identity(narrowed_alt_top)
    shortlist_candidates = (
        icon_shortlist.get("top_candidates")
        if isinstance(icon_shortlist.get("top_candidates"), Sequence)
        else []
    )
    shortlist_identities = {
        _identity(candidate)
        for candidate in shortlist_candidates
        if isinstance(candidate, Mapping) and _identity(candidate)
    }
    # 优先级 1：双字体（SimHei + SimSun）top1 相同 → 直接确认，2 帧稳定
    if (
        text_identity
        and text_identity == alt_identity
        and text_confidence >= DUAL_FONT_CONFIDENCE
        and alt_confidence >= DUAL_FONT_CONFIDENCE
    ):
        return _candidate_from_top(
            slot,
            text_top,
            evidence=text,
            confidence=max(text_confidence, alt_confidence),
            rule="dual_font",
            required_frames=2,
        )

    # 优先级 2：单字体强匹配（置信度 ≥ 0.74 且 margin ≥ 0.025），2 帧稳定
    strong_channels = (
        (text, text_top, text_identity, text_confidence, text_margin, "strong_text"),
        (text_alt, alt_top, alt_identity, alt_confidence, alt_margin, "strong_text_alt"),
    )
    for evidence, candidate, identity, confidence, margin, rule in sorted(
        strong_channels,
        key=lambda item: item[3],
        reverse=True,
    ):
        if (
            identity
            and not text_conflict
            and confidence >= STRONG_TEXT_CONFIDENCE
            and (margin >= STRONG_TEXT_MARGIN or confidence >= 0.92)
        ):
            return _candidate_from_top(
                slot,
                candidate,
                evidence=evidence,
                confidence=confidence,
                rule=rule,
                required_frames=2,
            )

    narrowed_confidence = _number(narrowed_top.get("confidence"))
    narrowed_alt_confidence = _number(narrowed_alt_top.get("confidence"))
    narrowed_margin = _number(text_narrowed.get("margin"))
    narrowed_alt_margin = _number(text_alt_narrowed.get("margin"))
    narrowed_conflict = bool(
        narrowed_identity
        and narrowed_alt_identity
        and narrowed_identity != narrowed_alt_identity
        and narrowed_confidence >= SHORTLIST_DUAL_FONT_CONFIDENCE
        and narrowed_alt_confidence >= SHORTLIST_DUAL_FONT_CONFIDENCE
        and narrowed_margin >= SHORTLIST_TEXT_MARGIN
        and narrowed_alt_margin >= SHORTLIST_TEXT_MARGIN
    )
    # 优先级 3：图标短名单缩小搜索空间后，双字体同结果 → 2 帧稳定
    if (
        narrowed_identity
        and narrowed_identity in shortlist_identities
        and narrowed_alt_identity == narrowed_identity
        and narrowed_confidence >= SHORTLIST_DUAL_FONT_CONFIDENCE
        and narrowed_alt_confidence >= SHORTLIST_DUAL_FONT_CONFIDENCE
        and not has_high_icon_conflict(narrowed_identity)
    ):
        return _candidate_from_top(
            slot,
            narrowed_top,
            evidence=text_narrowed,
            confidence=max(narrowed_confidence, narrowed_alt_confidence),
            rule="icon_shortlist_dual_font",
            required_frames=2,
        )

    # 优先级 4：图标短名单 + 单字体（无冲突），3 帧稳定
    narrowed_channels = (
        (text_narrowed, narrowed_top, narrowed_identity, narrowed_confidence, narrowed_margin),
        (text_alt_narrowed, narrowed_alt_top, narrowed_alt_identity, narrowed_alt_confidence, narrowed_alt_margin),
    )
    for evidence, candidate, identity, confidence, margin in sorted(
        narrowed_channels,
        key=lambda item: item[3],
        reverse=True,
    ):
        if (
            identity
            and identity in shortlist_identities
            and not narrowed_conflict
            and confidence >= SHORTLIST_TEXT_CONFIDENCE
            and margin >= SHORTLIST_TEXT_MARGIN
            and not has_high_icon_conflict(identity)
        ):
            return _candidate_from_top(
                slot,
                candidate,
                evidence=evidence,
                confidence=confidence,
                rule="icon_shortlist_temporal",
                required_frames=3,
            )

    # 优先级 5：弱文字时间确认（置信度 ≥ 0.68，无冲突，无图标高冲突），3 帧稳定
    weak_channels = (
        (text, text_top, text_identity, text_confidence, text_margin, "temporal_text"),
        (text_alt, alt_top, alt_identity, alt_confidence, alt_margin, "temporal_text_alt"),
    )
    for evidence, candidate, identity, confidence, margin, rule in sorted(
        weak_channels,
        key=lambda item: item[3],
        reverse=True,
    ):
        if (
            identity
            and not text_conflict
            and confidence >= WEAK_TEXT_CONFIDENCE
            and margin >= WEAK_TEXT_MARGIN
            and not has_high_icon_conflict(identity)
        ):
            return _candidate_from_top(
                slot,
                candidate,
                evidence=evidence,
                confidence=confidence,
                rule=rule,
                required_frames=3,
            )
    return None


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

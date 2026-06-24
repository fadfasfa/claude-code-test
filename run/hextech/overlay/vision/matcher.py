"""Overlay V2 槽位候选判定策略。

本模块只消费 sidecar 已计算的 icon/SimHei/SimSun 通道分数，把单帧结果收口成
可供状态机累计的候选。它不截图、不加载模板，也不直接决定窗口显隐。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


STRONG_TEXT_CONFIDENCE = 0.74
STRONG_TEXT_MARGIN = 0.025
DUAL_FONT_CONFIDENCE = 0.70
WEAK_TEXT_CONFIDENCE = 0.68
WEAK_TEXT_MARGIN = 0.01
HIGH_CONFLICT_ICON_CONFIDENCE = 0.90
HIGH_CONFLICT_ICON_MARGIN = 0.03
SHORTLIST_DUAL_FONT_CONFIDENCE = 0.66
SHORTLIST_TEXT_CONFIDENCE = 0.68
SHORTLIST_TEXT_MARGIN = 0.02


@dataclass(frozen=True)
class SlotCandidate:
    slot: int
    augment_id: str
    name: str
    tier: str
    summary: str
    confidence: float
    rule: str
    required_frames: int
    diagnostic: str
    top_candidates: tuple[dict[str, Any], ...]
    channels: dict[str, Any]

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
    """把单帧槽位分数转换为强、双字体或弱候选。"""

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

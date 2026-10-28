"""未获验收的严格标点 OCR 策略，仅供离线比较；产品入口不得导入。

固定 truth 中漏读标点使覆盖率从 27/27 降为 26/27，且未达到人工真值及
独立 holdout 门。完整标点有机会区分不同 ID，但裸名 1322 与漏标点 3004
仍无法区分，1401 的“进/迸”也不能修复。所有结果始终 eligible=false。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import unicodedata
from typing import Any, Sequence


@dataclass(frozen=True)
class StrictOcrVocabularyEntry:
    canonical_id: str
    name: str
    normalized_name: str


def normalize_strict_ocr_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if not char.isspace())


def build_strict_ocr_vocabulary(entries: Sequence[Any]) -> tuple[StrictOcrVocabularyEntry, ...]:
    result: dict[tuple[str, str], StrictOcrVocabularyEntry] = {}
    for entry in entries:
        identity = str(getattr(entry, "canonical_id", "") or getattr(entry, "augment_id", "") or "").strip()
        name = str(getattr(entry, "name", "") or "").strip()
        key = normalize_strict_ocr_text(name)
        if identity and key:
            result.setdefault((identity, key), StrictOcrVocabularyEntry(identity, name, key))
    return tuple(result.values())


def _punctuation_missing(observed: str, expected: str) -> bool:
    if not observed or observed == expected:
        return False
    position = 0
    for char in expected:
        if position < len(observed) and observed[position] == char:
            position += 1
        elif not unicodedata.category(char).startswith("P"):
            return False
    return position == len(observed)


def match_strict_ocr_text(raw_text: object, vocabulary: Sequence[Any]) -> dict[str, Any]:
    """重新建立严格 key，不信任来自生产去标点词表的 normalized_name。"""
    entries = build_strict_ocr_vocabulary(vocabulary)
    normalized = normalize_strict_ocr_text(raw_text)
    exact = [entry for entry in entries if normalized and entry.normalized_name == normalized]
    exact_by_id = {entry.canonical_id: entry for entry in exact}
    missing = [entry for entry in entries if _punctuation_missing(normalized, entry.normalized_name)]
    candidate_ids = sorted({entry.canonical_id for entry in (*exact, *missing)})
    reason = "exact_name_missing"
    matched = None
    if len(exact_by_id) > 1:
        reason = "ambiguous_identity"
    elif missing and (not exact_by_id or len(candidate_ids) > 1):
        reason = "punctuation_ambiguous" if exact_by_id else "punctuation_missing"
    elif len(exact_by_id) == 1:
        matched = next(iter(exact_by_id.values()))
        reason = "exact"
    return {
        "policy": "strict_literal_punctuation_v1",
        "eligible": False,
        "eligibility_reason": "truth_regression_and_holdout_incomplete",
        "normalized_text": normalized,
        "matched_id": matched.canonical_id if matched is not None else "",
        "matched_name": matched.name if matched is not None else "",
        "match_rule": "exact" if matched is not None else "",
        "match_reason": reason,
        "candidate_ids": candidate_ids,
        "punctuation_collision_ids": candidate_ids if missing else [],
    }


def evaluate_strict_ocr_candidate(raw_text: object, confidence: float, vocabulary: Sequence[Any]) -> dict[str, Any]:
    """给出离线 would_admit，绝不生成生产 admitted 证据。"""
    result = match_strict_ocr_text(raw_text, vocabulary)
    reason = str(result["match_reason"])
    if not math.isfinite(confidence) or confidence < 0.95:
        reason = "low_confidence"
    would_admit = reason == "exact"
    return {**result, "state": "offline_candidate", "confidence": confidence,
            "would_admit": would_admit, "reason": "" if would_admit else reason}


def evaluate_collision_aware_candidate(raw_text: object, confidence: float, vocabulary: Sequence[Any]) -> dict[str, Any]:
    """只在跨 ID 兼容 key 碰撞组内保标点；尚未通过人工/holdout 准入。"""
    from hextech.infrastructure.vision.ocr_vocabulary import (
        OcrVocabularyEntry, match_ocr_text, normalize_ocr_text,
    )

    entries = build_strict_ocr_vocabulary(vocabulary)
    compatible = tuple(OcrVocabularyEntry(entry.canonical_id, entry.name, normalize_ocr_text(entry.name))
                       for entry in entries)
    result = match_ocr_text(raw_text, compatible)
    exact_ids = result["exact_candidate_ids"]
    if len(exact_ids) > 1:
        group = tuple(entry for entry in entries if entry.canonical_id in exact_ids
                      and normalize_ocr_text(entry.name) == normalize_ocr_text(raw_text))
        result = {**result, **match_strict_ocr_text(raw_text, group)}
    reason = str(result["match_reason"])
    if not math.isfinite(confidence) or confidence < 0.95:
        reason = "low_confidence"
    would_admit = reason == "exact" and bool(result.get("matched_id"))
    return {**result, "policy": "collision_punctuation_v1", "eligible": False,
            "eligibility_reason": "manual_truth_and_holdout_incomplete",
            "state": "offline_candidate", "confidence": confidence,
            "would_admit": would_admit, "reason": "" if would_admit else reason}

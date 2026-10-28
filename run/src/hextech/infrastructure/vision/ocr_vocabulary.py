"""生产 OCR 的纯词法 key、canonical 词表与匹配诊断。

不拥有模型推理、异步任务、图像缓存或准入状态；严格标点候选仍只在离线 tooling。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Sequence


def normalize_ocr_text(value: object) -> str:
    """保持已验收的字母数字 key；严格保标点策略只保留为离线候选。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def exact_name_key(value: object) -> str:
    """完整词法观察键；统一全半角与空白，保留标点，不做字符修正。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if not char.isspace())


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, start=1):
        current = [row]
        for column, left_char in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class OcrVocabularyEntry:
    canonical_id: str
    name: str
    normalized_name: str


def build_ocr_vocabulary(template_index: Sequence[Any]) -> tuple[OcrVocabularyEntry, ...]:
    """只从当前 production template index 建立固定词表。"""

    entries: list[OcrVocabularyEntry] = []
    seen: set[tuple[str, str]] = set()
    for template in template_index:
        canonical_id = str(getattr(template, "augment_id", "") or "").strip()
        name = str(getattr(template, "name", "") or "").strip()
        normalized = normalize_ocr_text(name)
        key = canonical_id, normalized
        if not canonical_id or not normalized or key in seen:
            continue
        seen.add(key)
        entries.append(OcrVocabularyEntry(canonical_id, name, normalized))
    return tuple(entries)


def match_ocr_text(raw_text: object, vocabulary: Sequence[OcrVocabularyEntry]) -> dict[str, Any]:
    """兼容既有 key，按 canonical ID 合并 aliases；跨 ID 碰撞拒绝。"""

    text = str(raw_text or "")
    normalized = normalize_ocr_text(text)
    exact = [
        entry
        for entry in vocabulary
        if normalized and entry.normalized_name and entry.normalized_name == normalized
    ]
    exact_by_id = {entry.canonical_id: entry for entry in sorted(exact, key=lambda entry: entry.name)}
    # This evidence is independent of confidence and containment diagnostics.
    # It can invalidate an old identity, but cannot admit a new one by itself.
    exact_candidate_ids = sorted(exact_by_id)
    candidate_ids = sorted(exact_by_id)
    # Do not pick an ID by catalog order when multiple identities share a key.
    ambiguous = len(exact_by_id) > 1
    accepted: OcrVocabularyEntry | None = (
        next(iter(exact_by_id.values())) if len(exact_by_id) == 1 else None
    )
    rule = "exact" if accepted is not None else ""
    reason = "exact_name_missing"
    if ambiguous:
        reason = "ambiguous_identity"
    if accepted is None and normalized and not ambiguous:
        contained = [
            entry
            for entry in vocabulary
            if len(entry.normalized_name) >= 3
            and entry.normalized_name in normalized
            and len(normalized) - len(entry.normalized_name) <= 2
        ]
        contained_by_id = {entry.canonical_id: entry for entry in sorted(contained, key=lambda entry: entry.name)}
        if len(contained_by_id) == 1:
            accepted = next(iter(contained_by_id.values()))
            rule = "unique_containment"
        elif len(contained_by_id) > 1:
            reason = "ambiguous_identity"
            candidate_ids = sorted(contained_by_id)

    ranked = sorted(
        (
            (_levenshtein(normalized, entry.normalized_name), abs(len(normalized) - len(entry.normalized_name)), entry)
            for entry in vocabulary
            if entry.normalized_name
        ),
        key=lambda item: (item[0], item[1], item[2].name, item[2].canonical_id),
    )
    ranked_by_id: dict[str, tuple[int, int, OcrVocabularyEntry]] = {}
    for item in ranked:
        ranked_by_id.setdefault(item[2].canonical_id, item)
    return {
        "normalized_text": normalized,
        "exact_name_key": exact_name_key(text),
        "exact_candidate_ids": exact_candidate_ids,
        "matched_id": accepted.canonical_id if accepted is not None else "",
        "matched_name": accepted.name if accepted is not None else "",
        "match_rule": rule,
        "match_reason": rule or reason,
        "candidate_ids": candidate_ids or ([accepted.canonical_id] if accepted is not None else []),
        "punctuation_collision_ids": candidate_ids if ambiguous
        and len({unicodedata.normalize("NFKC", entry.name).casefold() for entry in exact}) > 1 else [],
        "top3": [
            {
                "canonical_id": entry.canonical_id,
                "name": entry.name,
                "distance": distance,
            }
            for distance, _length_delta, entry in list(ranked_by_id.values())[:3]
        ],
    }


__all__ = ["OcrVocabularyEntry", "build_ocr_vocabulary", "exact_name_key", "match_ocr_text", "normalize_ocr_text"]

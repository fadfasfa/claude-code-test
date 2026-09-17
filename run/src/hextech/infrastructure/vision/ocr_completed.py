"""生产 OCR 完成证据的不可变身份和有界回流邮箱。"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import math
import time
from typing import Any, Mapping, Sequence
from PIL import Image

from hextech.infrastructure.vision.scene_negative import classify_scene_negative


COMPLETED_OCR_CAPACITY = 32
COMPLETED_OCR_MAX_AGE_SECONDS = 6.0


def production_needed(slot: Mapping[str, Any]) -> bool:
    reason = str(
        slot.get("rejection_reason")
        or slot.get("unstable_reason")
        or slot.get("diagnostic")
        or ""
    )
    if reason in {
        "text_icon_disagree",
        "dual_font_conflict",
        "low_confidence",
        "replacement_transition",
        "ocr_template_conflict",
    } or slot.get("transition_observation"):
        return True
    return not (
        str(slot.get("evidence_grade") or "").casefold() in {"strong", "medium"}
        and slot.get("augment_id")
    )


@dataclass(frozen=True)
class OcrEvidenceContext:
    session_id: str
    selection_epoch: int
    slot_index: int
    slot_generation: int
    input_sha256: str
    perceptual_fingerprint: str
    captured_frame_id: int
    captured_at: float
    template_candidate_id: str = ""
    template_evidence_grade: str = ""

    @property
    def work_key(self) -> tuple[str, int, int, int]:
        return (self.session_id, self.selection_epoch, self.slot_index, self.slot_generation)

    @property
    def inflight_key(self) -> tuple[str, ...]:
        return (
            "production",
            self.session_id,
            str(self.selection_epoch),
            str(self.slot_index),
            str(self.slot_generation),
            self.input_sha256,
        )


@dataclass(frozen=True)
class OcrBatch:
    input_sha256s: tuple[str, ...]
    images: tuple[Image.Image, ...]
    production_context_keys: tuple[OcrEvidenceContext | None, ...] = ()
    inflight_keys: tuple[tuple[str, ...], ...] = ()
    production: bool = False
    submitted_at: float = 0.0
    submitted_wall_at: float = 0.0


def build_inference_result(input_sha256: str, raw_text: str, confidence: float, match: Mapping[str, Any],
                           *, elapsed_ms: float, task: OcrBatch, started_at: float) -> dict[str, Any]:
    return {"state": "ready", "input_sha256": input_sha256, "raw_text": str(raw_text),
            "confidence": round(float(confidence), 6), "elapsed_ms": elapsed_ms,
            "scene_negative": classify_scene_negative(raw_text, confidence), **match,
            "inference_timing": {"batch_first_queued_at": task.submitted_wall_at,
                                 "inference_started_at": started_at,
                                 "inference_completed_at": time.time()}}


def build_production_evidence(
    result: Mapping[str, Any],
    context: OcrEvidenceContext,
    *,
    minimum_confidence: float,
    cache_hit: bool = False,
) -> dict[str, Any]:
    admitted = bool(
        str(result.get("match_rule") or "") == "exact"
        and result.get("matched_id")
        and float(result.get("confidence") or 0.0) >= minimum_confidence
    )
    scene_negative = classify_scene_negative(
        result.get("raw_text"),
        result.get("confidence"),
    )
    return {
        "schema_version": 1,
        "state": "admitted" if admitted else "rejected",
        "reason": "" if admitted else "ocr_exact_gate_rejected",
        "session_id": context.session_id,
        "selection_epoch": context.selection_epoch,
        "slot_index": context.slot_index,
        "slot_generation": context.slot_generation,
        "rgb_sha256": context.input_sha256,
        "perceptual_fingerprint": context.perceptual_fingerprint,
        "captured_frame_id": context.captured_frame_id,
        "captured_at": context.captured_at,
        "canonical_id": str(result.get("matched_id") or "") if admitted else "",
        "name": str(result.get("matched_name") or "") if admitted else "",
        "confidence": float(result.get("confidence") or 0.0),
        "match_rule": str(result.get("match_rule") or ""),
        "acceptance_rule": "ocr_exact_fallback" if admitted else "",
        "scene_negative": scene_negative,
        # Cached inference retains its original clock, never pretends to be a
        # fresh inference for a new captured frame. Context binding is separate.
        "inference_timing": dict(result.get("inference_timing") or {}),
        "inference_reused": cache_hit,
        "evidence_bound_at": time.time(),
    }


class CompletedOcrMailbox:
    """由 ``OcrShadowRuntime`` 的同一 Condition 锁保护。"""

    def __init__(self, capacity: int = COMPLETED_OCR_CAPACITY) -> None:
        self.capacity = max(1, int(capacity))
        self._items: deque[dict[str, Any]] = deque()
        self._counts: Counter[str] = Counter()

    def put(
        self,
        result: Mapping[str, Any],
        context: OcrEvidenceContext,
        *,
        minimum_confidence: float,
    ) -> None:
        evidence = build_production_evidence(
            result,
            context,
            minimum_confidence=minimum_confidence,
        )
        self._items.append(
            {
                "evidence": evidence,
                "template_candidate_id": context.template_candidate_id,
                "template_evidence_grade": context.template_evidence_grade,
            }
        )
        self._counts["enqueued"] += 1
        while len(self._items) > self.capacity:
            self._items.popleft()
            self._counts["dropped_capacity"] += 1

    def drain(
        self,
        *,
        session_id: str,
        selection_epoch: int,
        slot_generations: Sequence[int],
        observed_at: float,
        scene_open: bool,
    ) -> list[dict[str, Any]]:
        drained = list(self._items)
        self._items.clear()
        valid: list[dict[str, Any]] = []
        for item in drained:
            evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
            try:
                slot_index = int(evidence.get("slot_index", -1))
                captured_at = float(evidence.get("captured_at") or 0.0)
                context_valid = bool(
                    str(evidence.get("session_id") or "") == str(session_id or "")
                    and int(evidence.get("selection_epoch") or 0) == int(selection_epoch)
                    and 0 <= slot_index < len(slot_generations)
                    and int(evidence.get("slot_generation") or 0)
                    == int(slot_generations[slot_index])
                )
            except (TypeError, ValueError):
                context_valid = False
                captured_at = 0.0
            clock_valid = math.isfinite(captured_at) and math.isfinite(observed_at)
            if not scene_open:
                self._counts["rejected_scene"] += 1
            elif not context_valid:
                self._counts["rejected_context"] += 1
            elif not clock_valid:
                self._counts["rejected_clock"] += 1
            elif captured_at > observed_at:
                self._counts["rejected_future"] += 1
            elif captured_at <= 0.0 or observed_at - captured_at > COMPLETED_OCR_MAX_AGE_SECONDS:
                self._counts["rejected_late"] += 1
            else:
                valid.append(item)
        valid.sort(
            key=lambda item: (
                float(item["evidence"]["captured_at"]),
                int(item["evidence"]["captured_frame_id"]),
                int(item["evidence"]["slot_index"]),
            )
        )
        self._counts["delivered"] += len(valid)
        return valid

    def record_outcomes(self, outcomes: Sequence[str]) -> None:
        for outcome in outcomes:
            self._counts[f"outcome_{str(outcome or 'unknown')}"] += 1

    def clear(self) -> None:
        self._counts["dropped_shutdown"] += len(self._items)
        self._items.clear()

    def status(self) -> dict[str, int]:
        fields = {
            "completed_queue_depth": len(self._items),
            "completed_queue_capacity": self.capacity,
        }
        for key in (
            "enqueued",
            "delivered",
            "dropped_capacity",
            "dropped_shutdown",
            "rejected_scene",
            "rejected_context",
            "rejected_future",
            "rejected_late",
            "rejected_clock",
            "outcome_upgraded",
            "outcome_duplicate",
            "outcome_evidence_window_expired",
            "outcome_ocr_template_conflict",
            "outcome_cross_slot_identity_conflict",
            "outcome_ocr_result_stale",
            "outcome_ocr_exact_gate_rejected",
        ):
            fields[f"completed_{key}"] = int(self._counts[key])
        return fields


__all__ = [
    "COMPLETED_OCR_CAPACITY",
    "COMPLETED_OCR_MAX_AGE_SECONDS",
    "CompletedOcrMailbox",
    "OcrEvidenceContext",
    "build_production_evidence",
    "production_needed",
]

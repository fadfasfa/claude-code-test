"""选择终态的有界摘要；不改变识别、投票或观察限频。"""

from __future__ import annotations

import json
import logging
import math
from copy import deepcopy
from collections.abc import Mapping, Sequence
from typing import Any

from hextech.infrastructure.vision.sidecar_common import SLOT_COUNT


def _metric_contract() -> dict[str, Any]:
    return {"metric_scope": "selection_epoch",
            "time_origin": "first_active_captured_recognition_in_epoch",
            "card_latency_qualified": False,
            "card_latency_reason": "physical_card_start_not_established"}


def _ready_identity(entry: Mapping[str, Any], slot: Mapping[str, Any]) -> dict[str, Any]:
    """标记第一次 READY 的结果；不把 epoch 起点冒充该卡出现的起点。"""
    return {"canonical_id": str(slot.get("augment_id") or slot.get("canonical_id") or ""),
            "frame_id": int(entry.get("captured_frame_id") or 0),
            "slot_generation": int(slot.get("slot_generation") or 0),
            "selection_revision": int(entry.get("selection_revision") or 0),
            "captured_at": entry.get("captured_at"),
            "recognition_completed_at": entry.get("recognition_completed_at")}


class EpochRecognitionAccumulator:
    """Constant-size counters updated before timeline retention truncates frames."""

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.last_captured_at = float("-inf")
        self.observations = 0
        self.truncated = False
        self.history_complete = True
        self.first_full_ready_ms: float | None = None
        self.slots = [{"slot": index, "first_ready_ms": None, "acceptance_rule": "",
                       "first_ready_identity": None, "card_latency_ms": None,
                       "last_pending": {}, "rejection_counts": {}, "ocr_rejection_counts": {}}
                      for index in range(SLOT_COUNT)]

    def observe(self, entry: Mapping[str, Any]) -> None:
        if entry.get("observation_kind") == "diagnostic_truncated":
            self.truncated = True
            return
        captured = entry.get("captured_at")
        if (entry.get("selection_type") != "hextech" or entry.get("selection_window_active") is not True
                or entry.get("scene_state") != "active" or entry.get("observation_kind", "recognition") != "recognition"
                or entry.get("capture_status", "captured") != "captured"
                or not isinstance(captured, (float, int)) or not math.isfinite(captured)
                or captured <= self.last_captured_at):
            return
        completed = entry.get("recognition_completed_at") or captured
        if not isinstance(completed, (float, int)) or not math.isfinite(completed) or completed < captured:
            return
        if self.started_at is None:
            self.started_at = float(captured)
        self.last_captured_at = float(captured)
        self.observations += 1
        elapsed = round((completed - self.started_at) * 1000, 3)
        rows = entry.get("slots")
        if not isinstance(rows, list):
            return
        ready = 0
        for index, raw in enumerate(rows[:SLOT_COUNT]):
            if not isinstance(raw, Mapping):
                continue
            slot = self.slots[index]
            if raw.get("state") == "ready":
                ready += 1
                if slot["first_ready_ms"] is None:
                    slot.update(first_ready_ms=elapsed, acceptance_rule=str(raw.get("acceptance_rule") or "")[:80],
                                first_ready_identity=_ready_identity(entry, raw))
                continue
            production = raw.get("ocr_production")
            production = production if isinstance(production, Mapping) else {}
            reason = str(raw.get("rejection_reason") or raw.get("diagnostic") or production.get("reason") or "unknown")[:80]
            counts = slot["rejection_counts"]
            bucket = reason if reason in counts or len(counts) < 15 else "other"
            counts[bucket] = counts.get(bucket, 0) + 1
            if production.get("state") == "rejected":
                ocr_reason = str(production.get("reason") or "unknown")[:80]
                ocr_counts = slot["ocr_rejection_counts"]
                ocr_bucket = ocr_reason if ocr_reason in ocr_counts or len(ocr_counts) < 15 else "other"
                ocr_counts[ocr_bucket] = ocr_counts.get(ocr_bucket, 0) + 1
            if slot["first_ready_ms"] is None:
                slot["last_pending"] = {"elapsed_ms": elapsed, "frame_id": entry.get("captured_frame_id", 0),
                    "state": str(raw.get("temporal_state") or "")[:80], "reason": reason,
                    "evidence_hits": int(raw.get("evidence_hits") or 0),
                    "required_hits": int(raw.get("required_hits") or 0),
                    "ocr_state": str(production.get("state") or "")[:80],
                    "ocr_reason": str(production.get("reason") or "")[:80]}
        if ready == SLOT_COUNT and self.first_full_ready_ms is None:
            self.first_full_ready_ms = elapsed

    def summary(self) -> dict[str, Any]:
        return {**_metric_contract(), "observations": self.observations, "started_at": self.started_at,
                "truncated": self.truncated, "summary_from_online_observations": True,
                "history_complete": self.history_complete, "first_full_ready_ms": self.first_full_ready_ms,
                "slots": deepcopy(self.slots) if self.observations else []}


def build_epoch_recognition_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """只保存三个槽位的等待证据，不保存 OCR 文本、截图或无界历史。"""

    observations = [
        entry for entry in entries
        if entry.get("selection_type") == "hextech"
        and entry.get("selection_window_active") is True
        and entry.get("scene_state") == "active"
        and entry.get("observation_kind", "recognition") == "recognition"
        and entry.get("capture_status", "captured") == "captured"
        and isinstance(entry.get("captured_at"), (int, float))
    ]
    truncated = any(entry.get("observation_kind") == "diagnostic_truncated" for entry in entries)
    if not observations:
        return {**_metric_contract(), "observations": 0, "slots": [], "truncated": truncated}
    started_at = float(observations[0]["captured_at"])
    slots: list[dict[str, Any]] = []
    for index in range(SLOT_COUNT):
        first_ready_ms = None
        last_pending: dict[str, Any] = {}
        acceptance_rule = ""
        first_ready_identity = None
        for entry in observations:
            raw_slots = entry.get("slots")
            if not isinstance(raw_slots, list) or index >= len(raw_slots):
                continue
            slot = raw_slots[index]
            if not isinstance(slot, Mapping):
                continue
            completed_at = float(entry.get("recognition_completed_at") or entry["captured_at"])
            elapsed_ms = round(max(0.0, completed_at - started_at) * 1000, 3)
            if slot.get("state") == "ready":
                first_ready_ms = elapsed_ms
                acceptance_rule = str(slot.get("acceptance_rule") or "")[:80]
                first_ready_identity = _ready_identity(entry, slot)
                break
            production = slot.get("ocr_production")
            production = production if isinstance(production, Mapping) else {}
            shadow = slot.get("ocr_shadow")
            shadow = shadow if isinstance(shadow, Mapping) else {}
            last_pending = {
                "elapsed_ms": elapsed_ms,
                "frame_id": int(entry.get("captured_frame_id") or 0),
                "state": str(slot.get("temporal_state") or "")[:80],
                "reason": str(slot.get("rejection_reason") or slot.get("diagnostic") or "")[:80],
                "evidence_hits": int(slot.get("evidence_hits") or 0),
                "required_hits": int(slot.get("required_hits") or 0),
                "ocr_state": str(production.get("state") or shadow.get("state") or "")[:80],
                "ocr_reason": str(production.get("reason") or "")[:80],
            }
        slots.append({
            "slot": index, "first_ready_ms": first_ready_ms,
            "first_ready_identity": first_ready_identity, "card_latency_ms": None,
            "acceptance_rule": acceptance_rule, "last_pending": last_pending,
        })
    return {
        **_metric_contract(), "observations": len(observations), "started_at": started_at,
        "truncated": truncated,
        "slots": slots,
    }


def log_epoch_summary(entry: Mapping[str, Any]) -> None:
    """由 timeline writer 在成功写入唯一终态后调用，复用既有轮转日志。"""

    logging.getLogger(__name__).info(
        "vision_epoch_summary=%s",
        json.dumps({name: entry[name] for name in (
            "build_id", "sidecar_instance_id", "session_id", "selection_epoch",
            "source_reason", "epoch_latency_ms", "epoch_recognition",
        )}, ensure_ascii=False, separators=(",", ":")),
    )

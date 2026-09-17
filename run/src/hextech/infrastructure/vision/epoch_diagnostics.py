"""选择终态的有界摘要；不改变识别、投票或观察限频。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from hextech.infrastructure.vision.sidecar_common import SLOT_COUNT


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
        return {"observations": 0, "slots": [], "truncated": truncated}
    started_at = float(observations[0]["captured_at"])
    slots: list[dict[str, Any]] = []
    for index in range(SLOT_COUNT):
        first_ready_ms = None
        last_pending: dict[str, Any] = {}
        acceptance_rule = ""
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
            "acceptance_rule": acceptance_rule, "last_pending": last_pending,
        })
    return {
        "observations": len(observations), "started_at": started_at,
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

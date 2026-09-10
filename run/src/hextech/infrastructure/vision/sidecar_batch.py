"""Sidecar 单帧三槽批量指纹投影。"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from PIL import Image

from hextech.infrastructure.vision.sidecar_common import (
    NAME_FINGERPRINT_SIZE,
    TemplateEntry,
    np,
)
from hextech.infrastructure.vision.sidecar_fingerprints import (
    _grayscale_levels,
    _icon_fingerprints,
    _levels_std,
    _normalized_fingerprint,
    _text_levels,
    _text_mask_levels,
)
from hextech.infrastructure.vision.slot_evidence import slot_evidence_fingerprint
from hextech.infrastructure.vision.sidecar_matching import (
    _compute_matrices,
    _dedupe_ranked,
    _detect_slot,
    _rank_batch_with_matrix,
    _rank_matrices,
)


def _detect_slots(
    frame: Image.Image,
    boxes: Sequence[tuple[int, int, int, int]],
    template_index: Sequence[TemplateEntry],
    *,
    name_boxes: Sequence[tuple[int, int, int, int]],
    name_masks: Sequence[Image.Image | None],
    min_confidence: float,
    stable_fingerprints: Sequence[set[str]] | None = None,
    name_crops: Sequence[Image.Image] | None = None,
    eligible_slots: Sequence[bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """提取三槽指纹后按通道批量投影，避免同一大矩阵每槽重复调用。"""

    total_started = time.perf_counter()
    evidence_started = time.perf_counter()
    icon_crops: list[Image.Image] = []
    shared_name_crops = name_crops
    name_crops = []
    ineligible = {i for i in range(len(boxes))
                  if eligible_slots is not None and (i >= len(eligible_slots) or not eligible_slots[i])}
    evidence_fingerprints: list[str] = []
    for index, box in enumerate(boxes):
        icon_crop = frame.crop(box)
        name_box = name_boxes[index] if index < len(name_boxes) else None
        name_crop = (shared_name_crops[index] if shared_name_crops is not None and index < len(shared_name_crops)
                     else frame.crop(name_box) if name_box is not None else Image.new("RGB", (1, 1)))
        icon_crops.append(icon_crop)
        name_crops.append(name_crop)
        evidence_fingerprints.append(slot_evidence_fingerprint(name_crop, icon_crop) if index not in ineligible else "")
    evidence_fingerprint_ms = (time.perf_counter() - evidence_started) * 1000.0
    stable_sets = list(stable_fingerprints or ())
    skipped_slots = {
        index
        for index, fingerprint in enumerate(evidence_fingerprints)
        if index < len(stable_sets) and fingerprint in stable_sets[index]
    }
    skipped_slots.update(ineligible)

    icon_feature_started = time.perf_counter()
    icon_stds: list[float] = [0.0 for _ in boxes]
    icon_fingerprints: list[list[Any]] = [[] for _ in boxes]
    for index, icon_crop in enumerate(icon_crops):
        if index in skipped_slots:
            continue
        icon_levels = _grayscale_levels(icon_crop)
        icon_stds[index] = _levels_std(icon_levels)
        icon_fingerprints[index] = list(_icon_fingerprints(icon_crop, template=False))
    icon_feature_ms = (time.perf_counter() - icon_feature_started) * 1000.0

    name_feature_started = time.perf_counter()
    name_stds: list[float] = [0.0 for _ in boxes]
    name_fingerprints: list[Any | None] = [None for _ in boxes]
    for index, name_crop in enumerate(name_crops):
        if index in skipped_slots:
            continue
        name_mask = name_masks[index] if index < len(name_masks) else None
        name_box = name_boxes[index] if index < len(name_boxes) else None
        name_levels = (
            _text_mask_levels(name_mask, NAME_FINGERPRINT_SIZE)
            if name_mask is not None
            else (_text_levels(name_crop) if name_box is not None else [])
        )
        name_stds[index] = _levels_std(name_levels)
        name_fingerprints[index] = _normalized_fingerprint(name_levels)
    name_feature_ms = (time.perf_counter() - name_feature_started) * 1000.0

    storage = _rank_matrices(template_index)
    compute = _compute_matrices(template_index)
    icon_started = time.perf_counter()
    flat_icons: list[np.ndarray] = []
    flat_icon_slots: list[int] = []
    for slot_index, fingerprints in enumerate(icon_fingerprints):
        if slot_index in skipped_slots:
            continue
        for fingerprint in fingerprints:
            flat_icons.append(fingerprint)
            flat_icon_slots.append(slot_index)
    flat_rankings = _rank_batch_with_matrix(flat_icons, storage.icon_templates, compute.icon_matrix)
    icon_projection_ms = (time.perf_counter() - icon_started) * 1000.0
    recall_started = time.perf_counter()
    icon_rankings: list[list[tuple[TemplateEntry, float]]] = [[] for _ in boxes]
    for slot_index, ranked in zip(flat_icon_slots, flat_rankings, strict=True):
        icon_rankings[slot_index].extend(ranked)
    icon_rankings = [_dedupe_ranked(ranked, by_name=False) for ranked in icon_rankings]
    recall_top_k_ms = (time.perf_counter() - recall_started) * 1000.0

    name_started = time.perf_counter()
    projected_names = [None if index in skipped_slots else value for index, value in enumerate(name_fingerprints)]
    primary = _rank_batch_with_matrix(projected_names, storage.name_templates, compute.name_matrix)
    alternate = _rank_batch_with_matrix(projected_names, storage.alt_name_templates, compute.alt_name_matrix)
    observed = _rank_batch_with_matrix(
        projected_names,
        storage.observed_name_templates,
        compute.observed_name_matrix,
    )
    name_projection_ms = (time.perf_counter() - name_started) * 1000.0
    recall_started = time.perf_counter()
    primary = [_dedupe_ranked(ranked, by_name=True) for ranked in primary]
    alternate = [_dedupe_ranked(ranked, by_name=True) for ranked in alternate]
    observed = [_dedupe_ranked(ranked, by_name=True) for ranked in observed]
    recall_top_k_ms += (time.perf_counter() - recall_started) * 1000.0

    decision_started = time.perf_counter()
    slots: list[dict[str, Any]] = []
    for index, box in enumerate(boxes):
        if index in ineligible:
            slots.append({"slot": index, "diagnostic": "hold_slot_ineligible", "recognition_skipped": True,
                          "evidence_fingerprint": "", "channels": {}, "top_candidates": []})
            continue
        if index in skipped_slots:
            slots.append(
                {
                    "slot": index,
                    "diagnostic": "stable_visual_unchanged",
                    "recognition_skipped": True,
                    "evidence_fingerprint": evidence_fingerprints[index],
                    "channels": {},
                    "top_candidates": [],
                }
            )
            continue
        slot = _detect_slot(
            frame,
            box,
            index,
            template_index,
            name_box=name_boxes[index] if index < len(name_boxes) else None,
            name_mask=name_masks[index] if index < len(name_masks) else None,
            min_confidence=min_confidence,
            precomputed={
                "crop_std": icon_stds[index],
                "icon_ranked": icon_rankings[index],
                "name_crop_std": name_stds[index],
                "name_ranked": primary[index],
                "alt_name_ranked": alternate[index],
                "observed_name_ranked": observed[index],
            },
        )
        # 只供 Sidecar 进程内逐槽换卡确认；公开事件写入前会移除 ``_raw_slots``。
        slot["evidence_fingerprint"] = evidence_fingerprints[index]
        if str(slot.get("diagnostic") or "") == "flat_crop":
            slot["transition_observation"] = "content_absent"
        slots.append(slot)
    decision_ms = (time.perf_counter() - decision_started) * 1000.0
    return slots, {
        "compute_profile": "float32_batched",
        "fingerprint_ms": round(
            evidence_fingerprint_ms + icon_feature_ms + name_feature_ms,
            3,
        ),
        "evidence_fingerprint_ms": round(evidence_fingerprint_ms, 3),
        "icon_feature_ms": round(icon_feature_ms, 3),
        "name_feature_ms": round(name_feature_ms, 3),
        "icon_projection_ms": round(icon_projection_ms, 3),
        "name_projection_ms": round(name_projection_ms, 3),
        "recall_top_k_ms": round(recall_top_k_ms, 3),
        "decision_ms": round(decision_ms, 3),
        "skipped_stable_slots": len(skipped_slots - ineligible),
        "skipped_ineligible_slots": len(ineligible),
        "total_ms": round((time.perf_counter() - total_started) * 1000.0, 3),
    }


__all__ = ["_detect_slots"]

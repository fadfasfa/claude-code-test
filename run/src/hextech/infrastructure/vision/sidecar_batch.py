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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """提取三槽指纹后按通道批量投影，避免同一大矩阵每槽重复调用。"""

    total_started = time.perf_counter()
    fingerprint_started = time.perf_counter()
    icon_stds: list[float] = []
    icon_fingerprints: list[list[np.ndarray]] = []
    name_stds: list[float] = []
    name_fingerprints: list[np.ndarray | None] = []
    for index, box in enumerate(boxes):
        icon_crop = frame.crop(box)
        icon_levels = _grayscale_levels(icon_crop)
        icon_stds.append(_levels_std(icon_levels))
        icon_fingerprints.append(list(_icon_fingerprints(icon_crop, template=False)))
        name_mask = name_masks[index] if index < len(name_masks) else None
        name_box = name_boxes[index] if index < len(name_boxes) else None
        name_levels = (
            _text_mask_levels(name_mask, NAME_FINGERPRINT_SIZE)
            if name_mask is not None
            else (_text_levels(frame.crop(name_box)) if name_box is not None else [])
        )
        name_stds.append(_levels_std(name_levels))
        name_fingerprints.append(_normalized_fingerprint(name_levels))
    fingerprint_ms = (time.perf_counter() - fingerprint_started) * 1000.0

    storage = _rank_matrices(template_index)
    compute = _compute_matrices(template_index)
    icon_started = time.perf_counter()
    flat_icons: list[np.ndarray] = []
    flat_icon_slots: list[int] = []
    for slot_index, fingerprints in enumerate(icon_fingerprints):
        for fingerprint in fingerprints:
            flat_icons.append(fingerprint)
            flat_icon_slots.append(slot_index)
    flat_rankings = _rank_batch_with_matrix(flat_icons, storage.icon_templates, compute.icon_matrix)
    icon_rankings: list[list[tuple[TemplateEntry, float]]] = [[] for _ in boxes]
    for slot_index, ranked in zip(flat_icon_slots, flat_rankings, strict=True):
        icon_rankings[slot_index].extend(ranked)
    icon_rankings = [_dedupe_ranked(ranked, by_name=False) for ranked in icon_rankings]
    icon_projection_ms = (time.perf_counter() - icon_started) * 1000.0

    name_started = time.perf_counter()
    primary = _rank_batch_with_matrix(name_fingerprints, storage.name_templates, compute.name_matrix)
    alternate = _rank_batch_with_matrix(name_fingerprints, storage.alt_name_templates, compute.alt_name_matrix)
    observed = _rank_batch_with_matrix(
        name_fingerprints,
        storage.observed_name_templates,
        compute.observed_name_matrix,
    )
    primary = [_dedupe_ranked(ranked, by_name=True) for ranked in primary]
    alternate = [_dedupe_ranked(ranked, by_name=True) for ranked in alternate]
    observed = [_dedupe_ranked(ranked, by_name=True) for ranked in observed]
    name_projection_ms = (time.perf_counter() - name_started) * 1000.0

    decision_started = time.perf_counter()
    slots = [
        _detect_slot(
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
        for index, box in enumerate(boxes)
    ]
    decision_ms = (time.perf_counter() - decision_started) * 1000.0
    return slots, {
        "compute_profile": "float32_batched",
        "fingerprint_ms": round(fingerprint_ms, 3),
        "icon_projection_ms": round(icon_projection_ms, 3),
        "name_projection_ms": round(name_projection_ms, 3),
        "decision_ms": round(decision_ms, 3),
        "total_ms": round((time.perf_counter() - total_started) * 1000.0, 3),
    }


__all__ = ["_detect_slots"]

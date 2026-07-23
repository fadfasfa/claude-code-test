"""Vision sidecar matching 职责模块。"""
from __future__ import annotations

from hextech.infrastructure.vision.sidecar_common import (
    Any,
    DEFAULT_MIN_MARGIN,
    DEFAULT_TEXT_MIN_CONFIDENCE,
    DEFAULT_TEXT_MIN_MARGIN,
    FLAT_CROP_STD_THRESHOLD,
    ICON_SHORTLIST_MAX_DELTA,
    ICON_SHORTLIST_MAX_GROUPS,
    ICON_SHORTLIST_MIN_CONFIDENCE,
    Image,
    Mapping,
    NAME_FINGERPRINT_SIZE,
    SCENE_SLOT_MIN_CONFIDENCE,
    TWIN_CONFIDENCE_OVERRIDE,
    Sequence,
    TemplateEntry,
    TemplateIndex,
    _RankMatrices,
    normalize_augment_id,
    np,
)
from hextech.infrastructure.vision.sidecar_fingerprints import (
    _RANK_MATRIX_CACHE,
    _RANK_MATRIX_CACHE_MAX,
    _cleaned_name_fingerprint,
    _grayscale_levels,
    _icon_fingerprints,
    _levels_std,
    _normalized_fingerprint,
    _text_levels,
    _text_mask_levels,
)

def _fingerprint_row(value: object) -> np.ndarray | None:
    """把构建期输入即时压入连续 float16 行，不保留 Python float tuple。"""

    try:
        row = np.asarray(value, dtype=np.float16)
    except (TypeError, ValueError):
        return None
    if row.ndim != 1 or row.size == 0 or not np.isfinite(row).all():
        return None
    return np.ascontiguousarray(row)


def _stack_fingerprints(rows: Sequence[object]) -> np.ndarray:
    prepared = [row for value in rows if (row := _fingerprint_row(value)) is not None]
    if not prepared:
        return np.empty((0, 0), dtype=np.float16)
    width = int(prepared[0].shape[0])
    same_width = [row for row in prepared if int(row.shape[0]) == width]
    return np.ascontiguousarray(np.stack(same_width), dtype=np.float16)


def _unique_fingerprint_rows(values: Sequence[object]) -> list[np.ndarray]:
    rows: list[np.ndarray] = []
    seen: set[bytes] = set()
    for value in values:
        row = _fingerprint_row(value)
        if row is None:
            continue
        digest = row.tobytes()
        if digest not in seen:
            seen.add(digest)
            rows.append(row)
    return rows


def _rank_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    attached = getattr(template_index, "rank_matrices", None)
    if isinstance(attached, _RankMatrices) and attached.index_ref is template_index:
        return attached
    key = id(template_index)
    cached = _RANK_MATRIX_CACHE.get(key)
    if cached is not None and cached.index_ref is template_index:
        return cached
    icon_rows: list[tuple[TemplateEntry, np.ndarray]] = []
    for template in template_index:
        variants = template.icon_fingerprints
        if not variants and template.fingerprint is not None:
            variants = (template.fingerprint,)
        icon_rows.extend((template, fingerprint) for fingerprint in variants)
    icon_templates = tuple(template for template, _fingerprint_row in icon_rows)
    icon_matrix = _stack_fingerprints([fingerprint_row for _template, fingerprint_row in icon_rows])
    name_rows: list[tuple[TemplateEntry, np.ndarray]] = []
    alt_name_rows: list[tuple[TemplateEntry, np.ndarray]] = []
    for template in template_index:
        primary_variants = _unique_fingerprint_rows(
            (template.name_fingerprint, _cleaned_name_fingerprint(template.name))
        )
        alt_variants = _unique_fingerprint_rows(
            (template.name_fingerprint_alt, _cleaned_name_fingerprint(template.name, family="alt"))
        )
        name_rows.extend((template, fingerprint) for fingerprint in primary_variants)
        alt_name_rows.extend((template, fingerprint) for fingerprint in alt_variants)
    name_templates = tuple(template for template, _fingerprint_row in name_rows)
    name_matrix = _stack_fingerprints([fingerprint_row for _template, fingerprint_row in name_rows])
    alt_name_templates = tuple(template for template, _fingerprint_row in alt_name_rows)
    alt_name_matrix = _stack_fingerprints([fingerprint_row for _template, fingerprint_row in alt_name_rows])
    observed_name_rows = [
        (template, fingerprint)
        for template in template_index
        for fingerprint in template.observed_name_fingerprints
        if fingerprint is not None
    ]
    observed_name_templates = tuple(template for template, _fingerprint_row in observed_name_rows)
    observed_name_matrix = _stack_fingerprints(
        [fingerprint_row for _template, fingerprint_row in observed_name_rows]
    )
    entry = _RankMatrices(
        template_index,
        icon_templates,
        icon_matrix,
        name_templates,
        name_matrix,
        alt_name_templates,
        alt_name_matrix,
        observed_name_templates,
        observed_name_matrix,
    )
    if key not in _RANK_MATRIX_CACHE and len(_RANK_MATRIX_CACHE) >= _RANK_MATRIX_CACHE_MAX:
        _RANK_MATRIX_CACHE.pop(next(iter(_RANK_MATRIX_CACHE)))
    _RANK_MATRIX_CACHE[key] = entry
    if isinstance(template_index, TemplateIndex):
        template_index.rank_matrices = entry
    return entry


def _rank_with_matrix(
    crop_fingerprint: Sequence[float],
    templates: Sequence[TemplateEntry],
    matrix: np.ndarray,
) -> list[tuple[TemplateEntry, float]]:
    """向量化 NCC：指纹已零均值/单位方差，置信度 = clip((M·v / D + 1) / 2)。"""

    if not templates or matrix.size == 0:
        return []
    vec = np.ascontiguousarray(np.asarray(crop_fingerprint, dtype=matrix.dtype))
    if vec.shape[0] != matrix.shape[1]:
        return []
    correlation = np.asarray((matrix @ vec) / vec.shape[0], dtype=np.float32)
    confidence = np.clip((correlation + 1.0) / 2.0, 0.0, 1.0)
    # argsort 升序取负 = 置信度降序；stable 保持模板原顺序，与旧 sorted(reverse=True) 对齐。
    order = np.argsort(-confidence, kind="stable")
    return [(templates[int(i)], float(confidence[int(i)])) for i in order]


def _rank_templates(
    crop: Image.Image,
    template_index: Sequence[TemplateEntry],
) -> tuple[float, list[tuple[TemplateEntry, float]]]:
    """返回图标 crop 形状标准差与按置信度降序的模板候选；平坦 crop 没有候选。"""

    levels = _grayscale_levels(crop)
    crop_std = _levels_std(levels)
    crop_fingerprints = _icon_fingerprints(crop, template=False)
    if not crop_fingerprints:
        return crop_std, []
    matrices = _rank_matrices(template_index)
    best_by_identity: dict[str, tuple[TemplateEntry, float]] = {}
    for crop_fingerprint in crop_fingerprints:
        for template, confidence in _rank_with_matrix(
            crop_fingerprint,
            matrices.icon_templates,
            matrices.icon_matrix,
        ):
            identity = template.augment_id or template.name
            previous = best_by_identity.get(identity)
            if previous is None or confidence > previous[1]:
                best_by_identity[identity] = (template, confidence)
    return crop_std, sorted(
        best_by_identity.values(),
        key=lambda item: (-item[1], -item[0].priority, item[0].name),
    )


def _rank_name_templates(
    crop: Image.Image | None,
    template_index: Sequence[TemplateEntry],
    *,
    family: str = "primary",
) -> tuple[float, list[tuple[TemplateEntry, float]]]:
    if crop is None:
        return 0.0, []
    levels = _text_levels(crop)
    crop_std = _levels_std(levels)
    crop_fingerprint = _normalized_fingerprint(levels)
    if crop_fingerprint is None:
        return crop_std, []
    return crop_std, _rank_name_fingerprint(crop_fingerprint, template_index, family=family)


def _rank_name_fingerprint(
    crop_fingerprint: Sequence[float],
    template_index: Sequence[TemplateEntry],
    *,
    family: str,
) -> list[tuple[TemplateEntry, float]]:
    """同一名称 ROI 只分割一次，再分别投影到 SimHei / SimSun 模板矩阵。"""

    matrices = _rank_matrices(template_index)
    if family == "alt":
        ranked = _rank_with_matrix(
            crop_fingerprint,
            matrices.alt_name_templates,
            matrices.alt_name_matrix,
        )
    else:
        ranked = _rank_with_matrix(crop_fingerprint, matrices.name_templates, matrices.name_matrix)
    # 文字只识别卡名。同名视觉版本若按 augment_id 分开参与排序，会把完全相同的
    # 文字模板当作 runner-up，错误压低 margin。
    best_by_identity: dict[str, tuple[TemplateEntry, float]] = {}
    for template, confidence in ranked:
        identity = normalize_augment_id(template.name)
        previous = best_by_identity.get(identity)
        if previous is None or confidence > previous[1]:
            best_by_identity[identity] = (template, confidence)
    return sorted(best_by_identity.values(), key=lambda item: (-item[1], -item[0].priority, item[0].name))


def _rank_observed_name_fingerprint(
    crop_fingerprint: Sequence[float],
    template_index: Sequence[TemplateEntry],
) -> list[tuple[TemplateEntry, float]]:
    """只匹配脱敏真机卡名样本；与图标 shortlist 完全独立。"""

    matrices = _rank_matrices(template_index)
    ranked = _rank_with_matrix(
        crop_fingerprint,
        matrices.observed_name_templates,
        matrices.observed_name_matrix,
    )
    best_by_identity: dict[str, tuple[TemplateEntry, float]] = {}
    for template, confidence in ranked:
        identity = normalize_augment_id(template.name)
        previous = best_by_identity.get(identity)
        if previous is None or confidence > previous[1]:
            best_by_identity[identity] = (template, confidence)
    return sorted(best_by_identity.values(), key=lambda item: (-item[1], -item[0].priority, item[0].name))


def _slot_match_decision(
    crop_std: float,
    confidence: float,
    margin: float,
    *,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
) -> bool:
    """槽位判定：平坦 crop 与低置信度直接拒绝；区分度不足时只接受极高置信度（孪生图标）。"""

    if crop_std < FLAT_CROP_STD_THRESHOLD or confidence < min_confidence:
        return False
    return margin >= min_margin or confidence >= TWIN_CONFIDENCE_OVERRIDE


def _top_candidates(
    ranked: Sequence[tuple[TemplateEntry, float]],
    *,
    limit: int = 3,
) -> list[dict[str, Any]]:
    return [
        {
            "augment_id": template.augment_id,
            "name": template.name,
            "recognition_key": normalize_augment_id(template.name),
            "visual_variant_id": template.augment_id,
            "name_variant_count": int(template.name_variant_count),
            "tier": template.tier,
            "summary": template.summary,
            "confidence": confidence,
            "icon_digest": template.icon_digest,
            "priority": template.priority,
        }
        for template, confidence in list(ranked)[: max(0, int(limit))]
    ]


def _template_group_key(template: TemplateEntry) -> str:
    return template.icon_digest or template.augment_id or template.name


def _build_icon_shortlist(
    ranked: Sequence[tuple[TemplateEntry, float]],
    *,
    min_confidence: float = ICON_SHORTLIST_MIN_CONFIDENCE,
    max_delta: float = ICON_SHORTLIST_MAX_DELTA,
    max_groups: int = ICON_SHORTLIST_MAX_GROUPS,
) -> list[tuple[TemplateEntry, float]]:
    """选择高分图标组；共享 digest 下的名称必须一起保留。"""

    if not ranked or max_groups <= 0:
        return []
    top_confidence = float(ranked[0][1])
    selected_groups: list[str] = []
    for template, confidence in ranked:
        if float(confidence) < min_confidence or top_confidence - float(confidence) > max_delta:
            continue
        group_key = _template_group_key(template)
        if group_key not in selected_groups:
            if len(selected_groups) >= max_groups:
                continue
            selected_groups.append(group_key)
    selected = set(selected_groups)
    return [
        (template, confidence)
        for template, confidence in ranked
        if _template_group_key(template) in selected
    ]


def _narrow_ranked_by_icon_shortlist(
    ranked: Sequence[tuple[TemplateEntry, float]],
    icon_shortlist: Sequence[tuple[TemplateEntry, float]],
) -> list[tuple[TemplateEntry, float]]:
    selected_groups = {_template_group_key(template) for template, _confidence in icon_shortlist}
    return [
        (template, confidence)
        for template, confidence in ranked
        if _template_group_key(template) in selected_groups
    ]


def _candidate_margin(ranked: Sequence[tuple[TemplateEntry, float]]) -> float:
    if not ranked:
        return 0.0
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    return ranked[0][1] - runner_up


def _channels_payload(
    *,
    icon_crop_std: float,
    icon_ranked: Sequence[tuple[TemplateEntry, float]],
    name_crop_std: float,
    name_ranked: Sequence[tuple[TemplateEntry, float]],
    alt_name_crop_std: float,
    alt_name_ranked: Sequence[tuple[TemplateEntry, float]],
    icon_shortlist: Sequence[tuple[TemplateEntry, float]],
    narrowed_name_ranked: Sequence[tuple[TemplateEntry, float]],
    narrowed_alt_name_ranked: Sequence[tuple[TemplateEntry, float]],
    observed_name_ranked: Sequence[tuple[TemplateEntry, float]],
) -> dict[str, Any]:
    return {
        "icon": {
            "crop_std": round(icon_crop_std, 3),
            "margin": round(_candidate_margin(icon_ranked), 4),
            "top_candidates": _top_candidates(icon_ranked),
        },
        "text": {
            "crop_std": round(name_crop_std, 3),
            "margin": round(_candidate_margin(name_ranked), 4),
            "top_candidates": _top_candidates(name_ranked),
        },
        "text_alt": {
            "crop_std": round(alt_name_crop_std, 3),
            "margin": round(_candidate_margin(alt_name_ranked), 4),
            "top_candidates": _top_candidates(alt_name_ranked),
        },
        "icon_shortlist": {
            "group_count": len({_template_group_key(template) for template, _confidence in icon_shortlist}),
            "top_candidates": _top_candidates(icon_shortlist, limit=len(icon_shortlist)),
        },
        "text_narrowed": {
            "margin": round(_candidate_margin(narrowed_name_ranked), 4),
            "top_candidates": _top_candidates(narrowed_name_ranked),
        },
        "text_alt_narrowed": {
            "margin": round(_candidate_margin(narrowed_alt_name_ranked), 4),
            "top_candidates": _top_candidates(narrowed_alt_name_ranked),
        },
        "observed_name": {
            "margin": round(_candidate_margin(observed_name_ranked), 4),
            "top_candidates": _top_candidates(observed_name_ranked),
        },
    }


def _slot_result(
    *,
    slot_index: int,
    state: str,
    template: TemplateEntry | None,
    confidence: float,
    diagnostic: str,
    top_candidates: Sequence[dict[str, Any]],
    channels: Mapping[str, Any],
    summary: str,
) -> dict[str, Any]:
    return {
        "slot": slot_index,
        "state": state,
        "augment_id": template.augment_id if template is not None and state == "ready" else "",
        "name": template.name if template is not None and state == "ready" else "",
        "tier": template.tier if template is not None and state == "ready" else "",
        "summary": template.summary if template is not None and state == "ready" else summary,
        "confidence": confidence,
        "diagnostic": diagnostic,
        "top_candidates": list(top_candidates),
        "channels": dict(channels),
    }


def _detect_slot(
    frame: Image.Image,
    box: tuple[int, int, int, int],
    slot_index: int,
    template_index: Sequence[TemplateEntry],
    *,
    name_box: tuple[int, int, int, int] | None = None,
    name_mask: Image.Image | None = None,
    min_confidence: float,
    min_margin: float = DEFAULT_MIN_MARGIN,
    min_text_confidence: float = DEFAULT_TEXT_MIN_CONFIDENCE,
    min_text_margin: float = DEFAULT_TEXT_MIN_MARGIN,
) -> dict[str, Any]:
    """单槽位双通道识别：图标指纹匹配 + SimHei/SimSun 双字体文字匹配。

    流程：
    1. 图标通道：截取卡片图标区域 → 灰度指纹 → 与模板库排名
    2. 文字通道：截取名称区域 → SimHei(主)/SimSun(备) 分别渲染 → 与截屏文字指纹排名
    3. 图标短名单：用图标通道 Top-N 缩窄文字候选空间
    4. 三通道结果汇入 channels 字典，供 matcher.candidate_from_slot 做最终判定
    """
    crop_std, ranked = _rank_templates(frame.crop(box), template_index)
    name_levels = (
        _text_mask_levels(name_mask, NAME_FINGERPRINT_SIZE)
        if name_mask is not None
        else (_text_levels(frame.crop(name_box)) if name_box is not None else [])
    )
    name_crop_std = _levels_std(name_levels)
    name_fingerprint = _normalized_fingerprint(name_levels)
    if name_fingerprint is None:
        name_ranked: list[tuple[TemplateEntry, float]] = []
        alt_name_ranked: list[tuple[TemplateEntry, float]] = []
        observed_name_ranked: list[tuple[TemplateEntry, float]] = []
    else:
        name_ranked = _rank_name_fingerprint(name_fingerprint, template_index, family="primary")
        alt_name_ranked = _rank_name_fingerprint(name_fingerprint, template_index, family="alt")
        observed_name_ranked = _rank_observed_name_fingerprint(name_fingerprint, template_index)
    alt_name_crop_std = name_crop_std
    icon_template, icon_confidence = ranked[0] if ranked else (None, 0.0)
    name_template, name_confidence = name_ranked[0] if name_ranked else (None, 0.0)
    alt_name_template, alt_name_confidence = alt_name_ranked[0] if alt_name_ranked else (None, 0.0)
    icon_margin = _candidate_margin(ranked)
    name_margin = _candidate_margin(name_ranked)
    alt_name_margin = _candidate_margin(alt_name_ranked)
    icon_shortlist = _build_icon_shortlist(ranked)
    narrowed_name_ranked = _narrow_ranked_by_icon_shortlist(name_ranked, icon_shortlist)
    narrowed_alt_name_ranked = _narrow_ranked_by_icon_shortlist(alt_name_ranked, icon_shortlist)
    icon_candidates = _top_candidates(ranked)
    name_candidates = _top_candidates(name_ranked)
    channels = _channels_payload(
        icon_crop_std=crop_std,
        icon_ranked=ranked,
        name_crop_std=name_crop_std,
        name_ranked=name_ranked,
        alt_name_crop_std=alt_name_crop_std,
        alt_name_ranked=alt_name_ranked,
        icon_shortlist=icon_shortlist,
        narrowed_name_ranked=narrowed_name_ranked,
        narrowed_alt_name_ranked=narrowed_alt_name_ranked,
        observed_name_ranked=observed_name_ranked,
    )
    name_box_present = name_box is not None
    has_name_channel = name_box_present and bool(name_ranked)
    primary_text_ready = (
        name_template is not None
        and name_crop_std >= FLAT_CROP_STD_THRESHOLD
        and name_confidence >= min_text_confidence
        and (name_margin >= min_text_margin or name_confidence >= TWIN_CONFIDENCE_OVERRIDE)
    )
    alt_text_ready = (
        alt_name_template is not None
        and alt_name_crop_std >= FLAT_CROP_STD_THRESHOLD
        and alt_name_confidence >= min_text_confidence
        and (alt_name_margin >= min_text_margin or alt_name_confidence >= TWIN_CONFIDENCE_OVERRIDE)
    )
    dual_font_ready = bool(
        name_template is not None
        and alt_name_template is not None
        and (name_template.augment_id or name_template.name) == (alt_name_template.augment_id or alt_name_template.name)
        and name_confidence >= 0.70
        and alt_name_confidence >= 0.70
    )
    icon_ready = (
        icon_template is not None
        and _slot_match_decision(crop_std, icon_confidence, icon_margin, min_confidence=min_confidence, min_margin=min_margin)
    )
    icon_supports_text = bool(
        icon_template is not None
        and (name_template is not None or alt_name_template is not None)
        and (
            icon_template.name in {
                template.name
                for template in (name_template, alt_name_template)
                if template is not None
            }
            or (
                icon_template.icon_digest
                and icon_template.icon_digest
                in {
                    template.icon_digest
                    for template in (name_template, alt_name_template)
                    if template is not None
                }
            )
        )
    )
    candidates = name_candidates if name_candidates else icon_candidates
    if crop_std < FLAT_CROP_STD_THRESHOLD:
        return _slot_result(
            slot_index=slot_index,
            state="empty",
            template=None,
            confidence=max(icon_confidence, name_confidence),
            diagnostic="flat_crop",
            top_candidates=candidates,
            channels=channels,
            summary="未检测到选择卡片",
        )
    text_conflict = bool(
        primary_text_ready
        and alt_text_ready
        and name_template is not None
        and alt_name_template is not None
        and (name_template.augment_id or name_template.name) != (alt_name_template.augment_id or alt_name_template.name)
    )
    selected_text: tuple[TemplateEntry, float, list[dict[str, Any]]] | None = None
    if dual_font_ready and name_template is not None:
        selected_text = (
            name_template,
            max(name_confidence, alt_name_confidence),
            name_candidates if name_confidence >= alt_name_confidence else _top_candidates(alt_name_ranked),
        )
    elif text_conflict:
        dominant_texts = [
            (template, confidence, candidate_rows)
            for template, confidence, candidate_rows in (
                (name_template, name_confidence, name_candidates),
                (alt_name_template, alt_name_confidence, _top_candidates(alt_name_ranked)),
            )
            if template is not None and confidence >= 0.95
        ]
        if dominant_texts:
            selected_template, selected_confidence, selected_candidates = max(dominant_texts, key=lambda item: item[1])
            other_confidence = alt_name_confidence if selected_template is name_template else name_confidence
            if selected_confidence - other_confidence >= 0.10:
                selected_text = (selected_template, selected_confidence, selected_candidates)
    elif not text_conflict:
        ready_texts = [
            (template, confidence, candidate_rows)
            for template, confidence, candidate_rows, ready in (
                (name_template, name_confidence, name_candidates, primary_text_ready),
                (alt_name_template, alt_name_confidence, _top_candidates(alt_name_ranked), alt_text_ready),
            )
            if template is not None and ready
        ]
        if ready_texts:
            selected_text = max(ready_texts, key=lambda item: item[1])

    if selected_text is not None:
        selected_template, selected_confidence, selected_candidates = selected_text
        diagnostic = "dual_font_ready" if dual_font_ready else (
            "text_icon_agree" if icon_supports_text else "text_channel_ready"
        )
        if icon_template is not None and not icon_supports_text and icon_confidence >= SCENE_SLOT_MIN_CONFIDENCE:
            diagnostic = "text_icon_disagree"
        return _slot_result(
            slot_index=slot_index,
            state="ready",
            template=selected_template,
            confidence=selected_confidence,
            diagnostic=diagnostic,
            top_candidates=selected_candidates,
            channels=channels,
            summary="",
        )
    # V2 不允许 icon-only 授权显示；图标只用于收窄候选和佐证文字。
    if icon_template is None:
        return _slot_result(
            slot_index=slot_index,
            state="detecting",
            template=None,
            confidence=max(icon_confidence, name_confidence),
            diagnostic="template_candidate_missing",
            top_candidates=candidates,
            channels=channels,
            summary="识别中",
        )
    if icon_confidence < min_confidence and max(name_confidence, alt_name_confidence) < min_text_confidence:
        best_confidence = max(icon_confidence, name_confidence, alt_name_confidence)
        state = "low_confidence" if best_confidence >= SCENE_SLOT_MIN_CONFIDENCE else "detecting"
        return _slot_result(
            slot_index=slot_index,
            state=state,
            template=None,
            confidence=best_confidence,
            diagnostic="confidence_below_threshold",
            top_candidates=candidates,
            channels=channels,
            summary="候选置信度不足",
        )
    diagnostic = "icon_only_low_confidence" if icon_ready else "margin_below_threshold"
    if name_box_present and not has_name_channel and icon_ready:
        diagnostic = "text_channel_missing"
    return _slot_result(
        slot_index=slot_index,
        state="low_confidence",
        template=None,
        confidence=max(icon_confidence, name_confidence, alt_name_confidence),
        diagnostic=diagnostic,
        top_candidates=candidates,
        channels=channels,
        summary="候选区分度不足",
    )



__all__ = [name for name in globals() if not name.startswith("__")]

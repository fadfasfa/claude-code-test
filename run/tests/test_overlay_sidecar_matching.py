"""验证 sidecar 连续矩阵的精度和模板输入边界。"""

from __future__ import annotations

import numpy as np
import pytest

from hextech.infrastructure.vision.sidecar_matching import (
    _rank_batch_with_matrix,
    _rank_with_matrix,
    prepare_compute_rank_matrices,
    _stack_fingerprints,
)
from hextech.infrastructure.vision.template_models import TemplateEntry, TemplateIndex, _RankMatrices


def test_stack_fingerprints_keeps_contiguous_float16_matrix() -> None:
    matrix = _stack_fingerprints(((0.0, 1.0, -1.0), (0.5, -0.5, 0.25)))

    assert matrix.dtype == np.float16
    assert matrix.flags.c_contiguous


def test_rank_with_matrix_accumulates_with_float32_crop_vector() -> None:
    base = np.linspace(-0.996, 0.996, 257, dtype=np.float32)
    matrix = np.ascontiguousarray(
        np.stack((base, base[::-1] * np.float32(0.871) + np.float32(0.062))).astype(np.float16)
    )
    crop = np.sin(np.linspace(-2.7, 2.1, 257, dtype=np.float32)).astype(np.float32)

    ranked = _rank_with_matrix(crop, ("first", "second"), matrix)
    expected = np.clip(((matrix.astype(np.float32) @ crop) / crop.size + 1.0) / 2.0, 0.0, 1.0)
    scores = {template: score for template, score in ranked}

    np.testing.assert_allclose(
        np.asarray([scores["first"], scores["second"]], dtype=np.float32),
        expected,
        rtol=0.0,
        atol=1e-7,
    )


def test_stack_fingerprints_rejects_mismatched_widths() -> None:
    with pytest.raises(ValueError, match="模板指纹宽度不一致"):
        _stack_fingerprints(((0.0, 1.0), (0.0, 1.0, 2.0)))


def test_float32_compute_mirror_is_built_once_per_template_index() -> None:
    entry = TemplateEntry("one", "一", "Gold", "")
    index = TemplateIndex([entry])
    storage = np.ascontiguousarray([[1.0, 0.0, -1.0]], dtype=np.float16)
    empty = np.empty((0, 0), dtype=np.float16)
    index.rank_matrices = _RankMatrices(
        index,
        (entry,),
        storage,
        (),
        empty,
        (),
        empty,
        (),
        empty,
    )

    first = prepare_compute_rank_matrices(index)
    compute = index.compute_rank_matrices
    second = prepare_compute_rank_matrices(index)

    assert first["compute_profile"] == "float32_batched"
    assert second == first
    assert index.compute_rank_matrices is compute
    assert compute.icon_matrix.dtype == np.float32  # type: ignore[union-attr]
    assert compute.icon_matrix.flags.c_contiguous  # type: ignore[union-attr]


def test_three_slot_batch_matches_per_slot_float32_reference_top_three() -> None:
    rng = np.random.default_rng(20260724)
    matrix = np.ascontiguousarray(rng.normal(size=(12, 257)), dtype=np.float32)
    vectors = [np.ascontiguousarray(rng.normal(size=257), dtype=np.float32) for _ in range(3)]
    templates = tuple(f"template-{index}" for index in range(matrix.shape[0]))

    batched = _rank_batch_with_matrix(vectors, templates, matrix)
    references = [_rank_with_matrix(vector, templates, matrix) for vector in vectors]

    for batch_rows, reference_rows in zip(batched, references, strict=True):
        assert [identity for identity, _score in batch_rows[:3]] == [
            identity for identity, _score in reference_rows[:3]
        ]
        np.testing.assert_allclose(
            [score for _identity, score in batch_rows[:3]],
            [score for _identity, score in reference_rows[:3]],
            rtol=0.0,
            atol=1e-4,
        )

"""验证 sidecar 连续矩阵的精度和模板输入边界。"""

from __future__ import annotations

import numpy as np
import pytest

from hextech.infrastructure.vision.sidecar_matching import _rank_with_matrix, _stack_fingerprints


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

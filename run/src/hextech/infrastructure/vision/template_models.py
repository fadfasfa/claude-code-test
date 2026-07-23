"""Vision 模板运行时的紧凑数据模型。

完整指纹只在构建阶段以连续 ``numpy`` 矩阵存在；发布到 sidecar 的
``TemplateEntry`` 仅保留身份和索引元数据，避免把每个浮点数展开成 Python tuple。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Sequence

import numpy as np


TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION = 5
TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE = np.float16


def _fingerprint_array(value: Any) -> np.ndarray | None:
    """把构建输入收窄为一维连续 float16 数组，不长期保留 Python float 容器。"""

    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        matrix = np.asarray(value, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE)
    except (TypeError, ValueError):
        return None
    if matrix.ndim != 1 or matrix.size == 0 or not np.isfinite(matrix).all():
        return None
    return np.ascontiguousarray(matrix)


def _fingerprint_arrays(value: Any) -> tuple[np.ndarray, ...]:
    """归一化多变体构建输入；数组元素始终由 NumPy 管理。"""

    if value is None or isinstance(value, (str, bytes)):
        return ()
    if isinstance(value, np.ndarray):
        if value.ndim == 1:
            item = _fingerprint_array(value)
            return (item,) if item is not None else ()
        if value.ndim == 2:
            return tuple(item for row in value if (item := _fingerprint_array(row)) is not None)
        return ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for row in value if (item := _fingerprint_array(row)) is not None)


@dataclass(frozen=True)
class TemplateEntry:
    """单个海克斯的身份元数据及仅构建期可用的 NumPy 指纹行。"""

    augment_id: str
    name: str
    tier: str
    summary: str
    # 这些字段在 cache 命中和 cold build 发布后都会清空；矩阵由 _RankMatrices 唯一持有。
    fingerprint: np.ndarray | None = field(default=None, repr=False, compare=False)
    icon_fingerprints: tuple[np.ndarray, ...] = field(default_factory=tuple, repr=False, compare=False)
    icon_digest: str = ""
    priority: int = 0
    name_fingerprint: np.ndarray | None = field(default=None, repr=False, compare=False)
    name_fingerprint_alt: np.ndarray | None = field(default=None, repr=False, compare=False)
    observed_name_fingerprints: tuple[np.ndarray, ...] = field(default_factory=tuple, repr=False, compare=False)
    source_icon_filenames: tuple[str, ...] = ()
    text_only_icon_filenames: tuple[str, ...] = ()
    name_variant_count: int = 1
    icon_variant_count: int = 0
    observed_name_variant_count: int = 0

    def __post_init__(self) -> None:
        fingerprint = _fingerprint_array(self.fingerprint)
        icon_fingerprints = _fingerprint_arrays(self.icon_fingerprints)
        name_fingerprint = _fingerprint_array(self.name_fingerprint)
        name_fingerprint_alt = _fingerprint_array(self.name_fingerprint_alt)
        observed_name_fingerprints = _fingerprint_arrays(self.observed_name_fingerprints)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "icon_fingerprints", icon_fingerprints)
        object.__setattr__(self, "name_fingerprint", name_fingerprint)
        object.__setattr__(self, "name_fingerprint_alt", name_fingerprint_alt)
        object.__setattr__(self, "observed_name_fingerprints", observed_name_fingerprints)
        object.__setattr__(self, "source_icon_filenames", tuple(str(item) for item in self.source_icon_filenames if str(item)))
        object.__setattr__(self, "text_only_icon_filenames", tuple(str(item) for item in self.text_only_icon_filenames if str(item)))
        object.__setattr__(self, "name_variant_count", max(1, int(self.name_variant_count or 1)))
        object.__setattr__(
            self,
            "icon_variant_count",
            max(int(self.icon_variant_count or 0), len(icon_fingerprints), 1 if fingerprint is not None else 0),
        )
        object.__setattr__(
            self,
            "observed_name_variant_count",
            max(int(self.observed_name_variant_count or 0), len(observed_name_fingerprints)),
        )

    def without_fingerprints(self) -> "TemplateEntry":
        """返回可长期驻留的元数据副本，释放构建期分散指纹数组。"""

        return replace(
            self,
            fingerprint=None,
            icon_fingerprints=(),
            name_fingerprint=None,
            name_fingerprint_alt=None,
            observed_name_fingerprints=(),
        )


class TemplateIndex(list[TemplateEntry]):
    """模板元数据列表，并强持有与其同一索引空间的矩阵。"""

    def __init__(self, entries: Sequence[TemplateEntry] = ()) -> None:
        super().__init__(entries)
        self.rank_matrices: _RankMatrices | None = None


@dataclass(frozen=True)
class _RankMatrices:
    """连续 float16 指纹矩阵及每行对应的模板元数据。"""

    index_ref: Sequence[TemplateEntry]
    icon_templates: tuple[TemplateEntry, ...]
    icon_matrix: np.ndarray
    name_templates: tuple[TemplateEntry, ...]
    name_matrix: np.ndarray
    alt_name_templates: tuple[TemplateEntry, ...]
    alt_name_matrix: np.ndarray
    observed_name_templates: tuple[TemplateEntry, ...]
    observed_name_matrix: np.ndarray

    def __post_init__(self) -> None:
        for field_name in (
            "icon_matrix",
            "name_matrix",
            "alt_name_matrix",
            "observed_name_matrix",
        ):
            matrix = np.asarray(getattr(self, field_name), dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE)
            if matrix.ndim != 2:
                raise ValueError(f"{field_name} must be two-dimensional")
            object.__setattr__(self, field_name, np.ascontiguousarray(matrix))


@dataclass(frozen=True)
class TemplateRuntime:
    """sidecar 启动所需的元数据索引、矩阵和可诊断构建统计。"""

    template_index: TemplateIndex
    matrices: _RankMatrices
    stats: dict[str, Any]

"""Vision 模板 runtime 的稳定组合入口。

构建、矩阵 cache、资源校验和内存诊断拆到专用模块。本文件保留历史导出与可替换
的调用点，便于 sidecar、测试和离线工具继续以同一入口工作。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from hextech.infrastructure.vision import template_build as _template_build
from hextech.infrastructure.vision import template_cache as _template_cache
from hextech.infrastructure.vision import template_diagnostics as _template_diagnostics
from hextech.infrastructure.vision.template_models import (
    TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE,
    TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
    TemplateEntry,
    TemplateIndex,
    TemplateRuntime,
    _RankMatrices,
)


logger = logging.getLogger(__name__)
TEMPLATE_RUNTIME_CACHE_V1_FILE = _template_cache.TEMPLATE_RUNTIME_CACHE_V1_FILE
TEMPLATE_RUNTIME_CACHE_FILE = _template_cache.TEMPLATE_RUNTIME_CACHE_FILE
TEMPLATE_RUNTIME_MAX_WORKING_SET_BYTES = _template_diagnostics.TEMPLATE_RUNTIME_MAX_WORKING_SET_BYTES


def _runtime_environment_signature() -> dict[str, Any]:
    return _template_diagnostics._runtime_environment_signature()


def _hash_runtime_resource_stats(paths: Sequence[Path]) -> str:
    return _template_diagnostics._hash_runtime_resource_stats(paths)


def template_runtime_resource_signature(base_dir: str | Path | None = None) -> dict[str, Any]:
    return _template_diagnostics.template_runtime_resource_signature(
        base_dir,
        schema_version=TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
    )


def template_runtime_hint_signature(hint_cache: Mapping[str, Any] | None) -> str:
    return _template_diagnostics.template_runtime_hint_signature(hint_cache)


def _hint_cache_signature(hint_cache: Mapping[str, Any] | None) -> str:
    return template_runtime_hint_signature(hint_cache)


@contextmanager
def _template_cache_build_lock(cache_file: Path, *, timeout_seconds: float = 120.0):
    with _template_cache.template_cache_build_lock(cache_file, timeout_seconds=timeout_seconds) as lock_wait_seconds:
        yield lock_wait_seconds


def _template_entry_to_manifest(entry: TemplateEntry, *, row_index: int) -> dict[str, Any]:
    return _template_cache.template_entry_to_manifest(entry, row_index=row_index)


def _template_entry_to_cache(entry: TemplateEntry) -> dict[str, Any]:
    return _template_cache.template_entry_to_cache(entry)


def _template_entry_from_cache(payload: Any) -> TemplateEntry:
    return _template_cache.template_entry_from_cache(payload)


def _template_entry_from_manifest(payload: Any) -> TemplateEntry:
    return _template_cache.template_entry_from_manifest(payload)


def _template_indices(template_index: Sequence[TemplateEntry], templates: Sequence[TemplateEntry]) -> list[int]:
    return _template_cache._template_indices(template_index, templates)


def _templates_by_index(template_index: Sequence[TemplateEntry], indices: Any) -> tuple[TemplateEntry, ...]:
    return _template_cache._templates_by_index(template_index, indices)


def _matrix_from_cache(arrays: Mapping[str, Any], key: str, templates: Sequence[TemplateEntry]) -> np.ndarray:
    return _template_cache._matrix_from_cache(arrays, key, templates)


def _cache_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    return _template_cache._cache_manifest_bytes(payload)


def _read_cache_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _template_cache._read_cache_manifest(payload)


def _resource_signature_matches(
    *,
    cached_signature: Any,
    resource_signature: Mapping[str, Any],
    schema_version: int,
) -> bool:
    return _template_cache._resource_signature_matches(
        cached_signature=cached_signature,
        resource_signature=resource_signature,
        schema_version=schema_version,
    )


def _rank_matrices_from_cache(
    template_index: TemplateIndex,
    *,
    metadata: Mapping[str, Any],
    arrays: Mapping[str, Any],
) -> _RankMatrices:
    return _template_cache._rank_matrices_from_cache(template_index, metadata=metadata, arrays=arrays)


def _read_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
) -> TemplateRuntime | None:
    return _template_cache.read_template_runtime_cache(
        cache_file,
        resource_signature=resource_signature,
        hint_signature=hint_signature,
        schema_version=TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
    )


def _write_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
    template_index: Sequence[TemplateEntry],
    matrices: _RankMatrices,
) -> dict[str, Any]:
    return _template_cache.write_template_runtime_cache(
        cache_file,
        resource_signature=resource_signature,
        hint_signature=hint_signature,
        template_index=template_index,
        matrices=matrices,
        schema_version=TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
    )


def _cleanup_legacy_template_runtime_cache(cache_file: Path) -> bool:
    return _template_cache.cleanup_legacy_template_runtime_cache(
        cache_file,
        default_cache_file=TEMPLATE_RUNTIME_CACHE_FILE,
        legacy_cache_file=TEMPLATE_RUNTIME_CACHE_V1_FILE,
    )


def _clean_text(value: Any, *, fallback: str = "") -> str:
    return _template_build._clean_text(value, fallback=fallback)


def _load_manifest_entries(root: Path, *, use_runtime_resources: bool = True) -> list[Mapping[str, Any]]:
    return _template_build._load_manifest_entries(root, use_runtime_resources=use_runtime_resources)


def _load_manifest_entries_by_name(
    root: Path,
    *,
    use_runtime_resources: bool = True,
) -> dict[str, list[Mapping[str, Any]]]:
    return _template_build._load_manifest_entries_by_name(root, use_runtime_resources=use_runtime_resources)


def _select_manifest_item(
    manifest_by_name: Mapping[str, list[Mapping[str, Any]]],
    name: str,
    mapped_icon: str,
) -> Mapping[str, Any]:
    return _template_build._select_manifest_item(manifest_by_name, name, mapped_icon)


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    return _template_build.build_template_index(raw_templates)


def _attach_observed_name_exemplars(template_index: Sequence[TemplateEntry], asset_dir: Path) -> list[TemplateEntry]:
    return _template_build._attach_observed_name_exemplars(template_index, asset_dir)


def _register_rank_matrices(template_index: TemplateIndex, matrices: _RankMatrices) -> None:
    template_index.rank_matrices = matrices
    from hextech.infrastructure.vision.sidecar_matching import _RANK_MATRIX_CACHE

    _RANK_MATRIX_CACHE[id(template_index)] = matrices


def _publish_template_index(
    raw_entries: Sequence[TemplateEntry],
    matrices: _RankMatrices,
) -> tuple[TemplateIndex, _RankMatrices]:
    index, published_matrices = _template_build.publish_template_index(raw_entries, matrices)
    _register_rank_matrices(index, published_matrices)
    return index, published_matrices


def load_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> TemplateIndex:
    """构建后立即发布 metadata-only index，外部调用不会持有分散指纹数组。"""

    raw_entries = _template_build.load_default_template_entries(base_dir, hint_cache=hint_cache)
    if not raw_entries:
        return TemplateIndex()
    matrices = _rank_matrices(raw_entries)
    index, _published_matrices = _publish_template_index(raw_entries, matrices)
    return index


def _rank_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    from hextech.infrastructure.vision.sidecar_matching import _rank_matrices as build_rank_matrices

    return build_rank_matrices(template_index)


def rank_template_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    attached = getattr(template_index, "rank_matrices", None)
    if isinstance(attached, _RankMatrices) and attached.index_ref is template_index:
        return attached
    return _rank_matrices(template_index)


def _runtime_stats_with_memory(stats: Mapping[str, Any], matrices: _RankMatrices) -> dict[str, Any]:
    result = dict(stats)
    result.update(_template_diagnostics.template_runtime_memory_profile(matrices))
    return result


def load_or_build_default_template_runtime(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    cache_file: str | Path | None = None,
    resource_signature: Mapping[str, Any] | None = None,
    status_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TemplateRuntime:
    """加载或构建 runtime，整个发布面只保留连续 float16 矩阵与元数据。"""

    started_at = time.perf_counter()
    target_cache = Path(cache_file) if cache_file is not None else TEMPLATE_RUNTIME_CACHE_FILE
    signature = dict(resource_signature or template_runtime_resource_signature(base_dir))
    hint_signature = _hint_cache_signature(hint_cache)
    if status_callback is not None:
        status_callback(
            "template_runtime_cache_lookup",
            {"schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION, "cache_file": str(target_cache)},
        )
    runtime = _read_template_runtime_cache(
        target_cache,
        resource_signature=signature,
        hint_signature=hint_signature,
    )
    if runtime is not None:
        runtime.stats.update(
            _runtime_stats_with_memory(
                {"build_seconds": 0.0, "load_seconds": round(time.perf_counter() - started_at, 3)},
                runtime.matrices,
            )
        )
        runtime.stats["legacy_v1_cache_removed"] = _cleanup_legacy_template_runtime_cache(target_cache)
        if status_callback is not None:
            status_callback("template_runtime_cache_ready", runtime.stats)
        return runtime

    if status_callback is not None:
        status_callback("template_runtime_cache_lock_wait", {"cache_file": str(target_cache)})
    with _template_cache_build_lock(target_cache) as lock_wait_seconds:
        runtime = _read_template_runtime_cache(
            target_cache,
            resource_signature=signature,
            hint_signature=hint_signature,
        )
        if runtime is not None:
            runtime.stats.update(
                _runtime_stats_with_memory(
                    {
                        "build_seconds": 0.0,
                        "load_seconds": round(time.perf_counter() - started_at, 3),
                        "lock_wait_seconds": lock_wait_seconds,
                    },
                    runtime.matrices,
                )
            )
            if status_callback is not None:
                status_callback("template_runtime_cache_ready", runtime.stats)
            return runtime

        if status_callback is not None:
            status_callback(
                "template_index_build",
                {
                    "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
                    "cache_hit": False,
                    "lock_wait_seconds": lock_wait_seconds,
                },
            )
        template_index = load_default_template_index(base_dir, hint_cache=hint_cache)
        if status_callback is not None:
            status_callback("rank_matrix_build", {"template_count": len(template_index)})
        matrices = rank_template_matrices(template_index)
        if not isinstance(template_index, TemplateIndex) or matrices.index_ref is not template_index:
            template_index, matrices = _publish_template_index(template_index, matrices)
        try:
            cache_write_stats = _write_template_runtime_cache(
                target_cache,
                resource_signature=signature,
                hint_signature=hint_signature,
                template_index=template_index,
                matrices=matrices,
            )
            cache_error = ""
        except Exception as exc:
            cache_write_stats = {
                "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
                "cache_file": str(target_cache),
                "cache_bytes": 0,
                "matrix_dtype": np.dtype(TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE).name,
            }
            cache_error = str(exc)
            logger.debug("写入 Vision 模板 runtime cache 失败。", exc_info=True)

    stats = _runtime_stats_with_memory(
        {
            "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
            "cache_hit": False,
            "cache_file": str(target_cache),
            "cache_bytes": int(cache_write_stats.get("cache_bytes") or 0),
            "cache_error": cache_error,
            "legacy_v1_cache_removed": _cleanup_legacy_template_runtime_cache(target_cache),
            "template_count": len(template_index),
            "matrix_dtype": str(cache_write_stats.get("matrix_dtype") or np.dtype(TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE).name),
            "build_seconds": round(time.perf_counter() - started_at, 3),
            "load_seconds": 0.0,
            "lock_wait_seconds": lock_wait_seconds,
        },
        matrices,
    )
    if status_callback is not None:
        status_callback("template_runtime_cache_ready", stats)
    return TemplateRuntime(template_index=template_index, matrices=matrices, stats=stats)


def template_runtime_memory_profile(matrices: _RankMatrices) -> dict[str, Any]:
    return _template_diagnostics.template_runtime_memory_profile(matrices)


# 这是供 sidecar、离线工具和测试使用的稳定组合面。不要再从 globals() 推导，
# 否则导入模块的实现细节会意外变成公开 API，Pyright 也无法校验该契约。
__all__ = (
    "TEMPLATE_RUNTIME_CACHE_FILE",
    "TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE",
    "TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION",
    "TEMPLATE_RUNTIME_CACHE_V1_FILE",
    "TEMPLATE_RUNTIME_MAX_WORKING_SET_BYTES",
    "TemplateEntry",
    "TemplateIndex",
    "TemplateRuntime",
    "build_template_index",
    "load_default_template_index",
    "load_or_build_default_template_runtime",
    "rank_template_matrices",
    "template_runtime_hint_signature",
    "template_runtime_memory_profile",
    "template_runtime_resource_signature",
)

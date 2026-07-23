"""Vision 模板矩阵 cache 的读写与并发锁。

cache manifest 只保存模板元数据和矩阵行索引；所有完整指纹均位于四块连续
float16 数组，读回后不会再展开为 Python float 列表。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from hextech.infrastructure.vision.template_models import (
    TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE,
    TemplateEntry,
    TemplateIndex,
    TemplateRuntime,
    _RankMatrices,
)
from hextech.modules.data.catalog.runtime_store import build_runtime_cache_path


logger = logging.getLogger(__name__)
TEMPLATE_RUNTIME_CACHE_V1_FILE = Path(build_runtime_cache_path("overlay_vision/template_runtime_cache.v1.npz"))
TEMPLATE_RUNTIME_CACHE_FILE = Path(build_runtime_cache_path("overlay_vision/template_runtime_cache.v2.npz"))
_CACHE_BUILD_LOCK_GUARD = threading.Lock()
_CACHE_BUILD_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock_for_cache(path: Path) -> threading.Lock:
    key = str(path.resolve()).casefold()
    with _CACHE_BUILD_LOCK_GUARD:
        return _CACHE_BUILD_THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def template_cache_build_lock(cache_file: Path, *, timeout_seconds: float = 120.0):
    """串行化同一 cache 的线程与进程构建，避免两份大矩阵同时驻留。"""

    started_at = time.perf_counter()
    thread_lock = _thread_lock_for_cache(cache_file)
    if not thread_lock.acquire(timeout=max(0.1, timeout_seconds)):
        raise TimeoutError("template_runtime_cache_thread_lock_timeout")
    lock_file = cache_file.with_suffix(f"{cache_file.suffix}.lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        handle = lock_file.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.perf_counter() - started_at >= timeout_seconds:
                    raise TimeoutError("template_runtime_cache_process_lock_timeout")
                time.sleep(0.05)
        yield round(time.perf_counter() - started_at, 3)
    finally:
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()
        thread_lock.release()


def _tuple_str(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def template_entry_to_manifest(entry: TemplateEntry, *, row_index: int) -> dict[str, Any]:
    return {
        "row_index": int(row_index),
        "augment_id": entry.augment_id,
        "name": entry.name,
        "tier": entry.tier,
        "summary": entry.summary,
        "icon_digest": entry.icon_digest,
        "priority": int(entry.priority),
        "source_icon_filenames": list(entry.source_icon_filenames),
        "text_only_icon_filenames": list(entry.text_only_icon_filenames),
        "name_variant_count": int(entry.name_variant_count),
        "icon_variant_count": int(entry.icon_variant_count),
        "observed_name_variant_count": int(entry.observed_name_variant_count),
    }


def template_entry_to_cache(entry: TemplateEntry) -> dict[str, Any]:
    """兼容旧测试/调用方的序列化入口；v5 不再写任何完整指纹。"""

    return template_entry_to_manifest(entry, row_index=0)


def template_entry_from_cache(payload: Any) -> TemplateEntry:
    return template_entry_from_manifest(payload)


def template_entry_from_manifest(payload: Any) -> TemplateEntry:
    if not isinstance(payload, Mapping):
        raise ValueError("template cache manifest entry schema mismatch")
    return TemplateEntry(
        augment_id=str(payload.get("augment_id") or ""),
        name=str(payload.get("name") or ""),
        tier=str(payload.get("tier") or ""),
        summary=str(payload.get("summary") or ""),
        icon_digest=str(payload.get("icon_digest") or ""),
        priority=int(payload.get("priority") or 0),
        source_icon_filenames=_tuple_str(payload.get("source_icon_filenames")),
        text_only_icon_filenames=_tuple_str(payload.get("text_only_icon_filenames")),
        name_variant_count=max(1, int(payload.get("name_variant_count") or 1)),
        icon_variant_count=max(0, int(payload.get("icon_variant_count") or 0)),
        observed_name_variant_count=max(0, int(payload.get("observed_name_variant_count") or 0)),
    )


def _template_indices(template_index: Sequence[TemplateEntry], templates: Sequence[TemplateEntry]) -> list[int]:
    index_by_identity = {id(template): index for index, template in enumerate(template_index)}
    return [index_by_identity[id(template)] for template in templates]


def _templates_by_index(template_index: Sequence[TemplateEntry], indices: Any) -> tuple[TemplateEntry, ...]:
    result: list[TemplateEntry] = []
    for raw_index in np.asarray(indices, dtype=np.int32).tolist():
        index = int(raw_index)
        if index < 0 or index >= len(template_index):
            raise ValueError("template cache matrix index out of range")
        result.append(template_index[index])
    return tuple(result)


def _matrix_from_cache(arrays: Mapping[str, Any], key: str, templates: Sequence[TemplateEntry]) -> np.ndarray:
    matrix = np.ascontiguousarray(np.asarray(arrays[key], dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE))
    if matrix.ndim != 2:
        raise ValueError("template cache matrix must be two-dimensional")
    if matrix.shape[0] != len(templates):
        raise ValueError("template cache matrix row count mismatch")
    if templates and matrix.shape[1] <= 0:
        raise ValueError("template cache matrix width mismatch")
    if not templates and matrix.shape != (0, 0):
        raise ValueError("empty template cache matrix shape mismatch")
    return matrix


def _cache_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _read_cache_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    manifest_key = "manifest_json" if "manifest_json" in payload else "metadata_json"
    raw_metadata = np.asarray(payload[manifest_key], dtype=np.uint8).tobytes().decode("utf-8")
    manifest = json.loads(raw_metadata)
    if not isinstance(manifest, dict):
        raise ValueError("template cache manifest schema mismatch")
    return manifest


def _resource_signature_matches(
    *,
    cached_signature: Any,
    resource_signature: Mapping[str, Any],
    schema_version: int,
) -> bool:
    expected = dict(resource_signature)
    # cache schema 与资源签名是两层独立约束：测试/离线工具可传入自己的资源
    # 版本字段，不能因此让刚写入的当前 cache 永远 miss。
    _ = schema_version
    return cached_signature == expected


def _rank_matrices_from_cache(
    template_index: TemplateIndex,
    *,
    metadata: Mapping[str, Any],
    arrays: Mapping[str, Any],
) -> _RankMatrices:
    icon_templates = _templates_by_index(template_index, metadata.get("icon_template_indices") or [])
    name_templates = _templates_by_index(template_index, metadata.get("name_template_indices") or [])
    alt_name_templates = _templates_by_index(template_index, metadata.get("alt_name_template_indices") or [])
    observed_name_templates = _templates_by_index(template_index, metadata.get("observed_name_template_indices") or [])
    return _RankMatrices(
        template_index,
        icon_templates,
        _matrix_from_cache(arrays, "icon_matrix", icon_templates),
        name_templates,
        _matrix_from_cache(arrays, "name_matrix", name_templates),
        alt_name_templates,
        _matrix_from_cache(arrays, "alt_name_matrix", alt_name_templates),
        observed_name_templates,
        _matrix_from_cache(arrays, "observed_name_matrix", observed_name_templates),
    )


def read_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
    schema_version: int,
) -> TemplateRuntime | None:
    started_at = time.perf_counter()
    target = Path(cache_file)
    try:
        cache_bytes = target.stat().st_size
    except OSError:
        return None
    try:
        with np.load(target, allow_pickle=False) as payload:
            metadata = _read_cache_manifest(payload)
            cached_schema_version = int(metadata.get("schema_version") or 0)
            if cached_schema_version != int(schema_version):
                return None
            if not _resource_signature_matches(
                cached_signature=metadata.get("resource_signature"),
                resource_signature=resource_signature,
                schema_version=cached_schema_version,
            ):
                return None
            if str(metadata.get("hint_signature") or "") != hint_signature:
                return None
            raw_templates = metadata.get("template_manifest")
            if not isinstance(raw_templates, list):
                return None
            template_index = TemplateIndex(template_entry_from_manifest(item) for item in raw_templates)
            matrices = _rank_matrices_from_cache(template_index, metadata=metadata, arrays=payload)
    except Exception:
        return None
    template_index.rank_matrices = matrices
    # 现有 matcher 的轻量缓存只记录矩阵引用，不复制数据。
    from hextech.infrastructure.vision.sidecar_matching import _RANK_MATRIX_CACHE

    _RANK_MATRIX_CACHE[id(template_index)] = matrices
    return TemplateRuntime(
        template_index=template_index,
        matrices=matrices,
        stats={
            "schema_version": int(schema_version),
            "cache_hit": True,
            "cache_file": str(target),
            "cache_bytes": int(cache_bytes),
            "template_count": len(template_index),
            "matrix_dtype": str(metadata.get("matrix_dtype") or ""),
            "load_seconds": round(time.perf_counter() - started_at, 3),
        },
    )


def write_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
    template_index: Sequence[TemplateEntry],
    matrices: _RankMatrices,
    schema_version: int,
) -> dict[str, Any]:
    target = Path(cache_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    manifest = {
        "schema_version": int(schema_version),
        "matrix_dtype": np.dtype(TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE).name,
        "resource_signature": dict(resource_signature),
        "hint_signature": hint_signature,
        "template_manifest": [template_entry_to_manifest(entry, row_index=index) for index, entry in enumerate(template_index)],
        "icon_template_indices": _template_indices(template_index, matrices.icon_templates),
        "name_template_indices": _template_indices(template_index, matrices.name_templates),
        "alt_name_template_indices": _template_indices(template_index, matrices.alt_name_templates),
        "observed_name_template_indices": _template_indices(template_index, matrices.observed_name_templates),
        "written_at": time.time(),
    }
    manifest_bytes = _cache_manifest_bytes(manifest)
    try:
        with temp_path.open("wb") as handle:
            np.savez(
                handle,
                manifest_json=np.frombuffer(manifest_bytes, dtype=np.uint8),
                icon_matrix=np.ascontiguousarray(matrices.icon_matrix, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE),
                name_matrix=np.ascontiguousarray(matrices.name_matrix, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE),
                alt_name_matrix=np.ascontiguousarray(matrices.alt_name_matrix, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE),
                observed_name_matrix=np.ascontiguousarray(
                    matrices.observed_name_matrix,
                    dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE,
                ),
            )
        os.replace(temp_path, target)
        return {
            "schema_version": int(schema_version),
            "cache_file": str(target),
            "cache_bytes": int(target.stat().st_size),
            "matrix_dtype": np.dtype(TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE).name,
        }
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def cleanup_legacy_template_runtime_cache(
    cache_file: Path,
    *,
    default_cache_file: Path,
    legacy_cache_file: Path,
) -> bool:
    """默认 v2 cache 就绪后才清理旧 cache；自定义路径绝不隐式删除。"""

    try:
        if cache_file.resolve() != default_cache_file.resolve():
            return False
    except OSError:
        return False
    try:
        legacy_cache_file.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("清理旧 Vision 模板 runtime cache v1 失败。", exc_info=True)
        return False

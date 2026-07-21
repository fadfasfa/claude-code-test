"""overlay vision 模板 runtime。

本模块收口模板索引加载、runtime cache 读写和资源签名计算。
调用方: overlay.vision.sidecar、runtime_supervisor、离线评测工具。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np
from PIL import Image

from hextech.modules.data.catalog.runtime_store import build_runtime_cache_path
from hextech.modules.data.catalog.version_catalog import load_augment_name_to_icon_map
from hextech.modules.recommendation.hints import normalize_augment_id, query_overlay_hint
from hextech.modules.data.ports.paths import ASSET_DIR, INDEX_DATA_DIR
logger = logging.getLogger(__name__)

TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION = 4
TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE = np.float16
TEMPLATE_RUNTIME_CACHE_V1_FILE = Path(build_runtime_cache_path("overlay_vision/template_runtime_cache.v1.npz"))
TEMPLATE_RUNTIME_CACHE_FILE = Path(build_runtime_cache_path("overlay_vision/template_runtime_cache.v2.npz"))
_CACHE_BUILD_LOCK_GUARD = threading.Lock()
_CACHE_BUILD_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock_for_cache(path: Path) -> threading.Lock:
    key = str(path.resolve()).casefold()
    with _CACHE_BUILD_LOCK_GUARD:
        return _CACHE_BUILD_THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _template_cache_build_lock(cache_file: Path, *, timeout_seconds: float = 120.0):
    """串行化同一 runtime cache 的线程和进程构建。"""

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


@dataclass(frozen=True)
class TemplateRuntime:
    """sidecar 启动所需的模板索引与矩阵，允许从本机 runtime cache 直接恢复。"""

    template_index: list[TemplateEntry]
    matrices: _RankMatrices
    stats: dict[str, Any]


@dataclass(frozen=True)
class _RankMatrices:
    """模板指纹的向量化缓存：把逐模板 Python NCC 循环换成单次矩阵乘。"""

    index_ref: Sequence[TemplateEntry]
    icon_templates: tuple[TemplateEntry, ...]
    icon_matrix: np.ndarray
    name_templates: tuple[TemplateEntry, ...]
    name_matrix: np.ndarray
    alt_name_templates: tuple[TemplateEntry, ...]
    alt_name_matrix: np.ndarray
    observed_name_templates: tuple[TemplateEntry, ...]
    observed_name_matrix: np.ndarray


@dataclass(frozen=True)
class TemplateEntry:
    augment_id: str
    name: str
    tier: str
    summary: str
    fingerprint: tuple[float, ...]
    icon_fingerprints: tuple[tuple[float, ...], ...] = ()
    icon_digest: str = ""
    priority: int = 0
    name_fingerprint: tuple[float, ...] | None = None
    name_fingerprint_alt: tuple[float, ...] | None = None
    # 仅含游戏内卡名像素的脱敏真机指纹。它是独立文字证据，不能由图标短名单生成。
    observed_name_fingerprints: tuple[tuple[float, ...], ...] = ()
    source_icon_filenames: tuple[str, ...] = ()
    text_only_icon_filenames: tuple[str, ...] = ()
    # 同一中文卡名可能对应多个 CDragon 视觉版本；文字识别只能确认卡名，
    # 只有唯一版本或图标证据充分时才可把具体 ID 带入后续统计解析。
    name_variant_count: int = 1


class TemplateIndex(list[TemplateEntry]):
    """模板列表的轻量子类，用于让 v2 cache hit 强持有矩阵。"""

    def __init__(self, entries: Sequence[TemplateEntry] = ()) -> None:
        super().__init__(entries)
        self.rank_matrices: _RankMatrices | None = None


def _runtime_environment_signature() -> dict[str, Any]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "numpy": str(getattr(np, "__version__", "")),
        "pillow": str(getattr(Image, "__version__", "")),
    }


def _hash_runtime_resource_stats(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    normalized_paths = sorted(
        ((str(path).replace("\\", "/").casefold(), path) for path in paths),
        key=lambda item: item[0],
    )
    for normalized_path, path in normalized_paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(normalized_path.encode("utf-8", errors="replace"))
        digest.update(str(int(stat.st_size)).encode("ascii"))
        digest.update(str(int(stat.st_mtime_ns)).encode("ascii"))
    return digest.hexdigest()


def template_runtime_resource_signature(base_dir: str | Path | None = None) -> dict[str, Any]:
    """生成模板缓存指纹；资源或代码 schema 变化时自动失效。"""

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "resources" / "catalog"
    asset_dir = Path(ASSET_DIR) if use_runtime_resources else root / "resources" / "assets"
    version_files = [path for path in version_dir.rglob("*.json") if path.is_file()] if version_dir.exists() else []
    asset_files = [
        path
        for path in asset_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ] if asset_dir.exists() else []
    return {
        "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
        "environment": _runtime_environment_signature(),
        "version_digest": _hash_runtime_resource_stats(version_files),
        "asset_digest": _hash_runtime_resource_stats(asset_files),
        "version_file_count": len(version_files),
        "asset_file_count": len(asset_files),
    }


def template_runtime_hint_signature(hint_cache: Mapping[str, Any] | None) -> str:
    """生成识别模板相关的 hint cache 指纹。"""

    if not isinstance(hint_cache, Mapping):
        return ""
    hints = hint_cache.get("hints")
    if not isinstance(hints, Mapping):
        hints = {}
    stable_hints: list[dict[str, Any]] = []
    stable_keys = (
        "augment_id",
        "id",
        "name",
        "localized_name",
        "en_name",
        "icon",
        "icon_path",
        "image",
        "cdragon_id",
        "augment_name_id",
    )
    for key, value in sorted(hints.items(), key=lambda item: str(item[0])):
        if not isinstance(value, Mapping):
            continue
        stable_hints.append(
            {
                "key": str(key),
                **{field: value.get(field) for field in stable_keys if value.get(field) not in (None, "")},
            }
        )
    name_index = hint_cache.get("name_index")
    stable_name_index = {
        str(key): str(value)
        for key, value in sorted(name_index.items(), key=lambda item: str(item[0]))
        if str(key or "").strip() and str(value or "").strip()
    } if isinstance(name_index, Mapping) else {}
    payload = {
        "schema_version": int(hint_cache.get("schema_version") or 0),
        "hints": stable_hints,
        "name_index": stable_name_index,
    }
    try:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except TypeError:
        return hashlib.sha256(repr(payload).encode("utf-8", errors="replace")).hexdigest()


def _hint_cache_signature(hint_cache: Mapping[str, Any] | None) -> str:
    return template_runtime_hint_signature(hint_cache)


def _tuple_float(value: Any) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    result: list[float] = []
    for item in value:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            return ()
    return tuple(result)


def _optional_tuple_float(value: Any) -> tuple[float, ...] | None:
    if value is None:
        return None
    result = _tuple_float(value)
    return result if result else None


def _tuple_str(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _tuple_tuple_float(value: Any) -> tuple[tuple[float, ...], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    rows: list[tuple[float, ...]] = []
    for item in value:
        row = _tuple_float(item)
        if row:
            rows.append(row)
    return tuple(rows)


def _template_entry_to_manifest(entry: TemplateEntry, *, row_index: int) -> dict[str, Any]:
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
    }


def _template_entry_to_cache(entry: TemplateEntry) -> dict[str, Any]:
    return {
        "augment_id": entry.augment_id,
        "name": entry.name,
        "tier": entry.tier,
        "summary": entry.summary,
        "fingerprint": list(entry.fingerprint),
        "icon_fingerprints": [list(item) for item in entry.icon_fingerprints],
        "icon_digest": entry.icon_digest,
        "priority": int(entry.priority),
        "name_fingerprint": list(entry.name_fingerprint) if entry.name_fingerprint is not None else None,
        "name_fingerprint_alt": list(entry.name_fingerprint_alt) if entry.name_fingerprint_alt is not None else None,
        "observed_name_fingerprints": [list(item) for item in entry.observed_name_fingerprints],
        "source_icon_filenames": list(entry.source_icon_filenames),
        "text_only_icon_filenames": list(entry.text_only_icon_filenames),
        "name_variant_count": int(entry.name_variant_count),
    }


def _template_entry_from_cache(payload: Any) -> TemplateEntry:
    if not isinstance(payload, Mapping):
        raise ValueError("template cache entry schema mismatch")
    return TemplateEntry(
        augment_id=str(payload.get("augment_id") or ""),
        name=str(payload.get("name") or ""),
        tier=str(payload.get("tier") or ""),
        summary=str(payload.get("summary") or ""),
        fingerprint=_tuple_float(payload.get("fingerprint")),
        icon_fingerprints=_tuple_tuple_float(payload.get("icon_fingerprints")),
        icon_digest=str(payload.get("icon_digest") or ""),
        priority=int(payload.get("priority") or 0),
        name_fingerprint=_optional_tuple_float(payload.get("name_fingerprint")),
        name_fingerprint_alt=_optional_tuple_float(payload.get("name_fingerprint_alt")),
        observed_name_fingerprints=_tuple_tuple_float(payload.get("observed_name_fingerprints")),
        source_icon_filenames=_tuple_str(payload.get("source_icon_filenames")),
        text_only_icon_filenames=_tuple_str(payload.get("text_only_icon_filenames")),
        name_variant_count=max(1, int(payload.get("name_variant_count") or 1)),
    )


def _template_entry_from_manifest(payload: Any) -> TemplateEntry:
    if not isinstance(payload, Mapping):
        raise ValueError("template cache manifest entry schema mismatch")
    return TemplateEntry(
        augment_id=str(payload.get("augment_id") or ""),
        name=str(payload.get("name") or ""),
        tier=str(payload.get("tier") or ""),
        summary=str(payload.get("summary") or ""),
        fingerprint=(),
        icon_fingerprints=(),
        icon_digest=str(payload.get("icon_digest") or ""),
        priority=int(payload.get("priority") or 0),
        name_fingerprint=None,
        name_fingerprint_alt=None,
        observed_name_fingerprints=(),
        source_icon_filenames=_tuple_str(payload.get("source_icon_filenames")),
        text_only_icon_filenames=_tuple_str(payload.get("text_only_icon_filenames")),
        name_variant_count=max(1, int(payload.get("name_variant_count") or 1)),
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
    matrix = np.asarray(arrays[key], dtype=np.float32).copy()
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
    if cached_signature == expected:
        return True
    if schema_version == 1:
        legacy_expected = {**expected, "schema_version": 1}
        return cached_signature == legacy_expected
    return False


def _rank_matrices_from_cache(
    template_index: list[TemplateEntry],
    *,
    metadata: Mapping[str, Any],
    arrays: Mapping[str, Any],
) -> _RankMatrices:
    icon_templates = _templates_by_index(template_index, metadata.get("icon_template_indices") or [])
    name_templates = _templates_by_index(template_index, metadata.get("name_template_indices") or [])
    alt_name_templates = _templates_by_index(template_index, metadata.get("alt_name_template_indices") or [])
    observed_name_templates = _templates_by_index(
        template_index,
        metadata.get("observed_name_template_indices") or [],
    )
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


def _read_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
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
            schema_version = int(metadata.get("schema_version") or 0)
            if schema_version not in {1, TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION}:
                return None
            if not _resource_signature_matches(
                cached_signature=metadata.get("resource_signature"),
                resource_signature=resource_signature,
                schema_version=schema_version,
            ):
                return None
            if str(metadata.get("hint_signature") or "") != hint_signature:
                return None
            raw_templates = metadata.get("template_manifest") if schema_version >= 2 else metadata.get("template_index")
            if not isinstance(raw_templates, list):
                return None
            template_index = TemplateIndex([
                _template_entry_from_manifest(item) if schema_version >= 2 else _template_entry_from_cache(item)
                for item in raw_templates
            ])
            matrices = _rank_matrices_from_cache(template_index, metadata=metadata, arrays=payload)
    except Exception:
        return None
    template_index.rank_matrices = matrices
    sidecar_rank_cache = __import__("hextech.infrastructure.vision.sidecar", fromlist=["_RANK_MATRIX_CACHE"])._RANK_MATRIX_CACHE
    sidecar_rank_cache[id(template_index)] = matrices
    return TemplateRuntime(
        template_index=template_index,
        matrices=matrices,
        stats={
            "schema_version": schema_version,
            "cache_hit": True,
            "cache_file": str(target),
            "cache_bytes": int(cache_bytes),
            "template_count": len(template_index),
            "matrix_dtype": str(metadata.get("matrix_dtype") or ""),
            "load_seconds": round(time.perf_counter() - started_at, 3),
        },
    )


def _write_template_runtime_cache(
    cache_file: str | Path,
    *,
    resource_signature: Mapping[str, Any],
    hint_signature: str,
    template_index: list[TemplateEntry],
    matrices: _RankMatrices,
) -> dict[str, Any]:
    target = Path(cache_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    manifest = {
        "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
        "matrix_dtype": np.dtype(TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE).name,
        "resource_signature": dict(resource_signature),
        "hint_signature": hint_signature,
        "template_manifest": [
            _template_entry_to_manifest(entry, row_index=index)
            for index, entry in enumerate(template_index)
        ],
        "icon_template_indices": _template_indices(template_index, matrices.icon_templates),
        "name_template_indices": _template_indices(template_index, matrices.name_templates),
        "alt_name_template_indices": _template_indices(template_index, matrices.alt_name_templates),
        "observed_name_template_indices": _template_indices(template_index, matrices.observed_name_templates),
        "written_at": time.time(),
    }
    manifest_bytes = _cache_manifest_bytes(manifest)
    try:
        with temp_path.open("wb") as f:
            np.savez(
                f,
                manifest_json=np.frombuffer(manifest_bytes, dtype=np.uint8),
                icon_matrix=np.asarray(matrices.icon_matrix, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE),
                name_matrix=np.asarray(matrices.name_matrix, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE),
                alt_name_matrix=np.asarray(matrices.alt_name_matrix, dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE),
                observed_name_matrix=np.asarray(
                    matrices.observed_name_matrix,
                    dtype=TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE,
                ),
            )
        os.replace(temp_path, target)
        return {
            "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
            "cache_file": str(target),
            "cache_bytes": int(target.stat().st_size),
            "matrix_dtype": np.dtype(TEMPLATE_RUNTIME_CACHE_MATRIX_DTYPE).name,
        }
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _cleanup_legacy_template_runtime_cache(cache_file: Path) -> bool:
    """默认 v2 cache ready 后清理旧 v1 大文件；自定义 cache_file 不做隐式删除。"""

    try:
        if cache_file.resolve() != TEMPLATE_RUNTIME_CACHE_FILE.resolve():
            return False
    except OSError:
        return False
    try:
        TEMPLATE_RUNTIME_CACHE_V1_FILE.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("清理旧 Vision 模板 runtime cache v1 失败。", exc_info=True)
        return False


def _clean_text(value: Any, *, fallback: str = "") -> str:
    from hextech.infrastructure.vision.sidecar_common import _clean_text as clean_text

    return clean_text(value, fallback=fallback)


def _load_manifest_entries(root: Path, *, use_runtime_resources: bool = True) -> list[Mapping[str, Any]]:
    from hextech.infrastructure.vision.sidecar_fingerprints import _load_manifest_entries as load_entries

    return load_entries(root, use_runtime_resources=use_runtime_resources)


def _load_manifest_entries_by_name(
    root: Path,
    *,
    use_runtime_resources: bool = True,
) -> dict[str, list[Mapping[str, Any]]]:
    from hextech.infrastructure.vision.sidecar_fingerprints import (
        _load_manifest_entries_by_name as load_entries_by_name,
    )

    return load_entries_by_name(root, use_runtime_resources=use_runtime_resources)


def _select_manifest_item(
    manifest_by_name: Mapping[str, list[Mapping[str, Any]]],
    name: str,
    mapped_icon: str,
) -> Mapping[str, Any]:
    from hextech.infrastructure.vision.sidecar_fingerprints import _select_manifest_item as select_item

    return select_item(manifest_by_name, name, mapped_icon)


def build_template_index(raw_templates: Mapping[str, Mapping[str, Any]]) -> list[TemplateEntry]:
    from hextech.infrastructure.vision import sidecar as _sidecar

    return _sidecar.build_template_index(raw_templates)


def _attach_observed_name_exemplars(
    template_index: Sequence[TemplateEntry],
    asset_dir: Path,
) -> list[TemplateEntry]:
    """把脱敏真机卡名 ROI 绑定到对应视觉模板，不读取完整游戏截图。"""

    from hextech.infrastructure.vision.sidecar_fingerprints import _normalized_fingerprint, _text_levels

    exemplar_dir = asset_dir / "vision" / "name_exemplars"
    fingerprints_by_id: dict[str, list[tuple[float, ...]]] = {}
    if exemplar_dir.is_dir():
        for path in sorted(exemplar_dir.glob("*.png")):
            augment_id = normalize_augment_id(path.stem.split("__", 1)[0])
            if not augment_id:
                continue
            try:
                with Image.open(path) as opened:
                    fingerprint = _normalized_fingerprint(_text_levels(opened.convert("RGB")))
            except OSError:
                continue
            if fingerprint is not None:
                fingerprints_by_id.setdefault(augment_id, []).append(fingerprint)

    return [
        replace(
            entry,
            observed_name_fingerprints=tuple(
                dict.fromkeys(fingerprints_by_id.get(normalize_augment_id(entry.augment_id), ()))
            ),
        )
        for entry in template_index
    ]


def load_default_template_index(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> list[TemplateEntry]:
    """从随包稳定资源加载海克斯图标模板，不触发远端抓取。"""

    use_runtime_resources = base_dir is None
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    version_data_dir = Path(INDEX_DATA_DIR) if use_runtime_resources else root / "resources" / "catalog"
    asset_dir = Path(ASSET_DIR) if use_runtime_resources else root / "resources" / "assets"
    try:
        name_to_icon = load_augment_name_to_icon_map(version_data_dir)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(name_to_icon, Mapping):
        return []

    manifest_entries = _load_manifest_entries(root, use_runtime_resources=use_runtime_resources)
    manifest_by_name = _load_manifest_entries_by_name(root, use_runtime_resources=use_runtime_resources)
    raw_templates: dict[str, dict[str, Any]] = {}
    names = {
        _clean_text(item.get("name"))
        for item in manifest_entries
        if _clean_text(item.get("name"))
    } | {_clean_text(name) for name in name_to_icon if _clean_text(name)}
    for name in sorted(names, key=normalize_augment_id):
        clean_name = _clean_text(name)
        manifest_items = manifest_by_name.get(clean_name) or manifest_by_name.get(normalize_augment_id(clean_name)) or []
        mapped_icon = str(name_to_icon.get(clean_name) or "")
        if not clean_name:
            continue
        hint_result = query_overlay_hint(hint_cache or {}, clean_name)
        hint_value = hint_result.get("hint")
        hint: Mapping[str, Any] = hint_value if hint_result.get("ok") and isinstance(hint_value, Mapping) else {}
        variants: list[tuple[Mapping[str, Any], list[str]]] = []
        for manifest_item in manifest_items:
            icon_path = str(manifest_item.get("local_path") or manifest_item.get("filename") or "")
            if icon_path:
                variants.append((manifest_item, [icon_path]))
        if not variants and mapped_icon:
            variants.append(({}, [mapped_icon]))

        for manifest_item, icon_paths in variants:
            images: list[Image.Image] = []
            filenames: list[str] = []
            loaded_paths: set[Path] = set()
            allowed_roots = (root.resolve(), asset_dir.resolve())
            for icon_path in icon_paths:
                relative_icon = str(icon_path or "").lstrip("/")
                if relative_icon.startswith("assets/"):
                    path = (asset_dir / relative_icon.removeprefix("assets/")).resolve()
                else:
                    path = (root / relative_icon).resolve()
                try:
                    if not any(path == allowed_root or allowed_root in path.parents for allowed_root in allowed_roots):
                        continue
                    if path in loaded_paths:
                        continue
                    with Image.open(path) as opened:
                        images.append(opened.copy())
                    filenames.append(path.name)
                    loaded_paths.add(path)
                except OSError:
                    continue
            if not images:
                continue
            template_id = normalize_augment_id(
                manifest_item.get("augment_name_id")
                or manifest_item.get("cdragon_id")
                or hint.get("augment_id")
                or clean_name,
                clean_name,
            )
            existing = raw_templates.get(template_id)
            if existing is not None and _clean_text(existing.get("name")) != clean_name:
                template_id = normalize_augment_id(f"{clean_name}_{manifest_item.get('cdragon_id') or ''}", clean_name)
                existing = raw_templates.get(template_id)
            if existing is not None:
                existing["images"] = [*existing.get("images", []), *images]
                existing["source_icon_filenames"] = list(
                    dict.fromkeys([*existing.get("source_icon_filenames", []), *filenames])
                )
                continue
            raw_templates[template_id] = {
                "name": clean_name,
                "tier": _clean_text(manifest_item.get("tier") or hint.get("tier"), fallback="Unknown"),
                "summary": _clean_text(
                    hint.get("summary") or manifest_item.get("tooltip_plain") or manifest_item.get("description"),
                    fallback="本地模板识别结果",
                ),
                "images": images,
                "source_icon_filenames": filenames,
                "priority": 1 if hint_result.get("ok") else 0,
            }
    return _attach_observed_name_exemplars(build_template_index(raw_templates), asset_dir)


def load_or_build_default_template_runtime(
    base_dir: str | Path | None = None,
    *,
    hint_cache: Mapping[str, Any] | None = None,
    cache_file: str | Path | None = None,
    resource_signature: Mapping[str, Any] | None = None,
    status_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TemplateRuntime:
    """加载 sidecar 模板 runtime；cache miss 才重建模板索引和矩阵。"""

    started_at = time.perf_counter()
    target_cache = Path(cache_file) if cache_file is not None else TEMPLATE_RUNTIME_CACHE_FILE
    signature = dict(resource_signature or template_runtime_resource_signature(base_dir))
    hint_signature = _hint_cache_signature(hint_cache)
    if status_callback is not None:
        status_callback(
            "template_runtime_cache_lookup",
            {
                "schema_version": TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION,
                "cache_file": str(target_cache),
            },
        )
    runtime = _read_template_runtime_cache(
        target_cache,
        resource_signature=signature,
        hint_signature=hint_signature,
    )
    if runtime is not None:
        runtime.stats.update({"build_seconds": 0.0, "load_seconds": round(time.perf_counter() - started_at, 3)})
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
                {
                    "build_seconds": 0.0,
                    "load_seconds": round(time.perf_counter() - started_at, 3),
                    "lock_wait_seconds": lock_wait_seconds,
                }
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
    stats = {
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
    }
    if status_callback is not None:
        status_callback("template_runtime_cache_ready", stats)
    return TemplateRuntime(template_index=template_index, matrices=matrices, stats=stats)


def rank_template_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    return _rank_matrices(template_index)


def _rank_matrices(template_index: Sequence[TemplateEntry]) -> _RankMatrices:
    from hextech.infrastructure.vision.sidecar_matching import _rank_matrices as build_rank_matrices

    return build_rank_matrices(template_index)

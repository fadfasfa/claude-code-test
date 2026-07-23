"""Vision 模板资源签名与内存诊断。

这里不参与识别决策，只提供 cache 失效依据和可重复的矩阵内存观测，方便避免
冷构建把完整指纹复制成大量 Python 对象。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image

from hextech.infrastructure.vision.template_models import _RankMatrices
from hextech.modules.data.ports.paths import ASSET_DIR, INDEX_DATA_DIR


TEMPLATE_RUNTIME_MAX_WORKING_SET_BYTES = int(1.5 * 1024 * 1024 * 1024)


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


def template_runtime_resource_signature(base_dir: str | Path | None = None, *, schema_version: int) -> dict[str, Any]:
    """生成模板资源签名；资源、环境或矩阵 schema 改变时 cache 自动失效。"""

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
        "schema_version": int(schema_version),
        "environment": _runtime_environment_signature(),
        "version_digest": _hash_runtime_resource_stats(version_files),
        "asset_digest": _hash_runtime_resource_stats(asset_files),
        "version_file_count": len(version_files),
        "asset_file_count": len(asset_files),
    }


def template_runtime_hint_signature(hint_cache: Mapping[str, Any] | None) -> str:
    """只纳入会影响模板身份/图标解析的 hint 字段，忽略运行时元数据。"""

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
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        serialized = repr(payload)
    return hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()


def template_runtime_matrix_bytes(matrices: _RankMatrices) -> int:
    return int(
        matrices.icon_matrix.nbytes
        + matrices.name_matrix.nbytes
        + matrices.alt_name_matrix.nbytes
        + matrices.observed_name_matrix.nbytes
    )


def template_runtime_memory_profile(matrices: _RankMatrices) -> dict[str, Any]:
    """返回矩阵占用与当前进程 RSS；RSS 无法读取时仍可验证矩阵预算。"""

    rss_bytes = 0
    try:
        import psutil

        rss_bytes = int(psutil.Process().memory_info().rss)
    except (ImportError, OSError):
        pass
    matrix_bytes = template_runtime_matrix_bytes(matrices)
    return {
        "matrix_bytes": matrix_bytes,
        "matrix_megabytes": round(matrix_bytes / (1024 * 1024), 3),
        "matrix_dtype": str(matrices.icon_matrix.dtype),
        "working_set_bytes": rss_bytes,
        "working_set_megabytes": round(rss_bytes / (1024 * 1024), 3),
        "within_working_set_budget": not rss_bytes or rss_bytes <= TEMPLATE_RUNTIME_MAX_WORKING_SET_BYTES,
    }

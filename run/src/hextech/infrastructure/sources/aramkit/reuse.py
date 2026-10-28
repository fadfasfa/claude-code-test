"""ARAMKit verified current 的 marker 与完整性复用判定。

本模块只读取并验证现有 source pointer/manifest，不抓取、不发布、不切换 current。
检查周期只决定何时检查；相同上游 marker 不因本地数据年龄重新构建。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from hextech.modules.data.source_runs import (
    SourceRunValidationError,
    load_source_current,
    load_source_run_manifest,
)


PointerValidator = Callable[[Mapping[str, Any]], object]


def load_reusable_current(
    marker: Mapping[str, Any],
    *,
    now: datetime,
    validator: PointerValidator,
    catalog_generation_id: str = "",
    catalog_sha256: str = "",
) -> tuple[dict[str, Any], bool]:
    try:
        pointer = load_source_current("aramkit", verify_hash=True)
    except (OSError, SourceRunValidationError, ValueError):
        return {}, False
    if not pointer:
        return {}, False
    if catalog_generation_id and str(pointer.get("catalog_generation_id") or "") != catalog_generation_id:
        return pointer, False
    if catalog_sha256 and str(pointer.get("catalog_sha256") or "") != catalog_sha256:
        return pointer, False
    if pointer.get("artifact", {}).get("role") == "hero_rankings":
        from hextech.modules.data.source_runs import source_root
        from hextech.modules.data.generation import DataSnapshotClient
        from hextech.modules.data.generation.release_store import closure
        from .schema import version_marker
        try:
            runtime = source_root("aramkit").parent.parent
            view = DataSnapshotClient(runtime / "snapshots").open_view()
            _catalog, ranking, heroes, _optional = closure(runtime, view.manifest)
            if (ranking["run_id"] != pointer["run_id"] or not heroes
                    or set(heroes) != set(view.manifest.components["champions"])):
                return pointer, False
            if any(component.get("source_version") != marker.get("dataPath") for component in view.manifest.components["champions"].values()):
                return pointer, False
            manifest = load_source_run_manifest("aramkit", pointer["run_id"])
            if manifest is None or version_marker(manifest.metadata.get("version", {})) != dict(marker):
                return pointer, False
            return pointer, True
        except (OSError, ValueError, RuntimeError):
            return {}, False
    try:
        validator(pointer)
    except (OSError, SourceRunValidationError, ValueError):
        return {}, False
    manifest = load_source_run_manifest("aramkit", str(pointer["run_id"]))
    if manifest is None:
        raise SourceRunValidationError("ARAMKit current manifest 缺失")
    current_marker = manifest.metadata.get("marker")
    if not isinstance(current_marker, Mapping) or dict(current_marker) != dict(marker):
        return pointer, False

    del now  # Compatibility with callers; age no longer controls content reuse.
    return pointer, True


__all__ = ["load_reusable_current"]

"""ARAMKit verified current 的 marker 与绝对时效复用判定。

本模块只读取并验证现有 source pointer/manifest，不抓取、不发布、不切换 current。
相同 marker 也必须服从共享五小时时效预算；非法时间 fail closed。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from hextech.modules.data.freshness import source_reuse_allowed
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
) -> tuple[dict[str, Any], bool]:
    pointer = load_source_current("aramkit", verify_hash=True)
    if not pointer:
        return {}, False
    validator(pointer)
    manifest = load_source_run_manifest("aramkit", str(pointer["run_id"]))
    if manifest is None:
        raise SourceRunValidationError("ARAMKit current manifest 缺失")
    current_marker = manifest.metadata.get("marker")
    if not isinstance(current_marker, Mapping) or dict(current_marker) != dict(marker):
        return pointer, False

    pointer_success_at = str(pointer.get("last_success_at") or "")
    # 非法值不能借 manifest 掩盖；只有旧 pointer 真正缺值时才兼容 completed_at。
    data_at = pointer_success_at or str(manifest.completed_at or "")
    return pointer, source_reuse_allowed("aramkit", data_at, now)


__all__ = ["load_reusable_current"]

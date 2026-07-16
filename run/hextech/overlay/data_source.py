"""游戏内显示的数据源边界。

当前实现从 DataService 发布的固定 generation 读取提示数据，同时复用本地事件和
英雄上下文；替换数据实现时只需保持 ``OverlayDataSource`` 接口不变。

调用方: overlay.__main__、overlay.host、overlay.lifecycle; 关键依赖: data_snapshot、overlay.events、overlay.context。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from hextech.data_core import SnapshotViewPort


class OverlayDataSource(Protocol):
    """host 每次绘制所需的最小只读数据接口。"""

    def read_event(self) -> dict[str, Any]: ...

    def read_hint_cache(self) -> dict[str, Any]: ...

    def read_context(self) -> dict[str, Any]: ...

    def open_view(self) -> SnapshotViewPort | None: ...


class SharedOverlayDataSource:
    """只读取 DataService 当前完整 generation，不混入旧共享 cache。"""

    def __init__(self, *, snapshot_client=None) -> None:
        from hextech.data_snapshot import DataSnapshotClient

        self._snapshot_client = snapshot_client or DataSnapshotClient()

    def read_event(self) -> dict[str, Any]:
        from hextech.overlay.events import read_overlay_event

        return read_overlay_event()

    def read_hint_cache(self) -> dict[str, Any]:
        try:
            snapshot_view = self._snapshot_client.open_view()
        except Exception:
            snapshot_view = None
        if snapshot_view is not None:
            status = snapshot_view.status()
            payload = snapshot_view.get_overlay_hints()
            payload.setdefault("snapshot", {}).update(status)
            return payload
        status = self._snapshot_client.status()
        return {
            "schema_version": 1,
            "source": {"private_policy_stats_enabled": False},
            "hints": {},
            "name_index": {},
            "snapshot": status,
        }

    def read_context(self) -> dict[str, Any]:
        from hextech.overlay.context import read_overlay_context

        return read_overlay_context()

    def open_view(self) -> SnapshotViewPort | None:
        """固定打开当前 generation；一次 render tick 内不得切换数据代。"""

        try:
            return self._snapshot_client.open_view()
        except Exception:
            return None


def prepare_shared_overlay_data() -> dict[str, Any]:
    """只读探测当前快照，不在 Overlay 启动路径构建或写入共享数据。"""

    return SharedOverlayDataSource().read_hint_cache()


def source_has_private_stats(cache: Mapping[str, Any] | None) -> bool:
    source = cache.get("source") if isinstance(cache, Mapping) else None
    return bool(isinstance(source, Mapping) and source.get("private_policy_stats_enabled") is True)

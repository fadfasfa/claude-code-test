"""命令内一次性执行 DataService cohort 刷新。

该入口供构建工具等非守护进程调用，复用正式 coordinator、source worker 与
generation publisher；不维护旧 CSV/DataFrame 刷新编排，也不自行拼接来源。
"""

from __future__ import annotations

from typing import Any

from hextech.bootstrap.data_service_runtime import build_snapshot_from_runtime
from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
from hextech.modules.data.generation import DataSnapshotPublisher


def refresh_runtime_once(*, force: bool = True) -> dict[str, Any]:
    """运行一个完整 refresh cycle，并返回 coordinator 的结构化状态。"""

    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(),
        builder=build_snapshot_from_runtime,
    )
    return coordinator.refresh(force=force)


__all__ = ["refresh_runtime_once"]

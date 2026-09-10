"""命令内一次性执行 DataService cohort 刷新。

该入口供构建工具等非守护进程调用，复用正式 coordinator、source worker 与
generation publisher；不维护旧 CSV/DataFrame 刷新编排，也不自行拼接来源。
"""

from __future__ import annotations

import threading
from typing import Any

from hextech.bootstrap.data_service_runtime import build_snapshot_from_runtime
from hextech.bootstrap.game_refresh_gate import probe_production_game_in_progress
from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
from hextech.infrastructure.sources.aramkit.service import probe_aramkit_upstream_marker
from hextech.modules.data.generation import DataSnapshotPublisher


REFRESH_ONCE_GAME_POLL_SECONDS = 0.05


def refresh_runtime_once(*, force: bool = True, scope: str = "due") -> dict[str, Any]:
    """运行一个完整 refresh cycle，并返回 coordinator 的结构化状态。"""

    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(),
        builder=build_snapshot_from_runtime,
        upstream_marker_probe=probe_aramkit_upstream_marker,
        game_state_probe=probe_production_game_in_progress,
    )
    monitor_stop = threading.Event()

    def monitor_game_state() -> None:
        while not monitor_stop.wait(REFRESH_ONCE_GAME_POLL_SECONDS):
            coordinator.poll_deferred_refresh()

    monitor = threading.Thread(
        target=monitor_game_state,
        daemon=True,
        name="hextech-refresh-once-game-monitor",
    )
    monitor.start()
    try:
        return coordinator.refresh(force=force, scope=scope)
    finally:
        monitor_stop.set()
        monitor.join(timeout=1.0)


__all__ = ["refresh_runtime_once"]

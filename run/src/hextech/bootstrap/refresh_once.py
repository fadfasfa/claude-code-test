"""命令内一次性执行 DataService cohort 刷新。

该入口供构建工具等非守护进程调用，复用正式 coordinator、source worker 与
generation publisher；不维护旧 CSV/DataFrame 刷新编排，也不自行拼接来源。
"""

from __future__ import annotations

import threading
from typing import Any

from hextech.bootstrap.game_refresh_gate import probe_production_game_in_progress
from hextech.infrastructure.sources.refresh_service import IncrementalRefreshService
from hextech.infrastructure.sources.download_context import read_priority_champion
from hextech.infrastructure.sources.aramkit.service import probe_aramkit_upstream_marker
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.data.generation import DataSnapshotPublisher


REFRESH_ONCE_GAME_POLL_SECONDS = 0.5


def refresh_runtime_once(*, force: bool = True, scope: str = "due") -> dict[str, Any]:
    """运行一个完整 refresh cycle，并返回 coordinator 的结构化状态。"""

    coordinator = IncrementalRefreshService(
        publisher=DataSnapshotPublisher(),
        root=get_var_dir(),
        game_state_probe=probe_production_game_in_progress,
        champion_probe=read_priority_champion,
        marker_probe=lambda: probe_aramkit_upstream_marker(
            conditional_cache_root=get_var_dir() / "state" / "http-validators"),
    )
    monitor_stop = threading.Event()

    def monitor_game_state() -> None:
        while not monitor_stop.wait(REFRESH_ONCE_GAME_POLL_SECONDS):
            coordinator.poll_context()

    monitor = threading.Thread(
        target=monitor_game_state,
        daemon=True,
        name="hextech-refresh-once-game-monitor",
    )
    monitor.start()
    try:
        result = coordinator.refresh(force=force, scope=scope)
        coordinator.wait_optional()
        return result
    finally:
        monitor_stop.set()
        monitor.join(timeout=1.0)


__all__ = ["refresh_runtime_once"]

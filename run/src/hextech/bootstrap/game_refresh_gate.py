"""对局期刷新延后状态机。

该模块只判断新鲜游戏状态并合并赛后恢复请求；worker 取消文件仍由 refresh
coordinator 持有，避免状态机接触进程生命周期和来源抓取细节。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping


GAME_STATUS_MAX_AGE_SECONDS = 10.0
POST_GAME_REFRESH_DELAY_SECONDS = 30.0


class GameRefreshDeferred(RuntimeError):
    """对局开始导致本轮协作取消；不属于来源失败或 backoff。"""


class GameRefreshGate:
    """记录合并后的延后请求，并在赛后静默期结束时只释放一次。"""

    def __init__(
        self,
        *,
        root: Path,
        current_generation_id: Callable[[], str],
        now: Callable[[], datetime],
        game_state_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.root = root
        self.current_generation_id = current_generation_id
        self.now = now
        self.game_state_probe = game_state_probe
        self._lock = threading.Lock()
        self._deferred = False
        self._force = False
        self._game_ended_at: datetime | None = None

    def in_progress(self) -> bool:
        """只在已有可用代时信任新鲜游戏态，冷启动不会被阻塞。"""

        if not self.current_generation_id():
            return False
        if self.game_state_probe is not None:
            try:
                return bool(self.game_state_probe())
            except Exception:
                return False
        path = self.root / "state" / "game_overlay_visibility.v1.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            updated_at = float(payload.get("updated_at") or 0.0)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if updated_at <= 0.0 or self.now().timestamp() - updated_at > GAME_STATUS_MAX_AGE_SECONDS:
            return False
        host = payload.get("host") if isinstance(payload.get("host"), Mapping) else {}
        return bool(host.get("gameflow")) or str(host.get("gameflow_state") or "") == "in_progress"

    def defer_result(self, *, force: bool) -> dict[str, Any]:
        with self._lock:
            self._deferred = True
            self._force = self._force or bool(force)
            self._game_ended_at = None
        return {
            "state": "ready",
            "refresh_state": "deferred",
            "deferred_reason": "game_in_progress",
            "reason_code": "game_in_progress",
            "generation_id": self.current_generation_id(),
        }

    def mark_worker_cancelled(self) -> None:
        with self._lock:
            self._deferred = True
            self._game_ended_at = None

    def poll(self, *, in_game: bool) -> bool | None:
        """返回一次合并后的 force 标记；None 表示无需恢复或仍在等待。"""

        with self._lock:
            if in_game:
                if self._deferred:
                    self._game_ended_at = None
                return None
            if not self._deferred:
                return None
            now = self.now()
            if self._game_ended_at is None:
                self._game_ended_at = now
                return None
            if now - self._game_ended_at < timedelta(seconds=POST_GAME_REFRESH_DELAY_SECONDS):
                return None
            force = self._force
            self._deferred = False
            self._force = False
            self._game_ended_at = None
            return force


__all__ = [
    "GAME_STATUS_MAX_AGE_SECONDS",
    "GameRefreshDeferred",
    "GameRefreshGate",
    "POST_GAME_REFRESH_DELAY_SECONDS",
]

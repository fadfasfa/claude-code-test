"""DataService 首次自动刷新宽限。

完整 verified bundle seed 已能直接服务时，本模块把首次网络刷新移出 Overlay
冷启动关键区；它不处理手动刷新，也不拥有 game_in_progress 延后逻辑。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


# 只避开首屏冷启动；代际收敛由真实安装凭据和消费者更新保证，不靠加长等待。
VERIFIED_SEED_AUTO_REFRESH_GRACE_SECONDS = 30.0


def initial_auto_refresh_delay_seconds(bootstrap_result: Mapping[str, Any]) -> float:
    if (
        str(bootstrap_result.get("state") or "") in {"ready", "degraded"}
        and str(bootstrap_result.get("source") or "") == "verified_seed"
        and bool(str(bootstrap_result.get("generation_id") or "").strip())
    ):
        return VERIFIED_SEED_AUTO_REFRESH_GRACE_SECONDS
    return 0.0


@dataclass
class StartupRefreshSchedule:
    """只消费一次的 monotonic deadline；不阻塞 DataService bootstrap。"""

    due_at: float
    delay_seconds: float
    pending: bool

    @classmethod
    def create(
        cls,
        bootstrap_result: Mapping[str, Any],
        *,
        skip: bool,
        now: float,
    ) -> "StartupRefreshSchedule":
        delay = initial_auto_refresh_delay_seconds(bootstrap_result)
        return cls(due_at=float(now) + delay, delay_seconds=delay, pending=not skip)

    def consume_if_due(self, now: float) -> bool:
        if not self.pending or float(now) < self.due_at:
            return False
        self.pending = False
        return True


__all__ = [
    "StartupRefreshSchedule",
    "VERIFIED_SEED_AUTO_REFRESH_GRACE_SECONDS",
    "initial_auto_refresh_delay_seconds",
]

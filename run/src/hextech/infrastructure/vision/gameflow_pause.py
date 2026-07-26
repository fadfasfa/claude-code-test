"""Vision Sidecar 暂停期的低频 gameflow 仲裁。

窗口短暂不可见时维持选择证据；只有本机 gameflow 明确不在对局中，才发布保留
原 epoch/revision 的结束事件。这一模块不访问窗口、截图或磁盘。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hextech.infrastructure.vision.state import SelectionTracker
from hextech.modules.vision.gameflow import GameflowState, probe_gameflow_state


logger = logging.getLogger(__name__)
GAMEFLOW_PAUSE_PROBE_SECONDS = 1.0


def pause_identity(session_id: str, game_identity: Mapping[str, object]) -> dict[str, object]:
    """为暂停和结束事件保留最后已知会话，供报告与时间线关联。"""

    return {
        "session_id": session_id,
        "game_instance_id": str(game_identity.get("game_instance_id") or session_id),
    }


@dataclass
class PausedGameflowProbe:
    """在窗口暂停期间缓存短时 gameflow 结论，避免高频请求本机接口。"""

    probe: Callable[[], GameflowState] = probe_gameflow_state
    interval_seconds: float = GAMEFLOW_PAUSE_PROBE_SECONDS
    last_probe_at: float | None = None
    state: GameflowState = GameflowState.UNKNOWN

    def reset(self) -> None:
        self.last_probe_at = None
        self.state = GameflowState.UNKNOWN

    def observe(self, *, should_probe: bool, now: float) -> GameflowState:
        if not should_probe:
            return GameflowState.UNKNOWN
        if self.last_probe_at is None or now - self.last_probe_at >= self.interval_seconds:
            self.last_probe_at = now
            try:
                self.state = self.probe()
            except Exception:
                # LCU/2999 瞬断只能降级为 unknown，不能把 Alt-Tab 当成对局结束。
                logger.debug("暂停期检查 gameflow 失败。", exc_info=True)
                self.state = GameflowState.UNKNOWN
        return self.state


def resolve_game_visibility_pause(
    tracker: SelectionTracker,
    *,
    reason: str,
    should_probe: bool,
    now: float,
    gameflow_probe: PausedGameflowProbe,
    identity_source: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """返回暂停或确认结束事件，并保留当前游戏的会话关联。"""

    state = gameflow_probe.observe(should_probe=should_probe, now=now)
    gameflow_ended = state is GameflowState.NOT_IN_PROGRESS
    event_payload = (
        tracker.complete("gameflow_ended", source=identity_source)
        if gameflow_ended
        else tracker.pause(reason, source=identity_source)
    )
    raw_source = event_payload.get("source")
    source = {str(key): value for key, value in raw_source.items()} if isinstance(raw_source, Mapping) else {}
    if isinstance(identity_source, Mapping):
        # 结束后 runner 会清空内存中的窗口身份；在此处复制有限身份字段，确保
        # session report 与 selection timeline 仍能关联刚刚结束的 epoch。
        for key in ("session_id", "game_instance_id"):
            if key in identity_source:
                source[key] = identity_source[key]
    source["gameflow_state"] = state.value
    if gameflow_ended:
        source["gameflow_ended"] = True
    event_payload["source"] = source
    return event_payload, gameflow_ended


__all__ = ["GAMEFLOW_PAUSE_PROBE_SECONDS", "PausedGameflowProbe", "pause_identity", "resolve_game_visibility_pause"]

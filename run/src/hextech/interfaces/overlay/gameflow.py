"""Overlay 对局状态兼容入口。

三态探测的权威实现位于 ``hextech.modules.vision.gameflow``；本模块保留既有
Overlay/桌面导入路径，并让测试与调用方继续使用相同的公开接口。
"""

from __future__ import annotations

from hextech.modules.vision import gameflow as _core
from hextech.modules.vision.gameflow import (
    DEFAULT_LCU_TIMEOUT_SECONDS,
    DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS,
    GameflowState,
    LcuScanner,
)


def configure_lcu_scanner(scanner: LcuScanner) -> None:
    _core.configure_lcu_scanner(scanner)


def lcu_scanner_configured() -> bool:
    return _core.lcu_scanner_configured()


def probe_live_client_in_progress(*, timeout: float = DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS) -> bool | None:
    return _core.probe_live_client_in_progress(timeout=timeout)


def probe_lcu_gameflow_in_progress(*, timeout: float = DEFAULT_LCU_TIMEOUT_SECONDS) -> bool | None:
    return _core.probe_lcu_gameflow_in_progress(timeout=timeout)


def probe_gameflow_state() -> GameflowState:
    """通过本兼容入口组装三态，保留调用方的可替换 probe seam。"""

    live_client_state = probe_live_client_in_progress()
    if live_client_state is True:
        return GameflowState.IN_PROGRESS
    lcu_state = probe_lcu_gameflow_in_progress()
    if lcu_state is None:
        return GameflowState.UNKNOWN
    return GameflowState.IN_PROGRESS if lcu_state else GameflowState.NOT_IN_PROGRESS


def probe_gameflow_in_progress() -> bool:
    return probe_gameflow_state() is GameflowState.IN_PROGRESS


__all__ = [
    "DEFAULT_LCU_TIMEOUT_SECONDS",
    "DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS",
    "GameflowState",
    "LcuScanner",
    "configure_lcu_scanner",
    "lcu_scanner_configured",
    "probe_gameflow_in_progress",
    "probe_gameflow_state",
    "probe_lcu_gameflow_in_progress",
    "probe_live_client_in_progress",
]

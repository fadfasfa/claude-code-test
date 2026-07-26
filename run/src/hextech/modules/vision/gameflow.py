"""本机 League 对局状态探测的可复用核心。

该模块只保存三态语义和可注入的 LCU 扫描器；Host、Desktop 与 Vision Sidecar
都从这里读取同一状态，避免基础设施层反向依赖接口层。
"""

from __future__ import annotations

import base64
import warnings
from collections.abc import Callable
from enum import Enum


LIVE_CLIENT_ACTIVE_PLAYER_ENDPOINT = "https://127.0.0.1:2999/liveclientdata/activeplayer"
LCU_GAMEFLOW_PHASE_ENDPOINT = "/lol-gameflow/v1/gameflow-phase"
DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS = 0.25
DEFAULT_LCU_TIMEOUT_SECONDS = 0.35
LcuScanner = Callable[[], tuple[int | None, str | None]]


def _empty_lcu_scanner() -> tuple[None, None]:
    return None, None


_lcu_scanner: LcuScanner = _empty_lcu_scanner


class GameflowState(str, Enum):
    """实际对局的三态结论；unknown 不得隐式等同于未开局。"""

    IN_PROGRESS = "in_progress"
    NOT_IN_PROGRESS = "not_in_progress"
    UNKNOWN = "unknown"


def configure_lcu_scanner(scanner: LcuScanner) -> None:
    """由组合根注入只读取本机 League 进程的扫描器。"""

    global _lcu_scanner
    _lcu_scanner = scanner


def lcu_scanner_configured() -> bool:
    """供运行时自检确认扫描器已注入，不实际发起请求。"""

    return _lcu_scanner is not _empty_lcu_scanner


def _http_get(url: str, **kwargs):
    import requests

    return requests.get(url, **kwargs)


def probe_live_client_in_progress(*, timeout: float = DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS) -> bool | None:
    """2999 live-client 可访问时视为正在实际对局；不可判断时返回 unknown。"""

    import urllib3

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            response = _http_get(
                LIVE_CLIENT_ACTIVE_PLAYER_ENDPOINT,
                headers={"Accept": "application/json"},
                verify=False,
                timeout=float(timeout),
            )
    except Exception:
        return None
    if int(getattr(response, "status_code", 0) or 0) != 200:
        return None
    return True


def probe_lcu_gameflow_in_progress(*, timeout: float = DEFAULT_LCU_TIMEOUT_SECONDS) -> bool | None:
    """通过本机 LCU gameflow 判断实际对局；接口不可用时返回 unknown。"""

    import urllib3

    port, token = _lcu_scanner()
    if not port or not token:
        return None
    auth = base64.b64encode(f"riot:{token}".encode("utf-8")).decode("ascii")
    url = f"https://127.0.0.1:{port}{LCU_GAMEFLOW_PHASE_ENDPOINT}"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            response = _http_get(
                url,
                headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
                verify=False,
                timeout=float(timeout),
            )
    except Exception:
        return None
    if int(getattr(response, "status_code", 0) or 0) != 200:
        return None
    try:
        phase = response.json()
    except ValueError:
        return None
    return str(phase or "").strip() == "InProgress"


def probe_gameflow_state() -> GameflowState:
    """优先使用 2999，再用 LCU；两者不可用时保留 unknown。"""

    live_client_state = probe_live_client_in_progress()
    if live_client_state is True:
        return GameflowState.IN_PROGRESS
    lcu_state = probe_lcu_gameflow_in_progress()
    if lcu_state is None:
        return GameflowState.UNKNOWN
    return GameflowState.IN_PROGRESS if lcu_state else GameflowState.NOT_IN_PROGRESS


def probe_gameflow_in_progress() -> bool:
    """兼容旧 bool 调用方；新调用方应直接消费三态接口。"""

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

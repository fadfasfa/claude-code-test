"""LoL 对局状态探测。

本模块只访问本机 LoL/LCU 接口，不读取凭据文件。LCU token 仅从
`LeagueClientUx.exe` 命令行实时提取，调用方不得把 token 写入日志。

调用方: display.desktop.runtime、overlay.host; 关键依赖: requests、overlay.providers.official。
"""

from __future__ import annotations

import base64
import warnings

LIVE_CLIENT_ACTIVE_PLAYER_ENDPOINT = "https://127.0.0.1:2999/liveclientdata/activeplayer"
LCU_GAMEFLOW_PHASE_ENDPOINT = "/lol-gameflow/v1/gameflow-phase"
DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS = 0.25
DEFAULT_LCU_TIMEOUT_SECONDS = 0.35


def _http_get(url: str, **kwargs):
    import requests

    return requests.get(url, **kwargs)


def probe_live_client_in_progress(*, timeout: float = DEFAULT_LIVE_CLIENT_TIMEOUT_SECONDS) -> bool | None:
    """2999 live-client 可访问时视为已进入实际对局；不可判断返回 None。"""

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
    return bool(int(getattr(response, "status_code", 0) or 0) == 200)


def probe_lcu_gameflow_in_progress(*, timeout: float = DEFAULT_LCU_TIMEOUT_SECONDS) -> bool | None:
    """通过 LCU gameflow 判断是否 InProgress；LCU 不可用时返回 None。"""

    from hextech.overlay.providers.official import scan_lcu_process
    import urllib3

    port, token = scan_lcu_process()
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


def probe_gameflow_in_progress() -> bool:
    """优先用 2999 判断实际对局，再用 LCU gameflow 兜底。"""

    live_client_state = probe_live_client_in_progress()
    if live_client_state is not None:
        return live_client_state
    lcu_state = probe_lcu_gameflow_in_progress()
    return bool(lcu_state)

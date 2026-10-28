"""桌面本机LCU观察；网络采集与窗口绘制不进入同一线程。"""
from __future__ import annotations
import base64
import threading
import warnings
import psutil
import requests
import urllib3
from typing import Any, TYPE_CHECKING
from hextech.modules.game_context.client import ClientContextProvider, parse_client_context
LCU_LOCAL_REQUEST_TIMEOUT_SECONDS = 1.0
if TYPE_CHECKING:
    from hextech.interfaces.desktop.app import HextechUI

def _parse_local_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def scan_lcu_process() -> tuple:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] == "LeagueClientUx.exe":
                port, token = None, None
                for arg in proc.info["cmdline"] or []:
                    if arg.startswith("--app-port="):
                        port = arg.split("=", 1)[1]
                    if arg.startswith("--remoting-auth-token="):
                        token = arg.split("=", 1)[1]
                if port and token:
                    return port, token
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return None, None


def _clean_champion_id(value) -> str:
    from hextech.contracts.identifiers import optional_champion_id

    return str(optional_champion_id(value) or "")


def _append_unique_champion_id(target: list[str], value) -> None:
    champion_id = _clean_champion_id(value)
    if champion_id and champion_id not in target:
        target.append(champion_id)


def build_lcu_candidate_groups(payload: dict) -> dict[str, list[str]]:
    """从 LCU champ-select payload 生成桌面悬浮窗候选分组。"""
    if not isinstance(payload, dict):
        return {"selected_champion_ids": [], "bench_champion_ids": []}
    return parse_client_context(payload).candidate_groups()


def _candidate_groups_to_id_set(candidate_groups: dict[str, list[str]]) -> set[str]:
    return {
        champion_id
        for key in ("selected_champion_ids", "bench_champion_ids")
        for champion_id in candidate_groups.get(key, [])
        if champion_id
    }


def _get_lcu_champ_select_session(url: str, headers: dict[str, str]) -> requests.Response:
    """LCU 是本机短轮询接口，不能复用资源下载的重试 session。"""

    return requests.get(
        url,
        headers=headers,
        verify=False,
        timeout=LCU_LOCAL_REQUEST_TIMEOUT_SECONDS,
    )


def _get_client_context_provider(ui: "HextechUI") -> ClientContextProvider:
    provider = getattr(ui, "_client_context_provider", None)
    if not isinstance(provider, ClientContextProvider):
        provider = ClientContextProvider()
        ui._client_context_provider = provider
    return provider


def _degraded_candidate_groups(ui: "HextechUI", error_code: str) -> dict[str, Any]:
    return _get_client_context_provider(ui).unavailable(error_code).candidate_groups(include_roles=True)


def poll_lcu_live_ids(ui: "HextechUI"):
    if not ui._lcu_port or not ui._lcu_token:
        port, token = scan_lcu_process()
        parsed_port = _parse_local_port(port)
        if parsed_port is None or not token:
            ui._lcu_port = None
            ui._lcu_token = None
            return _degraded_candidate_groups(ui, "client_not_found")
        ui._lcu_port = parsed_port
        ui._lcu_token = token

    current_port = _parse_local_port(ui._lcu_port)
    if current_port is None:
        ui._lcu_port = None
        ui._lcu_token = None
        return _degraded_candidate_groups(ui, "invalid_client_port")

    auth = base64.b64encode(f"riot:{ui._lcu_token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    url = f"https://127.0.0.1:{current_port}/lol-champ-select/v1/session"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            res = _get_lcu_champ_select_session(url, headers)
    except requests.exceptions.RequestException:
        ui._lcu_port = None
        ui._lcu_token = None
        return _degraded_candidate_groups(ui, "request_failed")

    if res.status_code == 404:
        return _get_client_context_provider(ui).not_in_champ_select().candidate_groups(include_roles=True)
    if res.status_code in (401, 403):
        ui._lcu_port = None
        ui._lcu_token = None
        return _degraded_candidate_groups(ui, "authorization_failed")
    if res.status_code != 200:
        ui._lcu_port = None
        return _degraded_candidate_groups(ui, f"http_{res.status_code}")

    try:
        payload = res.json()
    except ValueError:
        return _degraded_candidate_groups(ui, "invalid_json")

    client_context = _get_client_context_provider(ui).update(payload)
    candidate_groups = client_context.candidate_groups(include_roles=True)
    return candidate_groups


def initialize_window_threads(ui: "HextechUI") -> None:
    """在重服务/快照加载前启动窗口与选人观察；整个进程只启动一组。"""
    from hextech.interfaces.desktop.runtime_window import candidate_update_loop, lcu_polling_loop, window_sync_loop

    if getattr(ui, "_desktop_observers_started", False):
        return
    ui._desktop_observers_started = True
    threads = [
        threading.Thread(target=lcu_polling_loop, args=(ui,), daemon=True, name="desktop-client-context"),
        threading.Thread(target=window_sync_loop, args=(ui,), daemon=True, name="desktop-window-probe"),
        threading.Thread(target=candidate_update_loop, args=(ui,), daemon=True, name="desktop-candidates"),
    ]
    ui.threads.extend(threads)
    for thread in threads:
        thread.start()


def initialize_core_threads(ui: "HextechUI") -> None:
    initialize_window_threads(ui)

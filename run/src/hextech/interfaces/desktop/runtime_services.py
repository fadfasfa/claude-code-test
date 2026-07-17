"""桌面 UI 运行时辅助层。

文件职责：
- 承载桌面端后台线程、窗口联动和资源加载等非纯界面逻辑

核心输入：
- `HextechUI` 主类持有的状态、控件和会话对象
- Web live_state、LCU 本地接口和本地图片资源

核心输出：
- 桌面端后台刷新、英雄联动、图片缓存和窗口状态同步

主要依赖：
- `hextech.modules.data.ports.paths`

维护提醒：
- Tk 组件结构仍应留在 `hextech.interfaces.desktop.app`
- 新增后台线程、轮询或资源下载逻辑优先集中在本文件

调用方: dev_checks; 关键依赖: psutil、requests、display.web。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

import psutil
import requests
import urllib3
import win32gui
from PIL import Image, ImageDraw, ImageTk

from hextech.modules.game_context.client import ClientContextProvider, parse_client_context
from hextech.interfaces.overlay import context as overlay_context
from hextech.interfaces.overlay.gameflow import probe_lcu_gameflow_in_progress, probe_live_client_in_progress
from hextech.modules.vision.window import find_lol_game_window, is_window_renderable
from hextech.modules.vision.window_titles import LOL_CLIENT_WINDOW_TITLE
from hextech.modules.data.ports.paths import ASSET_DIR, BASE_DIR
from hextech.modules.vision.image_validation import is_valid_png_bytes

if TYPE_CHECKING:
    from hextech.interfaces.desktop.app import HextechUI


logger = logging.getLogger(__name__)
_preload_status_executor: ThreadPoolExecutor | None = None
GAMEFLOW_VISIBILITY_POLL_SECONDS = 1.0
LCU_LOCAL_REQUEST_TIMEOUT_SECONDS = 1.0



# ruff: noqa: E402, F401, F403, F405
from hextech.interfaces.desktop.runtime_processes import *


def _write_overlay_context_from_live_state(*args, **kwargs):
    """延迟进入交互层，避免 LCU 服务与 UI 预加载形成导入环。"""

    from hextech.interfaces.desktop.runtime_interaction import _write_overlay_context_from_live_state as write_context

    return write_context(*args, **kwargs)

def _parse_local_port(raw_port) -> int | None:
    try:
        port = int(str(raw_port or "").strip())
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _read_web_port_once(web_port_file: str) -> int | None:
    try:
        with open(web_port_file, "r", encoding="utf-8") as f:
            return _parse_local_port(f.read())
    except OSError:
        return None


def _is_safe_local_http_base(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and _parse_local_port(parsed.port) is not None


def resolve_web_base(web_port_file: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        port = _read_web_port_once(web_port_file)
        if port is not None:
            return f"http://127.0.0.1:{port}"
        time.sleep(0.1)
    return f"http://127.0.0.1:{SERVER_PORT}"


def _resolve_auth_token_file(web_port_file: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(web_port_file)), "auth_token.txt")


def _read_web_auth_token_once(web_port_file: str) -> str:
    try:
        with open(_resolve_auth_token_file(web_port_file), "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return ""
    return token if _AUTH_TOKEN_RE.fullmatch(token) else ""


def resolve_web_auth_token(web_port_file: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = _read_web_auth_token_once(web_port_file)
        if token:
            return token
        time.sleep(0.1)
    return _read_web_auth_token_once(web_port_file)


def _clear_web_readiness_files(web_port_file: str) -> None:
    """清理上一次 Web 启动留下的端口/token 文件，避免把旧状态误判为成功。"""

    for path in (web_port_file, _resolve_auth_token_file(web_port_file)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("清理 Web readiness 文件失败：%s", path, exc_info=True)


def _wait_for_web_startup(web_process, web_port_file: str, timeout: float = 8.0) -> None:
    """等待 Web 子进程写出端口和 token；进程早退时直接失败。"""

    deadline = time.time() + timeout
    while time.time() < deadline:
        poll = getattr(web_process, "poll", None)
        if callable(poll):
            exit_code = poll()
            if exit_code is not None:
                raise RuntimeError(f"Web 服务进程提前退出，exit_code={exit_code}")
        port = _read_web_port_once(web_port_file)
        token = _read_web_auth_token_once(web_port_file)
        if port is not None and token:
            return
        time.sleep(0.1)

    raise RuntimeError("Web 服务启动超时：未写出有效端口或 token")


def open_companion_browser(web_port_file: str) -> bool:
    """由桌面父进程打开可回收的本地 Web 浏览器窗口。"""

    web_base = resolve_web_base(web_port_file, timeout=1.0)
    return _web_runtime().open_managed_browser(
        web_base,
        replace_existing=True,
        allow_system_fallback=False,
    )


def close_companion_browser() -> bool:
    """关闭桌面父进程持有的受管浏览器窗口。"""

    return _web_runtime().terminate_managed_browser()


def _web_auth_headers(ui: "HextechUI", web_base: str, timeout: float = 0.5) -> dict[str, str]:
    web_port_file = str(getattr(ui, "web_port_file", "") or "")
    token = resolve_web_auth_token(web_port_file, timeout=timeout) if web_port_file else ""
    return {"Origin": web_base, "X-Hextech-Token": token}


def _web_frontend_available(ui: "HextechUI") -> bool:
    manager = getattr(ui, "service_manager", None)
    if manager is not None:
        return bool(manager.is_web_running())
    process = getattr(ui, "web_process", None)
    return bool(process and getattr(process, "poll", lambda: None)() is None)


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
        _write_overlay_context_from_live_state(ui, {"local_champion_id": 0}, source="lcu")
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
    local_champion_id = candidate_groups.get("local_champion_id")
    if local_champion_id:
        _write_overlay_context_from_live_state(
            ui,
            {**candidate_groups, "local_champion_id": local_champion_id},
            source="lcu",
        )
    else:
        _write_overlay_context_from_live_state(ui, {"local_champion_id": 0}, source="lcu")
    return candidate_groups


def start_web_server_process(web_port_file: str, *, auto_open_browser: bool = True):
    startupinfo = None
    child_env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        source_root = str(Path(__file__).resolve().parents[3])
        child_env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, child_env.get("PYTHONPATH", ""))))
    # 浏览器由桌面父进程打开和关闭；子进程只负责 Web 服务本体。
    child_env["HEXTECH_OPEN_BROWSER"] = "0"
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--web-server"]
        cwd = BASE_DIR
    else:
        command = [sys.executable, "-m", "hextech.bootstrap.web"]
        cwd = BASE_DIR
    _clear_web_readiness_files(web_port_file)
    web_process = subprocess.Popen(
        command,
        startupinfo=startupinfo,
        cwd=cwd,
        env=child_env,
    )
    try:
        _wait_for_web_startup(web_process, web_port_file, timeout=8.0)
    except Exception:
        try:
            web_process.terminate()
            web_process.wait(timeout=3)
        except Exception:
            try:
                web_process.kill()
            except Exception:
                pass
        raise
    return web_process


def initialize_core_threads(ui: "HextechUI") -> None:
    # 延迟导入拆分后的循环 owner，避免 runtime_window 回引本模块时形成导入环。
    from hextech.interfaces.desktop.runtime_interaction import run_terminal_loop
    from hextech.interfaces.desktop.runtime_window import lcu_polling_loop, window_sync_loop

    threads = [
        threading.Thread(target=lcu_polling_loop, args=(ui,), daemon=True),
        threading.Thread(target=window_sync_loop, args=(ui,), daemon=True),
        threading.Thread(target=run_terminal_loop, args=(ui,), daemon=True),
    ]
    ui.threads.extend(threads)
    for thread in threads:
        thread.start()



__all__ = [name for name in globals() if not name.startswith("__")]

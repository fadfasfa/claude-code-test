"""桌面 UI 运行时辅助层。

文件职责：
- 承载桌面端后台线程、窗口联动和资源加载等非纯界面逻辑

核心输入：
- `HextechUI` 主类持有的状态、控件和会话对象
- Web live_state、LCU 本地接口和本地图片资源

核心输出：
- 桌面端后台刷新、英雄联动、图片缓存和窗口状态同步

主要依赖：
- `hextech.catalog.query_terminal`
- `hextech.scraping._paths`

维护提醒：
- Tk 组件结构仍应留在 `hextech.display.desktop.app`
- 新增后台线程、轮询或资源下载逻辑优先集中在本文件
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
import threading
import time
import webbrowser
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

import psutil
import requests
import urllib3
import win32gui
from PIL import Image, ImageDraw, ImageTk

from hextech.display.web import runtime as web_runtime
from hextech.overlay import context as overlay_context
from hextech.overlay.window import find_lol_game_window, is_window_renderable
from hextech.catalog.query_terminal import display_hero_hextech, main_query, set_last_hero
from hextech.overlay.window_titles import LOL_CLIENT_WINDOW_TITLE
from hextech.scraping._paths import ASSET_DIR, BASE_DIR

if TYPE_CHECKING:
    from hextech.display.desktop.app import HextechUI


logger = logging.getLogger(__name__)
_preload_status_executor: ThreadPoolExecutor | None = None


def _executor_shutdown(executor: ThreadPoolExecutor | None) -> bool:
    return executor is None or bool(getattr(executor, "_shutdown", False))


def _executor_queue_size(executor: ThreadPoolExecutor | None) -> int:
    queue = getattr(executor, "_work_queue", None)
    qsize = getattr(queue, "qsize", None)
    if callable(qsize):
        try:
            return int(qsize())
        except Exception:
            return 0
    return 0


def _get_preload_status_executor() -> ThreadPoolExecutor:
    global _preload_status_executor
    if _executor_shutdown(_preload_status_executor):
        _preload_status_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ui-preload-status")
    return _preload_status_executor


def ensure_desktop_executors_started() -> None:
    _get_preload_status_executor()


def shutdown_desktop_executors(*, wait: bool = True, cancel_futures: bool = True) -> None:
    global _preload_status_executor
    if _preload_status_executor is not None and not _executor_shutdown(_preload_status_executor):
        _preload_status_executor.shutdown(wait=wait, cancel_futures=cancel_futures)
    _preload_status_executor = None


def get_desktop_executor_health() -> dict:
    return {
        "preload_status": {
            "shutdown": _executor_shutdown(_preload_status_executor),
            "queue_depth": _executor_queue_size(_preload_status_executor),
        }
    }


def _load_server_port() -> int:
    raw_port = str(os.getenv("HEXTECH_PORT", "8000")).strip()
    try:
        port = int(raw_port)
    except ValueError:
        return 8000
    return port if 1024 <= port <= 65535 else 8000


SERVER_PORT = _load_server_port()
_AUTH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


class _WindowsJobObject:
    """Windows kill-on-close Job Object 句柄；非 Windows 不创建。"""

    def __init__(self, process: subprocess.Popen) -> None:
        self.handle = None
        self.attached = False
        if os.name != "nt":
            return
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]
            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return
            if not kernel32.AssignProcessToJobObject(handle, int(process._handle)):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self._kernel32 = kernel32
            self.attached = True
        except Exception:
            logger.debug("Runtime Supervisor Job Object 绑定失败。", exc_info=True)

    def close(self) -> None:
        if self.handle:
            try:
                self._kernel32.CloseHandle(self.handle)
            except Exception:
                logger.debug("Runtime Supervisor Job Object 关闭失败。", exc_info=True)
            self.handle = None


@dataclass
class RuntimeSupervisorHandle:
    """桌面侧持有的 Runtime Supervisor bootstrap 信息。"""

    process: subprocess.Popen
    supervisor_instance_id: str
    port: int
    session_nonce: str
    pid: int
    job_object: _WindowsJobObject | None = None

    @property
    def job_object_attached(self) -> bool:
        return bool(self.job_object and self.job_object.attached)

    def renew_lease(self, *, control_instance_id: str) -> dict:
        response = requests.post(
            f"http://127.0.0.1:{self.port}/v1/lease/renew",
            headers={
                "Host": "127.0.0.1",
                "X-Hextech-Supervisor-Nonce": self.session_nonce,
            },
            json={"control_instance_id": control_instance_id},
            timeout=2,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _wait_supervisor_pid_exit(self, *, timeout: float) -> None:
        if self.pid == self.process.pid:
            return
        deadline = time.time() + max(0.1, float(timeout))
        while time.time() < deadline:
            if not psutil.pid_exists(self.pid):
                return
            time.sleep(0.05)
        try:
            process = psutil.Process(self.pid)
            process.terminate()
            process.wait(timeout=1.0)
        except psutil.TimeoutExpired:
            try:
                process.kill()
            except psutil.Error:
                pass
        except psutil.Error:
            pass

    def stop(self, *, timeout: float = 5.0) -> None:
        shutdown_requested = False
        try:
            requests.post(
                f"http://127.0.0.1:{self.port}/v1/shutdown",
                headers={
                    "Host": "127.0.0.1",
                    "X-Hextech-Supervisor-Nonce": self.session_nonce,
                },
                timeout=1,
            )
            shutdown_requested = True
        except Exception:
            logger.debug("Runtime Supervisor shutdown API 未响应，改用进程终止。", exc_info=True)
        if shutdown_requested and self.process.poll() is None:
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        self._wait_supervisor_pid_exit(timeout=timeout)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if self.job_object is not None:
            self.job_object.close()


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def start_runtime_supervisor_process(*, parent_pid: int | None = None, timeout: float = 8.0) -> RuntimeSupervisorHandle:
    """启动独立 Runtime Supervisor，并通过 stdout 匿名管道读取 bootstrap JSON。"""

    parent = int(parent_pid or os.getpid())
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--runtime-supervisor", "--parent-pid", str(parent)]
    else:
        command = [sys.executable, os.path.join(BASE_DIR, "hextech_ui.py"), "--runtime-supervisor", "--parent-pid", str(parent)]
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        startupinfo=_hidden_startupinfo(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    deadline = time.time() + float(timeout)
    line = ""
    line_queue: queue.Queue[str] = queue.Queue(maxsize=1)

    def _read_bootstrap_line() -> None:
        if process.stdout is None:
            return
        try:
            value = process.stdout.readline().strip()
        except Exception:
            value = ""
        if value:
            try:
                line_queue.put_nowait(value)
            except queue.Full:
                pass

    threading.Thread(target=_read_bootstrap_line, name="hextech-supervisor-bootstrap", daemon=True).start()
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"Runtime Supervisor 提前退出：code={process.returncode}; stderr={stderr[-500:]}")
            try:
                line = line_queue.get(timeout=0.05)
                break
            except queue.Empty:
                pass
            time.sleep(0.05)
        if not line:
            raise TimeoutError("Runtime Supervisor bootstrap 超时")
        payload = json.loads(line)
        handle = RuntimeSupervisorHandle(
            process=process,
            supervisor_instance_id=str(payload["supervisor_instance_id"]),
            port=int(payload["port"]),
            session_nonce=str(payload["session_nonce"]),
            pid=int(payload["pid"]),
        )
        handle.job_object = _WindowsJobObject(process)
        return handle
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        raise


def stop_runtime_supervisor_process(handle: RuntimeSupervisorHandle | None) -> None:
    """停止 Runtime Supervisor，供桌面 UI 退出路径统一调用。"""

    if handle is None:
        return
    handle.stop()


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
    return web_runtime.open_managed_browser(
        web_base,
        replace_existing=True,
        allow_system_fallback=False,
    )


def close_companion_browser() -> bool:
    """关闭桌面父进程持有的受管浏览器窗口。"""

    return web_runtime.terminate_managed_browser()


def _web_auth_headers(ui: "HextechUI", web_base: str, timeout: float = 0.5) -> dict[str, str]:
    token = resolve_web_auth_token(ui.web_port_file, timeout=timeout)
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
    text = str(value or "").strip()
    return text if text and text != "0" else ""


def _append_unique_champion_id(target: list[str], value) -> None:
    champion_id = _clean_champion_id(value)
    if champion_id and champion_id not in target:
        target.append(champion_id)


def build_lcu_candidate_groups(payload: dict) -> dict[str, list[str]]:
    """从 LCU champ-select payload 生成桌面悬浮窗候选分组。"""

    selected_ids: list[str] = []
    bench_ids: list[str] = []
    if not isinstance(payload, dict):
        return {"selected_champion_ids": selected_ids, "bench_champion_ids": bench_ids}
    for player in payload.get("myTeam", []):
        if isinstance(player, dict):
            _append_unique_champion_id(selected_ids, player.get("championId"))
    for champion in payload.get("benchChampions", []):
        if isinstance(champion, dict):
            _append_unique_champion_id(bench_ids, champion.get("championId"))
    selected_set = set(selected_ids)
    return {
        "selected_champion_ids": selected_ids,
        "bench_champion_ids": [champion_id for champion_id in bench_ids if champion_id not in selected_set],
    }


def _candidate_groups_to_id_set(candidate_groups: dict[str, list[str]]) -> set[str]:
    return {
        champion_id
        for key in ("selected_champion_ids", "bench_champion_ids")
        for champion_id in candidate_groups.get(key, [])
        if champion_id
    }


def poll_lcu_live_ids(ui: "HextechUI"):
    if not ui._lcu_port or not ui._lcu_token:
        port, token = scan_lcu_process()
        parsed_port = _parse_local_port(port)
        if parsed_port is None or not token:
            ui._lcu_port = None
            ui._lcu_token = None
            return None
        ui._lcu_port = parsed_port
        ui._lcu_token = token

    current_port = _parse_local_port(ui._lcu_port)
    if current_port is None:
        ui._lcu_port = None
        ui._lcu_token = None
        return None

    auth = base64.b64encode(f"riot:{ui._lcu_token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    url = f"https://127.0.0.1:{current_port}/lol-champ-select/v1/session"

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
            res = ui.session.get(url, headers=headers, verify=False, timeout=2.5)
    except requests.exceptions.RequestException:
        ui._lcu_port = None
        ui._lcu_token = None
        return None

    if res.status_code == 404:
        _write_overlay_context_from_live_state(ui, {"local_champion_id": 0}, source="lcu")
        return {"selected_champion_ids": [], "bench_champion_ids": []}
    if res.status_code in (401, 403):
        ui._lcu_port = None
        ui._lcu_token = None
        return None
    if res.status_code != 200:
        ui._lcu_port = None
        return None

    try:
        payload = res.json()
    except ValueError:
        return None

    candidate_groups = build_lcu_candidate_groups(payload)
    available_ids = _candidate_groups_to_id_set(candidate_groups)
    local_cell_id = payload.get("localPlayerCellId")
    local_champion_id = None
    for player in payload.get("myTeam", []):
        if not isinstance(player, dict):
            continue
        if player.get("cellId") == local_cell_id and player.get("championId"):
            local_champion_id = player.get("championId")
            available_ids.add(str(local_champion_id))
            break
    if local_champion_id:
        _write_overlay_context_from_live_state(
            ui,
            {"local_champion_id": local_champion_id},
            source="lcu",
        )
    else:
        _write_overlay_context_from_live_state(ui, {"local_champion_id": 0}, source="lcu")
    return candidate_groups


def start_web_server_process(web_port_file: str, *, auto_open_browser: bool = True):
    startupinfo = None
    child_env = os.environ.copy()
    if not getattr(sys, "frozen", False):
        child_env["HEXTECH_BASE_DIR"] = BASE_DIR
    # 浏览器由桌面父进程打开和关闭；子进程只负责 Web 服务本体。
    child_env["HEXTECH_OPEN_BROWSER"] = "0"
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--web-server"]
        cwd = BASE_DIR
    else:
        web_script = os.path.join(BASE_DIR, "web_server.py")
        command = [sys.executable, web_script]
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
    threads = [
        threading.Thread(target=lcu_polling_loop, args=(ui,), daemon=True),
        threading.Thread(target=window_sync_loop, args=(ui,), daemon=True),
        threading.Thread(target=run_terminal_loop, args=(ui,), daemon=True),
    ]
    ui.threads.extend(threads)
    for thread in threads:
        thread.start()


def run_terminal_loop(ui: "HextechUI") -> None:
    while not ui.stop_event.is_set():
        with ui._df_lock:
            is_empty = ui.df.empty
        if not is_empty:
            break
        time.sleep(0.5)
    if not ui.stop_event.is_set():
        with ui._df_lock:
            df_snapshot = ui.df
        main_query(shared_df=df_snapshot, ui_instance=ui)


def run_silent_sync(ui: "HextechUI", refresh_backend_data) -> None:
    """兼容旧入口；refresh 由 Runtime Supervisor 唯一发起。"""

    del refresh_backend_data
    if not ui.stop_event.is_set():
        logger.info("启动阶段静默刷新已停用：refresh 由 Runtime Supervisor action 发起。")


def _set_click_status(ui: "HextechUI", text: str, color: str) -> None:
    ui._hero_click_status = text
    ui._run_on_ui_thread(lambda: ui._set_status(text, color))


def _refresh_preload_ready(ui: "HextechUI", hero_name: str) -> bool:
    if not _web_frontend_available(ui):
        return False
    normalized_hero = str(hero_name or "").strip()
    if not normalized_hero:
        return False
    web_base = resolve_web_base(ui.web_port_file, timeout=1.0)
    try:
        response = requests.get(
            f"{web_base}/api/champion/{quote(normalized_hero)}/preload_status",
            headers=_web_auth_headers(ui, web_base, timeout=0.2),
            timeout=1.0,
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        is_ready = bool(payload.get("ready"))
        with ui._hero_preload_lock:
            ui._hero_preload_ready[normalized_hero] = is_ready
            if payload.get("pending"):
                ui._hero_preload_pending.add(normalized_hero)
            else:
                ui._hero_preload_pending.discard(normalized_hero)
        return is_ready
    except Exception:
        logger.debug("刷新英雄预热状态失败：hero=%s", normalized_hero, exc_info=True)
        return False


def _record_redirect_success(ui: "HextechUI", web_base: str) -> None:
    ui._last_redirect_success_base = web_base
    ui._last_redirect_success_at = time.time()


def _resolve_redirect_base(ui: "HextechUI") -> str:
    if ui._last_redirect_success_base and (time.time() - ui._last_redirect_success_at) < 60.0:
        return ui._last_redirect_success_base
    return resolve_web_base(ui.web_port_file, timeout=1.0)


def _store_live_state_marker(ui: "HextechUI", payload: dict, source: str) -> None:
    ui._last_live_state_version = int(payload.get("state_version", -1) or -1)
    ui._last_live_state_updated_at = float(payload.get("updated_at", 0.0) or 0.0)
    ui._last_live_state_source = source


def _is_newer_live_state(ui: "HextechUI", payload: dict, source: str) -> bool:
    state_version = int(payload.get("state_version", -1) or -1)
    updated_at = float(payload.get("updated_at", 0.0) or 0.0)
    if source != "web":
        return True
    if state_version > ui._last_live_state_version:
        return True
    if state_version == ui._last_live_state_version and updated_at > ui._last_live_state_updated_at:
        return True
    return False


def _sync_preload_state_for_candidates(ui: "HextechUI", hero_names: list[str]) -> None:
    if not hero_names:
        return
    normalized_names = [str(name).strip() for name in hero_names if str(name).strip()]
    with ui._hero_preload_lock:
        removed_names = [name for name in ui._hero_preload_ready.keys() if name not in normalized_names]
        for name in removed_names:
            ui._hero_preload_ready.pop(name, None)
            ui._hero_preload_pending.discard(name)


def _post_redirect(ui: "HextechUI", web_base: str, champ_id, hero_name, en_name: str) -> bool:
    response = requests.post(
        f"{web_base}/api/redirect",
        json={"hero_id": str(champ_id), "hero_name": hero_name},
        headers=_web_auth_headers(ui, web_base, timeout=0.2),
        timeout=1.5,
    )
    if response.status_code != 200:
        return False
    _record_redirect_success(ui, web_base)
    return True


def _open_detail_fallback(web_base: str, champ_id, hero_name: str, en_name: str) -> None:
    if not _is_safe_local_http_base(web_base):
        logger.warning("已拒绝打开非本机详情页地址：%s", web_base)
        return
    url = (
        f"{web_base}/detail.html"
        f"?hero={quote(str(hero_name or ''))}"
        f"&id={quote(str(champ_id or ''))}"
        f"&en={quote(str(en_name or ''))}"
        f"&auto=1"
        f"&detailFirst=1"
    )
    webbrowser.open(url)


def normalize_candidate_groups(candidate_groups) -> dict[str, list[str]]:
    """兼容旧 set/list 输入，并把候选分组收口到稳定 schema。"""

    if isinstance(candidate_groups, dict):
        selected = candidate_groups.get("selected_champion_ids") or candidate_groups.get("selected") or []
        bench = candidate_groups.get("bench_champion_ids") or candidate_groups.get("bench") or []
    else:
        selected = []
        bench = candidate_groups or []
    selected_ids: list[str] = []
    bench_ids: list[str] = []
    for value in selected:
        _append_unique_champion_id(selected_ids, value)
    for value in bench:
        _append_unique_champion_id(bench_ids, value)
    selected_set = set(selected_ids)
    return {
        "selected_champion_ids": selected_ids,
        "bench_champion_ids": [champion_id for champion_id in bench_ids if champion_id not in selected_set],
    }


def _resolve_candidate_hero_names(ui: "HextechUI", candidate_groups) -> list[str]:
    hero_names = []
    normalized = normalize_candidate_groups(candidate_groups)
    for key in ("selected_champion_ids", "bench_champion_ids"):
        for hero_id in normalized[key]:
            core_entry = ui.core_data.get(str(hero_id), {}) if isinstance(ui.core_data, dict) else {}
            hero_name = str(core_entry.get("name", "")).strip()
            if hero_name and hero_name not in hero_names:
                hero_names.append(hero_name)
    return hero_names


def _apply_candidate_update(ui: "HextechUI", candidate_groups, *, source: str, payload: dict | None = None) -> None:
    if payload and not _is_newer_live_state(ui, payload, source):
        return
    if payload:
        _store_live_state_marker(ui, payload, source)
        _write_overlay_context_from_live_state(ui, payload, source=source)
    normalized_groups = normalize_candidate_groups(candidate_groups)
    available_ids = _candidate_groups_to_id_set(normalized_groups)
    hero_names = _resolve_candidate_hero_names(ui, normalized_groups)
    _sync_preload_state_for_candidates(ui, hero_names)
    if available_ids != ui.current_hero_ids or normalized_groups != getattr(ui, "current_candidate_groups", {}):
        ui.current_hero_ids = available_ids.copy()
        ui.current_candidate_groups = normalized_groups
        if hero_names:
            _queue_ui_preload(ui, hero_names)
        ui.root.after(0, ui.update_ui, normalized_groups)
    elif hero_names:
        _queue_ui_preload(ui, hero_names)


def _fetch_web_live_state(ui: "HextechUI") -> tuple[dict[str, list[str]] | None, dict | None]:
    if not _web_frontend_available(ui):
        return None, None
    web_base = _resolve_redirect_base(ui)
    response = ui.session.get(f"{web_base}/api/live_state", timeout=2)
    if response.status_code != 200:
        return None, None
    payload = response.json()
    candidate_groups = normalize_candidate_groups(
        {
            "selected_champion_ids": payload.get("selected_champion_ids", []),
            "bench_champion_ids": payload.get("bench_champion_ids", payload.get("champion_ids", [])),
        }
    )
    web_ids = _candidate_groups_to_id_set(candidate_groups)
    local_champion_id = payload.get("local_champion_id")
    has_local_champion = False
    if isinstance(local_champion_id, int):
        has_local_champion = local_champion_id > 0
    else:
        local_text = str(local_champion_id or "").strip()
        has_local_champion = bool(local_text and local_text != "0")
    if web_ids or has_local_champion:
        return candidate_groups, payload
    return {"selected_champion_ids": [], "bench_champion_ids": []}, payload


def _clean_live_champion_id(value) -> str:
    text = str(value or "").strip()
    return text if text and text != "0" else ""


def _game_overlay_context_writable(ui: "HextechUI", *, context_path: str | os.PathLike[str] | None = None) -> bool:
    """只在 overlay 已运行或测试显式指定路径时写当前英雄上下文。"""

    if context_path is not None:
        return True
    service_manager = getattr(ui, "service_manager", None)
    is_running = getattr(service_manager, "is_game_overlay_running", None)
    if not callable(is_running):
        return False
    try:
        return bool(is_running())
    except Exception:
        logger.debug("检查游戏内 overlay 运行状态失败。", exc_info=True)
        return False


def _resolve_live_champion_name(ui: "HextechUI", champion_id: str, payload: dict) -> str:
    payload_name = str(payload.get("local_champion_name") or "").strip()
    if payload_name:
        return payload_name
    core_entry = ui.core_data.get(str(champion_id), {}) if isinstance(ui.core_data, dict) else {}
    return str(core_entry.get("name") or "").strip()


def _write_overlay_context_from_live_state(
    ui: "HextechUI",
    payload: dict,
    *,
    source: str,
    context_path: str | os.PathLike[str] | None = None,
) -> bool:
    """同步 overlay 当前英雄上下文；空英雄会写入明确缺失状态。"""

    if not isinstance(payload, dict):
        return False
    if not _game_overlay_context_writable(ui, context_path=context_path):
        return False
    champion_id = _clean_live_champion_id(payload.get("local_champion_id"))
    if not champion_id:
        try:
            overlay_context.write_missing_overlay_context(context_path, source=source)
        except OSError:
            logger.debug("写入 overlay 空英雄上下文失败。", exc_info=True)
        return False
    champion_name = _resolve_live_champion_name(ui, champion_id, payload)
    context_payload = overlay_context.build_overlay_context_payload(
        champion_id=champion_id,
        champion_name=champion_name,
        source=source,
    )
    try:
        overlay_context.write_overlay_context(context_payload, context_path)
    except OSError:
        logger.debug("写入 overlay 英雄上下文失败。", exc_info=True)
        return False
    return True


def _sync_candidate_ids(ui: "HextechUI", candidate_groups, *, source: str, payload: dict | None = None) -> None:
    if candidate_groups is None:
        return
    _apply_candidate_update(ui, candidate_groups, source=source, payload=payload)


def _fallback_live_state(ui: "HextechUI") -> dict[str, list[str]] | None:
    return poll_lcu_live_ids(ui)


def _handle_redirect_attempt(ui: "HextechUI", champ_id, hero_name: str, en_name: str) -> bool:
    if not _web_frontend_available(ui):
        return False
    web_base = _resolve_redirect_base(ui)
    try:
        return _post_redirect(ui, web_base, champ_id, hero_name, en_name)
    except Exception:
        logger.debug("请求 /api/redirect 失败，准备重试。", exc_info=True)
        return False


def _drain_preload_pending(ui: "HextechUI") -> None:
    if not _web_frontend_available(ui):
        return
    with ui._hero_preload_lock:
        pending_names = list(ui._hero_preload_pending)
    for hero_name in pending_names:
        _refresh_preload_ready(ui, hero_name)


def _wait_for_redirect_ready(ui: "HextechUI", hero_name: str) -> bool:
    normalized_hero = str(hero_name or "").strip()
    if not normalized_hero:
        return False
    deadline = time.time() + ui._hero_click_gate_timeout
    while time.time() < deadline and not ui.stop_event.is_set():
        if _refresh_preload_ready(ui, normalized_hero):
            return True
        time.sleep(ui._hero_click_gate_poll_interval)
    return _refresh_preload_ready(ui, normalized_hero)


def _normalize_hero_name(hero_name: str) -> str:
    return str(hero_name or "").strip()


def _mark_preload_pending(ui: "HextechUI", hero_names: list[str]) -> None:
    with ui._hero_preload_lock:
        for hero_name in hero_names:
            ui._hero_preload_pending.add(hero_name)
            ui._hero_preload_ready.setdefault(hero_name, False)


def _queue_preload_worker(ui: "HextechUI", hero_names: list[str]) -> None:
    if not _web_frontend_available(ui):
        return
    web_base = _resolve_redirect_base(ui)
    for hero_name in hero_names:
        try:
            requests.post(
                f"{web_base}/api/champion/{quote(hero_name)}/preload",
                headers=_web_auth_headers(ui, web_base, timeout=0.2),
                timeout=1.0,
            )
        except Exception:
            logger.debug("候选英雄预热请求失败：hero=%s", hero_name, exc_info=True)
        _refresh_preload_ready(ui, hero_name)


def _submit_preload(ui: "HextechUI", hero_names: list[str]) -> None:
    _get_preload_status_executor().submit(lambda: _queue_preload_worker(ui, hero_names))


def _queue_ui_preload(ui: "HextechUI", hero_names: list[str]) -> None:
    if not _web_frontend_available(ui):
        return
    normalized_names = []
    for hero_name in hero_names:
        normalized = _normalize_hero_name(hero_name)
        if normalized and normalized not in normalized_names:
            normalized_names.append(normalized)
    if not normalized_names:
        return
    _mark_preload_pending(ui, normalized_names)
    _submit_preload(ui, normalized_names)


def _refresh_clicked_hero_preload(ui: "HextechUI", hero_name: str) -> None:
    _refresh_preload_ready(ui, hero_name)


def _queue_clicked_hero_preload(ui: "HextechUI", hero_name: str) -> None:
    normalized_hero = _normalize_hero_name(hero_name)
    if not normalized_hero:
        return
    _queue_ui_preload(ui, [normalized_hero])
    _get_preload_status_executor().submit(lambda: _refresh_clicked_hero_preload(ui, normalized_hero))


def handle_hero_click(ui: "HextechUI", champ_id, hero_name) -> None:
    try:
        set_last_hero(hero_name)
    except Exception:
        logger.debug("记录最近一次英雄选择失败。", exc_info=True)

    def terminal_task():
        try:
            sys.stdout.write("\r" + " " * 80 + "\r")
            sys.stdout.flush()
            with ui._df_lock:
                df_snapshot = ui.df
            display_hero_hextech(df_snapshot, hero_name, is_from_ui=True)
        except Exception as exc:
            print(f"\n输出错误: {exc}")

    threading.Thread(target=terminal_task, daemon=True).start()

    def redirect_task():
        normalized_hero = _normalize_hero_name(hero_name)
        if not _web_frontend_available(ui):
            _set_click_status(ui, "Web 前端未启动，已跳过浏览器跳转", "#f9e2af")
            return
        en_name = ui.core_data.get(str(champ_id), {}).get("en_name", "")
        _set_click_status(ui, f"正在跳转 {normalized_hero}...", "#f9e2af")
        _queue_clicked_hero_preload(ui, normalized_hero)
        for _ in range(3):
            if _handle_redirect_attempt(ui, champ_id, hero_name, en_name):
                _set_click_status(ui, f"已跳转 {normalized_hero}，详情数据加载中", "#a6e3a1")
                return
            time.sleep(0.4)
        logger.debug("请求 /api/redirect 多次失败，回退到本地浏览器打开。")
        fallback_base = _resolve_redirect_base(ui)
        _set_click_status(ui, f"本地回退打开 {normalized_hero}", "#f9e2af")
        _open_detail_fallback(fallback_base, champ_id, hero_name, en_name)

    threading.Thread(target=redirect_task, daemon=True).start()



def lcu_polling_loop(ui: "HextechUI") -> None:
    """优先读取 Web live_state，失败时回退本地 LCU，持续同步可用英雄集合。"""
    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            time.sleep(1)
            continue

        available_ids = None
        payload = None
        if _web_frontend_available(ui):
            try:
                available_ids, payload = _fetch_web_live_state(ui)
            except Exception:
                available_ids = None
                payload = None

        if available_ids is None:
            available_ids = _fallback_live_state(ui)
            payload = None
            source = "lcu"
        else:
            source = "web"

        if available_ids is None:
            available_ids = set()

        _sync_candidate_ids(ui, available_ids, source=source, payload=payload)
        _drain_preload_pending(ui)
        time.sleep(1.5)


def _apply_rounded_corner(img: "Image.Image", radius: int = 8) -> "Image.Image":
    """为头像套一层圆角 alpha 遮罩，让头像在卡片背景上呈现柔和圆角。"""
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0], img.size[1]), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def load_and_set_img(ui: "HextechUI", champ_id, label) -> None:
    """加载英雄头像，优先命中本地缓存，缺失时远端下载后回写到本地。"""
    try:
        if not label.winfo_exists():
            return

        def _publish_cached(photo) -> None:
            if label.winfo_exists():
                label.config(image=photo)

        if champ_id in ui.image_cache:
            cached_photo = ui.image_cache[champ_id]
            ui._run_on_ui_thread(lambda p=cached_photo: _publish_cached(p))
            return

        img_path = os.path.join(ASSET_DIR, f"{champ_id}.png")
        if os.path.exists(img_path):
            with Image.open(img_path) as raw_img:
                img = raw_img.resize((48, 48), Image.Resampling.LANCZOS)
        else:
            if champ_id in ui.downloading_imgs:
                return
            ui.downloading_imgs.add(champ_id)
            url = f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-icons/{champ_id}.png"
            try:
                res = ui.session.get(url, verify=True, timeout=10)
                if res.status_code != 200:
                    return
                with ui.img_write_lock:
                    with open(img_path, "wb") as f:
                        f.write(res.content)
                with Image.open(BytesIO(res.content)) as raw_img:
                    img = raw_img.resize((48, 48), Image.Resampling.LANCZOS)
            finally:
                ui.downloading_imgs.discard(champ_id)

        # 头像渲染前套圆角遮罩，削弱方框直角的生硬感
        img = _apply_rounded_corner(img, radius=8)
        safe_img = img.copy()

        def _publish_loaded(image_obj=safe_img) -> None:
            photo = ImageTk.PhotoImage(image_obj)
            ui.image_cache[champ_id] = photo
            if label.winfo_exists():
                label.config(image=photo)

        ui._run_on_ui_thread(_publish_loaded)
    except Exception:
        logger.exception("加载英雄头像失败：champ_id=%s", champ_id)


def window_sync_loop(ui: "HextechUI") -> None:
    """根据客户端和游戏窗口状态控制伴生窗口显隐、置顶与持续跟随。"""
    manual_follow_cooldown = 8.0
    hide_grace_seconds = 1.0
    follow_resume_distance = 32
    last_visible_at = 0.0
    last_client_interaction_at = 0.0
    last_client_hwnd = None

    def _foreground_belongs_to_client(hwnd: int | None, foreground_hwnd: int | None) -> bool:
        if not hwnd or not foreground_hwnd:
            return False
        if foreground_hwnd == hwnd:
            return True
        try:
            return win32gui.IsChild(hwnd, foreground_hwnd)
        except Exception:
            return False

    def _has_recent_client_context(now_ts: float) -> bool:
        return (now_ts - last_client_interaction_at) < hide_grace_seconds

    def _target_overlay_position(hwnd_client: int, client_rect: tuple[int, int, int, int]) -> tuple[int, int]:
        try:
            client_area = win32gui.GetClientRect(hwnd_client)
            target_x, target_y = win32gui.ClientToScreen(hwnd_client, (client_area[2], 0))
            return (int(target_x), int(target_y))
        except Exception:
            logger.debug("计算客户端内容区右侧坐标失败，回退到窗口外框。", exc_info=True)
            return (int(client_rect[2]), int(client_rect[1]))

    def _client_rect_jump_detected(current_rect: tuple[int, int, int, int], previous_rect: tuple[int, int, int, int] | None) -> bool:
        if not previous_rect:
            return False
        return (
            abs(current_rect[0] - previous_rect[0]) > follow_resume_distance
            or abs(current_rect[1] - previous_rect[1]) > follow_resume_distance
            or abs(current_rect[2] - previous_rect[2]) > follow_resume_distance
            or abs(current_rect[3] - previous_rect[3]) > follow_resume_distance
        )

    def _update_overlay_position(target_pos: tuple[int, int]) -> None:
        ui._run_on_ui_thread(lambda pos=target_pos: ui._move_overlay_to(pos[0], pos[1]))

    def _should_keep_overlay_visible(client_active: bool, overlay_active: bool, now_ts: float) -> bool:
        return client_active or overlay_active or _has_recent_client_context(now_ts)

    def _set_client_interaction(now_ts: float, hwnd: int | None) -> None:
        nonlocal last_client_interaction_at, last_client_hwnd
        last_client_interaction_at = now_ts
        last_client_hwnd = hwnd

    def _reset_client_tracking() -> None:
        nonlocal last_client_hwnd
        last_client_hwnd = None
        ui._last_client_rect = None
        # LCU 客户端消失或切走时重置"首次吸附完成"标志，
        # 下次再次出现时仍能立即吸附而不必等客户端窗口实际位移
        ui._overlay_position_initialized = False

    def _resume_follow_if_ready(client_rect: tuple[int, int, int, int], target_pos: tuple[int, int]) -> None:
        client_jump_detected = _client_rect_jump_detected(client_rect, ui._last_client_rect)
        if client_jump_detected and ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
            ui._resume_auto_follow()
        if ui._auto_follow_enabled:
            _update_overlay_position(target_pos)
        elif ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
            ui._resume_auto_follow()
            _update_overlay_position(target_pos)

    def _sync_overlay_follow(hwnd_client: int, client_rect: tuple[int, int, int, int], should_show_overlay: bool) -> None:
        if not should_show_overlay:
            ui._last_client_rect = client_rect
            return
        rect_changed = client_rect != ui._last_client_rect
        # 首次显示时即使客户端窗口没动也强制吸附一次，避免玩家"挪一下客户端才跟随"的体感
        first_show = not ui._overlay_position_initialized
        target_pos = _target_overlay_position(hwnd_client, client_rect)
        if rect_changed or first_show:
            _resume_follow_if_ready(client_rect, target_pos)
            if ui._auto_follow_enabled:
                ui._overlay_position_initialized = True
        ui._last_client_rect = client_rect

    def _set_overlay_visibility(should_show_overlay: bool, should_keep_topmost: bool, now_ts: float) -> None:
        if should_show_overlay:
            nonlocal last_visible_at
            last_visible_at = now_ts
            ui._show_overlay(topmost=should_keep_topmost)
            return
        if ui._window_visible and (now_ts - last_visible_at) < hide_grace_seconds:
            ui._set_window_topmost(False)
            return
        ui._hide_overlay()

    def _update_client_visibility(now_ts: float, hwnd_client: int | None, client_visible: bool, client_active: bool) -> None:
        if client_visible and client_active:
            _set_client_interaction(now_ts, hwnd_client)
        elif not client_visible:
            _reset_client_tracking()

    def _is_same_client_window(hwnd: int | None) -> bool:
        return bool(hwnd and last_client_hwnd and hwnd == last_client_hwnd)

    def _overlay_active() -> bool:
        return ui._window_visible and is_self_fg

    def _resolve_overlay_policy(client_visible: bool, game_visible: bool, client_active: bool, overlay_active: bool, now_ts: float) -> tuple[bool, bool]:
        if game_visible:
            return False, False
        if not client_visible:
            return False, False
        should_show = _should_keep_overlay_visible(client_active, overlay_active, now_ts)
        return should_show, client_active

    def _sync_for_client(hwnd_client: int | None, client_visible: bool, should_show_overlay: bool) -> None:
        if not client_visible or not hwnd_client:
            _reset_client_tracking()
            if ui._manual_follow_cooldown_elapsed(manual_follow_cooldown):
                ui._resume_auto_follow()
            return
        client_rect = win32gui.GetWindowRect(hwnd_client)
        _sync_overlay_follow(hwnd_client, client_rect, should_show_overlay)

    def _client_active(is_client_fg: bool) -> bool:
        return is_client_fg

    def _client_or_overlay_active(is_client_fg: bool, is_self_fg_value: bool) -> tuple[bool, bool]:
        client_active = _client_active(is_client_fg)
        overlay_active = ui._window_visible and is_self_fg_value
        return client_active, overlay_active

    def _resolve_client_visibility(hwnd_client: int | None) -> bool:
        return bool(hwnd_client and win32gui.IsWindowVisible(hwnd_client) and not win32gui.IsIconic(hwnd_client))

    def _resolve_game_visibility(hwnd_game: int | None) -> bool:
        return is_window_renderable(hwnd_game)

    def _resolve_foreground_title(foreground_hwnd: int | None) -> str:
        return win32gui.GetWindowText(foreground_hwnd) if foreground_hwnd else ""

    def _resolve_self_fg(foreground_title: str) -> bool:
        return "Hextech" in foreground_title

    def _resolve_client_fg(hwnd_client: int | None, foreground_hwnd: int | None) -> bool:
        return _foreground_belongs_to_client(hwnd_client, foreground_hwnd)

    def _loop_once(now_ts: float) -> None:
        hwnd_client = win32gui.FindWindow(None, LOL_CLIENT_WINDOW_TITLE)
        game_target = find_lol_game_window()
        hwnd_game = game_target[0] if game_target is not None else None
        fg_window = win32gui.GetForegroundWindow()
        fg_title = _resolve_foreground_title(fg_window)
        is_client_fg = _resolve_client_fg(hwnd_client, fg_window)
        is_self_fg = _resolve_self_fg(fg_title)
        game_visible = _resolve_game_visibility(hwnd_game)
        client_visible = _resolve_client_visibility(hwnd_client)
        client_active, overlay_active = _client_or_overlay_active(is_client_fg, is_self_fg)
        _update_client_visibility(now_ts, hwnd_client, client_visible, client_active)
        should_show_overlay, should_keep_topmost = _resolve_overlay_policy(client_visible, game_visible, client_active, overlay_active, now_ts)
        _set_overlay_visibility(should_show_overlay, should_keep_topmost, now_ts)
        _sync_for_client(hwnd_client, client_visible, should_show_overlay)


    while not ui.stop_event.is_set():
        if ui.pause_event.is_set():
            time.sleep(1)
            continue
        try:
            now = time.time()
            _loop_once(now)
        except Exception:
            logger.exception("窗口同步循环异常。")
        time.sleep(0.2)

def start_background_scraper(ui: "HextechUI", refresh_backend_data) -> None:
    """兼容旧入口；不再启动桌面定时刷新线程。"""

    del refresh_backend_data
    if not ui.stop_event.is_set():
        logger.info("桌面定时刷新线程已停用：refresh 由 Runtime Supervisor action 发起。")

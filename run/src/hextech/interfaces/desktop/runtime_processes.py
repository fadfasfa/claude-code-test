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

def _web_runtime():
    """延迟导入 Web runtime，避免 Web 默认关闭时拖慢桌面首屏。"""

    from hextech.interfaces.web.backend import runtime as web_runtime

    return web_runtime


def _query_terminal():
    """延迟导入终端查询模块；它会加载 pandas，不进入桌面冷启动路径。"""

    from hextech.modules.data.catalog import query_terminal

    return query_terminal


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


def resolve_client_overlay_policy(
    *,
    client_visible: bool,
    game_hwnd_renderable: bool,
    gameflow_in_progress: bool,
    live_client_in_progress: bool,
    client_active: bool,
    overlay_active: bool,
    recent_client_context: bool,
) -> tuple[bool, bool]:
    """统一桌面伴生窗显隐：实际对局中必须隐藏客户端浮窗。"""

    game_visible = bool(game_hwnd_renderable or gameflow_in_progress or live_client_in_progress)
    if game_visible:
        return False, False
    if not client_visible:
        return False, False
    should_show = bool(client_active or overlay_active or recent_client_context)
    return should_show, bool(client_active)


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

    def _supervisor_headers(self) -> dict[str, str]:
        return {
            "Host": "127.0.0.1",
            "X-Hextech-Supervisor-Nonce": self.session_nonce,
        }

    def renew_lease(self, *, control_instance_id: str) -> dict:
        response = requests.post(
            f"http://127.0.0.1:{self.port}/v1/lease/renew",
            headers=self._supervisor_headers(),
            json={"control_instance_id": control_instance_id},
            timeout=2,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def get_status(self) -> dict:
        response = requests.get(
            f"http://127.0.0.1:{self.port}/v1/status",
            headers=self._supervisor_headers(),
            timeout=2,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def set_game_overlay_enabled(self, enabled: bool) -> dict:
        response = requests.post(
            f"http://127.0.0.1:{self.port}/v1/actions/game-overlay",
            headers=self._supervisor_headers(),
            json={"enabled": bool(enabled)},
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
                headers=self._supervisor_headers(),
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


@dataclass
class DataServiceHandle:
    """桌面持有的独立 DataService 进程与本机控制面。"""

    process: subprocess.Popen
    port: int
    session_nonce: str
    pid: int
    job_object: _WindowsJobObject | None = None

    def poll(self) -> int | None:
        """把受管子进程状态暴露给 ServiceManager。"""

        return self.process.poll()

    def close_exited_resources(self) -> bool:
        """仅在子进程已经退出后关闭管道和 Windows Job Object。"""

        if self.process.poll() is None:
            return False
        for stream in (self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if self.job_object is not None:
            self.job_object.close()
            self.job_object = None
        return True

    def _headers(self) -> dict[str, str]:
        return {"Host": "127.0.0.1", "X-Hextech-Data-Service-Nonce": self.session_nonce}

    def get_status(self) -> dict:
        response = requests.get(f"http://127.0.0.1:{self.port}/v1/status", headers=self._headers(), timeout=2)
        response.raise_for_status()
        return response.json()

    def refresh(self) -> dict:
        response = requests.post(
            f"http://127.0.0.1:{self.port}/v1/actions/refresh", headers=self._headers(), timeout=2
        )
        response.raise_for_status()
        return response.json()

    def _wait_for_action(self, action_id: str, *, timeout: float | None) -> dict:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while deadline is None or time.monotonic() < deadline:
            status = self.get_status()
            actions = status.get("actions")
            action = actions.get(action_id) if isinstance(actions, dict) else None
            if not isinstance(action, dict):
                last_action = status.get("last_action")
                action = last_action if isinstance(last_action, dict) and last_action.get("action_id") == action_id else None
            if isinstance(action, dict):
                result = action.get("result")
                return dict(result) if isinstance(result, dict) else {"state": "failed", "reason_code": "missing_result"}
            if self.process.poll() is not None:
                raise RuntimeError(f"DataService action 执行期间退出：code={self.process.returncode}")
            time.sleep(0.2)
        raise TimeoutError(f"DataService action 超时：action_id={action_id}")

    def set_private_stats(self, enabled: bool, *, timeout: float | None = None) -> dict:
        """等待同一策略 action 到终态，避免超时回滚后 action 又反向发布。"""

        response = requests.post(
            f"http://127.0.0.1:{self.port}/v1/actions/set-private-stats",
            headers=self._headers(),
            json={"enabled": bool(enabled)},
            timeout=2,
        )
        response.raise_for_status()
        accepted = response.json()
        action_id = str(accepted.get("action_id") or "")
        if not action_id:
            raise RuntimeError(str(accepted.get("reason_code") or "DataService action 未受理"))
        return self._wait_for_action(action_id, timeout=timeout)

    def stop(self, timeout: float = 5.0) -> None:
        try:
            requests.post(f"http://127.0.0.1:{self.port}/v1/shutdown", headers=self._headers(), timeout=1)
        except Exception:
            logger.debug("DataService shutdown API 未响应，改用进程终止。", exc_info=True)
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=timeout)
        self.close_exited_resources()


def _hidden_startupinfo():
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _drain_process_stream(
    stream,
    *,
    bootstrap_queue: queue.Queue[str] | None = None,
    tail: list[str] | None = None,
) -> None:
    """持续消费子进程文本管道，避免刷新日志填满 Windows pipe。"""

    bootstrap_pending = bootstrap_queue is not None
    try:
        while True:
            raw_line = stream.readline()
            if not raw_line:
                return
            line = raw_line.rstrip("\r\n")
            if bootstrap_pending and line:
                bootstrap_pending = False
                try:
                    bootstrap_queue.put_nowait(line)
                except queue.Full:
                    pass
                continue
            if tail is not None and line:
                tail.append(line)
                del tail[:-20]
    except (OSError, ValueError):
        return


def _pipe_tail_text(lines: list[str]) -> str:
    return "\n".join(lines)[-500:]


def _service_creationflags() -> int:
    """让长生命周期服务不继承 Desktop 控制台的 Ctrl+C 广播。"""

    return int(subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0


def start_runtime_supervisor_process(
    *,
    parent_pid: int | None = None,
    timeout: float = 15.0,
    prewarm_templates: bool = False,
) -> RuntimeSupervisorHandle:
    """启动独立 Runtime Supervisor，并通过 stdout 匿名管道读取 bootstrap JSON。"""

    parent = int(parent_pid or os.getpid())
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--runtime-supervisor", "--parent-pid", str(parent)]
        child_env = None
    else:
        command = [sys.executable, "-m", "hextech.bootstrap.supervisor", "--parent-pid", str(parent)]
        child_env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        child_env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, child_env.get("PYTHONPATH", ""))))
    if prewarm_templates:
        command.append("--prewarm-templates")
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        env=child_env,
        startupinfo=_hidden_startupinfo(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=_service_creationflags(),
    )
    deadline = time.time() + float(timeout)
    line = ""
    line_queue: queue.Queue[str] = queue.Queue(maxsize=1)
    stdout_tail: list[str] = []
    stderr_tail: list[str] = []
    if process.stdout is not None:
        threading.Thread(
            target=_drain_process_stream,
            kwargs={"stream": process.stdout, "bootstrap_queue": line_queue, "tail": stdout_tail},
            name="hextech-supervisor-stdout",
            daemon=True,
        ).start()
    if process.stderr is not None:
        threading.Thread(
            target=_drain_process_stream,
            kwargs={"stream": process.stderr, "tail": stderr_tail},
            name="hextech-supervisor-stderr",
            daemon=True,
        ).start()
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"Runtime Supervisor 提前退出：code={process.returncode}; stderr={_pipe_tail_text(stderr_tail)}"
                )
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


def start_data_service_process(
    *,
    parent_pid: int | None = None,
    timeout: float = 15.0,
    force_initial_refresh: bool = False,
) -> DataServiceHandle:
    """启动独立 DataService；bootstrap 后真实刷新在其后台线程继续。"""

    parent = int(parent_pid or os.getpid())
    command = (
        [sys.executable, "--data-service", "--parent-pid", str(parent)]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "hextech.bootstrap.data_service_runtime", "--parent-pid", str(parent)]
    )
    if force_initial_refresh:
        command.append("--force-initial-refresh")
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        startupinfo=_hidden_startupinfo(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=_service_creationflags(),
    )
    deadline = time.time() + timeout
    line = ""
    line_queue: queue.Queue[str] = queue.Queue(maxsize=1)
    stdout_tail: list[str] = []
    stderr_tail: list[str] = []
    if process.stdout is not None:
        threading.Thread(
            target=_drain_process_stream,
            kwargs={"stream": process.stdout, "bootstrap_queue": line_queue, "tail": stdout_tail},
            name="hextech-data-stdout",
            daemon=True,
        ).start()
    if process.stderr is not None:
        threading.Thread(
            target=_drain_process_stream,
            kwargs={"stream": process.stderr, "tail": stderr_tail},
            name="hextech-data-stderr",
            daemon=True,
        ).start()
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"DataService 提前退出：code={process.returncode}; stderr={_pipe_tail_text(stderr_tail)}")
            try:
                line = line_queue.get(timeout=0.05)
                break
            except queue.Empty:
                pass
            time.sleep(0.05)
        if not line:
            raise TimeoutError("DataService bootstrap 超时")
        payload = json.loads(line)
        handle = DataServiceHandle(
            process=process,
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


def stop_data_service_process(handle: DataServiceHandle | None) -> None:
    if handle is not None:
        handle.stop()


def stop_runtime_supervisor_process(handle: RuntimeSupervisorHandle | None) -> None:
    """停止 Runtime Supervisor，供桌面 UI 退出路径统一调用。"""

    if handle is None:
        return
    handle.stop()



__all__ = [name for name in globals() if not name.startswith("__")]

"""独立游戏内显示的进程生命周期。

Controller 把 Tk host 与 Vision sidecar 视为一个逻辑服务：启动失败原子回滚，停止时
先写 inactive 事件，再确保两个子进程都退出。桌面 UI 只调用本模块的统一接口。

调用方: display.desktop.app、display.desktop.service_manager、overlay.__main__; 关键依赖: catalog.runtime_store、overlay.events、support.atomic_io。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from hextech.modules.data.catalog.runtime_store import build_runtime_state_path, get_runtime_root_dir
from hextech.modules.vision.events import write_inactive_overlay_event
from hextech.modules.data.ports.atomic import atomic_write_json

from .context import start_overlay_context_poller
from hextech.modules.data.overlay_source import prepare_shared_overlay_data


logger = logging.getLogger(__name__)

OVERLAY_READY_FILE_ENV = "HEXTECH_OVERLAY_READY_FILE"
OVERLAY_READY_TOKEN_ENV = "HEXTECH_OVERLAY_READY_TOKEN"
OVERLAY_EXIT_FILE_ENV = "HEXTECH_OVERLAY_EXIT_FILE"
OVERLAY_SIDECAR_READY_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_FILE"
OVERLAY_SIDECAR_READY_TOKEN_ENV = "HEXTECH_OVERLAY_SIDECAR_READY_TOKEN"
OVERLAY_SIDECAR_BOOTSTRAP_FILE_ENV = "HEXTECH_OVERLAY_SIDECAR_BOOTSTRAP_FILE"
OVERLAY_GENERATION_ENV = "HEXTECH_OVERLAY_GENERATION"
OVERLAY_SIDECAR_DEBUG_DUMP_ENV = "HEXTECH_OVERLAY_SIDECAR_DEBUG_DUMP"
OVERLAY_READY_TIMEOUT_SECONDS = 5.0
OVERLAY_SIDECAR_READY_TIMEOUT_SECONDS = 180.0 if getattr(sys, "frozen", False) else 90.0
HOST_GRACEFUL_EXIT_TIMEOUT_SECONDS = 0.75
RUN_DIR = Path(__file__).resolve().parents[4]


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> Any: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[], ProcessLike]
ContextPollerFactory = Callable[[], Any]
_DEFAULT_CONTEXT_POLLER = object()


class SidecarBootstrapError(RuntimeError):
    """sidecar 在 readiness 前报告的结构化启动失败。"""

    def __init__(self, message: str, *, error_type: str, retryable: bool) -> None:
        super().__init__(message)
        self.error_type = str(error_type or "RuntimeError")
        self.retryable = bool(retryable)


class SidecarStartCancelled(RuntimeError):
    """sidecar readiness 等待被上层启动会话取消。"""


class SidecarCleanupError(RuntimeError):
    """sidecar 启动失败后无法确认进程已清理；禁止继续重试。"""

    retryable = False


def _hidden_startupinfo() -> Any:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _wait_for_host_ready(
    process: ProcessLike,
    ready_path: Path,
    *,
    expected_token: str = "",
    timeout_seconds: float = OVERLAY_READY_TIMEOUT_SECONDS,
) -> None:
    def _ready_pid(payload: dict) -> int:
        try:
            return int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            return 0

    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    ready_state = "missing"
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"game_overlay host 在 readiness 前退出，exit_code={exit_code}, pid={getattr(process, 'pid', None)}")
        try:
            payload = json.loads(ready_path.read_text(encoding="utf-8"))
            ready_state = "present"
        except FileNotFoundError:
            ready_state = "missing"
            time.sleep(0.05)
            continue
        except json.JSONDecodeError:
            ready_state = "invalid_json"
            time.sleep(0.05)
            continue
        except OSError:
            ready_state = "unreadable"
            time.sleep(0.05)
            continue
        pid = _ready_pid(payload)
        if expected_token and str(payload.get("token") or "") == expected_token:
            setattr(process, "_hextech_overlay_runtime_pid", pid or None)
            return
        if not expected_token and pid == int(process.pid):
            return
        raise RuntimeError(
            "game_overlay host readiness token 不匹配"
            f" (pid={payload.get('pid') or getattr(process, 'pid', None)}, ready_file=present, ready_name={ready_path.name})"
        )
    raise TimeoutError(
        "game_overlay host 启动超时："
        f"{float(timeout_seconds):.1f}s (ready_file={ready_state}, pid={getattr(process, 'pid', None)}, ready_name={ready_path.name})"
    )


def _wait_for_sidecar_ready(
    process: ProcessLike,
    ready_path: Path,
    *,
    bootstrap_path: Path | None = None,
    expected_token: str,
    timeout_seconds: float = OVERLAY_SIDECAR_READY_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> None:
    def _read_json(path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _bootstrap_retryable(payload: dict[str, Any]) -> bool:
        error_type = str(payload.get("error_type") or "")
        if error_type in {
            "FileNotFoundError",
            "PermissionError",
            "OSError",
            "ValueError",
            "JSONDecodeError",
            "UnicodeDecodeError",
        }:
            return False
        message = str(payload.get("error_message_sanitized") or "").casefold()
        deterministic_markers = ("template_missing", "模板缺失", "schema", "配置", "token 不匹配")
        return not any(marker.casefold() in message for marker in deterministic_markers)

    deadline = time.monotonic() + max(0.05, float(timeout_seconds))
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise SidecarStartCancelled("Vision sidecar 启动已取消")
        bootstrap = _read_json(bootstrap_path)
        if bootstrap is not None and str(bootstrap.get("token") or "") == expected_token:
            state = str(bootstrap.get("state") or "")
            if state == "failed":
                error_type = str(bootstrap.get("error_type") or "RuntimeError")
                message = str(bootstrap.get("error_message_sanitized") or "Vision sidecar 启动失败")
                raise SidecarBootstrapError(
                    f"Vision sidecar 启动失败：{message}",
                    error_type=error_type,
                    retryable=_bootstrap_retryable(bootstrap),
                )
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"Vision sidecar 在 readiness 前退出，exit_code={exit_code}")
        try:
            payload = json.loads(ready_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if str(payload.get("token") or "") != expected_token:
            raise RuntimeError("Vision sidecar readiness token 不匹配")
        setattr(process, "_hextech_overlay_sidecar_generation", str(payload.get("generation") or ""))
        return
    if cancel_event is not None and cancel_event.is_set():
        raise SidecarStartCancelled("Vision sidecar 启动已取消")
    raise TimeoutError(f"Vision sidecar 启动超时：{float(timeout_seconds):.1f}s")


def start_host_process() -> subprocess.Popen:
    """启动独立 Tk host，并等待 after_idle readiness。"""

    if getattr(sys, "frozen", False):
        command = [sys.executable, "--game-overlay"]
    else:
        command = [sys.executable, "-m", "hextech.interfaces.overlay.host"]
    ready_path = Path(build_runtime_state_path(f"game_overlay_host.{uuid.uuid4().hex}.ready.json"))
    exit_path = Path(build_runtime_state_path(f"game_overlay_host.{uuid.uuid4().hex}.exit.json"))
    ready_token = uuid.uuid4().hex
    env = os.environ.copy()
    env[OVERLAY_READY_FILE_ENV] = str(ready_path)
    env[OVERLAY_READY_TOKEN_ENV] = ready_token
    env[OVERLAY_EXIT_FILE_ENV] = str(exit_path)
    process = subprocess.Popen(command, cwd=RUN_DIR, startupinfo=_hidden_startupinfo(), env=env)
    setattr(process, "_hextech_overlay_exit_file", str(exit_path))
    try:
        _wait_for_host_ready(process, ready_path, expected_token=ready_token)
        return process
    except Exception:
        stop_process(process)
        raise
    finally:
        try:
            ready_path.unlink(missing_ok=True)
        except OSError:
            pass


def _sidecar_debug_dump_enabled(value: str | None = None) -> bool:
    """默认不落盘真机 ROI；只有显式诊断开关才写入 ignored runtime/debug。"""

    raw_value = os.environ.get(OVERLAY_SIDECAR_DEBUG_DUMP_ENV) if value is None else value
    return str(raw_value or "").strip().lower() in {"1", "true", "yes", "on"}


def start_sidecar_process(
    *,
    debug_dump: bool | None = None,
    readiness_timeout_seconds: float | None = None,
    cancel_event: threading.Event | None = None,
) -> subprocess.Popen:
    command = [sys.executable]
    if getattr(sys, "frozen", False):
        command.append("--overlay-sidecar")
    else:
        command.extend(["-m", "hextech.infrastructure.vision.sidecar"])
    command.extend([
        "--loop",
        "--preset",
        "auto",
        "--write-event",
    ])
    if debug_dump is True or (debug_dump is None and _sidecar_debug_dump_enabled()):
        diagnostic_dir = get_runtime_root_dir() / "debug" / "overlay_vision"
        command.extend(["--debug-dump", str(diagnostic_dir)])
    ready_path = Path(build_runtime_state_path(f"overlay_sidecar.{uuid.uuid4().hex}.ready.json"))
    bootstrap_path = Path(build_runtime_state_path(f"overlay_sidecar.{uuid.uuid4().hex}.bootstrap.json"))
    exit_path = Path(build_runtime_state_path(f"overlay_sidecar.{uuid.uuid4().hex}.exit.json"))
    ready_token = uuid.uuid4().hex
    generation = uuid.uuid4().hex
    env = os.environ.copy()
    env[OVERLAY_SIDECAR_READY_FILE_ENV] = str(ready_path)
    env[OVERLAY_SIDECAR_READY_TOKEN_ENV] = ready_token
    env[OVERLAY_SIDECAR_BOOTSTRAP_FILE_ENV] = str(bootstrap_path)
    env[OVERLAY_GENERATION_ENV] = generation
    env[OVERLAY_EXIT_FILE_ENV] = str(exit_path)
    process = subprocess.Popen(command, cwd=RUN_DIR, startupinfo=_hidden_startupinfo(), env=env)
    try:
        setattr(process, "_hextech_overlay_exit_file", str(exit_path))
        setattr(process, "_hextech_overlay_sidecar_generation", generation)
    except Exception:
        pass
    try:
        if callable(getattr(process, "poll", None)):
            _wait_for_sidecar_ready(
                process,
                ready_path,
                bootstrap_path=bootstrap_path,
                expected_token=ready_token,
                timeout_seconds=(
                    OVERLAY_SIDECAR_READY_TIMEOUT_SECONDS
                    if readiness_timeout_seconds is None
                    else max(0.05, float(readiness_timeout_seconds))
                ),
                cancel_event=cancel_event,
            )
        return process
    except Exception as exc:
        if not stop_process(process):
            raise SidecarCleanupError(f"Vision sidecar 启动失败且进程清理失败(pid={process.pid})") from exc
        raise
    finally:
        try:
            ready_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            bootstrap_path.unlink(missing_ok=True)
        except OSError:
            pass


def process_is_running(process: ProcessLike | None) -> bool:
    return bool(process is not None and process.poll() is None)


def _process_exit_path(process: ProcessLike | None) -> Path | None:
    value = str(getattr(process, "_hextech_overlay_exit_file", "") or "").strip()
    return Path(value) if value else None


def _cleanup_process_exit_signal(process: ProcessLike | None) -> None:
    exit_path = _process_exit_path(process)
    if exit_path is None:
        return
    try:
        exit_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("清理 overlay host 退出信号文件失败：%s", exit_path, exc_info=True)


def _request_process_exit(process: ProcessLike | None) -> bool:
    """先给 host 一个自愿退出窗口；无 signal 文件的 sidecar 继续走 terminate。"""

    if process is None or not process_is_running(process):
        return False
    exit_path = _process_exit_path(process)
    if exit_path is None:
        return False
    try:
        atomic_write_json(
            exit_path,
            {"pid": getattr(process, "pid", None), "requested_at": time.time()},
            ensure_ascii=False,
            indent=2,
        )
        return True
    except Exception:
        logger.warning("写入 overlay host 退出信号失败：%s", exit_path, exc_info=True)
        return False


def _wait_process_exit(process: ProcessLike, *, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except Exception:
        return not process_is_running(process)
    return not process_is_running(process)


def stop_process(process: ProcessLike | None) -> bool:
    if process is None or not process_is_running(process):
        _cleanup_process_exit_signal(process)
        return True
    if _request_process_exit(process) and _wait_process_exit(process, timeout=HOST_GRACEFUL_EXIT_TIMEOUT_SECONDS):
        _cleanup_process_exit_signal(process)
        return True
    try:
        process.terminate()
        process.wait(timeout=1.5)
        if not process_is_running(process):
            _cleanup_process_exit_signal(process)
            return True
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=1.0)
    except Exception:
        pass
    stopped = not process_is_running(process)
    if stopped:
        _cleanup_process_exit_signal(process)
    return stopped


class GameOverlayController:
    """Overlay 生命周期控制器；实际进程管理委托 Runtime Supervisor。"""

    def __init__(
        self,
        *,
        start_host_func: ProcessFactory = start_host_process,
        start_sidecar_func: ProcessFactory = start_sidecar_process,
        start_context_poller_func: ContextPollerFactory | None | object = _DEFAULT_CONTEXT_POLLER,
        prepare_data_func: Callable[[], Any] = prepare_shared_overlay_data,
        write_inactive_func: Callable[[], Any] = write_inactive_overlay_event,
    ) -> None:
        from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager

        if start_context_poller_func is _DEFAULT_CONTEXT_POLLER:
            context_factory = (
                start_overlay_context_poller
                if start_host_func is start_host_process and start_sidecar_func is start_sidecar_process
                else None
            )
        else:
            context_factory = cast(ContextPollerFactory | None, start_context_poller_func)
        self._runtime = OverlayRuntimeManager(
            start_host_func=start_host_func,
            start_sidecar_func=start_sidecar_func,
            start_context_poller_func=context_factory,
            prepare_data_func=prepare_data_func,
            write_inactive_func=write_inactive_func,
        )

    @property
    def host_process(self) -> ProcessLike | None:
        return self._runtime.host_process

    @property
    def sidecar_process(self) -> ProcessLike | None:
        return self._runtime.sidecar_process

    @property
    def context_poller(self) -> Any | None:
        return self._runtime.context_poller

    def host_running(self) -> bool:
        return process_is_running(self.host_process)

    def sidecar_running(self) -> bool:
        return process_is_running(self.sidecar_process)

    def context_poller_running(self) -> bool:
        return self.context_poller is not None

    def is_running(self) -> bool:
        return self.host_running() and self.sidecar_running()

    def start(self) -> None:
        self._runtime.set_enabled(True)

    def stop(self, *, write_inactive: bool = True) -> None:
        del write_inactive
        if self.host_process is None and self.sidecar_process is None and self.context_poller is None:
            return
        self._runtime.set_enabled(False)

    def snapshot(self) -> dict[str, Any]:
        snapshot = self._runtime.snapshot()
        host_running = self.host_running()
        sidecar_running = self.sidecar_running()
        residual_pids: dict[str, int] = {}
        if snapshot.get("status") == "error":
            if host_running and snapshot.get("host_pid") is not None:
                residual_pids["host"] = int(snapshot["host_pid"])
            if sidecar_running and snapshot.get("sidecar_pid") is not None:
                residual_pids["sidecar"] = int(snapshot["sidecar_pid"])
        return {
            "status": snapshot.get("status", "stopped"),
            "host_pid": snapshot.get("host_pid"),
            "sidecar_pid": snapshot.get("sidecar_pid"),
            "host_status": "running" if host_running else "stopped",
            "sidecar_status": "running" if sidecar_running else "stopped",
            "context_poller_status": snapshot.get("context_status", "stopped"),
            "context_poller_error": self._runtime.context_error,
            "last_error": snapshot.get("last_error", ""),
            "residual_pids": residual_pids,
            "updated_at": snapshot.get("updated_at", time.time()),
        }

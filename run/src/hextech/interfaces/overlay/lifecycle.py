"""独立游戏内显示的进程生命周期。

Controller 把 Tk host 与 Vision sidecar 视为一个逻辑服务：启动失败原子回滚，停止时
先写 inactive 事件，再确保两个子进程都退出。桌面 UI 只调用本模块的统一接口。

调用方: display.desktop.app、display.desktop.service_manager、overlay.__main__; 关键依赖: catalog.runtime_store、overlay.events、support.atomic_io。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from hextech.modules.data.catalog.runtime_store import get_runtime_root_dir
from hextech.modules.vision.events import write_inactive_overlay_event
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.vision.diagnostic_settings import ROI_DUMP_ENV, resolve_roi_dump_enabled

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
VISION_TARGET_GENERATION_ENV = "HEXTECH_VISION_TARGET_GENERATION_ID"
OVERLAY_SIDECAR_DEBUG_DUMP_ENV = ROI_DUMP_ENV
OVERLAY_READY_TIMEOUT_SECONDS = 5.0
OVERLAY_FROZEN_READY_TIMEOUT_SECONDS = 20.0
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


class HostCleanupError(RuntimeError):
    """host 启动失败后无法确认进程已清理；禁止继续拉起 sidecar。"""

    retryable = False


def _host_ready_timeout_seconds() -> float:
    """冻结包允许 GUI/runtime import 冷启动；源码入口仍快速暴露回归。"""

    return (
        OVERLAY_FROZEN_READY_TIMEOUT_SECONDS
        if getattr(sys, "frozen", False)
        else OVERLAY_READY_TIMEOUT_SECONDS
    )


def _host_start_failure_state(exc: Exception) -> str:
    text = str(exc or "")
    if isinstance(exc, TimeoutError):
        return "timeout"
    if "token 不匹配" in text:
        return "token_mismatch"
    if "readiness 前退出" in text:
        return "exited"
    return "failed"


def _hidden_startupinfo() -> Any:
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo







def _spawn_runtime_child(role, *, timeout, cancel_event=None, target_generation_id="", target_catalog_id="",
                         expected_vision_pool_fingerprint="", debug_dump=None):
    from hextech.modules.session.runtime_channel import ChildPipe
    from hextech.interfaces.desktop.runtime_process_windows import WindowsJobObject
    from hextech.modules.session.runtime_channel import active_owner
    owner = active_owner()
    if owner is None or not owner.is_running() or owner._shutdown_requested.is_set():
        raise RuntimeError("App runtime owner is required to start a production child")
    pipe = ChildPipe(role, on_message=lambda kind, payload: owner.receive(role, pipe, kind, payload),
                     on_close=lambda: owner.retire_pipe(role, pipe))
    env = os.environ.copy()
    env.update(pipe.environment())
    generation = uuid.uuid4().hex
    env[OVERLAY_GENERATION_ENV] = generation
    if target_generation_id:
        env[VISION_TARGET_GENERATION_ENV] = target_generation_id
    if target_catalog_id:
        env["HEXTECH_VISION_TARGET_CATALOG_ID"] = target_catalog_id
    command = ([sys.executable, "--game-overlay" if role == "host" else "--overlay-sidecar"]
               if getattr(sys, "frozen", False) else
               [sys.executable, "-m", "hextech.bootstrap.overlay" if role == "host" else "hextech.infrastructure.vision.sidecar"])
    if role == "vision":
        command.extend(["--loop", "--preset", "auto", "--write-event"])
        if resolve_roi_dump_enabled(debug_dump):
            command.extend(["--debug-dump", str(get_runtime_root_dir()/"debug"/"overlay_vision")])
    owner.attach_pipe(role, pipe)
    process = None
    try:
        process = subprocess.Popen(command, cwd=RUN_DIR, env=env, startupinfo=_hidden_startupinfo(),
            creationflags=int(subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0)
        process._hextech_runtime_pipe = pipe
        process._hextech_runtime_job = WindowsJobObject(process)
        if os.name == "nt" and not process._hextech_runtime_job.attached:
            raise RuntimeError("runtime_child_job_attachment_failed")
        process._hextech_overlay_sidecar_generation = generation
        owner.register_child(role, process)
        ready = pipe.wait_ready(process, float(timeout), cancel_event=cancel_event)
        profile = ready.get("startup_profile") or {}
        if role == "vision":
            if target_generation_id and profile.get("vision_pool_generation_id") != target_generation_id:
                raise ValueError("sidecar_ready_generation_mismatch")
            if target_catalog_id and profile.get("recognition_catalog_id", profile.get("catalog_generation_id")) != target_catalog_id:
                raise ValueError("sidecar_ready_catalog_mismatch")
            if expected_vision_pool_fingerprint and profile.get("vision_pool_fingerprint") != expected_vision_pool_fingerprint:
                raise ValueError("sidecar_ready_fingerprint_mismatch")
        else:
            process._hextech_overlay_host_startup_observation = {"pid":process.pid,"status":"ready","ready_state":"ready"}
        return process
    except BaseException as exc:
        cleaned = process is None or stop_process(process)
        pipe.close()
        if role == "host":
            observation = {"pid":getattr(process,"pid",None),"status":"failed",
                           "ready_state":_host_start_failure_state(exc),"cleanup_confirmed":cleaned}
            if not cleaned:
                failure=HostCleanupError("runtime_child_start_cleanup_failed")
                failure.host_startup_observation=observation
                raise failure from exc
            exc.host_startup_observation=observation
        elif not cleaned:
            raise SidecarCleanupError("runtime_child_start_cleanup_failed") from exc
        raise

def start_host_process() -> subprocess.Popen:
    return _spawn_runtime_child("host", timeout=_host_ready_timeout_seconds())


def _sidecar_debug_dump_enabled(value: str | None = None) -> bool:
    """兼容旧测试入口；显式 value 不读取持久设置。"""

    if value is not None:
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}
    return resolve_roi_dump_enabled()


def start_sidecar_process(*, debug_dump=None, readiness_timeout_seconds=None, cancel_event=None,
                          target_generation_id="", target_catalog_id="", expected_vision_pool_fingerprint=""):
    return _spawn_runtime_child("vision", timeout=readiness_timeout_seconds or OVERLAY_SIDECAR_READY_TIMEOUT_SECONDS,
        cancel_event=cancel_event, target_generation_id=target_generation_id, target_catalog_id=target_catalog_id,
        expected_vision_pool_fingerprint=expected_vision_pool_fingerprint, debug_dump=debug_dump)


def process_is_running(process: ProcessLike | None) -> bool:
    return bool(process is not None and process.poll() is None)


def _process_exit_path(process: ProcessLike | None) -> Path | None:
    value = str(getattr(process, "_hextech_overlay_exit_file", "") or "").strip()
    return Path(value) if value else None


def _cleanup_process_exit_signal(process: ProcessLike | None) -> None:
    pipe = getattr(process, "_hextech_runtime_pipe", None)
    if pipe is not None:
        pipe.close()
    job = getattr(process, "_hextech_runtime_job", None)
    if job is not None:
        job.close()
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
    pipe = getattr(process, "_hextech_runtime_pipe", None)
    if pipe is not None:
        return pipe.publish("shutdown", {"requested_at":time.time()})
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

"""独立游戏内显示的进程生命周期。

Controller 把 Tk host 与 Vision sidecar 视为一个逻辑服务：启动失败原子回滚，停止时
先写 inactive 事件，再确保两个子进程都退出。桌面 UI 只调用本模块的统一接口。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from processing.overlay_event_channel import write_inactive_overlay_event
from processing.runtime_store import build_runtime_state_path, get_runtime_root_dir
from tools.atomic_io import atomic_write_json

from .data_source import prepare_shared_overlay_data


logger = logging.getLogger(__name__)

OVERLAY_READY_FILE_ENV = "HEXTECH_OVERLAY_READY_FILE"
OVERLAY_EXIT_FILE_ENV = "HEXTECH_OVERLAY_EXIT_FILE"
OVERLAY_READY_TIMEOUT_SECONDS = 5.0
HOST_GRACEFUL_EXIT_TIMEOUT_SECONDS = 0.75
RUN_DIR = Path(__file__).resolve().parent.parent


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> Any: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[], ProcessLike]


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
    timeout_seconds: float = OVERLAY_READY_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"game_overlay host 在 readiness 前退出，exit_code={exit_code}")
        try:
            payload = json.loads(ready_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        if int(payload.get("pid") or 0) == int(process.pid):
            return
        raise RuntimeError("game_overlay host readiness PID 不匹配")
    raise TimeoutError(f"game_overlay host 启动超时：{float(timeout_seconds):.1f}s")


def start_host_process() -> subprocess.Popen:
    """启动独立 Tk host，并等待 after_idle readiness。"""

    if getattr(sys, "frozen", False):
        command = [sys.executable, "--game-overlay"]
    else:
        command = [sys.executable, str(RUN_DIR / "game_overlay_host.py")]
    ready_path = Path(build_runtime_state_path(f"game_overlay_host.{uuid.uuid4().hex}.ready.json"))
    exit_path = Path(build_runtime_state_path(f"game_overlay_host.{uuid.uuid4().hex}.exit.json"))
    env = os.environ.copy()
    env[OVERLAY_READY_FILE_ENV] = str(ready_path)
    env[OVERLAY_EXIT_FILE_ENV] = str(exit_path)
    process = subprocess.Popen(command, cwd=RUN_DIR, startupinfo=_hidden_startupinfo(), env=env)
    setattr(process, "_hextech_overlay_exit_file", str(exit_path))
    try:
        _wait_for_host_ready(process, ready_path)
        return process
    except Exception:
        stop_process(process)
        raise
    finally:
        try:
            ready_path.unlink(missing_ok=True)
        except OSError:
            pass


def start_sidecar_process() -> subprocess.Popen:
    diagnostic_dir = get_runtime_root_dir() / "debug" / "overlay_vision"
    command = [sys.executable]
    if getattr(sys, "frozen", False):
        command.append("--overlay-sidecar")
    else:
        command.extend(["-m", "processing.overlay_vision_sidecar"])
    command.extend([
        "--loop",
        "--preset",
        "auto",
        "--write-event",
        "--debug-dump",
        str(diagnostic_dir),
    ])
    return subprocess.Popen(command, cwd=RUN_DIR, startupinfo=_hidden_startupinfo())


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
    """host + sidecar 的单一启停与状态边界。"""

    def __init__(
        self,
        *,
        start_host_func: ProcessFactory = start_host_process,
        start_sidecar_func: ProcessFactory = start_sidecar_process,
        prepare_data_func: Callable[[], Any] = prepare_shared_overlay_data,
        write_inactive_func: Callable[[], Any] = write_inactive_overlay_event,
    ) -> None:
        self._start_host_func = start_host_func
        self._start_sidecar_func = start_sidecar_func
        self._prepare_data_func = prepare_data_func
        self._write_inactive_func = write_inactive_func
        self.host_process: ProcessLike | None = None
        self.sidecar_process: ProcessLike | None = None
        self.status = "stopped"
        self.last_error = ""
        self.updated_at = time.time()

    def _mark(self, status: str, *, error: str = "") -> None:
        self.status = status
        self.last_error = error
        self.updated_at = time.time()

    def is_running(self) -> bool:
        return process_is_running(self.host_process) and process_is_running(self.sidecar_process)

    def start(self) -> None:
        if self.is_running():
            self._mark("running")
            return
        try:
            self.stop(write_inactive=False)
        except Exception as exc:
            error = f"无法清理旧 game_overlay 实例：{exc}"
            self._mark("error", error=error)
            raise RuntimeError(error) from exc
        self._mark("starting")
        try:
            self._prepare_data_func()
            self._write_inactive_func()
            self.sidecar_process = self._start_sidecar_func()
            if not process_is_running(self.sidecar_process):
                raise RuntimeError("game_overlay sidecar 启动后立即退出")
            self.host_process = self._start_host_func()
            if not process_is_running(self.host_process):
                raise RuntimeError("game_overlay host 启动后立即退出")
            self._mark("running")
            logger.info(
                "game_overlay 已启动：host_pid=%s sidecar_pid=%s",
                getattr(self.host_process, "pid", None),
                getattr(self.sidecar_process, "pid", None),
            )
        except Exception as exc:
            rollback_errors: list[str] = []
            try:
                # host 失败时 sidecar 可能刚写过 active；先补 inactive fence 再终止。
                self._write_inactive_func()
            except Exception as fence_exc:
                rollback_errors.append(f"回滚 inactive 事件写入失败：{fence_exc}")
            host_process = self.host_process
            sidecar_process = self.sidecar_process
            host_stopped = stop_process(host_process)
            sidecar_stopped = stop_process(sidecar_process)
            self.host_process = None
            self.sidecar_process = None
            residual = [
                f"{name}(pid={getattr(process, 'pid', None)})"
                for name, stopped, process in (
                    ("host", host_stopped, host_process),
                    ("sidecar", sidecar_stopped, sidecar_process),
                )
                if not stopped
            ]
            error = str(exc)
            if rollback_errors:
                error = f"{error}；{'；'.join(rollback_errors)}"
            if residual:
                error = f"{error}；回滚后仍有残留：{', '.join(residual)}"
                logger.warning("game_overlay 启动回滚后仍有残留：%s", ", ".join(residual))
            self._mark("error", error=error)
            if residual or rollback_errors:
                raise RuntimeError(error) from exc
            raise

    def stop(self, *, write_inactive: bool = True) -> None:
        # 共享事件文件没有实例 ID；只有实际持有子进程引用的 Controller 才拥有
        # 写 inactive 的权限，避免第二个桌面实例退出时覆盖正在运行的第一个实例。
        if self.host_process is None and self.sidecar_process is None:
            self._mark("stopped")
            return
        errors: list[str] = []
        if write_inactive:
            try:
                self._write_inactive_func()
            except Exception as exc:
                errors.append(f"inactive 事件写入失败：{exc}")
        sidecar_process = self.sidecar_process
        host_process = self.host_process
        sidecar_stopped = stop_process(sidecar_process)
        # sidecar 退出前可能刚完成一次 active 原子写；退出后再写一次作为最终 fence。
        if write_inactive:
            try:
                self._write_inactive_func()
            except Exception as exc:
                errors.append(f"最终 inactive 事件写入失败：{exc}")
        host_stopped = stop_process(host_process)
        self.sidecar_process = None
        self.host_process = None
        if not sidecar_stopped:
            message = f"sidecar 停止失败(pid={getattr(sidecar_process, 'pid', None)})"
            errors.append(message)
            logger.warning("game_overlay %s", message)
        if not host_stopped:
            message = f"host 停止失败(pid={getattr(host_process, 'pid', None)})"
            errors.append(message)
            logger.warning("game_overlay %s", message)
        if errors:
            error = "；".join(errors)
            self._mark("error", error=error)
            raise RuntimeError(error)
        self._mark("stopped")
        logger.info("game_overlay 已停止")

    def snapshot(self) -> dict[str, Any]:
        host_running = process_is_running(self.host_process)
        sidecar_running = process_is_running(self.sidecar_process)
        if self.status == "running" and not (host_running and sidecar_running):
            missing = "host" if not host_running else "sidecar"
            self._mark("error", error=f"game_overlay {missing} 意外退出")
        return {
            "status": self.status,
            "pid": getattr(self.host_process, "pid", None),
            "host_pid": getattr(self.host_process, "pid", None),
            "sidecar_pid": getattr(self.sidecar_process, "pid", None),
            "host_status": "running" if host_running else "stopped",
            "sidecar_status": "running" if sidecar_running else "stopped",
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

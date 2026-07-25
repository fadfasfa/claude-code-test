"""Desktop 托盘待机与恢复状态机。

连续无 League 客户端/游戏进程时只停止重型服务，保留 Tk 与托盘控制器；用户
主动唤醒或 League 再次出现后按既有偏好恢复，不自动重复打开浏览器。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import psutil

from hextech.interfaces.desktop import runtime as ui_runtime


logger = logging.getLogger(__name__)
BACKGROUND_IDLE_TIMEOUT_SECONDS = 300.0
BACKGROUND_PROCESS_PROBE_SECONDS = 5.0
MANUAL_WINDOW_VISIBLE_SECONDS = 300.0
LEAGUE_CLIENT_PROCESS_NAMES = frozenset({"leagueclient.exe", "leagueclientux.exe"})
LEAGUE_GAME_PROCESS_NAMES = frozenset({"league of legends.exe"})


def probe_league_process_state(
    process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
) -> dict[str, bool]:
    """按实际进程而非窗口可见性判断 League 是否仍在使用。"""

    client_running = False
    game_running = False
    try:
        processes = process_iter(["name"])
        for process in processes:
            try:
                info = getattr(process, "info", {})
                name = str(info.get("name") if isinstance(info, dict) else process.name() or "").casefold()
            except (psutil.Error, OSError):
                continue
            client_running = client_running or name in LEAGUE_CLIENT_PROCESS_NAMES
            game_running = game_running or name in LEAGUE_GAME_PROCESS_NAMES
            if client_running and game_running:
                break
    except (psutil.Error, OSError):
        # 探针异常时 fail-safe：视为仍在使用，不能误停 Overlay。
        return {"probe_ok": False, "client_running": True, "game_running": True}
    return {"probe_ok": True, "client_running": client_running, "game_running": game_running}


def resolve_background_runtime_action(
    *,
    runtime_state: str,
    idle_started_at: float,
    now: float,
    manual_visible_until: float,
    process_state: dict[str, bool],
    idle_timeout_seconds: float = BACKGROUND_IDLE_TIMEOUT_SECONDS,
) -> tuple[str, float]:
    """纯函数决定一次探针应保持、休眠还是恢复，便于精确验证五分钟边界。"""

    if not process_state.get("probe_ok", False):
        return "none", now
    league_running = bool(process_state.get("client_running") or process_state.get("game_running"))
    if league_running:
        return ("resume" if runtime_state in {"suspended", "resume_failed"} else "none"), now
    if now < manual_visible_until:
        return "none", idle_started_at
    if runtime_state == "running" and now - idle_started_at >= idle_timeout_seconds:
        return "suspend", idle_started_at
    return "none", idle_started_at


class DesktopBackgroundRuntimeMixin:
    """为 HextechUI 提供托盘、单实例激活、待机与恢复能力。"""

    def _initialize_background_runtime(self) -> None:
        self._background_runtime_state = "running"
        self._background_runtime_reason = "startup"
        self._background_runtime_lock = threading.RLock()
        self._background_runtime_stop = threading.Event()
        self._background_runtime_thread: threading.Thread | None = None
        self._background_idle_started_at = time.monotonic()
        self._manual_window_visible_until = 0.0
        self._desktop_instance_owner = None
        self._last_activation_request_id = ""
        self._activation_poll_after_id = None
        self._tray_controller = None

    def _start_desktop_tray(self) -> bool:
        from hextech.interfaces.desktop.tray import DesktopTrayController

        if self._tray_controller is not None:
            return True
        controller = DesktopTrayController(
            dispatch=self._run_on_ui_thread,
            show_callback=lambda: self.request_runtime_resume(reason="tray_show", show_window=True),
            restart_recognition_callback=self.restart_recognition,
            exit_callback=self.exit_application,
            status_text=self._tray_status_text,
        )
        if not controller.start():
            return False
        self._tray_controller = controller
        return True

    def attach_instance_owner(self, owner) -> None:
        self._desktop_instance_owner = owner
        if self._activation_poll_after_id is None:
            self._activation_poll_after_id = self.root.after(250, self._poll_instance_activation)

    def _poll_instance_activation(self) -> None:
        self._activation_poll_after_id = None
        if self._closing:
            return
        owner = self._desktop_instance_owner
        if owner is not None:
            request = owner.consume_activation_request(self._last_activation_request_id)
            if request:
                self._last_activation_request_id = str(request.get("request_id") or "")
                self.request_runtime_resume(reason="shortcut_activation", show_window=True)
        self._activation_poll_after_id = self.root.after(250, self._poll_instance_activation)

    def start_background_runtime_monitor(self) -> None:
        if self._background_runtime_thread is not None and self._background_runtime_thread.is_alive():
            return
        self._background_runtime_stop.clear()
        self._background_idle_started_at = time.monotonic()
        self._background_runtime_thread = self._start_tracked_thread(
            self._background_runtime_loop,
            name="hextech-background-runtime",
        )

    def _background_runtime_loop(self) -> None:
        while not self._background_runtime_stop.wait(BACKGROUND_PROCESS_PROBE_SECONDS):
            if self._closing:
                return
            process_state = probe_league_process_state()
            now = time.monotonic()
            action, idle_started_at = resolve_background_runtime_action(
                runtime_state=self._background_runtime_state,
                idle_started_at=self._background_idle_started_at,
                now=now,
                manual_visible_until=self._manual_window_visible_until,
                process_state=process_state,
            )
            self._background_idle_started_at = idle_started_at
            if action == "resume":
                self.request_runtime_resume(reason="league_process_detected", show_window=False)
            elif action == "suspend":
                self._start_tracked_thread(self._suspend_background_runtime, name="hextech-runtime-suspend")

    def hide_to_tray(self) -> None:
        """右上角“×”与窗口关闭事件只隐藏，不终止后台控制器。"""

        if self._tray_controller is None and not self._start_desktop_tray():
            self._set_status("系统托盘不可用，窗口未隐藏", "#F38BA8")
            return
        self._manual_window_visible_until = 0.0
        self._hide_overlay()
        tray = self._tray_controller
        if tray is not None:
            tray.refresh()

    def request_runtime_resume(self, *, reason: str, show_window: bool) -> None:
        """手动或自动唤醒；显示动作立即反馈，重服务在后台恢复。"""

        if self._closing:
            return
        if show_window:
            self._manual_window_visible_until = time.monotonic() + MANUAL_WINDOW_VISIBLE_SECONDS
            process_state = probe_league_process_state()
            if not process_state["game_running"]:
                self._show_overlay(topmost=False)
        self._background_idle_started_at = time.monotonic()
        if self._background_runtime_state in {"suspended", "resume_failed"}:
            self._start_tracked_thread(
                lambda: self._resume_background_runtime(reason=reason),
                name="hextech-runtime-resume",
            )
        tray = self._tray_controller
        if tray is not None:
            tray.refresh()

    def _set_background_runtime_state(self, state: str, reason: str) -> None:
        self._background_runtime_state = state
        self._background_runtime_reason = reason
        tray = self._tray_controller
        if tray is not None:
            tray.refresh()
        text = self._tray_status_text().removeprefix("状态：")
        self._run_on_ui_thread(lambda: self._set_overlay_status_summary(f"游戏内显示: {text}", "#F5C26B"))

    def _stop_supervisor_for_standby(self) -> None:
        self._supervisor_lease_stop.set()
        handle = self.runtime_supervisor
        self.runtime_supervisor = None
        if handle is not None:
            ui_runtime.stop_runtime_supervisor_process(handle)
        lease_thread = self._supervisor_lease_thread
        if lease_thread is not None and lease_thread.is_alive() and lease_thread is not threading.current_thread():
            lease_thread.join(timeout=1.0)
        self._supervisor_lease_thread = None

    def _suspend_background_runtime(self) -> None:
        with self._background_runtime_lock:
            if self._closing or self._background_runtime_state != "running":
                return
            self._set_background_runtime_state("suspending", "idle_timeout")
            self.pause_event.set()
            try:
                manager = self.service_manager
                if manager is not None:
                    manager.stop_web()
                    manager.stop_data_service()
                with self._overlay_operation_lock:
                    self._stop_supervisor_for_standby()
                self.web_process = None
                self.data_service = None
                self._set_background_runtime_state("suspended", "idle_timeout")
            except Exception as exc:
                logger.exception("进入轻量托盘待机失败。")
                self._set_background_runtime_state("resume_failed", exc.__class__.__name__)

    def _resume_background_runtime(self, *, reason: str) -> None:
        with self._background_runtime_lock:
            if self._closing or self._background_runtime_state not in {"suspended", "resume_failed"}:
                return
            self._set_background_runtime_state("resuming", reason)
            self.pause_event.clear()
            try:
                manager = self.service_manager
                if manager is None:
                    raise RuntimeError("后台 ServiceManager 尚未就绪")
                self.data_service = manager.start_data_service()
                self._supervisor_lease_stop.clear()
                self._start_runtime_supervisor(restore_persisted_game_overlay=True)
                if self.runtime_supervisor is None:
                    raise RuntimeError("Runtime Supervisor 恢复失败")
                if self.feature_flags.get("web_frontend_enabled"):
                    manager.start_web()
                    self.web_process = manager.web.process
                self._background_idle_started_at = time.monotonic()
                self._set_background_runtime_state("running", reason)
            except Exception as exc:
                logger.exception("从轻量托盘待机恢复失败。")
                self._set_background_runtime_state("resume_failed", exc.__class__.__name__)

    def restart_recognition(self) -> None:
        if self._closing or self._background_runtime_state in {"suspending", "resuming", "restart_in_progress"}:
            return
        if self._background_runtime_state in {"suspended", "resume_failed"}:
            self.request_runtime_resume(reason="tray_restart_recognition", show_window=False)
            return

        def worker() -> None:
            with self._background_runtime_lock:
                self._set_background_runtime_state("restart_in_progress", "tray_restart_recognition")
                try:
                    with self._overlay_operation_lock:
                        self._stop_supervisor_for_standby()
                        self._supervisor_lease_stop.clear()
                        self._start_runtime_supervisor(restore_persisted_game_overlay=True)
                    if self.runtime_supervisor is None:
                        raise RuntimeError("Runtime Supervisor 重启失败")
                    self._set_background_runtime_state("running", "tray_restart_recognition")
                except Exception as exc:
                    logger.exception("托盘重启识别失败。")
                    self._set_background_runtime_state("resume_failed", exc.__class__.__name__)

        self._start_tracked_thread(worker, name="hextech-restart-recognition")

    def _tray_status_text(self) -> str:
        return {
            "running": "状态：后台服务运行中",
            "suspending": "状态：正在进入轻量待机",
            "suspended": "状态：轻量待机（识别已休眠）",
            "resuming": "状态：识别恢复中",
            "restart_in_progress": "状态：识别重启中",
            "resume_failed": "状态：识别恢复失败",
        }.get(self._background_runtime_state, "状态：未知")

    def stop_background_runtime(self) -> None:
        self._background_runtime_stop.set()
        if self._activation_poll_after_id is not None:
            try:
                self.root.after_cancel(self._activation_poll_after_id)
            except Exception:
                pass
            self._activation_poll_after_id = None
        tray = self._tray_controller
        self._tray_controller = None
        if tray is not None:
            tray.stop()


__all__ = [
    "BACKGROUND_IDLE_TIMEOUT_SECONDS",
    "BACKGROUND_PROCESS_PROBE_SECONDS",
    "DesktopBackgroundRuntimeMixin",
    "LEAGUE_CLIENT_PROCESS_NAMES",
    "LEAGUE_GAME_PROCESS_NAMES",
    "MANUAL_WINDOW_VISIBLE_SECONDS",
    "probe_league_process_state",
    "resolve_background_runtime_action",
]

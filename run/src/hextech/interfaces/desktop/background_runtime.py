"""Desktop 托盘待机与恢复状态机。

连续无 League 客户端/游戏进程时只停止重型服务，保留 Tk 与托盘控制器；用户
主动唤醒或 League 再次出现后按既有偏好恢复，不自动重复打开浏览器。
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import psutil

from hextech.interfaces.desktop import runtime as ui_runtime
from hextech.interfaces.desktop.background_runtime_diagnostics import record_background_runtime_transition
from hextech.modules.session.build_identity import current_build_id


logger = logging.getLogger(__name__)
BACKGROUND_IDLE_TIMEOUT_SECONDS = 300.0
# League 出现后需在 15 秒内恢复；1 秒探测加 13 秒端到端恢复预算，仍预留约 1 秒
# 给线程调度。恢复失败的重试另行限制为 5 秒，避免为了快速发现而重复拉起服务。
BACKGROUND_PROCESS_PROBE_SECONDS = 1.0
BACKGROUND_RESUME_RETRY_SECONDS = 5.0
MANUAL_WINDOW_VISIBLE_SECONDS = 300.0
BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS = 13.0
BACKGROUND_RESUME_HEALTH_POLL_SECONDS = 0.5
LEAGUE_CLIENT_PROCESS_NAMES = frozenset({"leagueclient.exe", "leagueclientux.exe"})
LEAGUE_GAME_PROCESS_NAMES = frozenset({"league of legends.exe"})


class _BackgroundRuntimeExitRequested(RuntimeError):
    """完全退出已开始；在途恢复必须回收而不能继续拉起子进程。"""


def probe_league_process_state(
    process_iter: Callable[..., Iterable[Any]] = psutil.process_iter,
) -> dict[str, object]:
    """按实际进程而非窗口可见性判断 League 是否仍在使用。"""

    client_running = False
    game_running = False
    matched_processes: set[str] = set()
    try:
        processes = process_iter(["name"])
        for process in processes:
            try:
                info = getattr(process, "info", {})
                name = str(info.get("name") if isinstance(info, dict) else process.name() or "").casefold()
            except (psutil.Error, OSError):
                continue
            if name in LEAGUE_CLIENT_PROCESS_NAMES:
                client_running = True
                matched_processes.add(name)
            if name in LEAGUE_GAME_PROCESS_NAMES:
                game_running = True
                matched_processes.add(name)
            if client_running and game_running:
                break
    except (psutil.Error, OSError):
        # 探针异常时 fail-safe：视为仍在使用，不能误停 Overlay。
        return {
            "probe_ok": False,
            "client_running": True,
            "game_running": True,
            "matched_processes": [],
        }
    return {
        "probe_ok": True,
        "client_running": client_running,
        "game_running": game_running,
        "matched_processes": sorted(matched_processes),
    }


def resolve_background_runtime_action(
    *,
    runtime_state: str,
    idle_started_at: float,
    now: float,
    manual_visible_until: float,
    process_state: Mapping[str, object],
    idle_timeout_seconds: float = BACKGROUND_IDLE_TIMEOUT_SECONDS,
) -> tuple[str, float]:
    """纯函数决定一次探针应保持、休眠还是恢复，便于精确验证五分钟边界。"""

    if not process_state.get("probe_ok", False):
        return "none", now
    league_running = bool(process_state.get("client_running") or process_state.get("game_running"))
    if league_running:
        return (
            "resume" if runtime_state in {"suspended", "resume_failed", "resume_cleanup_pending"} else "none"
        ), now
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
        self._background_next_resume_at = 0.0
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
                if now < self._background_next_resume_at:
                    continue
                self._background_next_resume_at = now + BACKGROUND_RESUME_RETRY_SECONDS
                self._record_background_runtime_transition(
                    state="wake_detected",
                    reason="league_process_detected",
                    process_state=process_state,
                )
                self.request_runtime_resume(reason="league_process_detected", show_window=False)
            elif action == "suspend":
                self._record_background_runtime_transition(
                    state="suspend_requested",
                    reason="idle_timeout",
                    process_state=process_state,
                )
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

        if self._closing or self._background_runtime_stop.is_set():
            return
        if show_window:
            self._manual_window_visible_until = time.monotonic() + MANUAL_WINDOW_VISIBLE_SECONDS
            process_state = probe_league_process_state()
            if not process_state["game_running"]:
                self._show_overlay(topmost=False)
        self._background_idle_started_at = time.monotonic()
        if self._background_runtime_state in {"suspended", "resume_failed", "resume_cleanup_pending"}:
            self._start_tracked_thread(
                lambda: self._resume_background_runtime(reason=reason),
                name="hextech-runtime-resume",
            )
        tray = self._tray_controller
        if tray is not None:
            tray.refresh()

    def _record_background_runtime_transition(
        self,
        *,
        state: str,
        reason: str,
        process_state: Mapping[str, object] | None = None,
        components: Mapping[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        try:
            matched = process_state.get("matched_processes", []) if isinstance(process_state, Mapping) else []
            record_background_runtime_transition(
                state=state,
                reason=reason,
                matched_processes=matched if isinstance(matched, Iterable) and not isinstance(matched, (str, bytes)) else (),
                components=components,
                error=error,
            )
        except Exception:
            logger.debug("写入后台运行态诊断失败。", exc_info=True)

    def _set_background_runtime_state(
        self,
        state: str,
        reason: str,
        *,
        process_state: Mapping[str, object] | None = None,
        components: Mapping[str, object] | None = None,
        error: BaseException | None = None,
    ) -> None:
        previous_state = str(getattr(self, "_background_runtime_state", ""))
        previous_reason = str(getattr(self, "_background_runtime_reason", ""))
        self._background_runtime_state = state
        self._background_runtime_reason = reason
        if state == "running":
            self._background_next_resume_at = 0.0
        elif state in {"resume_failed", "resume_cleanup_pending"}:
            self._background_next_resume_at = time.monotonic() + BACKGROUND_RESUME_RETRY_SECONDS
        if state != previous_state or reason != previous_reason:
            self._record_background_runtime_transition(
                state=state,
                reason=reason,
                process_state=process_state,
                components=components,
                error=error,
            )
        tray = self._tray_controller
        if tray is not None:
            tray.refresh()
        text = self._tray_status_text().removeprefix("状态：")
        self._run_on_ui_thread(lambda: self._set_overlay_status_summary(f"游戏内显示: {text}", "#F5C26B"))

    def _stop_supervisor_for_standby(self) -> None:
        self._supervisor_lease_stop.set()
        handle = self.runtime_supervisor
        try:
            if handle is not None:
                ui_runtime.stop_runtime_supervisor_process(handle)
        finally:
            lease_thread = self._supervisor_lease_thread
            if lease_thread is not None and lease_thread.is_alive() and lease_thread is not threading.current_thread():
                lease_thread.join(timeout=1.0)
            self._supervisor_lease_thread = None
        # 只有停止已返回才能丢弃 handle。若 terminate/kill 仍失败，下一轮恢复必须
        # 先继续回收它，绝不能把残留 Supervisor 当作不存在后再启动一套。
        self.runtime_supervisor = None

    def _suspend_background_runtime(self) -> None:
        with self._background_runtime_lock:
            if self._closing or self._background_runtime_state != "running":
                return
            # 休眠动作可能排队数秒；在真正关闭服务前再探测一次，避免 League 恰好
            # 在上一轮探测后启动而被错误停掉。
            process_state = probe_league_process_state()
            if bool(process_state.get("client_running") or process_state.get("game_running")):
                self._background_idle_started_at = time.monotonic()
                self._set_background_runtime_state(
                    "running",
                    "league_detected_before_suspend",
                    process_state=process_state,
                )
                return
            self._set_background_runtime_state("suspending", "idle_timeout", process_state=process_state)
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
                self._set_background_runtime_state("suspended", "idle_timeout", process_state=process_state)
            except Exception as exc:
                logger.exception("进入轻量托盘待机失败。")
                cleanup_ok = self._cleanup_partial_background_resume(stop_services=True)
                self._set_background_runtime_state(
                    "resume_failed" if cleanup_ok else "resume_cleanup_pending",
                    exc.__class__.__name__,
                    error=exc,
                )

    @staticmethod
    def _handle_is_running(handle: object | None) -> bool:
        if handle is None:
            return False
        managed_is_running = getattr(handle, "is_running", None)
        if callable(managed_is_running):
            try:
                return bool(managed_is_running())
            except Exception:
                return False
        process = getattr(handle, "process", None)
        poll = getattr(process, "poll", None)
        if callable(poll):
            try:
                return poll() is None
            except Exception:
                return False
        return True

    @staticmethod
    def _call_control_plane_with_optional_timeout(
        callback: Callable[..., object],
        *args: object,
        timeout: float | None,
    ) -> object:
        """给正式控制面传入 timeout，同时兼容不接收它的测试替身。"""

        if timeout is None:
            return callback(*args)
        try:
            parameters = inspect.signature(callback).parameters.values()
            accepts_timeout = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == "timeout"
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_timeout = False
        return callback(*args, timeout=timeout) if accepts_timeout else callback(*args)

    @staticmethod
    def _remaining_control_plane_timeout(deadline: float | None) -> float | None:
        """返回剩余恢复预算，避免一次 HTTP 请求跨过本轮 deadline。"""

        if deadline is None:
            return None
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0.0:
            return None
        return max(0.001, min(2.0, remaining))

    def _background_runtime_health(self, *, deadline: float | None = None) -> dict[str, str]:
        """验证恢复链的进程存活、Overlay 身份与 Sidecar 心跳。"""

        health: dict[str, str] = {}
        manager = self.service_manager
        data_service = getattr(manager, "data_service", None) if manager is not None else None
        data_is_running = getattr(data_service, "is_running", None)
        health["data_service"] = (
            "ready"
            if (callable(data_is_running) and bool(data_is_running())) or (data_service is None and self.data_service is not None)
            else "starting"
        )
        supervisor = self.runtime_supervisor
        health["supervisor"] = "ready" if self._handle_is_running(supervisor) else "failed"
        if self.feature_flags.get("web_frontend_enabled"):
            web = getattr(manager, "web", None) if manager is not None else None
            web_is_running = getattr(web, "is_running", None)
            if callable(web_is_running):
                # ManagedService 的 process 句柄可能仍在，但其子进程已经退出；
                # 真实恢复判定只能相信 is_running()，不能回退到“句柄非空”。
                health["web"] = "ready" if self._handle_is_running(web) else "starting"
            elif web is not None:
                # 轻量测试替身未实现存活协议时不伪造 ready，保留兼容但让诊断可见。
                health["web"] = "unverified"
            else:
                health["web"] = "starting"
        else:
            health["web"] = "disabled"

        if not self.feature_flags.get("game_overlay_enabled", True):
            health["overlay"] = "disabled"
            health["sidecar"] = "disabled"
            return health

        status_reader = getattr(supervisor, "get_status", None)
        if not callable(status_reader):
            # RuntimeSupervisorHandle 总会提供 get_status；此降级仅保留测试替身的
            # 兼容性，真实部署仍由下面的 Build ID 与心跳校验收口。
            health["overlay"] = "unverified"
            health["sidecar"] = "unverified"
            return health
        status_timeout = self._remaining_control_plane_timeout(deadline)
        if deadline is not None and status_timeout is None:
            health["overlay"] = "starting"
            health["sidecar"] = "starting"
            return health
        try:
            payload = self._call_control_plane_with_optional_timeout(status_reader, timeout=status_timeout)
            components = payload.get("components") if isinstance(payload, Mapping) else {}
            overlay = components.get("game_overlay") if isinstance(components, Mapping) else {}
            overlay = overlay if isinstance(overlay, Mapping) else {}
        except Exception as exc:
            health["overlay"] = f"error:{exc.__class__.__name__}"
            health["sidecar"] = "starting"
            return health

        expected_build_id = current_build_id()
        build_ids = overlay.get("build_ids") if isinstance(overlay.get("build_ids"), Mapping) else {}
        status = str(overlay.get("status") or "starting")
        liveness = overlay.get("sidecar_liveness") if isinstance(overlay.get("sidecar_liveness"), Mapping) else {}
        host_build = str(build_ids.get("host") or "")
        sidecar_build = str(build_ids.get("sidecar") or "")
        build_mismatch = bool(overlay.get("build_mismatch")) or any(
            value and value != expected_build_id
            for value in (str(overlay.get("build_id") or ""), host_build, sidecar_build)
        )
        build_identity_ready = bool(expected_build_id) and all(
            value == expected_build_id for value in (host_build, sidecar_build)
        )
        if build_mismatch:
            health["overlay"] = "build_mismatch"
            health["sidecar"] = "build_mismatch"
        elif not build_identity_ready:
            # 启动早期可能尚未写出 Host/Sidecar 身份；绝不能因此把旧状态文件
            # 当作当前 Build 健康。保持 starting，恢复预算会在 15 秒后走重试。
            health["overlay"] = "starting"
            health["sidecar"] = "starting"
        elif status == "running" and int(overlay.get("host_pid") or 0) > 0:
            health["overlay"] = "ready"
            health["sidecar"] = "ready" if str(liveness.get("status") or "") == "running" else "starting"
        elif status in {"error", "stale"}:
            health["overlay"] = status
            health["sidecar"] = str(liveness.get("status") or status)
        else:
            health["overlay"] = "starting"
            # 预热与异步 enable 尚未创建 sidecar 时，旧状态文件可能仍报告
            # failed/stale；在 Supervisor 自己的启动预算内只能视为 starting。
            health["sidecar"] = "starting"
        return health

    def _wait_for_background_runtime_health(self, *, deadline: float | None = None) -> dict[str, str]:
        deadline = (
            time.monotonic() + BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS
            if deadline is None
            else float(deadline)
        )
        last_health: dict[str, str] = {}
        while time.monotonic() < deadline:
            last_health = self._background_runtime_health(deadline=deadline)
            if all(value in {"ready", "disabled", "unverified"} for value in last_health.values()):
                return last_health
            if any(value in {"failed", "error", "stale", "build_mismatch"} or value.startswith("error:") for value in last_health.values()):
                break
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0.0:
                break
            time.sleep(min(BACKGROUND_RESUME_HEALTH_POLL_SECONDS, remaining))
        summary = ", ".join(f"{name}={value}" for name, value in sorted(last_health.items()))
        raise RuntimeError(f"后台恢复组件未就绪：{summary or '无状态'}")

    @staticmethod
    def _remaining_background_resume_timeout(deadline: float, *, component: str) -> float:
        """把同一端到端恢复 deadline 传给每个会阻塞的 bootstrap。"""

        remaining = float(deadline) - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"后台恢复预算耗尽，未启动 {component}")
        return max(0.1, remaining)

    def _restore_game_overlay_after_background_resume(self, *, deadline: float | None = None) -> None:
        """在后台线程直接恢复 Supervisor 侧的 Overlay 期望状态。

        ``_restore_persisted_game_overlay()`` 会读取 Tk 的 ``BooleanVar``，只能由 UI
        线程调用。轻量待机的恢复和托盘“重启识别”运行在后台线程，因此这里以已经
        持久化的 Desktop feature flag 为唯一输入，直接调用 Supervisor 控制面，避免
        跨线程访问 Tk 后把整次恢复误判成失败。
        """

        if not bool(self.feature_flags.get("game_overlay_enabled", True)):
            return
        supervisor = self.runtime_supervisor
        setter = getattr(supervisor, "set_game_overlay_enabled", None)
        if not callable(setter):
            raise RuntimeError("Runtime Supervisor 缺少游戏内显示控制面")
        action_timeout = self._remaining_control_plane_timeout(deadline)
        if deadline is not None and action_timeout is None:
            raise TimeoutError("后台恢复预算耗尽，未恢复游戏内显示")
        action = self._call_control_plane_with_optional_timeout(setter, True, timeout=action_timeout)
        if isinstance(action, Mapping) and str(action.get("status") or "").strip() in {
            "error",
            "failed",
            "rejected",
        }:
            raise RuntimeError(f"游戏内显示恢复被拒绝：{action.get('status')}")

    def _cleanup_partial_background_resume(self, *, stop_services: bool) -> bool:
        """回收失败恢复已拉起的组件，避免下一轮探针复制整套进程。

        恢复本身是顺序启动：DataService → Supervisor/Overlay → Web。任一步或健康
        校验失败时，下一次 5 秒探针必须从干净的 ``resume_failed`` 状态重试；保留
        半套 Supervisor 会让 Host、Sidecar 和 lease 产生重复所有者。
        """

        cleanup_ok = True
        if stop_services:
            manager = self.service_manager
            if manager is not None:
                try:
                    manager.stop_web()
                except Exception:
                    cleanup_ok = False
                    logger.debug("恢复失败后停止 Web 失败。", exc_info=True)
                try:
                    manager.stop_data_service()
                except Exception:
                    cleanup_ok = False
                    logger.debug("恢复失败后停止 DataService 失败。", exc_info=True)
            if cleanup_ok:
                self.web_process = None
                self.data_service = None
        try:
            with self._overlay_operation_lock:
                self._stop_supervisor_for_standby()
        except Exception:
            cleanup_ok = False
            logger.debug("恢复失败后停止 Runtime Supervisor 失败。", exc_info=True)
        return cleanup_ok

    def _raise_if_background_runtime_exit_requested(self) -> None:
        """在每个可阻塞 bootstrap 后观察完全退出，避免退出后重新拉起服务。"""

        if self._closing or self._background_runtime_stop.is_set():
            raise _BackgroundRuntimeExitRequested("桌面正在完全退出")

    def _resume_background_runtime(self, *, reason: str) -> None:
        with self._background_runtime_lock:
            if self._closing or self._background_runtime_stop.is_set() or self._background_runtime_state not in {
                "suspended",
                "resume_failed",
                "resume_cleanup_pending",
            }:
                return
            # 预算从恢复线程真正接手时开始，包含 DataService/Supervisor 启动和
            # Sidecar 心跳校验，不能只给最后的 health polling 单独再开 13 秒。
            resume_deadline = time.monotonic() + BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS
            process_state = probe_league_process_state()
            if self._background_runtime_state in {"resume_failed", "resume_cleanup_pending"} and not self._cleanup_partial_background_resume(
                stop_services=True
            ):
                self.pause_event.set()
                self._set_background_runtime_state("resume_cleanup_pending", "cleanup_retry_pending", process_state=process_state)
                return
            self._set_background_runtime_state("resuming", reason, process_state=process_state)
            self.pause_event.clear()
            try:
                self._raise_if_background_runtime_exit_requested()
                manager = self.service_manager
                if manager is None:
                    raise RuntimeError("后台 ServiceManager 尚未就绪")
                self.data_service = manager.start_data_service(
                    timeout=self._remaining_background_resume_timeout(resume_deadline, component="DataService")
                )
                self._raise_if_background_runtime_exit_requested()
                self._supervisor_lease_stop.clear()
                self._start_runtime_supervisor(
                    restore_persisted_game_overlay=False,
                    startup_timeout=self._remaining_background_resume_timeout(
                        resume_deadline,
                        component="Runtime Supervisor",
                    ),
                )
                self._raise_if_background_runtime_exit_requested()
                if self.runtime_supervisor is None:
                    raise RuntimeError("Runtime Supervisor 恢复失败")
                self._restore_game_overlay_after_background_resume(deadline=resume_deadline)
                self._raise_if_background_runtime_exit_requested()
                if self.feature_flags.get("web_frontend_enabled"):
                    manager.start_web(
                        timeout=self._remaining_background_resume_timeout(
                            resume_deadline,
                            component="Web",
                        )
                    )
                    self.web_process = manager.web.process
                self._raise_if_background_runtime_exit_requested()
                components = self._wait_for_background_runtime_health(deadline=resume_deadline)
                self._raise_if_background_runtime_exit_requested()
                self._background_idle_started_at = time.monotonic()
                self._set_background_runtime_state("running", reason, process_state=process_state, components=components)
            except _BackgroundRuntimeExitRequested:
                self._cleanup_partial_background_resume(stop_services=True)
                self.pause_event.set()
                return
            except Exception as exc:
                logger.exception("从轻量托盘待机恢复失败。")
                cleanup_ok = self._cleanup_partial_background_resume(stop_services=True)
                self.pause_event.set()
                self._set_background_runtime_state(
                    "resume_failed" if cleanup_ok else "resume_cleanup_pending",
                    exc.__class__.__name__,
                    error=exc,
                )

    def restart_recognition(self) -> None:
        if self._closing or self._background_runtime_stop.is_set() or self._background_runtime_state in {"suspending", "resuming", "restart_in_progress"}:
            return
        if self._background_runtime_state in {"suspended", "resume_failed", "resume_cleanup_pending"}:
            self.request_runtime_resume(reason="tray_restart_recognition", show_window=False)
            return

        def worker() -> None:
            with self._background_runtime_lock:
                if self._closing or self._background_runtime_stop.is_set():
                    return
                self._set_background_runtime_state("restart_in_progress", "tray_restart_recognition")
                try:
                    with self._overlay_operation_lock:
                        self._stop_supervisor_for_standby()
                        self._raise_if_background_runtime_exit_requested()
                        self._supervisor_lease_stop.clear()
                        self._start_runtime_supervisor(restore_persisted_game_overlay=False)
                    self._raise_if_background_runtime_exit_requested()
                    if self.runtime_supervisor is None:
                        raise RuntimeError("Runtime Supervisor 重启失败")
                    self._restore_game_overlay_after_background_resume()
                    self._raise_if_background_runtime_exit_requested()
                    components = self._wait_for_background_runtime_health()
                    self._raise_if_background_runtime_exit_requested()
                    self._set_background_runtime_state("running", "tray_restart_recognition", components=components)
                except _BackgroundRuntimeExitRequested:
                    self._cleanup_partial_background_resume(stop_services=False)
                    self.pause_event.set()
                    return
                except Exception as exc:
                    logger.exception("托盘重启识别失败。")
                    cleanup_ok = self._cleanup_partial_background_resume(stop_services=False)
                    self._set_background_runtime_state(
                        "resume_failed" if cleanup_ok else "resume_cleanup_pending",
                        exc.__class__.__name__,
                        error=exc,
                    )

        self._start_tracked_thread(worker, name="hextech-restart-recognition")

    def _tray_status_text(self) -> str:
        return {
            "running": "状态：后台服务运行中",
            "suspending": "状态：正在进入轻量待机",
            "suspended": "状态：轻量待机（识别已休眠）",
            "resuming": "状态：识别恢复中",
            "restart_in_progress": "状态：识别重启中",
            "resume_failed": "状态：识别恢复失败",
            "resume_cleanup_pending": "状态：正在回收失败的识别进程",
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
    "BACKGROUND_RESUME_RETRY_SECONDS",
    "BACKGROUND_RESUME_HEALTH_POLL_SECONDS",
    "BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS",
    "DesktopBackgroundRuntimeMixin",
    "LEAGUE_CLIENT_PROCESS_NAMES",
    "LEAGUE_GAME_PROCESS_NAMES",
    "MANUAL_WINDOW_VISIBLE_SECONDS",
    "probe_league_process_state",
    "resolve_background_runtime_action",
]

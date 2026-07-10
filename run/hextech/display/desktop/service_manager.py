"""桌面端服务生命周期管理。

本模块统一管理 Web 前端进程、游戏内 overlay host 进程、Vision sidecar 进程
和低频 LoL 状态监听。它不做图像识别，只负责生命周期边界。

调用方: display.desktop.app、tests.test_overlay_watchdog_contract、dev_checks; 关键依赖: core.settings、overlay.events、overlay.lifecycle。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import win32gui
except ImportError:  # pragma: no cover - 仅用于非 Windows 诊断环境兜底
    win32gui = None

from hextech.core.settings import load_ui_feature_flags, save_ui_feature_flags
from hextech.overlay.events import read_overlay_event
from hextech.overlay.lifecycle import GameOverlayController
from hextech.overlay.runtime_paths import overlay_runtime_state_path
from hextech.overlay.window import find_lol_game_window
from hextech.overlay.window_titles import LOL_CLIENT_WINDOW_TITLE


ProcessFactory = Callable[[], Any]
OVERLAY_HOST_VISIBILITY_STALE_SECONDS = 6.0
OVERLAY_VISION_TRACE_FILE = Path(overlay_runtime_state_path("overlay_vision_trace.v1.json"))
OVERLAY_HOST_VISIBILITY_FILE = Path(overlay_runtime_state_path("game_overlay_visibility.v1.json"))


@dataclass
class ManagedService:
    """单个子服务的进程与状态快照。"""

    name: str
    process: Any = None
    status: str = "stopped"
    last_error: str = ""
    updated_at: float = field(default_factory=time.time)

    def mark(self, status: str, *, error: str = "") -> None:
        self.status = status
        self.last_error = error
        self.updated_at = time.time()

    def pid(self) -> int | None:
        return getattr(self.process, "pid", None) if self.process is not None else None

    def is_running(self) -> bool:
        if self.process is None:
            return False
        poll = getattr(self.process, "poll", None)
        if callable(poll):
            return poll() is None
        return self.status == "running"

    def snapshot(self) -> dict[str, Any]:
        if self.status == "running" and not self.is_running():
            self.process = None
            self.mark("stopped")
        return {
            "status": self.status,
            "pid": self.pid(),
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


class ServiceManager:
    """协调 Web 前端、游戏内 overlay 和低频监听的启动停止。"""

    def __init__(
        self,
        *,
        start_web_func: ProcessFactory,
        overlay_controller: GameOverlayController | None = None,
        start_overlay_func: ProcessFactory | None = None,
        start_vision_sidecar_func: ProcessFactory | None = None,
        prepare_overlay_hint_cache_func: Callable[[], Any] | None = None,
        write_inactive_overlay_event_func: Callable[[], Any] | None = None,
        listener_interval_seconds: float = 3.0,
        manage_overlay_runtime: bool = False,
    ) -> None:
        self._start_web_func = start_web_func
        self._manage_overlay_runtime = bool(manage_overlay_runtime)
        if not self._manage_overlay_runtime:
            overlay_controller = None
        elif overlay_controller is None:
            controller_kwargs: dict[str, Any] = {}
            if start_overlay_func is not None:
                controller_kwargs["start_host_func"] = start_overlay_func
            if start_vision_sidecar_func is not None:
                controller_kwargs["start_sidecar_func"] = start_vision_sidecar_func
            if prepare_overlay_hint_cache_func is not None:
                controller_kwargs["prepare_data_func"] = prepare_overlay_hint_cache_func
            if write_inactive_overlay_event_func is not None:
                controller_kwargs["write_inactive_func"] = write_inactive_overlay_event_func
            overlay_controller = GameOverlayController(**controller_kwargs)
        self._overlay_controller = overlay_controller
        self.web = ManagedService("web")
        # 兼容桌面状态栏的字段访问；实际启停与回滚全部由 Controller 负责。
        self.game_overlay = ManagedService("game_overlay")
        self.vision_sidecar = ManagedService("vision_sidecar")
        self._lock = threading.RLock()
        self._listener_stop = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._shutdown_thread: threading.Thread | None = None
        self._shutdown_done: threading.Event | None = None
        self._listener_interval_seconds = max(2.0, min(5.0, float(listener_interval_seconds)))
        self._listener_enabled = True
        self._shutdown_requested = False
        self._listener_snapshot: dict[str, Any] = {
            "enabled": True,
            "interval_seconds": self._listener_interval_seconds,
            "checks": 0,
            "last_checked_at": 0.0,
            "lol_client_visible": False,
            "lol_game_visible": False,
        }
        self._overlay_watchdog: dict[str, Any] = {
            "enabled": False,
            "last_checked_at": 0.0,
            "last_action": "",
            "last_action_at": 0.0,
            "last_error": "",
            "state_age_ms": None,
            "state_path": str(OVERLAY_VISION_TRACE_FILE),
        }

    def start_web(self) -> None:
        with self._lock:
            if self.web.is_running():
                self.web.mark("running")
                return
            self.web.mark("starting")
            try:
                self.web.process = self._start_process(self.web.name, self._start_web_func)
                self.web.mark("running")
            except Exception as exc:
                self.web.process = None
                self.web.mark("error", error=str(exc))
                raise

    def stop_web(self) -> None:
        with self._lock:
            self._stop_service(self.web)

    def start_game_overlay(self) -> None:
        with self._lock:
            if self._overlay_controller is None:
                raise RuntimeError("game_overlay 由 Runtime Supervisor 管理")
            if self._shutdown_requested:
                raise RuntimeError("ServiceManager 已进入关闭流程，拒绝重新启动 game_overlay")
            if self._overlay_controller.is_running():
                self._sync_overlay_compat_state()
                return
            self.game_overlay.mark("starting")
            try:
                self._overlay_controller.start()
                self._sync_overlay_compat_state()
            except Exception as exc:
                self._sync_overlay_compat_state()
                self.game_overlay.mark("error", error=str(exc))
                self.vision_sidecar.mark("error", error=str(exc))
                raise

    def stop_game_overlay(self) -> None:
        with self._lock:
            if self._overlay_controller is None:
                self.game_overlay.mark("stopped")
                self.vision_sidecar.mark("stopped")
                return
            self._overlay_controller.stop()
            self._sync_overlay_compat_state()

    def is_web_running(self) -> bool:
        return self.web.is_running()

    def is_game_overlay_running(self) -> bool:
        if self._overlay_controller is None:
            return False
        return self._overlay_controller.is_running()

    def ensure_game_overlay_healthy(self, *, enabled: bool) -> dict[str, Any]:
        """保留旧 snapshot 字段；overlay 健康与重启只由 Runtime Supervisor 决定。"""

        with self._lock:
            now = time.time()
            effective_enabled = bool(enabled) and not self._shutdown_requested
            self._overlay_watchdog["enabled"] = effective_enabled
            self._overlay_watchdog["last_checked_at"] = now
            self._overlay_watchdog["last_error"] = ""
            self._overlay_watchdog["state_age_ms"] = None
            self._overlay_watchdog["last_action"] = "supervisor_owned" if effective_enabled else "disabled"
            return dict(self._overlay_watchdog)

    def _sync_overlay_compat_state(self) -> None:
        if self._overlay_controller is None:
            self.game_overlay.process = None
            self.game_overlay.mark("stopped")
            self.vision_sidecar.process = None
            self.vision_sidecar.mark("stopped")
            return
        snapshot = self._overlay_controller.snapshot()
        self.game_overlay.process = self._overlay_controller.host_process
        self.game_overlay.mark(snapshot["status"], error=str(snapshot.get("last_error") or ""))
        self.vision_sidecar.process = self._overlay_controller.sidecar_process
        self.vision_sidecar.mark(
            str(snapshot.get("sidecar_status") or "stopped"),
            error=str(snapshot.get("last_error") or "") if snapshot.get("status") == "error" else "",
        )

    def set_low_frequency_listener_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._listener_enabled = bool(enabled)
            self._listener_snapshot["enabled"] = self._listener_enabled

    def start_low_frequency_listener(self) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self._listener_stop.clear()
        self._listener_thread = threading.Thread(
            target=self._low_frequency_listener_loop,
            name="hextech-low-frequency-listener",
            daemon=True,
        )
        self._listener_thread.start()

    def persist_user_preferences(self, flags: dict[str, Any]) -> dict[str, bool]:
        return save_ui_feature_flags(flags)

    def load_user_preferences(self) -> dict[str, bool]:
        return load_ui_feature_flags()

    def get_status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            overlay_snapshot = self._overlay_controller.snapshot() if self._overlay_controller is not None else {
                "status": "supervisor_owned",
                "host_pid": None,
                "sidecar_pid": None,
                "host_status": "unknown",
                "sidecar_status": "unknown",
                "context_poller_status": "unknown",
                "context_poller_error": "",
                "last_error": "",
                "residual_pids": {},
                "updated_at": time.time(),
            }
            return {
                "web": self.web.snapshot(),
                "game_overlay": overlay_snapshot,
                "vision_sidecar": {
                    "status": overlay_snapshot["sidecar_status"],
                    "pid": overlay_snapshot["sidecar_pid"],
                    "last_error": overlay_snapshot["last_error"] if overlay_snapshot["status"] == "error" else "",
                    "updated_at": overlay_snapshot["updated_at"],
                },
                "low_frequency_listener": dict(self._listener_snapshot),
                "overlay_event": self._overlay_event_status(),
                "overlay_visibility": self._overlay_host_visibility_status(),
                "overlay_watchdog": dict(self._overlay_watchdog),
            }

    def shutdown(self, *, timeout_seconds: float = 5.0, final_timeout_seconds: float | None = 20.0) -> None:
        """关闭所有受管服务。

        overlay/web 的停止可能等待 terminate/kill 兜底。这里先在后台线程执行，
        主线程等待一个短窗口；默认退出路径再做最终 join，避免 UI 销毁时留下
        孤儿进程。需要明确快返回的调用方可传 ``final_timeout_seconds=None``。
        """

        self._listener_stop.set()
        with self._lock:
            self._shutdown_requested = True
        self._shutdown_done = threading.Event()

        def _stop_all() -> None:
            try:
                self.stop_game_overlay()
            finally:
                self.stop_web()
                self._shutdown_done.set()

        self._shutdown_thread = threading.Thread(
            target=_stop_all,
            name="hextech-shutdown",
            daemon=final_timeout_seconds is None,
        )
        self._shutdown_thread.start()
        if self._shutdown_done.wait(timeout=timeout_seconds):
            return
        if final_timeout_seconds is not None:
            self._shutdown_thread.join(timeout=final_timeout_seconds)

    @staticmethod
    def _overlay_event_status() -> dict[str, Any]:
        try:
            snapshot = read_overlay_event()
        except Exception as exc:
            return {"ok": False, "error": exc.__class__.__name__, "visible": False, "reason": "event_read_failed"}
        source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
        return {
            "ok": bool(snapshot.get("ok")),
            "visible": bool(snapshot.get("visible")),
            "active": bool(snapshot.get("active")),
            "error": str(snapshot.get("error") or ""),
            "reason": str(source.get("reason") or ""),
            "selection_type": str(snapshot.get("selection_type") or ""),
            "updated_at": float(snapshot.get("generated_at") or 0.0),
        }

    @staticmethod
    def _overlay_host_visibility_status() -> dict[str, Any]:
        try:
            snapshot = json.loads(OVERLAY_HOST_VISIBILITY_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"ok": False, "error": "visibility_status_missing", "visible": False, "reason": ""}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {"ok": False, "error": exc.__class__.__name__, "visible": False, "reason": ""}
        if not isinstance(snapshot, dict):
            return {"ok": False, "error": "visibility_status_invalid", "visible": False, "reason": ""}
        if int(snapshot.get("schema_version") or 0) != 1:
            return {"ok": False, "error": "visibility_status_unknown_schema", "visible": False, "reason": ""}
        try:
            updated_at = float(snapshot.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at <= 0.0 or time.time() - updated_at > OVERLAY_HOST_VISIBILITY_STALE_SECONDS:
            return {"ok": False, "error": "visibility_status_stale", "visible": False, "reason": ""}
        decision = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
        host = snapshot.get("host") if isinstance(snapshot.get("host"), dict) else {}
        scene = snapshot.get("scene") if isinstance(snapshot.get("scene"), dict) else {}
        context = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else {}
        return {
            "ok": True,
            "visible": bool(decision.get("window_visible")),
            "reason": str(decision.get("reason") or ""),
            "updated_at": updated_at,
            "host": dict(host),
            "scene": dict(scene),
            "context": dict(context),
        }

    @staticmethod
    def _start_process(service_name: str, factory: ProcessFactory) -> Any:
        process = factory()
        if process is None:
            raise RuntimeError(f"{service_name} 启动失败：未返回进程对象")
        poll = getattr(process, "poll", None)
        if callable(poll):
            exit_code = poll()
            if exit_code is not None:
                raise RuntimeError(f"{service_name} 启动失败：进程已退出，exit_code={exit_code}")
        return process

    def _stop_service(self, service: ManagedService) -> None:
        process = service.process
        if not process:
            service.mark("stopped")
            return
        service.mark("stopping")
        try:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                terminate()

            if not self._wait_for_process_exit(process, timeout=3):
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                if not self._wait_for_process_exit(process, timeout=3):
                    service.mark("error", error=f"{service.name} 停止失败：进程未退出")
                    return

            service.process = None
            service.mark("stopped")
        except Exception as exc:
            service.mark("error", error=str(exc))

    @staticmethod
    def _wait_for_process_exit(process: Any, *, timeout: float) -> bool:
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=timeout)
            except Exception:
                return False
        poll = getattr(process, "poll", None)
        if callable(poll):
            return poll() is not None
        return True

    def _low_frequency_listener_loop(self) -> None:
        while not self._listener_stop.is_set():
            with self._lock:
                listener_enabled = self._listener_enabled
            if listener_enabled:
                snapshot = self._poll_lol_window_state()
                with self._lock:
                    self._listener_snapshot.update(snapshot)
                    self._listener_snapshot["enabled"] = True
                    self._listener_snapshot["interval_seconds"] = self._listener_interval_seconds
                    self._listener_snapshot["checks"] = int(self._listener_snapshot.get("checks", 0)) + 1
                    self._listener_snapshot["last_checked_at"] = time.time()
            self._listener_stop.wait(self._listener_interval_seconds)

    @staticmethod
    def _poll_lol_window_state() -> dict[str, bool]:
        if win32gui is None:
            return {"lol_client_visible": False, "lol_game_visible": False}
        client = win32gui.FindWindow(None, LOL_CLIENT_WINDOW_TITLE)
        game = find_lol_game_window()
        return {
            "lol_client_visible": bool(client and win32gui.IsWindowVisible(client) and not win32gui.IsIconic(client)),
            "lol_game_visible": game is not None,
        }

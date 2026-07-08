"""Hextech Runtime Supervisor。

文件职责：
- 作为桌面 UI 之外的执行面，承载受管组件状态、lease、控制 API 与结构化事件日志。
- 首版只实现可独立验证的控制面骨架和 refresh action；具体 Web/overlay 组件逐步接入。

维护边界：
- nonce 只在内存和父子匿名管道中传递，不写入日志或状态文件。
- 控制 API 面向本机进程调用，使用 loopback 绑定、Host header 校验和 nonce header 鉴权。

调用方: hextech_ui、tests.test_runtime_supervisor; 关键依赖: psutil、catalog.runtime_store、core.refresh。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import psutil

from hextech.catalog.runtime_store import build_runtime_state_path, ensure_private_runtime_dir
from hextech.core.refresh import refresh_backend_data, sanitize_event_message
from hextech.overlay.events import write_inactive_overlay_event
from hextech.overlay.runtime_paths import overlay_runtime_state_path
from hextech.overlay.lifecycle import (
    ProcessFactory,
    ProcessLike,
    start_host_process,
    start_sidecar_process,
    stop_process,
)
from hextech.overlay.context import start_overlay_context_poller, write_missing_overlay_context
from hextech.overlay.data_source import prepare_shared_overlay_data

SUPERVISOR_NONCE_HEADER = "X-Hextech-Supervisor-Nonce"
SUPERVISOR_EVENT_SCHEMA_VERSION = 1
SAFE_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DEFAULT_REFRESH_INTERVAL_SECONDS = 4 * 60 * 60
STARTUP_REFRESH_DELAY_SECONDS = 10.0
OVERLAY_HOST_VISIBILITY_STALE_SECONDS = 6.0
TEMPLATE_PREWARM_WAIT_TIMEOUT_SECONDS = 8.0


class OverlayRuntimeManager:
    """Supervisor 持有的游戏内显示运行态。

    这里刻意只做生命周期编排和状态汇总：host、Vision sidecar、context poller
    与模板 runtime cache 的 owner 都收敛到 Runtime Supervisor。桌面 UI 只提交
    desired state，不再直接启动这些进程。
    """

    def __init__(
        self,
        *,
        start_host_func: ProcessFactory = start_host_process,
        start_sidecar_func: ProcessFactory = start_sidecar_process,
        start_context_poller_func: Callable[[], Any] | None = start_overlay_context_poller,
        prepare_data_func: Callable[[], Mapping[str, Any]] = prepare_shared_overlay_data,
        write_inactive_func: Callable[[], Any] = write_inactive_overlay_event,
        load_template_runtime_func: Callable[..., Any] | None = None,
        visibility_status_file: str | Path | None = None,
        prewarm_wait_timeout_seconds: float = TEMPLATE_PREWARM_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._start_host_func = start_host_func
        self._start_sidecar_func = start_sidecar_func
        self._start_context_poller_func = start_context_poller_func
        self._prepare_data_func = prepare_data_func
        self._write_inactive_func = write_inactive_func
        self._load_template_runtime_func = load_template_runtime_func
        self._visibility_status_file = Path(visibility_status_file) if visibility_status_file is not None else Path(overlay_runtime_state_path("game_overlay_visibility.v1.json"))
        self._prewarm_wait_timeout_seconds = max(0.1, float(prewarm_wait_timeout_seconds))
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._prewarm_thread: threading.Thread | None = None
        self.host_process: ProcessLike | None = None
        self.sidecar_process: ProcessLike | None = None
        self.context_poller: Any | None = None
        self.context_error = ""
        self.desired_enabled = False
        self.status = "stopped"
        self.phase = "idle"
        self.cache_status = "idle"
        self.cache_hit: bool | None = None
        self.cache_stats: dict[str, Any] = {}
        self.startup_seconds = 0.0
        self.visible_reason = ""
        self.last_error = ""
        self.last_start_failure_kind = ""
        self.updated_at = time.time()

    def _mark(self, *, status: str | None = None, phase: str | None = None, error: str | None = None) -> None:
        if status is not None:
            self.status = status
        if phase is not None:
            self.phase = phase
        if error is not None:
            self.last_error = error
        self.updated_at = time.time()

    @staticmethod
    def _process_running(process: ProcessLike | None) -> bool:
        return bool(process is not None and process.poll() is None)

    def _host_pid(self) -> int | None:
        return getattr(self.host_process, "_hextech_overlay_runtime_pid", None) or getattr(self.host_process, "pid", None)

    def _sidecar_pid(self) -> int | None:
        return getattr(self.sidecar_process, "pid", None)

    def _start_context_poller(self) -> None:
        if self.context_poller is not None or self._start_context_poller_func is None:
            return
        try:
            self.context_poller = self._start_context_poller_func()
            self.context_error = ""
        except Exception as exc:
            self.context_poller = None
            self.context_error = str(exc)

    def _stop_context_poller(self) -> None:
        poller = self.context_poller
        self.context_poller = None
        self.context_error = ""
        if poller is None:
            return
        stop = getattr(poller, "stop", None)
        if callable(stop):
            stop()
            return
        if callable(poller):
            poller()

    def _context_status(self) -> str:
        if self.context_poller is not None:
            return "running"
        return "degraded" if self.context_error else "stopped"

    def _read_visible_reason(self) -> str:
        try:
            payload = json.loads(self._visibility_status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return ""
        if not isinstance(payload, Mapping) or int(payload.get("schema_version") or 0) != 1:
            return ""
        try:
            updated_at = float(payload.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        if updated_at <= 0.0 or time.time() - updated_at > OVERLAY_HOST_VISIBILITY_STALE_SECONDS:
            return ""
        decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
        return str(decision.get("reason") or "").strip()

    def _template_loader(self) -> Callable[..., Any]:
        if self._load_template_runtime_func is not None:
            return self._load_template_runtime_func
        from hextech.overlay.vision.sidecar import load_or_build_default_template_runtime

        return load_or_build_default_template_runtime

    def start_template_prewarm(self) -> None:
        with self._lock:
            if self._prewarm_thread is not None and self._prewarm_thread.is_alive():
                return
            self.cache_status = "queued"
            self.phase = "prewarming" if not self.desired_enabled else self.phase
            self.cache_hit = None
            self.cache_stats = {}
        thread = threading.Thread(target=self._prewarm_templates, name="hextech-overlay-template-prewarm", daemon=True)
        self._prewarm_thread = thread
        thread.start()

    def _prewarm_templates(self) -> None:
        started_at = time.perf_counter()
        try:
            with self._lock:
                self.cache_status = "prewarming"
            hint_cache = dict(self._prepare_data_func() or {})

            def _cache_status(phase: str, fields: Mapping[str, Any]) -> None:
                with self._lock:
                    self.phase = "prewarming" if not self.desired_enabled else self.phase
                    if phase == "template_runtime_cache_lookup":
                        self.cache_status = "lookup"
                    elif phase in {"template_index_build", "rank_matrix_build"}:
                        self.cache_status = "building"
                    else:
                        self.cache_status = str(phase or "prewarming")
                    if "cache_hit" in fields:
                        self.cache_hit = bool(fields.get("cache_hit"))
                    self.cache_stats = dict(fields)
                    self.startup_seconds = round(time.perf_counter() - started_at, 3)

            runtime = self._template_loader()(hint_cache=hint_cache, status_callback=_cache_status)
            with self._lock:
                self.cache_status = "ready"
                if self.cache_hit is not False:
                    self.cache_hit = True
                stats = getattr(runtime, "stats", None)
                if isinstance(stats, Mapping):
                    self.cache_stats = dict(stats)
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
                if not self.desired_enabled and self.status == "stopped":
                    self.phase = "ready"
                self.last_error = ""
        except Exception as exc:
            with self._lock:
                self.cache_status = "error"
                self.last_error = str(exc)
                self.startup_seconds = round(time.perf_counter() - started_at, 3)

    def _raise_template_prewarm_error_if_any(self) -> None:
        if self.cache_status == "error":
            raise RuntimeError(f"template runtime cache 预热失败：{self.last_error or 'unknown'}")

    def _wait_for_template_prewarm(self, *, started_at: float) -> None:
        deadline = time.perf_counter() + self._prewarm_wait_timeout_seconds
        with self._lock:
            self._raise_template_prewarm_error_if_any()
            thread = self._prewarm_thread if self._prewarm_thread is not None and self._prewarm_thread.is_alive() else None
            if thread is not None:
                self.phase = "cache_wait"
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
        if thread is None:
            return
        while thread.is_alive():
            if time.perf_counter() >= deadline:
                with self._lock:
                    self.phase = "cache_wait_timeout"
                    self.last_error = "template runtime cache 预热仍在进行，请稍后重试"
                    self.last_start_failure_kind = "template_prewarm_timeout"
                    self.startup_seconds = round(time.perf_counter() - started_at, 3)
                raise TimeoutError(self.last_error)
            thread.join(timeout=0.25)
            with self._lock:
                self.phase = "cache_wait"
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
        with self._lock:
            self._raise_template_prewarm_error_if_any()

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        with self._operation_lock:
            return self._start() if bool(enabled) else self._stop("disabled")

    def _start(self) -> dict[str, Any]:
        started_at = time.perf_counter()
        with self._lock:
            self.desired_enabled = True
            if self._process_running(self.host_process) and self._process_running(self.sidecar_process):
                self._start_context_poller()
                self._mark(status="running", phase="running", error="")
                self.last_start_failure_kind = ""
                return self.snapshot()
            self._mark(
                status="starting",
                phase="prepare_data",
                error=None if self.cache_status == "error" else "",
            )
        try:
            self._prepare_data_func()
            self._write_inactive_func()
            with self._lock:
                self.phase = "context_start"
            self._start_context_poller()
            with self._lock:
                self.phase = "host_start"
            if not self._process_running(self.host_process):
                self.host_process = self._start_host_func()
            if not self._process_running(self.host_process):
                raise RuntimeError("game_overlay host 启动后立即退出")
            try:
                self._wait_for_template_prewarm(started_at=started_at)
            except TimeoutError:
                with self._lock:
                    self.startup_seconds = round(time.perf_counter() - started_at, 3)
                    self._mark(status="starting", phase="vision_prewarming")
                return self.snapshot()
            with self._lock:
                self.phase = "sidecar_start"
            if not self._process_running(self.sidecar_process):
                self.sidecar_process = self._start_sidecar_func()
            if not self._process_running(self.sidecar_process):
                raise RuntimeError("game_overlay sidecar 启动后立即退出")
            with self._lock:
                self.startup_seconds = round(time.perf_counter() - started_at, 3)
                self._mark(status="running", phase="running", error="")
                self.last_start_failure_kind = ""
            return self.snapshot()
        except Exception as exc:
            self._rollback_failed_start(str(exc))
            raise

    def _rollback_failed_start(self, reason: str) -> None:
        errors: list[str] = []
        try:
            self._write_inactive_func()
        except Exception as exc:
            errors.append(f"inactive 事件写入失败：{exc}")
        try:
            self._stop_context_poller()
        except Exception as exc:
            errors.append(f"context poller 停止失败：{exc}")
        if not stop_process(self.sidecar_process):
            errors.append(f"sidecar 停止失败(pid={self._sidecar_pid()})")
        else:
            self.sidecar_process = None
        if not stop_process(self.host_process):
            errors.append(f"host 停止失败(pid={self._host_pid()})")
        else:
            self.host_process = None
        error = "；".join([reason, *errors]) if errors else reason
        with self._lock:
            self._mark(status="error", phase="failed", error=error)
            self.last_start_failure_kind = self._classify_start_failure_kind(reason)

    @staticmethod
    def _classify_start_failure_kind(reason: str) -> str:
        text = str(reason or "")
        if "game_overlay host 启动超时" in text:
            return "host_readiness_timeout"
        if "sidecar" in text:
            return "sidecar_failed"
        if "readiness token 不匹配" in text:
            return "host_readiness_token_mismatch"
        if "game_overlay host 在 readiness 前退出" in text or "host 启动后立即退出" in text:
            return "host_exited"
        return "start_failed"

    def _stop(self, reason: str) -> dict[str, Any]:
        with self._lock:
            self.desired_enabled = False
            self._mark(status="stopping", phase=reason)
        errors: list[str] = []
        try:
            self._stop_context_poller()
        except Exception as exc:
            errors.append(f"context poller 停止失败：{exc}")
        try:
            self._write_inactive_func()
        except Exception as exc:
            errors.append(f"inactive 事件写入失败：{exc}")
        try:
            write_missing_overlay_context(source=f"supervisor-{reason}")
        except Exception as exc:
            errors.append(f"空上下文写入失败：{exc}")
        sidecar_stopped = stop_process(self.sidecar_process)
        # sidecar graceful exit 期间可能最后写出一帧 active 事件；停止后再写一次
        # inactive，确保 host/renderer 最终看到隐藏态。
        try:
            self._write_inactive_func()
        except Exception as exc:
            errors.append(f"最终 inactive 事件写入失败：{exc}")
        host_stopped = stop_process(self.host_process)
        if sidecar_stopped:
            self.sidecar_process = None
        else:
            errors.append(f"sidecar 停止失败(pid={self._sidecar_pid()})")
        if host_stopped:
            self.host_process = None
        else:
            errors.append(f"host 停止失败(pid={self._host_pid()})")
        with self._lock:
            if errors:
                self._mark(status="error", phase="stop_failed", error="；".join(errors))
            else:
                self._mark(status="stopped", phase="stopped", error="")
                self.last_start_failure_kind = ""
        if errors:
            raise RuntimeError("；".join(errors))
        return self.snapshot()

    def shutdown(self, reason: str = "shutdown") -> None:
        with self._operation_lock:
            self._stop(str(reason or "shutdown"))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self.status == "running":
                if not self._process_running(self.host_process):
                    self._mark(status="error", phase="host_exited", error="game_overlay host 意外退出")
                elif self.sidecar_process is not None and not self._process_running(self.sidecar_process):
                    self._mark(status="error", phase="sidecar_exited", error="game_overlay sidecar 意外退出")
            self.visible_reason = self._read_visible_reason()
            return {
                "desired_enabled": self.desired_enabled,
                "status": self.status,
                "phase": self.phase,
                "host_pid": self._host_pid() if self._process_running(self.host_process) else None,
                "sidecar_pid": self._sidecar_pid() if self._process_running(self.sidecar_process) else None,
                "context_status": self._context_status(),
                "cache_status": self.cache_status,
                "cache_hit": self.cache_hit,
                "cache_stats": dict(self.cache_stats),
                "startup_seconds": self.startup_seconds,
                "visible_reason": self.visible_reason,
                "last_error": self.last_error,
                "last_start_failure_kind": self.last_start_failure_kind,
                "updated_at": self.updated_at,
            }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host_name_from_header(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("[::1]"):
        return "[::1]"
    if ":" in text:
        return text.rsplit(":", 1)[0]
    return text


def host_header_allowed(value: str) -> bool:
    return _host_name_from_header(value) in SAFE_HOSTS


@dataclass
class SupervisorHttpServer:
    server: ThreadingHTTPServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class RuntimeSupervisor:
    """Runtime Supervisor 控制面核心。"""

    def __init__(
        self,
        *,
        parent_pid: int,
        session_nonce: str | None = None,
        refresh_func: Callable[..., Any] | None = None,
        overlay_runtime: Any | None = None,
        event_log_path: str | Path | None = None,
        lease_timeout_seconds: float = 6.0,
        orphan_grace_seconds: float = 15.0,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.supervisor_instance_id = f"sup-{uuid.uuid4().hex}"
        self.parent_pid = int(parent_pid or 0)
        self.session_nonce = session_nonce or secrets.token_urlsafe(24)
        self._refresh_func = refresh_func or refresh_backend_data
        self._overlay_runtime = overlay_runtime or OverlayRuntimeManager()
        self._event_log_path = Path(event_log_path) if event_log_path is not None else Path(build_runtime_state_path("supervisor_events.v1.jsonl"))
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._lease_timeout_seconds = max(1.0, float(lease_timeout_seconds))
        self._orphan_grace_seconds = max(1.0, float(orphan_grace_seconds))
        self._refresh_interval_seconds = max(60.0, float(refresh_interval_seconds))
        self._lease: dict[str, Any] = {
            "control_instance_id": "",
            "last_renewed_at": 0.0,
            "state": "disconnected",
        }
        self._desired_state: dict[str, Any] = {}
        self._actions: dict[str, dict[str, Any]] = {}
        self._components: dict[str, dict[str, Any]] = {}
        self._shutdown_requested = threading.Event()
        self._shutdown_reason = ""
        self._overlay_shutdown_called = False
        self._active_refresh_action_id = ""
        self._active_overlay_action_id = ""
        self._overlay_retry_after_prewarm_used = False
        self._last_refresh_at = 0.0
        # 首刷延迟到 UI 启动后，避免恢复旧的首 tick 同步卡顿，同时保证无人手动触发时也会自动刷新。
        self._next_refresh_at = self._started_at + STARTUP_REFRESH_DELAY_SECONDS

    def parent_alive(self) -> bool:
        return bool(self.parent_pid and psutil.pid_exists(self.parent_pid))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lease_age = time.time() - float(self._lease.get("last_renewed_at") or 0.0)
            components = dict(self._components)
            try:
                components["game_overlay"] = self._overlay_runtime.snapshot()
            except Exception as exc:
                components["game_overlay"] = {
                    "desired_enabled": False,
                    "status": "error",
                    "phase": "snapshot_failed",
                    "host_pid": None,
                    "sidecar_pid": None,
                    "context_status": "unknown",
                    "cache_status": "unknown",
                    "cache_hit": None,
                    "startup_seconds": 0.0,
                    "visible_reason": "",
                    "last_error": sanitize_event_message(str(exc)),
                    "last_start_failure_kind": "snapshot_failed",
                }
            return {
                "supervisor_instance_id": self.supervisor_instance_id,
                "parent_pid": self.parent_pid,
                "parent_alive": self.parent_alive(),
                "uptime_seconds": int(time.time() - self._started_at),
                "lease": dict(self._lease),
                "lease_age_seconds": int(lease_age) if self._lease.get("last_renewed_at") else None,
                "lease_timeout_seconds": self._lease_timeout_seconds,
                "orphan_grace_seconds": self._orphan_grace_seconds,
                "last_refresh_at": self._last_refresh_at or None,
                "next_refresh_at": self._next_refresh_at or None,
                "shutdown_reason": self._shutdown_reason,
                "desired_state": dict(self._desired_state),
                "components": components,
                "actions": dict(self._actions),
            }

    def start_overlay_template_prewarm(self) -> None:
        start_prewarm = getattr(self._overlay_runtime, "start_template_prewarm", None)
        if callable(start_prewarm):
            start_prewarm()

    def renew_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        control_instance_id = str(payload.get("control_instance_id") or "").strip()
        now = time.time()
        with self._lock:
            previous = str(self._lease.get("control_instance_id") or "")
            self._lease.update(
                {
                    "control_instance_id": control_instance_id,
                    "last_renewed_at": now,
                    "state": "connected",
                    "renewed_at": _utc_now_iso(),
                }
            )
        if previous and previous != control_instance_id:
            self.append_event(
                {
                    "event": "lease.replaced",
                    "component": "supervisor",
                    "previous_control_instance_id": previous,
                    "control_instance_id": control_instance_id,
                }
            )
        else:
            self.append_event(
                {
                    "event": "lease.renewed",
                    "component": "supervisor",
                    "control_instance_id": control_instance_id,
                }
            )
        return self.snapshot()

    def update_desired_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._desired_state.update(dict(payload))
        self.append_event({"event": "desired_state.updated", "component": "supervisor"})
        return self.snapshot()

    def _result_payload(self, result: Any) -> dict[str, Any]:
        if is_dataclass(result):
            return asdict(result)
        if isinstance(result, dict):
            return dict(result)
        if hasattr(result, "__dict__"):
            return dict(result.__dict__)
        return {"result": bool(result)}

    def _execute_refresh_action(self, action_id: str, *, force: bool, started_at: float) -> None:
        try:
            result_payload = self._result_payload(self._refresh_func(force=force))
            status = "completed"
            self.append_event(
                {
                    "event": "refresh.completed",
                    "component": "refresh",
                    "correlation_id": action_id,
                    "duration_seconds": int(max(0.0, time.time() - started_at)),
                    "result_state": result_payload.get("state", ""),
                }
            )
        except Exception as exc:
            status = "failed"
            result_payload = {"error_type": exc.__class__.__name__, "error_message": sanitize_event_message(str(exc))}
            self.append_event(
                {
                    "event": "refresh.failed",
                    "level": "ERROR",
                    "component": "refresh",
                    "correlation_id": action_id,
                    "error_type": exc.__class__.__name__,
                    "error_message_sanitized": str(exc),
                }
            )
        completed_at = time.time()
        with self._lock:
            self._actions[action_id] = {
                **self._actions.get(action_id, {}),
                "status": status,
                "completed_at": completed_at,
                "completed_at_iso": _utc_now_iso(),
                "result": result_payload,
            }
            if self._active_refresh_action_id == action_id:
                self._active_refresh_action_id = ""
            self._last_refresh_at = completed_at
            self._next_refresh_at = completed_at + self._refresh_interval_seconds

    def run_refresh_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action_id = f"act-{uuid.uuid4().hex}"
        force = bool(payload.get("force"))
        started_at = time.time()
        with self._lock:
            if self._active_refresh_action_id:
                action = self._actions.get(self._active_refresh_action_id)
                if action and action.get("status") == "running":
                    return dict(action)
            self._actions[action_id] = {
                "action_id": action_id,
                "type": "refresh",
                "status": "running",
                "force": force,
                "started_at": started_at,
                "started_at_iso": _utc_now_iso(),
            }
            self._active_refresh_action_id = action_id
        self.append_event({"event": "refresh.started", "component": "refresh", "correlation_id": action_id})
        thread = threading.Thread(
            target=self._execute_refresh_action,
            kwargs={"action_id": action_id, "force": force, "started_at": started_at},
            name="hextech-supervisor-refresh",
            daemon=True,
        )
        thread.start()
        return self.get_action(action_id) or {"action_id": action_id, "type": "refresh", "status": "running"}

    def _execute_game_overlay_action(self, action_id: str, *, enabled: bool, started_at: float) -> None:
        try:
            result_payload = self._result_payload(self._overlay_runtime.set_enabled(enabled))
            status = "completed"
            self.append_event(
                {
                    "event": "game_overlay.completed",
                    "component": "game_overlay",
                    "correlation_id": action_id,
                    "duration_seconds": int(max(0.0, time.time() - started_at)),
                    "desired_enabled": enabled,
                    "result_status": result_payload.get("status", ""),
                    "result_phase": result_payload.get("phase", ""),
                }
            )
        except Exception as exc:
            status = "failed"
            result_payload = {"error_type": exc.__class__.__name__, "error_message": sanitize_event_message(str(exc))}
            self.append_event(
                {
                    "event": "game_overlay.failed",
                    "level": "ERROR",
                    "component": "game_overlay",
                    "correlation_id": action_id,
                    "desired_enabled": enabled,
                    "error_type": exc.__class__.__name__,
                    "error_message_sanitized": str(exc),
                }
            )
        with self._lock:
            self._actions[action_id] = {
                **self._actions.get(action_id, {}),
                "status": status,
                "completed_at": time.time(),
                "completed_at_iso": _utc_now_iso(),
                "result": result_payload,
            }
            if self._active_overlay_action_id == action_id:
                self._active_overlay_action_id = ""

    def run_game_overlay_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action_id = f"act-{uuid.uuid4().hex}"
        enabled = bool(payload.get("enabled"))
        retry_after_prewarm = bool(payload.get("_retry_after_prewarm"))
        started_at = time.time()
        with self._lock:
            if retry_after_prewarm:
                if self._overlay_retry_after_prewarm_used:
                    return {
                        "action_id": "",
                        "type": "game_overlay",
                        "status": "skipped",
                        "enabled": enabled,
                        "reason": "retry_after_prewarm_used",
                    }
                if self._active_overlay_action_id:
                    action = self._actions.get(self._active_overlay_action_id)
                    return dict(action) if action else {
                        "action_id": "",
                        "type": "game_overlay",
                        "status": "skipped",
                        "enabled": enabled,
                        "reason": "overlay_action_running",
                    }
            for action in reversed(list(self._actions.values())):
                if (
                    action.get("type") == "game_overlay"
                    and action.get("status") == "running"
                    and bool(action.get("enabled")) == enabled
                ):
                    return dict(action)
            self._actions[action_id] = {
                "action_id": action_id,
                "type": "game_overlay",
                "status": "running",
                "enabled": enabled,
                "started_at": started_at,
                "started_at_iso": _utc_now_iso(),
            }
            self._active_overlay_action_id = action_id
            if retry_after_prewarm:
                self._overlay_retry_after_prewarm_used = True
            elif enabled:
                self._overlay_retry_after_prewarm_used = False
        self.append_event(
            {
                "event": "game_overlay.started",
                "component": "game_overlay",
                "correlation_id": action_id,
                "desired_enabled": enabled,
                "retry_after_prewarm": retry_after_prewarm,
            }
        )
        thread = threading.Thread(
            target=self._execute_game_overlay_action,
            kwargs={"action_id": action_id, "enabled": enabled, "started_at": started_at},
            name="hextech-supervisor-game-overlay",
            daemon=True,
        )
        thread.start()
        return self.get_action(action_id) or {"action_id": action_id, "type": "game_overlay", "status": "running"}

    def _maybe_retry_game_overlay_after_cache_ready(self) -> None:
        """冷 cache 首次启动超时时，在预热完成后按 desired state 自动补启动。"""

        with self._lock:
            if self._active_overlay_action_id:
                return
        try:
            overlay = self._overlay_runtime.snapshot()
        except Exception:
            return
        if not bool(overlay.get("desired_enabled")):
            return
        status = str(overlay.get("status") or "")
        if status == "running" and overlay.get("sidecar_pid") is not None:
            return
        if str(overlay.get("cache_status") or "") != "ready":
            return
        failure_kind = str(overlay.get("last_start_failure_kind") or "")
        phase = str(overlay.get("phase") or "")
        if failure_kind != "template_prewarm_timeout":
            return
        if phase not in {"failed", "cache_wait_timeout", "vision_prewarming"}:
            return
        self.append_event(
            {
                "event": "game_overlay.retry_after_prewarm",
                "component": "game_overlay",
                "desired_enabled": True,
                "phase": phase,
                "cache_status": str(overlay.get("cache_status") or ""),
                "failure_kind": failure_kind,
                "last_error": sanitize_event_message(str(overlay.get("last_error") or "")),
            }
        )
        self.run_game_overlay_action({"enabled": True, "_retry_after_prewarm": True})

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            action = self._actions.get(action_id)
            return dict(action) if action else None

    def request_shutdown(self, reason: str = "requested") -> None:
        """请求 supervisor 主循环退出；HTTP handler 只置位，不直接杀进程。"""

        shutdown_reason = str(reason or "requested")
        should_stop_overlay = False
        with self._lock:
            self._shutdown_reason = shutdown_reason
            if not self._overlay_shutdown_called:
                self._overlay_shutdown_called = True
                should_stop_overlay = True
        if should_stop_overlay:
            try:
                self._overlay_runtime.shutdown(shutdown_reason)
            except Exception as exc:
                self.append_event(
                    {
                        "event": "game_overlay.shutdown_failed",
                        "level": "ERROR",
                        "component": "game_overlay",
                        "error_type": exc.__class__.__name__,
                        "error_message_sanitized": str(exc),
                    }
                )
        self._shutdown_requested.set()

    def wait_for_shutdown(self, timeout: float) -> bool:
        return self._shutdown_requested.wait(timeout)

    def tick(self) -> None:
        now = time.time()
        if self.parent_pid and not self.parent_alive():
            self.append_event({"event": "shutdown.parent_gone", "component": "supervisor", "level": "WARNING"})
            self.request_shutdown("parent_gone")
            return
        with self._lock:
            last_lease = float(self._lease.get("last_renewed_at") or 0.0)
            active_refresh = bool(self._active_refresh_action_id)
            next_refresh_at = float(self._next_refresh_at or 0.0)
            last_refresh_at = float(self._last_refresh_at or 0.0)
        if last_lease and now - last_lease > self._lease_timeout_seconds + self._orphan_grace_seconds:
            self.append_event({"event": "lease.expired", "component": "supervisor", "level": "WARNING"})
            with self._lock:
                self._lease["state"] = "expired"
            self.request_shutdown("lease_expired")
            return
        self._maybe_retry_game_overlay_after_cache_ready()
        if not active_refresh and next_refresh_at > 0.0 and now >= next_refresh_at:
            self.run_refresh_action({"force": False})

    def append_event(self, payload: dict[str, Any]) -> None:
        target = self._event_log_path
        ensure_private_runtime_dir(target.parent)
        event = dict(payload)
        event.setdefault("schema_version", SUPERVISOR_EVENT_SCHEMA_VERSION)
        event.setdefault("timestamp", _utc_now_iso())
        event.setdefault("level", "INFO")
        event.setdefault("supervisor_instance_id", self.supervisor_instance_id)
        event.setdefault("component", "supervisor")
        if "error_message_sanitized" in event:
            event["error_message_sanitized"] = sanitize_event_message(event.get("error_message_sanitized"))
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    def serve_in_thread(self, *, port: int = 0) -> SupervisorHttpServer:
        handler = self._build_handler()
        server = ThreadingHTTPServer(("127.0.0.1", int(port)), handler)
        thread = threading.Thread(target=server.serve_forever, name="hextech-runtime-supervisor", daemon=True)
        thread.start()
        return SupervisorHttpServer(server=server, thread=thread)

    def _build_handler(self):
        supervisor = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HextechRuntimeSupervisor/1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
                return

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {}
                return payload if isinstance(payload, dict) else {}

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorize(self) -> bool:
                if not host_header_allowed(self.headers.get("Host", "")):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"})
                    return False
                if self.path == "/healthz":
                    return True
                token = str(self.headers.get(SUPERVISOR_NONCE_HEADER, "")).strip()
                if token != supervisor.session_nonce:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_nonce"})
                    return False
                return True

            def do_GET(self) -> None:
                if not self._authorize():
                    return
                path = urlparse(self.path).path
                if path == "/healthz":
                    self._send_json(HTTPStatus.OK, {"ok": True})
                elif path == "/v1/status":
                    self._send_json(HTTPStatus.OK, supervisor.snapshot())
                elif path.startswith("/v1/actions/"):
                    action_id = path.rsplit("/", 1)[-1]
                    action = supervisor.get_action(action_id)
                    self._send_json(HTTPStatus.OK if action else HTTPStatus.NOT_FOUND, action or {"error": "not_found"})
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:
                if not self._authorize():
                    return
                path = urlparse(self.path).path
                payload = self._read_json()
                if path == "/v1/lease/renew":
                    self._send_json(HTTPStatus.OK, supervisor.renew_lease(payload))
                elif path == "/v1/actions/refresh":
                    self._send_json(HTTPStatus.ACCEPTED, supervisor.run_refresh_action(payload))
                elif path == "/v1/actions/game-overlay":
                    self._send_json(HTTPStatus.ACCEPTED, supervisor.run_game_overlay_action(payload))
                elif path == "/v1/shutdown":
                    supervisor.append_event({"event": "shutdown.requested", "component": "supervisor"})
                    self._send_json(HTTPStatus.OK, {"accepted": True})
                    supervisor.request_shutdown("requested")
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_PUT(self) -> None:
                if not self._authorize():
                    return
                path = urlparse(self.path).path
                if path == "/v1/desired-state":
                    self._send_json(HTTPStatus.OK, supervisor.update_desired_state(self._read_json()))
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hextech Runtime Supervisor")
    parser.add_argument("--parent-pid", type=int, default=os.getppid())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--prewarm-templates", action="store_true")
    args = parser.parse_args(argv)

    supervisor = RuntimeSupervisor(parent_pid=args.parent_pid)
    server = supervisor.serve_in_thread(port=args.port)
    if args.prewarm_templates:
        supervisor.start_overlay_template_prewarm()
    print(
        json.dumps(
            {
                "supervisor_instance_id": supervisor.supervisor_instance_id,
                "port": server.port,
                "session_nonce": supervisor.session_nonce,
                "pid": os.getpid(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        while not supervisor.wait_for_shutdown(0.25):
            supervisor.tick()
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

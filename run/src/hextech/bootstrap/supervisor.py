"""Hextech Runtime Supervisor。

文件职责：
- 作为桌面 UI 之外的执行面，承载受管组件状态、lease、控制 API 与结构化事件日志。
- 只管理 Overlay 生命周期和桌面控制面；数据刷新由 DataService 独占。

维护边界：
- nonce 只在内存和父子匿名管道中传递，不写入日志或状态文件。
- 控制 API 面向本机进程调用，使用 loopback 绑定、Host header 校验和 nonce header 鉴权。

调用方: hextech_ui、tests.test_runtime_supervisor; 关键依赖: psutil、catalog.runtime_store、overlay runtime。
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
from typing import Any
from urllib.parse import urlparse

import psutil

from hextech.modules.data.catalog.runtime_store import build_runtime_state_path, ensure_private_runtime_dir
from hextech.infrastructure.transport.loopback_http import LoopbackThreadingHTTPServer
from hextech.infrastructure.observability.sanitization import sanitize_event_message
from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager
from hextech.modules.session.build_identity import current_build_id

SUPERVISOR_NONCE_HEADER = "X-Hextech-Supervisor-Nonce"
SUPERVISOR_EVENT_SCHEMA_VERSION = 1
SUPERVISOR_EVENT_ACTIVE_MAX_BYTES = 1 * 1024 * 1024
SUPERVISOR_EVENT_BACKUP_COUNT = 3
SAFE_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
OVERLAY_HOST_VISIBILITY_STALE_SECONDS = 6.0
TEMPLATE_PREWARM_WAIT_TIMEOUT_SECONDS = 8.0
OVERLAY_WARM_STARTUP_BUDGET_SECONDS = 30.0
OVERLAY_COLD_STARTUP_BUDGET_SECONDS = 60.0
OVERLAY_CONTINUATION_SECONDS = 120.0
SIDECAR_ATTEMPT_TIMEOUT_SECONDS = 60.0
SIDECAR_RETRY_DELAYS_SECONDS = (1.0,)
OVERLAY_SHUTDOWN_WAIT_SECONDS = 12.0


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
        overlay_runtime: Any | None = None,
        event_log_path: str | Path | None = None,
        lease_timeout_seconds: float = 6.0,
        orphan_grace_seconds: float = 15.0,
        retention_worker: Any | None = None,
    ) -> None:
        self.supervisor_instance_id = f"sup-{uuid.uuid4().hex}"
        self.parent_pid = int(parent_pid or 0)
        self.session_nonce = session_nonce or secrets.token_urlsafe(24)
        self._overlay_runtime = overlay_runtime or OverlayRuntimeManager()
        self._event_log_path = Path(event_log_path) if event_log_path is not None else Path(build_runtime_state_path("supervisor_events.v1.jsonl"))
        if retention_worker is None and event_log_path is None:
            from hextech.infrastructure.persistence.diagnostic_retention import get_diagnostic_retention_worker

            retention_worker = get_diagnostic_retention_worker()
        self._retention_worker = retention_worker
        self._event_write_lock = threading.Lock()
        self._lock = threading.RLock()
        self._started_at = time.time()
        self._lease_timeout_seconds = max(1.0, float(lease_timeout_seconds))
        self._orphan_grace_seconds = max(1.0, float(orphan_grace_seconds))
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
        self._overlay_shutdown_thread: threading.Thread | None = None
        self._active_overlay_action_id = ""

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
        return self.snapshot()

    def update_desired_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._desired_state.update(dict(payload))
        self.append_event({"event": "desired_state.updated", "component": "supervisor"})
        return self.snapshot()

    def _result_payload(self, result: Any) -> dict[str, Any]:
        if is_dataclass(result) and not isinstance(result, type):
            return asdict(result)
        if isinstance(result, dict):
            return dict(result)
        if hasattr(result, "__dict__"):
            return dict(result.__dict__)
        return {"result": bool(result)}

    @staticmethod
    def _overlay_start_event_fields(snapshot: dict[str, Any]) -> dict[str, Any]:
        """只复制启动诊断字段；事件不得携带控制面 nonce 或路径。"""

        return {
            "host_startup_seconds": float(snapshot.get("host_startup_seconds") or 0.0),
            "host_ready_state": str(snapshot.get("host_ready_state") or "unknown"),
            "host_startup_attempts": [
                dict(attempt)
                for attempt in (
                    snapshot.get("host_startup_attempts")
                    if isinstance(snapshot.get("host_startup_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
            ],
            "host_pid": snapshot.get("host_pid"),
            "sidecar_pid": snapshot.get("sidecar_pid"),
            "last_start_failure_kind": str(snapshot.get("last_start_failure_kind") or ""),
        }

    def _execute_game_overlay_action(self, action_id: str, *, enabled: bool, started_at: float) -> None:
        with self._lock:
            action_context = dict(self._actions.get(action_id) or {})
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
                    **self._overlay_start_event_fields(result_payload),
                }
            )
        except Exception as exc:
            status = "failed"
            result_payload = {"error_type": exc.__class__.__name__, "error_message": sanitize_event_message(str(exc))}
            try:
                failure_snapshot = self._result_payload(self._overlay_runtime.snapshot())
            except Exception:
                failure_snapshot = {}
            self.append_event(
                {
                    "event": "game_overlay.failed",
                    "level": "ERROR",
                    "component": "game_overlay",
                    "correlation_id": action_id,
                    "desired_enabled": enabled,
                    "error_type": exc.__class__.__name__,
                    "error_message_sanitized": str(exc),
                    **self._overlay_start_event_fields(failure_snapshot),
                }
            )
            result_payload["runtime"] = failure_snapshot
        old_sidecar_pid = int(action_context.get("old_sidecar_pid") or 0)
        if status == "completed" and old_sidecar_pid:
            new_sidecar_pid = int(result_payload.get("sidecar_pid") or 0)
            self.append_event(
                {
                    "event": (
                        "game_overlay.sidecar_restart"
                        if new_sidecar_pid and new_sidecar_pid != old_sidecar_pid
                        else "game_overlay.sidecar_recovered"
                    ),
                    "component": "game_overlay",
                    "old_sidecar_pid": old_sidecar_pid,
                    "sidecar_pid": new_sidecar_pid or old_sidecar_pid,
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
        started_at = time.time()
        with self._lock:
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
                "old_sidecar_pid": int(payload.get("old_sidecar_pid") or 0),
                "action_reason": str(payload.get("action_reason") or ""),
            }
            self._active_overlay_action_id = action_id
        self.append_event(
            {
                "event": "game_overlay.started",
                "component": "game_overlay",
                "correlation_id": action_id,
                "desired_enabled": enabled,
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

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            action = self._actions.get(action_id)
            return dict(action) if action else None

    def request_shutdown(self, reason: str = "requested") -> None:
        """请求 supervisor 主循环退出；HTTP handler 只置位，不直接杀进程。"""

        shutdown_reason = str(reason or "requested")
        thread: threading.Thread | None = None
        with self._lock:
            self._shutdown_reason = shutdown_reason
            if not self._overlay_shutdown_called:
                self._overlay_shutdown_called = True
                thread = threading.Thread(
                    target=self._shutdown_overlay_runtime,
                    args=(shutdown_reason,),
                    name="hextech-supervisor-overlay-shutdown",
                    daemon=True,
                )
                self._overlay_shutdown_thread = thread
        if thread is not None:
            thread.start()
        # 必须在清理线程登记后发布退出事件；否则主循环可在两步之间退出，
        # wait_for_overlay_shutdown() 会误判没有清理任务并遗留 host/sidecar。
        self._shutdown_requested.set()

    def _shutdown_overlay_runtime(self, shutdown_reason: str) -> None:
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

    def wait_for_shutdown(self, timeout: float) -> bool:
        return self._shutdown_requested.wait(timeout)

    def wait_for_overlay_shutdown(self, timeout: float) -> bool:
        with self._lock:
            thread = self._overlay_shutdown_thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def tick(self) -> None:
        if self.parent_pid and not self.parent_alive():
            self.append_event({"event": "shutdown.parent_gone", "component": "supervisor", "level": "WARNING"})
            self.request_shutdown("parent_gone")
            return
        with self._lock:
            last_lease = float(self._lease.get("last_renewed_at") or 0.0)
        now = time.time()
        if last_lease and now - last_lease > self._lease_timeout_seconds + self._orphan_grace_seconds:
            self.append_event({"event": "lease.expired", "component": "supervisor", "level": "WARNING"})
            with self._lock:
                self._lease["state"] = "expired"
            self.request_shutdown("lease_expired")
            return
        try:
            overlay = self._overlay_runtime.snapshot()
        except Exception:
            return
        observe_generation = getattr(self._overlay_runtime, "observe_data_generation", None)
        raw_generation_observation = observe_generation() if callable(observe_generation) else None
        generation_observation: dict[str, Any] = (
            dict(raw_generation_observation)
            if isinstance(raw_generation_observation, dict)
            else {"changed": False, "state": "unsupported"}
        )
        if generation_observation.get("changed"):
            self.append_event(
                {
                    "event": "game_overlay.data_generation_observed",
                    "component": "game_overlay",
                    **generation_observation,
                }
            )
        should_restart_sidecar = bool(
            overlay.get("desired_enabled")
            and overlay.get("status") == "stale"
            and overlay.get("phase") == "sidecar_stale"
        )
        with self._lock:
            action_running = bool(self._active_overlay_action_id)
        if generation_observation.get("state") == "ready" and not action_running:
            prepare_handoff = getattr(self._overlay_runtime, "prepare_vision_handoff", None)
            if callable(prepare_handoff) and prepare_handoff():
                old_sidecar_pid = int(overlay.get("sidecar_pid") or 0)
                self.append_event(
                    {
                        "event": "game_overlay.vision_handoff_started",
                        "component": "game_overlay",
                        "old_sidecar_pid": old_sidecar_pid,
                        "target_generation_id": generation_observation.get(
                            "observed_data_generation_id", ""
                        ),
                        "target_vision_pool_fingerprint": generation_observation.get(
                            "vision_pool_fingerprint", ""
                        ),
                    }
                )
                self.run_game_overlay_action(
                    {
                        "enabled": True,
                        "old_sidecar_pid": old_sidecar_pid,
                        "action_reason": "vision_handoff",
                    }
                )
                return
        if should_restart_sidecar and not action_running:
            prepare_restart = getattr(self._overlay_runtime, "prepare_sidecar_restart", None)
            if callable(prepare_restart) and not prepare_restart():
                recovered = self._overlay_runtime.snapshot()
                if recovered.get("phase") == "sidecar_recovered":
                    self.append_event(
                        {
                            "event": "game_overlay.sidecar_recovered",
                            "component": "game_overlay",
                            "sidecar_pid": recovered.get("sidecar_pid"),
                        }
                    )
                return
            self.append_event(
                {
                    "event": "game_overlay.sidecar_restart_requested",
                    "component": "game_overlay",
                    "level": "WARNING",
                }
            )
            self.run_game_overlay_action(
                {
                    "enabled": True,
                    "old_sidecar_pid": int(overlay.get("sidecar_pid") or 0),
                    "action_reason": "sidecar_stale",
                }
            )

    def append_event(self, payload: dict[str, Any]) -> None:
        target = self._event_log_path
        ensure_private_runtime_dir(target.parent)
        event = dict(payload)
        event.setdefault("schema_version", SUPERVISOR_EVENT_SCHEMA_VERSION)
        event.setdefault("timestamp", _utc_now_iso())
        event.setdefault("level", "INFO")
        event.setdefault("supervisor_instance_id", self.supervisor_instance_id)
        event.setdefault("component", "supervisor")
        event.setdefault("build_id", current_build_id())
        if "error_message_sanitized" in event:
            event["error_message_sanitized"] = sanitize_event_message(event.get("error_message_sanitized"))
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))
        with self._event_write_lock:
            try:
                current_size = target.stat().st_size
            except OSError:
                current_size = 0
            if current_size > 0 and current_size + encoded_size > SUPERVISOR_EVENT_ACTIVE_MAX_BYTES:
                oldest = target.with_name(f"{target.name}.{SUPERVISOR_EVENT_BACKUP_COUNT}")
                oldest.unlink(missing_ok=True)
                for index in range(SUPERVISOR_EVENT_BACKUP_COUNT - 1, 0, -1):
                    source = target.with_name(f"{target.name}.{index}")
                    destination = target.with_name(f"{target.name}.{index + 1}")
                    if source.exists():
                        os.replace(source, destination)
                os.replace(target, target.with_name(f"{target.name}.1"))
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
        request = getattr(self._retention_worker, "request", None)
        if callable(request):
            request()

    def serve_in_thread(self, *, port: int = 0) -> SupervisorHttpServer:
        handler = self._build_handler()
        server = LoopbackThreadingHTTPServer(("127.0.0.1", int(port)), handler)
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

    from hextech.infrastructure.vision.sidecar import load_or_build_default_template_runtime
    from hextech.infrastructure.vision.template_runtime import vision_pool_fingerprint
    from hextech.infrastructure.vision.data_source import prepare_catalog_vision_data, recognition_switch_blocked

    overlay_runtime = OverlayRuntimeManager(
        load_template_runtime_func=load_or_build_default_template_runtime,
        vision_pool_fingerprint_func=vision_pool_fingerprint,
        prepare_data_func=prepare_catalog_vision_data,
        game_active_probe=recognition_switch_blocked,
    )
    from hextech.modules.session.runtime_role_owner import publish_role_owner, remove_role_owner

    supervisor = RuntimeSupervisor(parent_pid=args.parent_pid, overlay_runtime=overlay_runtime)
    server = supervisor.serve_in_thread(port=args.port)
    publish_role_owner("runtime-supervisor")
    try:
        if args.prewarm_templates:
            supervisor.start_overlay_template_prewarm()
        from hextech.modules.session.process_bootstrap import publish_process_bootstrap

        publish_process_bootstrap(
            {
                "supervisor_instance_id": supervisor.supervisor_instance_id,
                "port": server.port,
                "session_nonce": supervisor.session_nonce,
                "pid": os.getpid(),
            }
        )
        while not supervisor.wait_for_shutdown(0.25):
            supervisor.tick()
    except KeyboardInterrupt:
        return 0
    finally:
        # 所有退出路径都先登记同一个幂等 shutdown，确保 Ctrl+C 也会回收受管进程。
        supervisor.request_shutdown("finally")
        supervisor.wait_for_overlay_shutdown(OVERLAY_SHUTDOWN_WAIT_SECONDS)
        server.shutdown()
        remove_role_owner("runtime-supervisor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

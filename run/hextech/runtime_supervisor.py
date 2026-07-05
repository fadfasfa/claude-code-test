from __future__ import annotations

"""Hextech Runtime Supervisor。

文件职责：
- 作为桌面 UI 之外的执行面，承载受管组件状态、lease、控制 API 与结构化事件日志。
- 首版只实现可独立验证的控制面骨架和 refresh action；具体 Web/overlay 组件逐步接入。

维护边界：
- nonce 只在内存和父子匿名管道中传递，不写入日志或状态文件。
- 控制 API 面向本机进程调用，使用 loopback 绑定、Host header 校验和 nonce header 鉴权。
"""

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
from typing import Any, Callable
from urllib.parse import urlparse

import psutil

from hextech.catalog.runtime_store import build_runtime_state_path, ensure_private_runtime_dir
from hextech.core.refresh import refresh_backend_data, sanitize_event_message

SUPERVISOR_NONCE_HEADER = "X-Hextech-Supervisor-Nonce"
SUPERVISOR_EVENT_SCHEMA_VERSION = 1
SAFE_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DEFAULT_REFRESH_INTERVAL_SECONDS = 4 * 60 * 60


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
        event_log_path: str | Path | None = None,
        lease_timeout_seconds: float = 6.0,
        orphan_grace_seconds: float = 15.0,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.supervisor_instance_id = f"sup-{uuid.uuid4().hex}"
        self.parent_pid = int(parent_pid or 0)
        self.session_nonce = session_nonce or secrets.token_urlsafe(24)
        self._refresh_func = refresh_func or refresh_backend_data
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
        self._active_refresh_action_id = ""
        self._last_refresh_at = 0.0
        self._next_refresh_at = 0.0

    def parent_alive(self) -> bool:
        return bool(self.parent_pid and psutil.pid_exists(self.parent_pid))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lease_age = time.time() - float(self._lease.get("last_renewed_at") or 0.0)
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
                "components": dict(self._components),
                "actions": dict(self._actions),
            }

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

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            action = self._actions.get(action_id)
            return dict(action) if action else None

    def request_shutdown(self, reason: str = "requested") -> None:
        """请求 supervisor 主循环退出；HTTP handler 只置位，不直接杀进程。"""

        with self._lock:
            self._shutdown_reason = str(reason or "requested")
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
    args = parser.parse_args(argv)

    supervisor = RuntimeSupervisor(parent_pid=args.parent_pid)
    server = supervisor.serve_in_thread(port=args.port)
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

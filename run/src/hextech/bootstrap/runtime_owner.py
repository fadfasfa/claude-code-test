"""App 内唯一生命周期 owner；只管理 Overlay/Vision 两个常驻子进程。"""
from __future__ import annotations
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from hextech.interfaces.overlay.runtime_manager import OverlayRuntimeManager
from hextech.modules.session.build_identity import current_build_id
from hextech.modules.data.catalog.runtime_store import build_runtime_state_path, ensure_private_runtime_dir
from hextech.infrastructure.observability.sanitization import sanitize_event_message
from hextech.modules.session.runtime_channel import install_app_transport, publish_runtime, active_owner, register_owner, publication_guard
RUNTIME_EVENT_SCHEMA_VERSION = 1
RUNTIME_EVENT_ACTIVE_MAX_BYTES = 1024*1024
RUNTIME_EVENT_BACKUP_COUNT = 3

def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compose_overlay_runtime() -> OverlayRuntimeManager:
    """Source and frozen App share the same complete Vision composition."""
    from hextech.infrastructure.vision.data_source import prepare_catalog_vision_data, recognition_switch_blocked
    from hextech.infrastructure.vision.template_runtime import load_or_build_default_template_runtime, vision_pool_fingerprint

    return OverlayRuntimeManager(
        load_template_runtime_func=load_or_build_default_template_runtime,
        vision_pool_fingerprint_func=vision_pool_fingerprint,
        prepare_data_func=prepare_catalog_vision_data,
        game_active_probe=recognition_switch_blocked,
    )


class AppRuntimeOwner:
    def __init__(self, *, overlay_runtime=None, event_log_path=None, retention_worker=None, monitor=True):
        previous = active_owner()
        if previous is not None and previous.is_running():
            raise RuntimeError("App runtime owner already exists")
        self.instance_id = "app-"+uuid.uuid4().hex
        self._overlay_runtime = overlay_runtime if overlay_runtime is not None else _compose_overlay_runtime()
        self._event_log_path = Path(event_log_path) if event_log_path else Path(build_runtime_state_path("runtime_events.v1.jsonl"))
        if retention_worker is None:
            from hextech.infrastructure.persistence.diagnostic_retention import get_diagnostic_retention_worker
            retention_worker = get_diagnostic_retention_worker()
        self._retention_worker = retention_worker
        self._event_write_lock = threading.Lock()
        self._lock = threading.RLock()
        self._actions = {}
        self._shutdown_requested = threading.Event()
        self._closed = False
        self._shutdown_reason = ""
        self._overlay_shutdown_called = False
        self._shutdown_error = ""
        self._overlay_shutdown_thread = None
        self._active_overlay_action_id = ""
        self._pipes = {}
        self._children = {}
        register_owner(self)
        install_app_transport(self._forward)
        self._latest_status = self.snapshot()
        self._monitor = None
        if monitor:
            self._monitor = threading.Thread(target=self._monitor_loop,name="app-runtime-owner",daemon=True)
            self._monitor.start()

    def is_running(self):
        return not self._closed

    def _monitor_loop(self):
        while not self._shutdown_requested.wait(.5):
            try:
                self.tick()
                latest = self.snapshot()
                with self._lock:
                    self._latest_status = latest
            except Exception:
                logging.getLogger(__name__).exception("App runtime monitor failed")

    def start_overlay_template_prewarm(self):
        self._overlay_runtime.start_template_prewarm()

    def snapshot(self):
        return {"runtime_instance_id":self.instance_id,"components":{"game_overlay":self._overlay_runtime.snapshot()},
                "actions":dict(self._actions)}

    def get_status(self, *, timeout=None):
        # GUI status polling consumes memory only; monitoring owns all file reads.
        with self._lock:
            return deepcopy(self._latest_status)

    def set_game_overlay_enabled(self, enabled, *, timeout=None):
        if self._shutdown_requested.is_set():
            raise RuntimeError("runtime_shutting_down")
        return self.run_game_overlay_action({"enabled":bool(enabled)})

    def attach_pipe(self, role, pipe):
        with self._lock:
            old = self._pipes.get(role)
            self._pipes[role] = pipe
        if old is not None:
            old.close()

    def retire_pipe(self, role, pipe):
        with self._lock:
            if self._pipes.get(role) is pipe:
                self._pipes.pop(role, None)

    def register_child(self, role, process):
        with self._lock:
            if self._shutdown_requested.is_set():
                raise RuntimeError("runtime_shutting_down")
            previous = self._children.get(role)
            if previous is not None and previous is not process and previous.poll() is None:
                raise RuntimeError("previous_runtime_child_still_running")
            self._children[role] = process

    def receive(self, role, pipe, kind, payload):
        with publication_guard(), self._lock:
            if self._pipes.get(role) is not pipe or getattr(pipe, "closed", False) or self._shutdown_requested.is_set():
                return
            if kind == "event" and role == "vision":
                if payload.get("build_id") != current_build_id():
                    return
                publish_runtime("event", payload)
            elif kind == "ready":
                if not getattr(pipe, "ready_verified", False):
                    return
                from hextech.modules.session.runtime_channel import runtime_payload
                context = runtime_payload("context")
                if context:
                    pipe.publish("context", context)
                if role == "host":
                    event = runtime_payload("event")
                    if event:
                        pipe.publish("event", event)
            elif kind == "retention":
                self._retention_worker.request()

    def _forward(self, kind, payload):
        with self._lock:
            pipes = list(self._pipes.items())
        for role, pipe in pipes:
            if kind == "context" or kind == "event" and role == "host":
                pipe.publish(kind, payload)

    def stop(self, *, timeout=8.0):
        self.request_shutdown("app_exit")
        if not self.wait_for_overlay_shutdown(timeout):
            raise TimeoutError("runtime_children_shutdown_incomplete")
        from hextech.interfaces.overlay.lifecycle import stop_process
        with self._lock:
            children = list(self._children.values())
        for process in children:
            if not stop_process(process):
                self._shutdown_error = "runtime_child_cleanup_failed"
        if self._shutdown_error:
            raise RuntimeError("runtime_children_shutdown_failed: " + self._shutdown_error)
        with self._lock:
            self._children.clear()
            for pipe in list(self._pipes.values()):
                pipe.close()
            self._pipes.clear()
        install_app_transport(None)
        self._closed = True

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
            status = "failed" if result_payload.get("status") == "error" else "completed"
            self.append_event(
                {
                    "event": f"game_overlay.{status}",
                    "level": "ERROR" if status == "failed" else "INFO",
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
        if status == "completed" and old_sidecar_pid and result_payload.get("status") == "running":
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
            completed_ids = [key for key, value in self._actions.items() if value.get("status") != "running"]
            for key in completed_ids[:-32]:
                self._actions.pop(key, None)
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
            name="hextech-runtime-game-overlay",
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
            if not self._overlay_shutdown_called or (self._shutdown_error and
                    self._overlay_shutdown_thread is not None and not self._overlay_shutdown_thread.is_alive()):
                self._shutdown_error = ""
                self._overlay_shutdown_called = True
                thread = threading.Thread(
                    target=self._shutdown_overlay_runtime,
                    args=(shutdown_reason,),
                    name="hextech-runtime-overlay-shutdown",
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
            self._shutdown_error = str(exc)
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
        event.setdefault("schema_version", RUNTIME_EVENT_SCHEMA_VERSION)
        event.setdefault("timestamp", _utc_now_iso())
        event.setdefault("level", "INFO")
        event.setdefault("runtime_instance_id", self.instance_id)
        event.setdefault("component", "app")
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
            if current_size > 0 and current_size + encoded_size > RUNTIME_EVENT_ACTIVE_MAX_BYTES:
                oldest = target.with_name(f"{target.name}.{RUNTIME_EVENT_BACKUP_COUNT}")
                oldest.unlink(missing_ok=True)
                for index in range(RUNTIME_EVENT_BACKUP_COUNT - 1, 0, -1):
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

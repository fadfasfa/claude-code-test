"""App 内数据任务生命周期；不启动服务进程，不管理Overlay。"""
from __future__ import annotations
from dataclasses import dataclass
import threading
from typing import Any

@dataclass
class ManagedService:
    name: str
    process: Any = None
    status: str = "stopped"
    last_error: str = ""
    def is_running(self):
        return self.process is not None and self.process.poll() is None
    def snapshot(self):
        return {"status": self.status, "last_error":self.last_error}

class ServiceManager:
    def __init__(self, *, start_data_service_func, listener_snapshot_func=None):
        self._start_data_service_func = start_data_service_func
        self._listener_snapshot_func = listener_snapshot_func
        self.data_service = ManagedService("data_tasks")
        self._lock = threading.RLock()
        self._shutdown_requested = False
        self._session_end_requested = threading.Event()
        self._listener_enabled = True

    def start_data_service(self, *, timeout=None):
        with self._lock:
            if self._shutdown_requested or self._session_end_requested.is_set():
                raise RuntimeError("App is shutting down")
            current = self.data_service.process
            if current is not None and getattr(current, "_stop", None) is not None and current._stop.is_set() and current.poll() is None:
                raise RuntimeError("previous_data_tasks_are_still_stopping")
            if not self.data_service.is_running():
                self.data_service.process = self._start_data_service_func()
                self.data_service.status = "running"
            return self.data_service.process

    def stop_data_service(self):
        with self._lock:
            handle = self.data_service.process
            if handle is not None:
                handle.stop()
                self.data_service.process = None
            self.data_service.status = "stopped"

    def set_shutdown_requested(self, requested):
        if requested:
            self._session_end_requested.set()
        else:
            self._session_end_requested.clear()

    def set_low_frequency_listener_enabled(self, enabled):
        self._listener_enabled = bool(enabled)

    def get_status_snapshot(self):
        observation = self._listener_snapshot_func() if self._listener_enabled and self._listener_snapshot_func is not None else {}
        return {"data_service": self.data_service.snapshot(),
                "low_frequency_listener": {"enabled": self._listener_enabled, "observation": observation}}

    def suspend_for_standby(self, **kwargs):
        self.stop_data_service()

    def shutdown(self, *, timeout_seconds=.5, final_timeout_seconds=4.5):
        self._shutdown_requested = True
        self.stop_data_service()

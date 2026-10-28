"""App 与单个受管子进程的有界本地管道；热事件不落盘。"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from multiprocessing.connection import Client, Listener
from typing import Any, Callable, Mapping

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_MESSAGE_KINDS = 8
ADDRESS_ENV = "HEXTECH_RUNTIME_PIPE"
KEY_ENV = "HEXTECH_RUNTIME_PIPE_KEY"
ROLE_ENV = "HEXTECH_RUNTIME_PIPE_ROLE"


class RuntimeBootstrapError(RuntimeError):
    def __init__(self,message,*,error_type):
        super().__init__(message)
        self.error_type=error_type
        markers=("template_missing", "模板缺失", "schema", "配置", "token 不匹配")
        self.retryable=(error_type not in {"PermissionError","FileNotFoundError","OSError","ValueError","TypeError",
                                          "MemoryError","JSONDecodeError","UnicodeDecodeError"}
                        and not any(marker in str(message).casefold() for marker in markers))


class RuntimeChannel:
    """一个读线程和一个写线程；每类消息最多保留最新一份。"""

    def __init__(self, connection: Any, *, on_message: Callable[[str, dict], None] | None = None, on_close=None):
        self.connection = connection
        self.on_message = on_message
        self.on_close = on_close
        self._condition = threading.Condition()
        self._pending: OrderedDict[str, bytes] = OrderedDict()
        self._latest: dict[str, dict] = {}
        self._closed = False
        self._sending = False
        self.error = ""
        self._threads = [threading.Thread(target=self._read, name="runtime-pipe-reader", daemon=True),
                         threading.Thread(target=self._write, name="runtime-pipe-writer", daemon=True)]
        for thread in self._threads:
            thread.start()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, kind: str, payload: Mapping[str, Any]) -> bool:
        if kind not in {"event", "context", "ready", "status", "shutdown", "retention", "identity"}:
            raise ValueError("runtime_channel_message_kind")
        encoded = json.dumps({"kind": kind, "payload": dict(payload)}, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError("runtime_channel_message_too_large")
        with self._condition:
            if self._closed:
                return False
            if kind not in self._pending and len(self._pending) >= MAX_MESSAGE_KINDS:
                return False
            self._pending[kind] = encoded
            self._condition.notify_all()
            return True

    def latest(self, kind: str) -> dict:
        with self._condition:
            return deepcopy(self._latest.get(kind, {}))

    def wait(self, kind: str, timeout: float) -> dict:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while not self._closed and kind not in self._latest:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            return deepcopy(self._latest.get(kind, {}))

    def _read(self) -> None:
        try:
            while True:
                message = json.loads(self.connection.recv_bytes(MAX_MESSAGE_BYTES).decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("runtime_channel_invalid_message")
                kind, payload = message.get("kind"), message.get("payload")
                if kind not in {"event", "context", "ready", "status", "shutdown", "retention", "identity"} or not isinstance(payload, dict):
                    raise ValueError("runtime_channel_invalid_message")
                with self._condition:
                    self._latest[kind] = payload
                    self._condition.notify_all()
                if self.on_message is not None:
                    self.on_message(kind, deepcopy(payload))
        except (EOFError, OSError, ValueError, TypeError) as exc:
            self.error = type(exc).__name__
        finally:
            self.close()

    def _write(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait()
                    if self._closed:
                        return
                    _, encoded = self._pending.popitem(last=False)
                    self._sending = True
                self.connection.send_bytes(encoded)
                with self._condition:
                    self._sending = False
                    self._condition.notify_all()
        except (EOFError, OSError, ValueError) as exc:
            self.error = type(exc).__name__
            self.close()

    def flush(self, timeout=.5):
        deadline=time.monotonic()+timeout
        with self._condition:
            while not self._closed and (self._pending or self._sending):
                remaining=deadline-time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return not self._closed

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()
        self.connection.close()
        if self.on_close is not None:
            self.on_close()


class ChildPipe:
    """App 唯一持有 listener；认证材料只经子进程环境传递，不写文件。"""

    def __init__(self, role: str, *, on_message: Callable[[str, dict], None] | None = None, on_close=None):
        if os.name != "nt":
            raise RuntimeError("production_runtime_requires_windows_named_pipe")
        self.address = rf"\\.\pipe\hextech-{uuid.uuid4().hex}"
        self.key = secrets.token_bytes(32)
        self.role = role
        self.on_message = on_message
        self.ready_verified = False
        self.on_close = on_close
        self._listener = Listener(self.address, family="AF_PIPE", authkey=self.key)
        self.channel: RuntimeChannel | None = None
        self._accepted = threading.Event()
        self._closed = False
        def accept() -> None:
            try:
                connection = self._listener.accept()
                if self._closed:
                    connection.close()
                else:
                    self.channel = RuntimeChannel(connection, on_message=self._dispatch, on_close=self.close)
            except (OSError, EOFError):
                pass
            finally:
                self._accepted.set()
        threading.Thread(target=accept, name=f"{role}-pipe-accept", daemon=True).start()

    def _dispatch(self, kind, payload):
        # ready is replayed only after the parent verifies Build and actual child PID.
        if kind != "ready" and self.on_message is not None:
            self.on_message(kind, payload)

    @property
    def closed(self):
        return self._closed or bool(self.channel and self.channel.closed)

    def environment(self) -> dict[str, str]:
        return {ADDRESS_ENV: self.address, KEY_ENV: self.key.hex(), ROLE_ENV: self.role}

    def wait_ready(self, process: Any, timeout: float, *, cancel_event: threading.Event | None = None) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("runtime_start_cancelled")
            channel = self.channel
            if channel is not None:
                status = channel.latest("status")
                if status.get("state") == "failed":
                    raise RuntimeBootstrapError(str(status.get("error_message_sanitized") or "runtime_bootstrap_failed"),
                                                error_type=str(status.get("error_type") or "RuntimeError"))
                ready = channel.wait("ready", min(.05, max(0, deadline-time.monotonic())))
                if ready:
                    from hextech.modules.session.build_identity import current_build_id
                    pid = int(ready.get("pid") or 0)
                    verified_pid = pid == process.pid
                    if not verified_pid and pid > 0:
                        import psutil
                        try:
                            child = psutil.Process(pid)
                            verified_pid = any(parent.pid == process.pid for parent in child.parents())
                        except psutil.Error:
                            verified_pid = False
                    if ready.get("build_id") != current_build_id() or not verified_pid:
                        raise ValueError("runtime_pipe_ready_identity_mismatch")
                    process._hextech_overlay_runtime_pid = pid
                    self.ready_verified = True
                    if self.on_message is not None:
                        self.on_message("ready", ready)
                    return ready
                if channel.closed:
                    status = channel.latest("status")
                    if status.get("state") == "failed":
                        raise RuntimeBootstrapError(str(status.get("error_message_sanitized") or "runtime_bootstrap_failed"),
                                                    error_type=str(status.get("error_type") or "RuntimeError"))
                    raise RuntimeError("runtime_pipe_closed_before_ready")
            else:
                self._accepted.wait(.05)
            if process.poll() is not None:
                status = channel.latest("status") if channel is not None else {}
                if status.get("state") == "failed":
                    raise RuntimeBootstrapError(str(status.get("error_message_sanitized") or "runtime_bootstrap_failed"),
                                                error_type=str(status.get("error_type") or "RuntimeError"))
                raise RuntimeError(f"{self.role}_exited_before_ready")
        raise TimeoutError(f"{self.role}_ready_timeout")

    def publish(self, kind: str, payload: Mapping[str, Any]) -> bool:
        return bool(self.channel and self.channel.publish(kind, payload))

    def latest(self, kind: str) -> dict:
        return self.channel.latest(kind) if self.channel else {}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.on_close is not None:
            self.on_close()
        if self.channel:
            self.channel.close()
        self._listener.close()


_child_channel: RuntimeChannel | None = None
_child_lock = threading.Lock()
_app_event: dict = {}
_app_context: dict = {}
_app_publish: Callable[[str, dict], None] | None = None
_publication_lock = threading.RLock()

def publication_guard():
    return _publication_lock


def child_channel() -> RuntimeChannel | None:
    global _child_channel
    with _child_lock:
        if _child_channel is None and os.environ.get(ADDRESS_ENV):
            connection = Client(os.environ[ADDRESS_ENV], family="AF_PIPE", authkey=bytes.fromhex(os.environ[KEY_ENV]))
            _child_channel = RuntimeChannel(connection)
        return _child_channel


def install_app_transport(publish: Callable[[str, dict], None] | None) -> None:
    global _app_publish, _app_event, _app_context
    with _publication_lock:
        _app_publish = publish
        _app_event = {}
        _app_context = {}


def publish_runtime(kind: str, payload: Mapping[str, Any]) -> bool:
    global _app_event, _app_context
    with _publication_lock:
        channel = child_channel()
        if channel is not None:
            return channel.publish(kind, payload)
        publisher = _app_publish
        if publisher is not None:
            if kind == "event":
                _app_event = deepcopy(dict(payload))
            elif kind == "context":
                _app_context = deepcopy(dict(payload))
            publisher(kind, dict(payload))
            return True
        return False


def runtime_payload(kind: str) -> dict | None:
    with _publication_lock:
        channel = child_channel()
        if channel is not None:
            return channel.latest(kind)
        if _app_publish is not None:
            return deepcopy(_app_event if kind == "event" else _app_context)
        return None


def signal_ready(**fields: Any) -> bool:
    from hextech.modules.session.build_identity import current_build_id
    result=publish_runtime("ready", {"pid": os.getpid(), "build_id": current_build_id(), **fields})
    channel=child_channel()
    if result and channel is not None:
        channel.flush(.5)
    return result


def shutdown_requested() -> bool:
    channel = child_channel()
    return bool(channel is not None and (channel.closed or channel.latest("shutdown")))


_owner = None

def active_owner():
    return _owner

def register_owner(owner):
    global _owner
    _owner = owner

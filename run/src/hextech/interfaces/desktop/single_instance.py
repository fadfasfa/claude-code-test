"""桌面控制面的单实例 owner/lock。

同一 worktree 只能有一个 Tk 控制面写共享 runtime state。活 owner 存在时，
第二实例写入一次带 owner 身份的激活请求后正常退出；stale owner 才允许接管。

调用方: display.desktop.app、tests.test_desktop_single_instance; 关键依赖: psutil、scraping._paths、support.atomic_io。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import psutil

from hextech.modules.session.build_identity import get_build_identity
from hextech.modules.data.ports.paths import RUNTIME_DATA_DIR
from hextech.modules.data.ports.atomic import atomic_write_json


def _runtime_subdir_path(subdir: str, filename: str) -> Path:
    name = str(filename or "").strip()
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"invalid runtime filename: {filename!r}")
    return Path(RUNTIME_DATA_DIR) / subdir / name


DEFAULT_LOCK_FILE = _runtime_subdir_path("locks", "desktop_ui.lock")
DEFAULT_OWNER_FILE = _runtime_subdir_path("state", "desktop_ui_owner.v1.json")
DEFAULT_ACTIVATION_FILE = _runtime_subdir_path("state", "desktop_ui_activation.v1.json")
DEFAULT_CONFLICT_FILE = _runtime_subdir_path("state", "desktop_ui_build_conflict.v1.json")


class DesktopInstanceAlreadyRunning(RuntimeError):
    """同一 worktree 已有桌面控制面运行。"""

    def __init__(self, owner: Mapping[str, Any], *, activation_sent: bool = False):
        self.owner = dict(owner)
        self.activation_sent = bool(activation_sent)
        pid = self.owner.get("pid") or "unknown"
        cwd = self.owner.get("cwd") or "unknown"
        super().__init__(f"Hextech 桌面控制面已在运行：pid={pid} cwd={cwd}")


class DesktopBuildConflict(RuntimeError):
    """共享 runtime 已由不同或无法确认身份的 Build 持有。"""

    def __init__(self, owner: Mapping[str, Any], requester: Mapping[str, Any], *, conflict_path: Path):
        self.owner = dict(owner)
        self.requester = dict(requester)
        self.conflict_path = Path(conflict_path)
        owner_build = str(self.owner.get("build_id") or "unknown")
        requester_build = str(self.requester.get("build_id") or "unknown")
        owner_executable = str(self.owner.get("executable") or "unknown")
        super().__init__(
            "检测到不同 Hextech Build 正在使用共享运行态："
            f"active={owner_build} requested={requester_build} executable={owner_executable}"
        )


@dataclass
class DesktopInstanceOwner:
    lock_path: Path = DEFAULT_LOCK_FILE
    owner_path: Path = DEFAULT_OWNER_FILE
    activation_path: Path | None = None
    conflict_path: Path | None = None
    build_identity: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        self.lock_path = Path(self.lock_path)
        self.owner_path = Path(self.owner_path)
        self.activation_path = Path(self.activation_path or self.owner_path.with_name(DEFAULT_ACTIVATION_FILE.name))
        self.conflict_path = Path(self.conflict_path or self.owner_path.with_name(DEFAULT_CONFLICT_FILE.name))
        identity = dict(self.build_identity or get_build_identity())
        self.build_id = str(identity.get("build_id") or "").strip()
        self.source_fingerprint = str(identity.get("source_fingerprint") or "").strip()
        self.launch_executable = _normalized_executable(sys.executable)
        try:
            current_process = psutil.Process(os.getpid())
            self.process_started_at = float(current_process.create_time())
            self.executable = _normalized_executable(current_process.exe())
        except (psutil.Error, OSError, ValueError):
            self.process_started_at = time.time()
            self.executable = self.launch_executable
        self.owner_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._fd: int | None = None

    def __enter__(self) -> "DesktopInstanceOwner":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()

    def acquire(self) -> None:
        """创建独占锁；如果旧 owner 已退出，清理后接管。"""

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, self.owner_id.encode("utf-8"))
                try:
                    self._write_owner()
                except Exception:
                    self._close_fd()
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                return
            except FileExistsError:
                owner = self._read_owner()
                if _owner_is_alive(owner):
                    if self._same_build(owner):
                        raise DesktopInstanceAlreadyRunning(owner, activation_sent=self._request_activation(owner))
                    requester = self._requester_identity()
                    self._write_conflict(owner, requester, reason="active_build_mismatch")
                    raise DesktopBuildConflict(owner, requester, conflict_path=Path(self.conflict_path))
                self._remove_stale_files()
        owner = self._read_owner()
        raise DesktopInstanceAlreadyRunning(owner)

    def release(self) -> None:
        """只释放自己持有的 owner；避免误删新实例接管后的状态。"""

        owner = self._read_owner()
        self._close_fd()
        if owner.get("owner_id") != self.owner_id:
            return
        for path in (self.owner_path, self.lock_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def consume_activation_request(self, last_request_id: str = "") -> dict[str, Any] | None:
        """消费发给当前 owner 的最新激活请求；不接受旧实例或其他 worktree 请求。"""

        try:
            payload = json.loads(Path(self.activation_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        request_id = str(payload.get("request_id") or "")
        if (
            not request_id
            or request_id == str(last_request_id or "")
            or str(payload.get("target_owner_id") or "") != self.owner_id
        ):
            return None
        try:
            requested_at = float(payload.get("requested_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if requested_at <= 0.0 or time.time() - requested_at > 30.0:
            return None
        requester_build = str(payload.get("requester_build_id") or "").strip()
        requester_fingerprint = str(payload.get("requester_source_fingerprint") or "").strip()
        same_build = bool(requester_build and requester_build == self.build_id)
        if self.source_fingerprint or requester_fingerprint:
            same_build = same_build and requester_fingerprint == self.source_fingerprint
        normalized = dict(payload)
        if not same_build:
            normalized["request_kind"] = "build_conflict"
            requester = {
                "build_id": requester_build,
                "source_fingerprint": requester_fingerprint,
                "pid": payload.get("requester_pid"),
                "executable": str(payload.get("requester_executable") or ""),
                "process_started_at": payload.get("requester_process_started_at"),
            }
            self._write_conflict(
                self._read_owner(),
                requester,
                reason="legacy_activation" if not requester_build else "activation_build_mismatch",
            )
        else:
            normalized["request_kind"] = "activation"
        return normalized

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def _write_owner(self) -> None:
        atomic_write_json(
            self.owner_path,
            {
                "schema_version": 1,
                "owner_schema_version": 2,
                "owner_id": self.owner_id,
                "pid": os.getpid(),
                "process_started_at": self.process_started_at,
                "executable": self.executable,
                "launch_executable": self.launch_executable,
                "cwd": os.getcwd(),
                "started_at": time.time(),
                "build_id": self.build_id,
                "source_fingerprint": self.source_fingerprint,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _request_activation(self, owner: Mapping[str, Any]) -> bool:
        owner_id = str(owner.get("owner_id") or "").strip()
        if not owner_id:
            return False
        try:
            activation_path = Path(self.activation_path)
            activation_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                activation_path,
                {
                    "schema_version": 1,
                    "request_id": uuid.uuid4().hex,
                    "target_owner_id": owner_id,
                    "requester_pid": os.getpid(),
                    "requester_process_started_at": self.process_started_at,
                    "requester_executable": self.executable,
                    "requester_build_id": self.build_id,
                    "requester_source_fingerprint": self.source_fingerprint,
                    "requested_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            )
        except OSError:
            return False
        return True

    def _same_build(self, owner: Mapping[str, Any]) -> bool:
        if int(owner.get("owner_schema_version") or 0) != 2:
            return False
        owner_build = str(owner.get("build_id") or "").strip()
        owner_fingerprint = str(owner.get("source_fingerprint") or "").strip()
        if not owner_build or not self.build_id or owner_build != self.build_id:
            return False
        if owner_fingerprint or self.source_fingerprint:
            return owner_fingerprint == self.source_fingerprint
        # 源码态 build_id=dev 时继续用解释器和 cwd 区分工作区。
        return (
            _normalized_executable(owner.get("executable")) == self.executable
            and os.path.normcase(str(owner.get("cwd") or "")) == os.path.normcase(os.getcwd())
        )

    def _requester_identity(self) -> dict[str, Any]:
        return {
            "build_id": self.build_id,
            "source_fingerprint": self.source_fingerprint,
            "pid": os.getpid(),
            "process_started_at": self.process_started_at,
            "executable": self.executable,
        }

    def _write_conflict(
        self,
        owner: Mapping[str, Any],
        requester: Mapping[str, Any],
        *,
        reason: str,
    ) -> None:
        try:
            target = Path(self.conflict_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                target,
                {
                    "schema_version": 1,
                    "recorded_at": time.time(),
                    "reason": str(reason or "build_conflict"),
                    "active": {
                        "owner_id": str(owner.get("owner_id") or ""),
                        "pid": owner.get("pid"),
                        "process_started_at": owner.get("process_started_at"),
                        "executable": str(owner.get("executable") or ""),
                        "build_id": str(owner.get("build_id") or ""),
                        "source_fingerprint": str(owner.get("source_fingerprint") or ""),
                    },
                    "requester": {
                        "pid": requester.get("pid"),
                        "process_started_at": requester.get("process_started_at"),
                        "executable": str(requester.get("executable") or ""),
                        "build_id": str(requester.get("build_id") or ""),
                        "source_fingerprint": str(requester.get("source_fingerprint") or ""),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        except OSError:
            return

    def _read_owner(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.owner_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _remove_stale_files(self) -> None:
        for path in (self.lock_path, self.owner_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _owner_is_alive(owner: Mapping[str, Any]) -> bool:
    try:
        pid = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        try:
            expected_started_at = float(owner.get("process_started_at") or 0.0)
        except (TypeError, ValueError):
            expected_started_at = 0.0
        return int(owner.get("owner_schema_version") or 0) != 2 or (
            expected_started_at > 0.0 and abs(float(psutil.Process(pid).create_time()) - expected_started_at) <= 1.0
        )
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        if int(owner.get("owner_schema_version") or 0) != 2:
            return True
        expected_started_at = float(owner.get("process_started_at") or 0.0)
        if expected_started_at <= 0.0 or abs(float(process.create_time()) - expected_started_at) > 1.0:
            return False
        expected_executable = _normalized_executable(owner.get("executable"))
        actual_executable = _normalized_executable(process.exe())
        return bool(expected_executable and expected_executable == actual_executable)
    except (psutil.Error, OSError, TypeError, ValueError):
        return False


def _normalized_executable(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        resolved = Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = Path(text)
    return os.path.normcase(os.fspath(resolved))


def show_build_conflict_message(owner: Mapping[str, Any], requester: Mapping[str, Any] | None = None) -> None:
    """GUI 进程中显示有限冲突信息；失败时由 conflict state 保留证据。"""

    active_build = str(owner.get("build_id") or "unknown")
    requested_build = str((requester or {}).get("build_id") or "unknown")
    executable = str(owner.get("executable") or "unknown")
    message = (
        "检测到另一个 Hextech Build 正在运行。\n\n"
        f"当前 Build：{active_build}\n"
        f"请求 Build：{requested_build}\n"
        f"当前程序：{executable}\n\n"
        "请先从当前 Hextech 托盘完整退出，再启动另一个候选。程序不会自动终止任何进程。"
    )
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Hextech Build 冲突", 0x30 | 0x00040000)
    except (AttributeError, OSError):
        return


__all__ = [
    "DesktopBuildConflict",
    "DesktopInstanceAlreadyRunning",
    "DesktopInstanceOwner",
    "show_build_conflict_message",
]

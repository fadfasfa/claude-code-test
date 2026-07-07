"""桌面控制面的单实例 owner/lock。

同一 worktree 只能有一个 Tk 控制面写共享 runtime state。这里不负责杀旧进程；
活 owner 存在时直接拒绝第二实例，stale owner 才允许接管。

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

from hextech.scraping._paths import RUNTIME_DATA_DIR
from hextech.support.atomic_io import atomic_write_json


def _runtime_subdir_path(subdir: str, filename: str) -> Path:
    name = str(filename or "").strip()
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError(f"invalid runtime filename: {filename!r}")
    return Path(RUNTIME_DATA_DIR) / subdir / name


DEFAULT_LOCK_FILE = _runtime_subdir_path("locks", "desktop_ui.lock")
DEFAULT_OWNER_FILE = _runtime_subdir_path("state", "desktop_ui_owner.v1.json")


class DesktopInstanceAlreadyRunning(RuntimeError):
    """同一 worktree 已有桌面控制面运行。"""

    def __init__(self, owner: Mapping[str, Any]):
        self.owner = dict(owner)
        pid = self.owner.get("pid") or "unknown"
        cwd = self.owner.get("cwd") or "unknown"
        super().__init__(f"Hextech 桌面控制面已在运行：pid={pid} cwd={cwd}")


@dataclass
class DesktopInstanceOwner:
    lock_path: Path = DEFAULT_LOCK_FILE
    owner_path: Path = DEFAULT_OWNER_FILE

    def __post_init__(self) -> None:
        self.lock_path = Path(self.lock_path)
        self.owner_path = Path(self.owner_path)
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
                    raise DesktopInstanceAlreadyRunning(owner)
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
                "owner_id": self.owner_id,
                "pid": os.getpid(),
                "executable": sys.executable,
                "cwd": os.getcwd(),
                "started_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        )

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
        return True
    return psutil.pid_exists(pid)

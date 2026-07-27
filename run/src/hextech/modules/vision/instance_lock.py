"""Overlay Host 与 Vision Sidecar 共用的进程独占锁。"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class OverlayInstanceLock:
    """持有一个字节的 OS 文件锁，进程退出时由系统自动释放。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    def acquire(self) -> bool:
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_obj = self.path.open("a+b")
        try:
            file_obj.seek(0, os.SEEK_END)
            if file_obj.tell() == 0:
                file_obj.write(b"0")
                file_obj.flush()
            file_obj.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            file_obj.seek(0)
            file_obj.truncate()
            file_obj.write(json.dumps({"pid": os.getpid()}).encode("ascii"))
            file_obj.flush()
        except (OSError, BlockingIOError):
            file_obj.close()
            return False
        self._file = file_obj
        return True

    def release(self) -> None:
        file_obj = self._file
        self._file = None
        if file_obj is None:
            return
        try:
            file_obj.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
        finally:
            file_obj.close()


@contextmanager
def overlay_instance_lock(path: str | Path) -> Iterator[bool]:
    """返回是否取得锁，并确保 owner 正常退出时主动释放。"""

    lock = OverlayInstanceLock(path)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


__all__ = ["OverlayInstanceLock", "overlay_instance_lock"]

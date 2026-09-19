"""跨进程独占文件锁，供单实例和 promotion 事务复用。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import BinaryIO


class InterProcessFileLock:
    """持有一个字节的 OS 文件锁；进程退出时由系统自动释放。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> bool:
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Lock before touching contents. A buffered pre-lock initialization can
        # race another owner's truncate and then fail again while close flushes.
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        file_obj = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            file_obj.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            file_obj.seek(0)
            metadata = json.dumps({"pid": os.getpid()}).encode("ascii")
            file_obj.write(metadata)
            file_obj.truncate(len(metadata))
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


__all__ = ["InterProcessFileLock"]

"""DataService 单实例文件锁。"""

from __future__ import annotations

import json
import os
from pathlib import Path
class DataServiceInstanceLock:
    """持有进程级文件锁，避免多个桌面实例同时发布 generation。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> bool:
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

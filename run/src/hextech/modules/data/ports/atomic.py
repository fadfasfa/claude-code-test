"""原子写入工具。

运行时代码通过本模块写文本、JSON 和 CSV：先写临时文件，再用 `os.replace`
替换目标文件。Windows 上目标文件短暂被读取占用时会进行小范围重试，避免半写文件。

调用方: core.refresh、core.settings、display.desktop.single_instance; 关键依赖: 见 imports。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import weakref
from typing import Any


_REPLACE_RETRY_DELAYS_SECONDS = (0.02, 0.05, 0.10, 0.20, 0.40)
_TRANSIENT_REPLACE_WINERRORS = {5, 32, 33, 80, 183}
_WRITE_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: weakref.WeakValueDictionary[str, threading.Lock] = weakref.WeakValueDictionary()


def _coerce_path(path: str | os.PathLike[str]) -> str:
    return os.fspath(path)


def _target_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _target_lock(path: str) -> threading.Lock:
    key = _target_key(path)
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _is_transient_replace_error(exc: OSError) -> bool:
    return isinstance(exc, (PermissionError, FileExistsError)) or getattr(exc, "winerror", None) in _TRANSIENT_REPLACE_WINERRORS


def _replace_with_retry(tmp_path: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """Windows 下读写并发偶发占用目标文件时，短重试再暴露真实错误。"""

    for attempt, delay in enumerate((0.0, *_REPLACE_RETRY_DELAYS_SECONDS)):
        if delay:
            time.sleep(delay)
        try:
            os.replace(tmp_path, target)
            return
        except OSError as exc:
            if attempt == len(_REPLACE_RETRY_DELAYS_SECONDS) or not _is_transient_replace_error(exc):
                raise


def _flush_and_fsync(file_obj) -> None:
    file_obj.flush()
    os.fsync(file_obj.fileno())


def atomic_write_text(path: str | os.PathLike[str], content: str, *, encoding: str = "utf-8") -> None:
    target = _coerce_path(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(target)}-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            _flush_and_fsync(f)
        with _target_lock(target):
            _replace_with_retry(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_json(
    path: str | os.PathLike[str],
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
) -> None:
    target = _coerce_path(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(target)}-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=ensure_ascii, indent=indent, separators=separators)
            _flush_and_fsync(f)
        with _target_lock(target):
            _replace_with_retry(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def atomic_write_csv(path: str | os.PathLike[str], dataframe, *, index: bool = False, encoding: str = "utf-8-sig") -> None:
    target = _coerce_path(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(target)}-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            dataframe.to_csv(f, index=index)
            _flush_and_fsync(f)
        with _target_lock(target):
            _replace_with_retry(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

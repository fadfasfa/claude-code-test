"""带 Windows Job Object 所有权和硬超时的隔离进程执行器。

来源 worker 可能继续派生 browser 进程。仅终止直接子进程无法保证退出，因此
Windows 上把 worker 立即加入启用 ``KILL_ON_JOB_CLOSE`` 的 Job Object；无论成功、
失败还是超时，关闭 job 都会回收仍存活的本程序子进程树。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class IsolatedProcessResult:
    returncode: int
    elapsed_seconds: float
    timed_out: bool
    stdout: str
    stderr: str


class _WindowsJobOwner:
    def __init__(self, pid: int) -> None:
        self._job = None
        if os.name != "nt":
            return
        import win32api
        import win32con
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        access = win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE
        process_handle = win32api.OpenProcess(access, False, int(pid))
        try:
            win32job.AssignProcessToJobObject(job, process_handle)
        except Exception:
            win32api.CloseHandle(job)
            raise
        finally:
            win32api.CloseHandle(process_handle)
        self._job = job

    def close(self) -> None:
        job = self._job
        self._job = None
        if job is not None:
            import win32api

            win32api.CloseHandle(job)


def _bounded(text: str | None, *, limit: int = 64 * 1024) -> str:
    value = text or ""
    return value[-limit:]


def run_isolated_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    cancel_file: Path,
    cancel_grace_seconds: float = 2.0,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> IsolatedProcessResult:
    """运行一个来源 worker；超时时先协作取消，再回收完整进程树。"""

    cancel_file.unlink(missing_ok=True)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=os.fspath(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    try:
        owner = _WindowsJobOwner(process.pid)
    except Exception:
        process.kill()
        process.wait(timeout=5)
        raise

    timed_out = False
    stdout = ""
    stderr = ""
    try:
        try:
            stdout, stderr = process.communicate(timeout=max(0.1, float(timeout_seconds)))
        except subprocess.TimeoutExpired:
            timed_out = True
            cancel_file.parent.mkdir(parents=True, exist_ok=True)
            cancel_file.touch()
            try:
                stdout, stderr = process.communicate(timeout=max(0.1, float(cancel_grace_seconds)))
            except subprocess.TimeoutExpired as exc:
                stdout = str(exc.stdout or "")
                stderr = str(exc.stderr or "")
                owner.close()
                if process.poll() is None:
                    process.kill()
                try:
                    tail_out, tail_err = process.communicate(timeout=3)
                    stdout += tail_out or ""
                    stderr += tail_err or ""
                except subprocess.TimeoutExpired:
                    pass
    finally:
        owner.close()
        cancel_file.unlink(missing_ok=True)
    return IsolatedProcessResult(
        returncode=int(process.returncode if process.returncode is not None else -9),
        elapsed_seconds=time.monotonic() - started,
        timed_out=timed_out,
        stdout=_bounded(stdout),
        stderr=_bounded(stderr),
    )


__all__ = ["IsolatedProcessResult", "run_isolated_process"]

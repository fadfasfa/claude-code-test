"""Supervisor 与 lifecycle 测试共用的进程替身。"""

from __future__ import annotations


class FakeProcess:
    """提供 Popen 结果最小契约，并记录停止状态而不创建真实子进程。"""

    def __init__(self, pid: int = 1, *, exit_code: int | None = None) -> None:
        self.pid = pid
        self.returncode = exit_code
        self.stopped = exit_code is not None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.stopped = True
        if self.returncode is None:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.stopped = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.stopped = True
        if self.returncode is None:
            self.returncode = 0

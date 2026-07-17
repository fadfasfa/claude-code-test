"""三个抓取来源共用的超时、重试、并发与 host 熔断策略。"""

from __future__ import annotations

import random
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from hextech.contracts import FailureKind


RETRYABLE_FAILURES = frozenset(
    {FailureKind.TIMEOUT, FailureKind.TLS_ERROR, FailureKind.NETWORK_ERROR, FailureKind.HTTP_5XX}
)
CIRCUIT_FAILURES = frozenset({FailureKind.HTTP_403, FailureKind.HTTP_429})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.35
    max_delay_seconds: float = 4.0
    jitter_ratio: float = 0.2

    def delay_seconds(self, completed_attempts: int, *, rng: random.Random | None = None) -> float:
        if completed_attempts < 1:
            return 0.0
        base = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (completed_attempts - 1)))
        generator = rng or random
        jitter = base * self.jitter_ratio * generator.uniform(-1.0, 1.0)
        return max(0.0, base + jitter)


class HostCircuitOpen(RuntimeError):
    def __init__(self, host: str, failure_kind: FailureKind):
        super().__init__(f"host circuit open: {host} ({failure_kind.value})")
        self.host = host
        self.failure_kind = failure_kind


class HostCircuitBreaker:
    """403/429 后按 host 冷却，避免并发 worker 继续冲击同一站点。"""

    def __init__(self, cooldown_seconds: float = 300.0, *, clock=time.monotonic):
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._blocked_until: dict[str, tuple[float, FailureKind]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def check(self, url: str) -> None:
        host = self.host(url)
        with self._lock:
            blocked_until, failure = self._blocked_until.get(host, (0.0, FailureKind.HTTP_429))
            if blocked_until <= self._clock():
                self._blocked_until.pop(host, None)
                return
        raise HostCircuitOpen(host, failure)

    def record(self, url: str, failure: FailureKind | None) -> None:
        if failure not in CIRCUIT_FAILURES:
            return
        host = self.host(url)
        with self._lock:
            previous_until, _ = self._blocked_until.get(host, (0.0, failure))
            self._blocked_until[host] = (max(previous_until, self._clock() + self.cooldown_seconds), failure)


class ConcurrencyPolicy:
    """同步抓取 worker 使用的全局、host 和 browser 三层并发门禁。"""

    def __init__(self, *, global_limit: int = 8, host_limit: int = 4, browser_limit: int = 1):
        self._global = threading.BoundedSemaphore(global_limit)
        self._browser = threading.BoundedSemaphore(browser_limit)
        self._host_limit = host_limit
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def _host_semaphore(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            return self._hosts.setdefault(host, threading.BoundedSemaphore(self._host_limit))

    @contextmanager
    def acquire(self, url: str, *, browser: bool = False):
        host = HostCircuitBreaker.host(url)
        with ExitStack() as stack:
            stack.enter_context(self._global)
            stack.enter_context(self._host_semaphore(host))
            if browser:
                stack.enter_context(self._browser)
            yield


def is_retryable(failure: FailureKind | None) -> bool:
    return failure in RETRYABLE_FAILURES


__all__ = [
    "CIRCUIT_FAILURES",
    "ConcurrencyPolicy",
    "HostCircuitBreaker",
    "HostCircuitOpen",
    "RETRYABLE_FAILURES",
    "RetryPolicy",
    "is_retryable",
]

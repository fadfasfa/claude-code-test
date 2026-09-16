"""Thread-safe per-response and per-refresh ARAMKit download budget."""

from __future__ import annotations

import threading

from .catalog_binding import AramkitRefreshError


class ByteBudget:
    def __init__(self, *, per_response: int, total: int) -> None:
        self.per_response = per_response
        self.total = total
        self.used = 0
        self._lock = threading.Lock()

    def reserve(self, size: int) -> None:
        if size > self.per_response:
            raise AramkitRefreshError(
                "response_too_large",
                f"ARAMKit 单响应超过 {self.per_response} bytes：{size}",
            )
        with self._lock:
            if self.used + size > self.total:
                raise AramkitRefreshError(
                    "download_budget_exceeded",
                    f"ARAMKit 整轮响应超过 {self.total} bytes",
                )
            self.used += size


__all__ = ["ByteBudget"]

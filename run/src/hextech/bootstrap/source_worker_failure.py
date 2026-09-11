"""隔离来源 worker 的结构化失败。

协调器读取本异常携带的有限 payload 并写入刷新 schedule；本模块不运行抓取、
不处理 fallback，也不负责重试策略。
"""

from __future__ import annotations

import json
from typing import Any, Mapping


class SourceWorkerFailure(RuntimeError):
    """跨越 worker 进程边界后仍保留稳定原因码。"""

    def __init__(self, source: str, payload: Mapping[str, Any]) -> None:
        self.source = source
        self.payload = dict(payload)
        super().__init__(f"{source} worker 失败：{json.dumps(self.payload, ensure_ascii=False)}")


def refresh_failure_kind(payload: Mapping[str, Any], *, blocked: bool) -> str:
    """选择 schedule 的稳定失败原因，避免被通用异常类型覆盖。"""

    if blocked:
        return "http_blocked"
    return str(
        payload.get("failure_kind")
        or payload.get("reason_code")
        or payload.get("error_type")
        or "worker_failed"
    )


__all__ = ["SourceWorkerFailure", "refresh_failure_kind"]

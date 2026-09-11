"""数据来源时间解析与绝对时效判定。

从 refresh_coordinator 拆出的纯函数集合：ISO 时间解析/格式化、pointer 成功
时间读取、阻断类失败识别，以及"data_at 距发布时刻是否超过刷新周期阈值"
的过期判定。不持有状态、不做 IO，refresh_coordinator 是唯一预期调用方。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from hextech.modules.data.freshness import (
    STALE_AGE_FACTOR,
    evaluate_source_expiry,
    parse_refresh_time,
)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def pointer_success_at(pointer: Mapping[str, Any]) -> str:
    return str(pointer.get("last_success_at") or "")


def is_blocked_failure(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("reason_code") or "").strip().lower() == "blocked":
        return True
    text = json.dumps(payload, ensure_ascii=True).lower()
    return any(token in text for token in ("http_403", "http_429", '"status_code": 403', '"status_code": 429'))


__all__ = [
    "STALE_AGE_FACTOR",
    "evaluate_source_expiry",
    "is_blocked_failure",
    "iso_utc",
    "parse_refresh_time",
    "pointer_success_at",
]

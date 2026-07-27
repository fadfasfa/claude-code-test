"""数据来源时间解析与绝对时效判定。

从 refresh_coordinator 拆出的纯函数集合：ISO 时间解析/格式化、pointer 成功
时间读取、阻断类失败识别，以及"data_at 距发布时刻是否超过刷新周期阈值"
的过期判定。不持有状态、不做 IO，refresh_coordinator 是唯一预期调用方。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

# 数据绝对时效阈值 = 刷新周期 × 1.25：容纳 worker 时长（apex 上限 60min）、
# 15 分钟调度粒度和一次 30min 非阻断 backoff 的正常抖动；超过即视为数据过期。
# freshness 仍只表达"本轮候选来源"，过期通过 data_status=data_stale 暴露。
STALE_AGE_FACTOR = 1.25


def parse_refresh_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def pointer_success_at(pointer: Mapping[str, Any]) -> str:
    return str(pointer.get("last_success_at") or "")


def is_blocked_failure(payload: Mapping[str, Any]) -> bool:
    text = json.dumps(payload, ensure_ascii=True).lower()
    return any(token in text for token in ("http_403", "http_429", '"status_code": 403', '"status_code": 429'))


def evaluate_source_expiry(data_at_text: str, interval: timedelta, completed: datetime) -> tuple[bool, int]:
    """判定数据是否过期，返回 (expired, 超龄秒数)。

    data_at 不可解析时保持不过期，不虚构陈旧状态；未 due 被跳过或 saved
    candidate 复用的源同样按真实 data_at 判定，冻结超阈值时如实暴露。
    """

    parsed = parse_refresh_time(data_at_text)
    if parsed is None:
        return (False, 0)
    age = completed - parsed
    if age > interval * STALE_AGE_FACTOR:
        return (True, int(age.total_seconds()))
    return (False, 0)

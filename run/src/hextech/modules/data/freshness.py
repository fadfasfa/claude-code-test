"""DataService 来源的共享刷新周期与实时绝对时效判定。

发布器和只读 snapshot view 都使用这里的纯函数。它不读写文件、不改变
generation lineage，也不负责调度或来源失败分类。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# legacy hextech 与当前 ARAMKit 都是英雄海克斯统计；旧 generation 没有
# ARAMKit source_status 时仍按相同四小时刷新口径投影，而不是永久显示 fresh。
SOURCE_INTERVALS: dict[str, timedelta] = {
    "catalog": timedelta(hours=24),
    "hextech": timedelta(hours=4),
    "aramkit": timedelta(hours=4),
    "blitz": timedelta(hours=2),
    "apex": timedelta(hours=72),
    "mayhem": timedelta(hours=72),
}
STALE_AGE_FACTOR = 1.25


def parse_refresh_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def normalize_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def evaluate_source_expiry(
    data_at_text: object,
    interval: timedelta,
    completed: datetime,
) -> tuple[bool, int]:
    """返回 ``(是否过期, 总年龄秒数)``；不可解析时间不虚构过期。"""

    parsed = parse_refresh_time(data_at_text)
    if parsed is None:
        return (False, 0)
    age = normalize_utc(completed) - parsed.astimezone(timezone.utc)
    if age > interval * STALE_AGE_FACTOR:
        return (True, max(0, int(age.total_seconds())))
    return (False, 0)


def source_reuse_allowed(source: str, data_at_text: object, now: datetime) -> bool:
    """相同 marker 的 verified pointer 是否仍可复用；缺失/非法时间 fail closed。"""

    parsed = parse_refresh_time(data_at_text)
    interval = SOURCE_INTERVALS.get(str(source or ""))
    if parsed is None or interval is None:
        return False
    age = normalize_utc(now) - parsed.astimezone(timezone.utc)
    return age <= interval * STALE_AGE_FACTOR


__all__ = [
    "SOURCE_INTERVALS",
    "STALE_AGE_FACTOR",
    "evaluate_source_expiry",
    "normalize_utc",
    "parse_refresh_time",
    "source_reuse_allowed",
]

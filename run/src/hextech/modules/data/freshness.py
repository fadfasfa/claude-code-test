"""DataService 来源的共享检测周期与兼容时间解析。

检测周期只回答“何时再次检查上游”，不能据此宣称上游已经变化，也不能把
已验证数据按生成时间自动降级。这里的函数不读写文件、不改变 generation
lineage，也不负责调度或来源失败分类。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# legacy hextech 与当前 ARAMKit 都是英雄海克斯统计。周期仅用于安排检测；
# 数据是否最新由持久化的上游检查证据决定。
SOURCE_INTERVALS: dict[str, timedelta] = {
    "catalog": timedelta(hours=24),
    "hextech": timedelta(hours=4),
    "aramkit": timedelta(hours=4),
    "blitz": timedelta(hours=2),
    "apex": timedelta(hours=4),
    "mayhem": timedelta(hours=4),
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
    """兼容旧诊断的年龄计算；不得再用于决定数据是否与上游一致。"""

    parsed = parse_refresh_time(data_at_text)
    if parsed is None:
        return (False, 0)
    age = normalize_utc(completed) - parsed.astimezone(timezone.utc)
    if age > interval * STALE_AGE_FACTOR:
        return (True, max(0, int(age.total_seconds())))
    return (False, 0)


def source_reuse_allowed(source: str, data_at_text: object, now: datetime) -> bool:
    """相同 marker 的 verified pointer 是否可复用。

    调用者已经比较过上游 marker；因此数据年龄不再形成第二个隐式失效门。
    时间只做基本完整性校验，缺失、非法或明显来自未来的指针 fail closed。
    ``source`` 参数保留以兼容现有调用接口。
    """

    parsed = parse_refresh_time(data_at_text)
    if parsed is None or str(source or "") not in SOURCE_INTERVALS:
        return False
    return parsed.astimezone(timezone.utc) <= normalize_utc(now) + timedelta(minutes=5)


__all__ = [
    "SOURCE_INTERVALS",
    "STALE_AGE_FACTOR",
    "evaluate_source_expiry",
    "normalize_utc",
    "parse_refresh_time",
    "source_reuse_allowed",
]

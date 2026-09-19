"""Overlay 只显示实际缺失/损坏；数据年龄与待切代信息留在诊断中。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, NotRequired, TypedDict


DataNoticeState = Literal["fresh", "stale", "updating", "unavailable"]


class DataNoticeModel(TypedDict):
    text: str
    source: str
    reason: str
    data_at: str
    state: DataNoticeState
    age_seconds: NotRequired[int | None]


def stats_stale_text(reason: str, data_at: str, *, now: datetime | None = None) -> str:
    """过期统计只显示可核验年龄；错误/未来时间不虚构新鲜度。"""

    if reason == "source_data_expired" and data_at:
        try:
            parsed = datetime.fromisoformat(data_at.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            current = now if now is not None else datetime.now(timezone.utc)
            age_seconds = (current - parsed).total_seconds()
            if age_seconds >= 3600:
                hours = int(age_seconds // 3600)
                return f"统计数据为 {hours // 24} 天前" if hours >= 48 else f"统计数据为 {hours} 小时前"
    return "统计数据暂非最新"


def data_age_seconds(data_at: str, *, now: datetime | None = None) -> int | None:
    if not data_at:
        return None
    try:
        parsed = datetime.fromisoformat(data_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    age = (current - parsed).total_seconds()
    return max(0, int(age)) if age >= 0 else None


def build_data_notice(
    snapshot_status: Mapping[str, Any] | None,
    *,
    rows: Sequence[Mapping[str, Any]] = (),
    stats_scope: Mapping[str, Any] | None = None,
) -> DataNoticeModel | None:
    """时效不影响已经验证的数据可用性，不能单独生成 Canvas 提示。"""

    source_status = snapshot_status.get("source_status") if isinstance(snapshot_status, Mapping) else None
    source_status = source_status if isinstance(source_status, Mapping) else {}
    for name in ("aramkit", "hextech"):
        source = source_status.get(name)
        if not isinstance(source, Mapping):
            continue
        reason = str(source.get("data_reason") or "")
        unavailable = (
            source.get("data_status") in {"failed", "unavailable", "missing", "invalid"}
            or source.get("state") in {"failed", "unavailable"}
            or any(part in reason for part in ("missing", "corrupt", "invalid", "unavailable"))
        )
        if unavailable:
            return {
                "text": "统计暂不可用",
                "source": name,
                "reason": reason or "source_unavailable",
                "data_at": str(source.get("data_at") or ""),
                "state": "unavailable",
                "age_seconds": None,
            }
    return None


__all__ = ["build_data_notice", "data_age_seconds", "stats_stale_text"]

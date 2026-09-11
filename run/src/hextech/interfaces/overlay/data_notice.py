"""Overlay 阶段附近的数据时效提示。"""

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
    """把来源 freshness 汇总为阶段附近的单条提示，不污染卡片正文。"""

    source_status = snapshot_status.get("source_status") if isinstance(snapshot_status, Mapping) else None
    source_status = source_status if isinstance(source_status, Mapping) else {}
    if isinstance(stats_scope, Mapping) and bool(stats_scope.get("new_generation_available")):
        return {
            "text": "当前选择沿用上一代统计，下一轮采用新数据",
            "source": "aramkit",
            "reason": "new_generation_available",
            "data_at": "",
            "state": "updating",
            "age_seconds": None,
        }
    for primary_source in ("aramkit", "hextech"):
        primary = source_status.get(primary_source)
        if not isinstance(primary, Mapping):
            continue
        stale = (
            str(primary.get("freshness") or "unknown") != "fresh"
            or str(primary.get("data_status") or "unknown") == "data_stale"
        )
        if stale:
            reason = str(primary.get("data_reason") or "")
            data_at = str(primary.get("data_at") or "")
            return {
                "text": stats_stale_text(reason, data_at),
                "source": primary_source,
                "reason": reason,
                "data_at": data_at,
                "state": "stale",
                "age_seconds": data_age_seconds(data_at),
            }
    blitz = source_status.get("blitz")
    if isinstance(blitz, Mapping):
        stale = (
            str(blitz.get("freshness") or "unknown") != "fresh"
            or str(blitz.get("data_status") or "unknown") == "data_stale"
        )
        if stale:
            data_at = str(blitz.get("data_at") or "")
            return {
                "text": "Blitz 排名暂不可用",
                "source": "blitz",
                "reason": str(blitz.get("data_reason") or "optional_source_stale"),
                "data_at": data_at,
                "state": "unavailable",
                "age_seconds": data_age_seconds(data_at),
            }
    for row in rows:
        if str(row.get("stats_source") or "") != "aramkit" or str(row.get("data_status") or "") != "stale":
            continue
        data_at = str(row.get("source_data_at") or "")
        reason = str(row.get("data_reason") or "")
        return {
            "text": stats_stale_text(reason, data_at),
            "source": "aramkit",
            "reason": reason,
            "data_at": data_at,
            "state": "stale",
            "age_seconds": data_age_seconds(data_at),
        }
    return None


__all__ = ["build_data_notice", "data_age_seconds", "stats_stale_text"]

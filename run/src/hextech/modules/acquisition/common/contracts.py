"""抓取基础设施的版本化运行契约。

本模块只描述一次请求、单项结果和来源 run，不包含站点解析逻辑。诊断报告和
``current.v1.json`` 均从这些对象序列化，避免三个来源各自发明状态字段。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from hextech.contracts import FailureKind, SourceHealth


ItemState = Literal["success", "confirmed_empty", "failed"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FetchAttempt:
    url: str
    backend: str
    status_code: int | None
    elapsed_ms: int
    attempts: int
    failure_kind: FailureKind | None = None
    retryable: bool = False
    fetched_at: str = field(default_factory=utc_now_iso)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.failure_kind is None and self.status_code is not None and 200 <= self.status_code < 400

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else ""
        return payload


@dataclass(frozen=True)
class ItemOutcome:
    item_id: str
    state: ItemState
    stage: str
    record_count: int = 0
    failure_kind: FailureKind | None = None
    attempt: FetchAttempt | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else ""
        payload["attempt"] = self.attempt.to_dict() if self.attempt else None
        return payload


@dataclass(frozen=True)
class SourceRunManifest:
    source: str
    run_id: str
    health: SourceHealth
    started_at: str
    finished_at: str
    expected_items: int
    successful_items: int
    confirmed_empty_items: int
    failed_items: int
    record_count: int
    artifact: str
    schema_version: int = 1
    outcomes: tuple[ItemOutcome, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def publishable(self) -> bool:
        return (
            self.health is SourceHealth.HEALTHY
            and self.failed_items == 0
            and self.successful_items + self.confirmed_empty_items == self.expected_items
            and self.record_count > 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["health"] = self.health.value
        payload["outcomes"] = [outcome.to_dict() for outcome in self.outcomes]
        payload["publishable"] = self.publishable
        return payload


__all__ = ["FetchAttempt", "ItemOutcome", "ItemState", "SourceRunManifest", "utc_now_iso"]

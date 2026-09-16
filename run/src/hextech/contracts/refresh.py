"""刷新执行者发布的进度；展示端不得重新推测来源或阶段。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Mapping
import math
import time


@dataclass(frozen=True)
class RefreshProgress:
    request_id: str = ""
    state: str = "idle"
    source: str = ""
    phase: str = ""
    completed_items: int = 0
    total_items: int = 0
    reason_code: str = ""
    generation_id: str = ""
    core_published_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    updated_at: float = 0.0
    checked_at: float = 0.0
    data_at: str = ""
    catalog_state: str = ""
    checked: bool = False
    content_changed: bool = False
    catalog_changed: bool = False
    source_outcomes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in {"idle", "queued", "running", "core_ready", "completed", "unchanged", "failed", "deferred"}:
            raise ValueError("invalid refresh progress state")
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0
               for n in (self.completed_items, self.total_items)):
            raise ValueError("invalid refresh progress counts")
        if self.completed_items > self.total_items:
            raise ValueError("refresh completed exceeds total")
        if any(not math.isfinite(t) or t < 0 for t in
               (self.started_at, self.completed_at, self.core_published_at, self.updated_at, self.checked_at)):
            raise ValueError("invalid refresh progress time")
        if any(type(value) is not bool for value in (self.checked, self.content_changed, self.catalog_changed)):
            raise ValueError("invalid refresh progress change flags")
        if not isinstance(self.source_outcomes, Mapping) or any(
            not isinstance(source, str) or not isinstance(outcome, Mapping)
            for source, outcome in self.source_outcomes.items()
        ):
            raise ValueError("invalid refresh source outcomes")

    def advance(self, **changes: Any) -> RefreshProgress:
        return replace(self, updated_at=time.time(), **changes)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "pending_items": self.total_items - self.completed_items}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RefreshProgress:
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})

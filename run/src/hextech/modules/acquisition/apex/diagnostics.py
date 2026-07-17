"""Apex 三态结果的统一诊断构建。"""

from __future__ import annotations

from hextech.modules.acquisition.common.contracts import ItemOutcome
from hextech.modules.acquisition.apex.parser import ApexPageOutcome, ApexPageState


def item_outcome(
    champion_id: str,
    page: ApexPageOutcome,
    *,
    record_count: int,
    backend: str,
    status_code: int | None,
    url: str,
) -> ItemOutcome:
    state = "success" if page.state is ApexPageState.HAS_SYNERGY else page.state.value
    return ItemOutcome(
        item_id=str(champion_id),
        state=state,
        stage="detail",
        record_count=record_count,
        failure_kind=page.failure_kind,
        details={"backend": backend, "status_code": status_code, "url": url, "evidence": page.evidence},
    )


__all__ = ["item_outcome"]

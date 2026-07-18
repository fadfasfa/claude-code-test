"""Mayhem reject 原因聚合与有限样本。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


def summarize_rejects(rejects: Iterable[Mapping[str, Any]], *, sample_limit: int = 20) -> dict[str, Any]:
    items = [dict(item) for item in rejects]
    reasons = Counter(str(item.get("reason") or "unknown_reject") for item in items)
    return {"count": len(items), "reasons": dict(sorted(reasons.items())), "samples": items[:sample_limit]}


__all__ = ["summarize_rejects"]

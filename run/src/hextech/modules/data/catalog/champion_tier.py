"""英雄综合评分到 T1–T5 的唯一评级规则。

Web、Desktop 与 generation DTO 都通过本模块解释评分，避免前端与 Python
分别维护会悄然漂移的阈值。缺少或非法评分时回退 T3：它表示中性、可展示，
而不是不确定的 ``T?``。
"""

from __future__ import annotations

import math
from typing import Any


CHAMPION_TIERS = ("T1", "T2", "T3", "T4", "T5")


def champion_tier_from_score(value: Any) -> str:
    """按综合评分返回稳定的 T1–T5；非法输入显式采用中性 T3。"""

    try:
        score = float(value)
    except (TypeError, ValueError):
        return "T3"
    if not math.isfinite(score):
        return "T3"
    if score >= 1.5:
        return "T1"
    if score >= 0.8:
        return "T2"
    if score >= 0.3:
        return "T3"
    if score >= -0.3:
        return "T4"
    return "T5"


def normalized_champion_tier(value: Any, *, score: Any = None) -> str:
    """优先采用 DTO 的合法评级，旧 DTO 才依据同一评分规则回退。"""

    tier = str(value or "").strip().upper()
    return tier if tier in CHAMPION_TIERS else champion_tier_from_score(score)


__all__ = ["CHAMPION_TIERS", "champion_tier_from_score", "normalized_champion_tier"]

"""Hextech 解析层到发布层之间的类型化统计记录。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HextechStatRecord:
    champion_id: str
    champion_name: str
    champion_tier: str
    champion_win_rate: float
    champion_pick_rate: float
    augment_id: str
    source_rank: int
    source_tier: str
    augment_tier: str
    augment_name: str
    augment_win_rate: float
    augment_pick_rate: float

    def __post_init__(self) -> None:
        if not self.champion_id or not self.champion_name or not self.augment_id or not self.augment_name:
            raise ValueError("Hextech 统计身份字段不能为空")
        for name in ("champion_win_rate", "champion_pick_rate", "augment_win_rate", "augment_pick_rate"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"Hextech {name} 超出 [0, 1]：{value}")
        if self.source_rank <= 0:
            raise ValueError("Hextech source_rank 必须大于 0")

    def to_csv_row(self) -> dict[str, object]:
        """中文列名只在 CSV adapter 边界出现，业务代码只使用类型字段。"""

        return {
            "英雄ID": self.champion_id,
            "英雄名称": self.champion_name,
            "英雄评级": self.champion_tier,
            "英雄胜率": self.champion_win_rate,
            "英雄出场率": self.champion_pick_rate,
            "海克斯ID": self.augment_id,
            "源站排名": self.source_rank,
            "源站层级": self.source_tier,
            "海克斯阶级": self.augment_tier,
            "海克斯名称": self.augment_name,
            "海克斯胜率": self.augment_win_rate,
            "海克斯出场率": self.augment_pick_rate,
        }


__all__ = ["HextechStatRecord"]

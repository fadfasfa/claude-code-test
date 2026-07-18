"""Hextech 全量行级 schema、身份、数值和重复记录门禁。"""

from __future__ import annotations

import math
from collections.abc import Iterable

import pandas as pd


HEXTECH_REQUIRED_COLUMNS = (
    "英雄ID",
    "英雄名称",
    "英雄评级",
    "英雄胜率",
    "英雄出场率",
    "海克斯ID",
    "海克斯阶级",
    "海克斯名称",
    "海克斯胜率",
    "海克斯出场率",
    "胜率差",
    "综合得分",
)


class HextechSchemaChanged(ValueError):
    pass


def validate_hextech_frame(frame: pd.DataFrame, expected_hero_ids: Iterable[str], *, min_rows: int = 300) -> None:
    expected = {str(value).strip() for value in expected_hero_ids if str(value).strip()}
    missing_columns = [column for column in HEXTECH_REQUIRED_COLUMNS if column not in frame.columns]
    if missing_columns:
        raise HextechSchemaChanged(f"Hextech CSV 缺少字段：{missing_columns}")
    if len(frame) < min_rows:
        raise HextechSchemaChanged(f"Hextech CSV 行数异常：{len(frame)} < {min_rows}")

    hero_ids = frame["英雄ID"].astype(str).str.replace(".0", "", regex=False).str.strip()
    actual = set(hero_ids)
    if actual != expected:
        raise HextechSchemaChanged(
            f"Hextech 英雄覆盖不完整：missing={sorted(expected - actual)} unexpected={sorted(actual - expected)}"
        )
    per_hero = hero_ids.value_counts()
    empty_heroes = sorted(hero_id for hero_id in expected if int(per_hero.get(hero_id, 0)) <= 0)
    if empty_heroes:
        raise HextechSchemaChanged(f"Hextech 英雄统计为空：{empty_heroes}")

    duplicate_mask = frame.assign(_hero_id=hero_ids).duplicated(["_hero_id", "海克斯ID"], keep=False)
    if duplicate_mask.any():
        sample = frame.loc[duplicate_mask, ["英雄ID", "海克斯ID"]].head(10).to_dict("records")
        raise HextechSchemaChanged(f"Hextech 英雄海克斯记录重复：{sample}")

    numeric_columns = ("英雄胜率", "英雄出场率", "海克斯胜率", "海克斯出场率", "胜率差", "综合得分")
    for column in numeric_columns:
        values = pd.to_numeric(frame[column], errors="coerce")
        if values.isna().any() or not all(math.isfinite(float(value)) for value in values):
            raise HextechSchemaChanged(f"Hextech 数值字段异常：{column}")
    for column in ("英雄胜率", "英雄出场率", "海克斯胜率", "海克斯出场率"):
        values = pd.to_numeric(frame[column], errors="coerce")
        if ((values < 0) | (values > 1)).any():
            raise HextechSchemaChanged(f"Hextech 比例字段超出 [0, 1]：{column}")


__all__ = ["HEXTECH_REQUIRED_COLUMNS", "HextechSchemaChanged", "validate_hextech_frame"]

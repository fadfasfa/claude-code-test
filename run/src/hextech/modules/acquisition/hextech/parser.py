"""Hextech 行级 schema、英雄身份与重复记录校验。"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from hextech.modules.data.catalog.runtime_store import CSV_REQUIRED_COLUMNS


class HextechSchemaChanged(ValueError):
    pass


def validate_hextech_frame(frame: pd.DataFrame, expected_hero_ids: Iterable[str], *, min_rows: int = 300) -> None:
    expected = {str(value) for value in expected_hero_ids}
    missing_columns = [column for column in CSV_REQUIRED_COLUMNS if column not in frame.columns]
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

    duplicate_key = "海克斯ID" if "海克斯ID" in frame.columns else "海克斯名称"
    duplicate_mask = frame.assign(_hero_id=hero_ids).duplicated(["_hero_id", duplicate_key], keep=False)
    if duplicate_mask.any():
        sample = frame.loc[duplicate_mask, ["英雄ID", duplicate_key]].head(10).to_dict("records")
        raise HextechSchemaChanged(f"Hextech 英雄海克斯记录重复：{sample}")

    numeric_columns = ("英雄胜率", "英雄出场率", "海克斯胜率", "海克斯出场率", "胜率差", "综合得分")
    invalid_columns = [column for column in numeric_columns if pd.to_numeric(frame[column], errors="coerce").isna().any()]
    if invalid_columns:
        raise HextechSchemaChanged(f"Hextech 数值字段类型异常：{invalid_columns}")


__all__ = ["HextechSchemaChanged", "validate_hextech_frame"]

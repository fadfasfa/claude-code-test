"""旧 CSV 到 generation DTO 的只读兼容适配。

新 production generation 不调用本模块；它仅供历史测试、工具与旧代迁移读取，
不负责来源刷新或写入 runtime cache。
"""

from __future__ import annotations

from typing import Any


def query_payloads_from_dataframe(dataframe) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    import pandas as pd

    from hextech.modules.data.catalog.view_adapter import process_champions_data

    id_column = "英雄ID" if "英雄ID" in dataframe.columns else "英雄 ID"
    required = {id_column, "英雄名称", "海克斯ID", "海克斯名称"}
    if not required.issubset(set(dataframe.columns)):
        raise ValueError("DataService generation 源 CSV schema 不完整")

    def clean_value(value: Any) -> Any:
        if pd.isna(value):
            return None
        if hasattr(value, "item"):
            return value.item()
        return value

    id_by_name: dict[str, str] = {}
    for hero_name, group in dataframe.groupby("英雄名称", sort=False):
        name = str(hero_name or "").strip()
        if name:
            id_by_name[name] = str(int(float(group.iloc[0][id_column])))
    champions = process_champions_data(dataframe, use_runtime_cache=False, log_columns=False)
    for champion in champions:
        name = str(champion.get("英雄名称") or "").strip()
        champion_id = str(champion.get("英雄 ID") or id_by_name.get(name, "")).strip()
        champion.update({"英雄 ID": champion_id, "id": champion_id, "name": name})

    details: dict[str, dict[str, Any]] = {}
    for hero_name, group in dataframe.groupby("英雄名称", sort=False):
        name = str(hero_name or "").strip()
        if not name:
            continue
        champion_id = str(int(float(group.iloc[0][id_column])))
        cards: list[dict[str, Any]] = []
        for raw in group.to_dict(orient="records"):
            card = {str(key): clean_value(value) for key, value in raw.items()}
            augment_id = str(int(float(card["海克斯ID"])))
            card.update({"id": augment_id, "hero_id": champion_id, "hero_name": name})
            cards.append(card)
        if cards:
            details[name] = {"hero_id": champion_id, "comprehensive": cards}
    if not champions or any(not item.get("英雄 ID") for item in champions):
        raise ValueError("DataService 冷启动英雄 DTO 构建不完整")
    return champions, details


__all__ = ["query_payloads_from_dataframe"]

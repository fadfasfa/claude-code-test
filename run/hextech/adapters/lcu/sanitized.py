"""生成不包含认证、账号或聊天内容的 LCU 诊断摘要。"""

from __future__ import annotations

from typing import Any, Mapping

from hextech.contracts.identifiers import optional_champion_id


def summarize_lcu_context(payload: Mapping[str, Any]) -> dict[str, object]:
    team = payload.get("myTeam") if isinstance(payload.get("myTeam"), list) else []
    bench = payload.get("benchChampions") if isinstance(payload.get("benchChampions"), list) else []

    def summarize(values: list[Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for item in values:
            if not isinstance(item, Mapping):
                continue
            raw = item.get("championId")
            normalized = optional_champion_id(raw)
            result.append(
                {
                    "cell_id": str(item.get("cellId") or ""),
                    "champion_id": str(normalized or ""),
                    "raw_type": type(raw).__name__,
                }
            )
        return result

    return {
        "schema_version": 1,
        "local_cell_id": str(payload.get("localPlayerCellId") or ""),
        "team": summarize(team),
        "bench": summarize(bench),
    }

"""验证 Web、Desktop 与数据 DTO 共用的英雄 T1–T5 规则。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hextech.modules.data.catalog.champion_tier import (
    champion_tier_from_score,
    normalized_champion_tier,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (1.5, "T1"),
        (0.8, "T2"),
        (0.3, "T3"),
        (-0.3, "T4"),
        (-0.300001, "T5"),
    ],
)
def test_score_boundary_maps_to_expected_tier(score: float, expected: str) -> None:
    assert champion_tier_from_score(score) == expected


@pytest.mark.parametrize("value", (None, "", "T?", "S", float("nan"), float("inf")))
def test_invalid_tier_or_score_falls_back_to_neutral_t3(value: object) -> None:
    assert normalized_champion_tier(value, score=value) == "T3"


def test_processed_champion_dto_derives_authoritative_tier_from_same_score(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.modules.data.catalog import view_adapter

    monkeypatch.setattr(view_adapter, "_validate_runtime_schema", lambda *_args: True)
    monkeypatch.setattr(view_adapter, "_get_champion_maps", lambda: ({}, {}))
    dataframe = pd.DataFrame(
        [
            {"英雄名称": "英雄甲", "英雄胜率": 0.62, "英雄出场率": 0.20},
            {"英雄名称": "英雄乙", "英雄胜率": 0.50, "英雄出场率": 0.10},
            {"英雄名称": "英雄丙", "英雄胜率": 0.38, "英雄出场率": 0.02},
        ]
    )

    rows = view_adapter.process_champions_data(
        dataframe,
        use_runtime_cache=False,
        log_columns=False,
    )

    assert rows
    assert {
        row["英雄评级"] == champion_tier_from_score(row["综合分数"])
        for row in rows
    } == {True}


def test_desktop_fallback_and_web_grouping_never_emit_t_question_mark() -> None:
    from hextech.interfaces.desktop import app_view

    assert app_view.normalized_champion_tier("T?", score=1.5) == "T1"
    assert app_view.normalized_champion_tier("", score=-0.31) == "T5"

    index_script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "hextech"
        / "interfaces"
        / "web"
        / "backend"
        / "static"
        / "js"
        / "index.js"
    ).read_text(encoding="utf-8")
    assert "champ['英雄评级']" in index_script
    assert "TIER_BY_ID.get('T3')" in index_script
    assert "T?" not in index_script

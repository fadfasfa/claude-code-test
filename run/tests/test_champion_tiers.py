"""验证 Web、Desktop 与数据 DTO 共用的英雄 T1–T5 规则。"""

from __future__ import annotations


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


def test_champion_ranking_dto_derives_tier_from_its_score():
    from hextech.modules.recommendation.hero_rankings import build_hero_rankings
    inputs = [{"id": str(i), "win_rate": win, "pick_rate": pick, "sample_count": 1000,
               "source_rank": i, "source_tier": "S"}
              for i, win, pick in [(1, .62, .2), (2, .5, .1), (3, .38, .02)]]
    rows = build_hero_rankings(inputs, {str(i): {"name": f"英雄{i}"} for i in (1, 2, 3)})
    assert len(rows) == 3
    assert all(row["英雄评级"] == champion_tier_from_score(row["综合分数"]) for row in rows)


def test_desktop_never_emits_t_question_mark():
    from hextech.interfaces.desktop import app_view
    assert app_view.normalized_champion_tier("T?", score=1.5) == "T1"
    assert app_view.normalized_champion_tier("", score=-0.31) == "T5"

"""Overlay Stage 统计优先级、样本门与 Blitz 回退测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from types import MappingProxyType

import pytest

from hextech.contracts import (
    GameSessionState,
    GenerationId,
    GameSessionId,
    HealthState,
    RecommendationModel,
)
from hextech.interfaces.overlay.renderer import _scoped_stage_stats_display, build_render_model_from_session
from hextech.modules.data.scoped_stats import ScopedStatsView
from hextech.modules.game_context.stage_context import StageContextV1
from hextech.modules.recommendation.stage_projection import apply_scoped_stage_stats


def _state(*, with_blitz: bool = True) -> GameSessionState:
    stats = {"source_tier": 3, "champion_tier": 1, "stats_scope": "top_champion_tier"} if with_blitz else {}
    recommendation = RecommendationModel(
        generation_id=GenerationId("generation-1"),
        session_id=GameSessionId("session-1"),
        observed_at=1.0,
        augment_slots=(
            {
                "slot": 0,
                "state": "ready",
                "augment_id": "100",
                "canonical_id": "100",
                "canonical_augment_id": "100",
                "name": "测试海克斯",
                "tier": "黄金",
                "status_code": "READY" if with_blitz else "SOURCE_STAT_MISSING",
                "data_status": "ready" if with_blitz else "missing",
                "stats": stats,
            },
        ),
    )
    return GameSessionState(
        session_id=GameSessionId("session-1"),
        generation_id=GenerationId("generation-1"),
        observed_at=1.0,
        phase="ready",  # type: ignore[arg-type]
        visibility=None,  # type: ignore[arg-type]
        recommendation=recommendation,
        health=HealthState.READY,
    )


def _record(sample_count: int, *, win_rate: float = 0.6) -> MappingProxyType:
    return MappingProxyType(
        {
            "id": "100",
            "source_rank": 1,
            "sample_count": sample_count,
            "win_rate": win_rate,
            "pick_rate": 0.05,
            "blue_win_rate": win_rate,
            "red_win_rate": win_rate,
        }
    )


def _view(*, stage_record: MappingProxyType | None, all_record: MappingProxyType | None) -> ScopedStatsView:
    return ScopedStatsView(
        generation_id="generation-1",
        run_id="aramkit-run",
        champion_id="1",
        version="16.16",
        data_path="data/test",
        all_stats=MappingProxyType({"100": all_record} if all_record is not None else {}),
        stage_stats=MappingProxyType(
            {
                1: MappingProxyType({"100": stage_record} if stage_record is not None else {}),
                2: MappingProxyType({}),
                3: MappingProxyType({}),
                4: MappingProxyType({}),
            }
        ),
    )


def _context(stage: int | None = 1) -> StageContextV1:
    return StageContextV1(champion_id="1", game_instance_id="game-1", stage=stage, status="ready")


def test_fresh_hero_statistics_do_not_inherit_old_global_ranking_freshness():
    now = datetime.now(timezone.utc)
    hero_at = (now - timedelta(minutes=5)).isoformat()
    scoped = replace(_view(stage_record=_record(1500), all_record=_record(2000)),
                     run_id="hero-fresh", data_at=hero_at)
    status = {"source_status": {"aramkit": {
        "run_id": "ranking-old", "freshness": "last_good", "data_status": "data_stale",
        "data_reason": "source_data_expired", "data_at": "2020-01-01T00:00:00Z",
        "check_status": "up_to_date", "check_evidence_bound": True,
        "upstream_revision": "data/test", "applied_revision": "data/test",
    }}, "components": {
        "ranking": {"run_id": "ranking-old", "source_version": "data/test", "catalog_id": "catalog-1"},
        "champions": {"1": {"run_id": "hero-fresh", "source_version": "data/test",
                                "catalog_id": "catalog-1", "complete": True}},
    }}
    projected = apply_scoped_stage_stats(_state(), stage_context=_context(), scoped_view=scoped,
        scope_status="ready", scope_reason="", snapshot_status=status, now=now)
    row = projected.recommendation.augment_slots[0]
    assert row["source_run_id"] == "hero-fresh"
    assert row["source_data_at"] == hero_at
    assert row["source_freshness"] == "fresh"
    assert row["data_reason"] == ""
    assert row["status_code"] == "READY"


def _source_status() -> dict:
    return {
        "state": "ready",
        "source_status": {
            "aramkit": {"freshness": "fresh", "data_status": "fresh", "run_id": "aramkit-ranking",
                        "check_status": "up_to_date", "check_evidence_bound": True,
                        "upstream_revision": "data/test", "applied_revision": "data/test"},
            "blitz": {"freshness": "fresh", "data_status": "fresh", "run_id": "blitz-run",
                      "check_status": "up_to_date", "check_evidence_bound": True,
                      "upstream_revision": "blitz-v1", "applied_revision": "blitz-v1"},
        },
        "components": {
            "ranking": {"run_id": "aramkit-ranking", "source_version": "data/test",
                        "catalog_id": "catalog-1"},
            "champions": {"1": {"run_id": "aramkit-run", "source_version": "data/test",
                                    "catalog_id": "catalog-1", "complete": True}},
        },
    }


def test_stage_record_overrides_blitz_and_marks_low_sample() -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(1),
        scoped_view=_view(stage_record=_record(578), all_record=_record(2000)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=_source_status(),
    )
    row = projected.recommendation.augment_slots[0]  # type: ignore[union-attr]
    model = build_render_model_from_session(projected, stats_scope={"stage": 1})
    rendered = model["stats"][0]

    assert row["stats_source"] == "aramkit"
    assert row["stats_scope"] == "stage_1"
    assert rendered["stats_text"] == "胜率 60.0% · 出场 5.0%"
    assert rendered["stats_tone"] == "low_sample"
    assert rendered["low_sample_outline"] is False
    assert model["stage_indicator"] == {"stage": 1, "label": "阶段 1"}


def test_blitz_refresh_failure_does_not_hide_aramkit_stage_stats() -> None:
    source_status = _source_status()
    source_status["state"] = "degraded"
    source_status["source_status"]["blitz"] = {
        "freshness": "stale",
        "data_status": "data_stale",
        "data_reason": "connection_reset_last_good_preserved",
        "run_id": "blitz-last-good",
    }

    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(1),
        scoped_view=_view(stage_record=_record(1500), all_record=_record(2000)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=source_status,
    )
    row = projected.recommendation.augment_slots[0]  # type: ignore[union-attr]
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert row["stats_source"] == "aramkit"
    assert row["status_code"] == "READY"
    assert rendered["stats_text"] == "胜率 60.0% · 出场 5.0%"
    assert rendered["stats_tone"] == "default"


def test_stale_aramkit_record_keeps_private_and_visible_percentages_with_stage_notice() -> None:
    source_status = _source_status()
    source_status["state"] = "degraded"
    source_status["source_status"]["aramkit"] = {
        "freshness": "last_good",
        "data_status": "data_stale",
        "data_reason": "source_data_expired",
        "data_at": (datetime.now(timezone.utc) - timedelta(hours=100, minutes=30)).isoformat(),
        "run_id": "aramkit-last-good",
    }
    source_status["source_status"]["blitz"] = {
        "freshness": "stale",
        "data_status": "data_stale",
        "data_reason": "connection_reset_last_good_preserved",
        "run_id": "blitz-last-good",
    }

    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(3),
        scoped_view=_view(stage_record=_record(1500), all_record=_record(2000)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=source_status,
    )
    row = projected.recommendation.augment_slots[0]  # type: ignore[union-attr]
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert row["status_code"] == "GENERATION_DEGRADED"
    assert row["data_status"] == "stale"
    assert row["stats_source"] == "aramkit"
    assert row["source_run_id"] == "aramkit-run"
    assert row["stats_generation_id"] == "generation-1"
    # 内部 DTO 保留时效诊断；Canvas 只显示仍可信的百分比，不显示年龄。
    assert row["stats"]["winrate"] == 0.6
    assert row["stats"]["pickrate"] == 0.05
    assert rendered["stats_text"] == "胜率 60.0% · 出场 5.0%"
    assert rendered["status_text"] == ""
    session_model = build_render_model_from_session(projected)
    assert session_model.get("data_notice") is None
    visible = " ".join(
        str(rendered.get(field) or "")
        for field in ("stats_text", "status_text", "winrate_text", "pickrate_text")
    )
    assert "60.0%" in visible
    assert "5.0%" in visible


def test_new_generation_remains_diagnostic_without_canvas_age_notice() -> None:
    source_status = _source_status()
    source_status["source_status"]["aramkit"] = {
        "freshness": "last_good",
        "data_status": "data_stale",
        "data_reason": "source_data_expired",
        "data_at": (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat(),
        "run_id": "aramkit-last-good",
    }
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(1),
        scoped_view=_view(stage_record=_record(1500), all_record=_record(2000)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=source_status,
    )

    model = build_render_model_from_session(
        projected,
        stats_scope={
            "stage": 1,
            "new_generation_available": True,
            "new_generation_id": "generation-2",
        },
    )

    assert model["stats"][0]["stats_text"] == "胜率 60.0% · 出场 5.0%"
    assert model.get("data_notice") is None
    assert model["stage_indicator"].get("data_notice") is None


def test_missing_stage_falls_back_to_all_and_hides_tiny_sample_rates() -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(2),
        scoped_view=_view(stage_record=None, all_record=_record(42)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=_source_status(),
    )
    row = projected.recommendation.augment_slots[0]  # type: ignore[union-attr]
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert row["stats_scope"] == "all"
    assert row["stats_fallback_reason"] == "stage_stat_missing_fallback_all"
    assert rendered["stats_text"] == "胜率 60.0% · 出场数 42"
    assert rendered["winrate_text"] == "60.0%"
    assert rendered["pickrate_text"] == "5.0%"
    assert rendered["stats_tone"] == "aggregate"
    assert rendered["low_sample_outline"] is True


def test_missing_aramkit_champion_does_not_consume_retired_blitz() -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(1),
        scoped_view=None,
        scope_status="fallback",
        scope_reason="aramkit_champion_missing",
        snapshot_status=_source_status(),
    )
    row = projected.recommendation.augment_slots[0]  # type: ignore[union-attr]
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert row["stats_source"] == ""
    assert row["stats_fallback_reason"] == "aramkit_champion_missing"
    assert rendered["status_code"] == "CHAMPION_STAT_MISSING"
    assert not row["stats"]
    assert rendered["winrate_text"] == ""


def test_stale_verified_blitz_remains_readable_but_not_a_runtime_fallback() -> None:
    source_status = _source_status()
    source_status["state"] = "degraded"
    source_status["source_status"]["blitz"] = {
        "freshness": "last_good",
        "data_status": "data_stale",
        "data_reason": "production_coverage_insufficient",
        "run_id": "blitz-last-good",
    }
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(1),
        scoped_view=None,
        scope_status="fallback",
        scope_reason="aramkit_champion_missing",
        snapshot_status=source_status,
    )
    row = projected.recommendation.augment_slots[0]  # type: ignore[union-attr]
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert row["status_code"] == "CHAMPION_STAT_MISSING"
    assert row["data_status"] == "missing"
    assert not row["stats"]
    assert "T3" not in rendered["stats_text"]


def test_preparing_scope_does_not_show_blitz_early() -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(None),
        scoped_view=None,
        scope_status="preparing",
        scope_reason="stage_context_waiting",
        snapshot_status=_source_status(),
    )
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert rendered["status_code"] == "STATS_PREPARING"
    assert rendered["stats_text"] == "统计准备中"


def test_stage_and_all_missing_leave_explicit_public_source_gap() -> None:
    projected = apply_scoped_stage_stats(
        _state(with_blitz=False),
        stage_context=_context(1),
        scoped_view=_view(stage_record=None, all_record=None),
        scope_status="ready",
        scope_reason="",
        snapshot_status=_source_status(),
    )
    rendered = build_render_model_from_session(projected)["stats"][0]

    assert rendered["status_code"] == "SOURCE_STAT_MISSING"
    assert rendered["stats_text"] == "公开来源未提供此海克斯统计"


@pytest.mark.parametrize(
    ("sample_count", "expected_text", "expected_tone"),
    [
        (99, "胜率 60.0% · 出场数 99", "low_sample"),
        (100, "胜率 60.0% · 出场 5.0%", "low_sample"),
        (999, "胜率 60.0% · 出场 5.0%", "low_sample"),
        (1000, "胜率 60.0% · 出场 5.0%", "default"),
    ],
)
def test_stage_sample_boundaries(
    sample_count: int,
    expected_text: str,
    expected_tone: str,
) -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(1),
        scoped_view=_view(stage_record=_record(sample_count), all_record=_record(2000)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=_source_status(),
    )

    rendered = build_render_model_from_session(projected)["stats"][0]

    assert rendered["stats_text"] == expected_text
    assert rendered["stats_tone"] == expected_tone
    assert rendered["low_sample_outline"] is False


@pytest.mark.parametrize(
    ("sample_count", "expected_text", "expected_outline"),
    [
        (99, "胜率 60.0% · 出场数 99", True),
        (100, "胜率 60.0% · 出场 5.0%", True),
        (999, "胜率 60.0% · 出场 5.0%", True),
        (1000, "胜率 60.0% · 出场 5.0%", False),
    ],
)
def test_aggregate_sample_boundaries(
    sample_count: int,
    expected_text: str,
    expected_outline: bool,
) -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(2),
        scoped_view=_view(stage_record=None, all_record=_record(sample_count)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=_source_status(),
    )

    rendered = build_render_model_from_session(projected)["stats"][0]

    assert rendered["stats_text"] == expected_text
    assert rendered["stats_tone"] == "aggregate"
    assert rendered["low_sample_outline"] is expected_outline


@pytest.mark.parametrize("sample_count", [None, -1, True, 99.5, "invalid"])
def test_missing_or_invalid_sample_count_keeps_percentages(sample_count: object) -> None:
    state = _state()
    row = dict(state.recommendation.augment_slots[0])  # type: ignore[union-attr]
    row["stats"] = {
        "stats_source": "aramkit",
        "stats_scope": "stage_1",
        "win_rate": 0.6,
        "pick_rate": 0.05,
        "sample_count": sample_count,
    }
    projected = GameSessionState(
        session_id=state.session_id,
        generation_id=state.generation_id,
        observed_at=state.observed_at,
        phase=state.phase,
        visibility=state.visibility,
        recommendation=RecommendationModel(
            generation_id=state.recommendation.generation_id,  # type: ignore[union-attr]
            session_id=state.recommendation.session_id,  # type: ignore[union-attr]
            observed_at=state.recommendation.observed_at,  # type: ignore[union-attr]
            augment_slots=(row,),
        ),
        health=state.health,
    )

    rendered = build_render_model_from_session(projected)["stats"][0]

    assert rendered["stats_text"] == "胜率 60.0% · 出场 5.0%"
    assert rendered["stats_tone"] == "default"
    assert rendered["low_sample_outline"] is False


@pytest.mark.parametrize("stage", [None, 0, 5, True, "2"])
def test_unknown_or_invalid_stage_hides_indicator(stage: object) -> None:
    model = build_render_model_from_session(_state(), stats_scope={"stage": stage})

    assert "stage_indicator" not in model


def test_card_text_never_contains_diagnostic_scope_or_sample_copy() -> None:
    projected = apply_scoped_stage_stats(
        _state(),
        stage_context=_context(2),
        scoped_view=_view(stage_record=None, all_record=_record(42)),
        scope_status="ready",
        scope_reason="",
        snapshot_status=_source_status(),
    )

    text = build_render_model_from_session(projected)["stats"][0]["stats_text"]

    for forbidden in ("阶段", "低样本", "样本不足", "综合", "回退", "过期"):
        assert forbidden not in text


def test_incomplete_stats_keep_scope_diagnostic_without_inventing_low_sample_state() -> None:
    display = _scoped_stage_stats_display(
        {
            "stats_source": "aramkit",
            "stats_scope": "all",
            "win_rate": 0.6,
            "pick_rate": None,
            "sample_count": 42,
            "fallback_reason": "stage_stat_missing_fallback_all",
        }
    )

    assert display is not None
    assert display["text"] == "统计字段不完整"
    assert display["stats_tone"] == "aggregate"
    assert display["low_sample_outline"] is False
    assert display["sample_count"] == 42
    assert display["fallback_reason"] == "stage_stat_missing_fallback_all"

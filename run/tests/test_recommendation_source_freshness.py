"""验证 Overlay 数字统计与联动状态只受各自来源 freshness 影响。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hextech.contracts import (
    AugmentId,
    ChampionId,
    GameContext,
    SourceStatusV2,
    VisionEpoch,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)
from hextech.interfaces.overlay.renderer import _synergy_stale_text, build_render_model
from hextech.modules.data.generation import DataSnapshotManifest, DataSnapshotView
from hextech.modules.recommendation import RecommendationService


def _view(
    *,
    source_status: dict[str, SourceStatusV2] | None,
    degraded: bool = True,
) -> DataSnapshotView:
    manifest = DataSnapshotManifest(
        schema_version=2,
        generation_id="g-freshness",
        created_at="now",
        content_fingerprint="f" * 64,
        source_files=(),
        champion_count=1,
        augment_count=1,
        stat_record_count=1,
        files=(),
        health="degraded" if degraded else "healthy",
        source_status=source_status or {},
    )
    return DataSnapshotView(
        manifest,
        {
            "champions": [{"id": "63", "name": "复仇焰魂"}],
            "champion_hextech": {
                "复仇焰魂": {
                    "hero_id": "63",
                    "augments": [{"id": "100", "winrate": 0.55, "pickrate": 0.12}],
                }
            },
            "overlay_hints": {},
            "identities": {
                "augments": {"100": "虚幻武器"},
                "augment_aliases": {"illusory_weapons": "100"},
                "catalog_augments": {
                    "illusory_weapons": {
                        "vision_id": "illusory_weapons",
                        "name": "虚幻武器",
                        "canonical_id": "100",
                    }
                },
            },
        },
        degraded=degraded,
    )


def _recommend(view: DataSnapshotView) -> dict[str, object]:
    context = GameContext(
        session_id="session",  # type: ignore[arg-type]
        observed_at=1,
        local_champion_id=ChampionId("63"),
    )
    vision = VisionSelection(
        session_id="session",  # type: ignore[arg-type]
        epoch=VisionEpoch(1),
        observed_at=2,
        scene_state=VisionSceneState.ACTIVE,
        slots=(VisionSlot(0, VisionSlotState.READY, AugmentId("illusory_weapons")),),
    )
    return dict(RecommendationService().build(context, view, vision=vision).augment_slots[0])


def test_generation_degraded_does_not_taint_fresh_hextech_rows() -> None:
    row = _recommend(
        _view(
            source_status={
                "hextech": SourceStatusV2(freshness="fresh", data_status="fresh", run_id="hex-new"),
                "apex": SourceStatusV2(freshness="last_good", run_id="apex-old"),
                "mayhem": SourceStatusV2(freshness="fresh", run_id="mayhem-new"),
            }
        )
    )

    assert row["status_code"] == "READY"
    assert row["source_freshness"] == "fresh"
    assert row["source_run_id"] == "hex-new"
    assert row["synergy_data_status"] == "degraded"


def test_hextech_last_good_marks_only_numeric_stats_degraded() -> None:
    row = _recommend(
        _view(
            source_status={
                "hextech": SourceStatusV2(
                    freshness="last_good",
                    data_status="data_stale",
                    data_reason="candidate_rejected_last_good_preserved",
                ),
                "apex": SourceStatusV2(freshness="fresh", data_status="fresh"),
                "mayhem": SourceStatusV2(freshness="fresh", data_status="fresh"),
            }
        )
    )

    assert row["status_code"] == "GENERATION_DEGRADED"
    assert row["data_reason"] == "candidate_rejected_last_good_preserved"
    assert row["synergy_data_status"] == "ready"


def test_old_manifest_without_source_status_falls_back_to_aggregate_health() -> None:
    row = _recommend(_view(source_status=None, degraded=True))

    assert row["status_code"] == "GENERATION_DEGRADED"
    assert row["source_freshness"] == "unknown"


def test_present_but_unknown_hextech_status_never_claims_fresh_data() -> None:
    row = _recommend(_view(source_status={"hextech": SourceStatusV2()}, degraded=False))

    assert row["status_code"] == "GENERATION_DEGRADED"


def test_renderer_shows_stale_synergy_without_tainting_fresh_hextech_stats() -> None:
    model = build_render_model(
        {
            "active": True,
            "source": {"generation_id": "g-freshness"},
            "slots": [{"slot": 0, "state": "ready", "augment_id": "illusory_weapons", "name": "虚幻武器"}],
        },
        hint_cache={
            "snapshot": {
                "state": "degraded",
                "generation_id": "g-freshness",
                "source_status": {
                    "hextech": {"freshness": "fresh", "data_status": "fresh"},
                    "apex": {"freshness": "last_good"},
                    "mayhem": {"freshness": "fresh"},
                },
            },
            "source": {"private_policy_stats_enabled": True},
            "hints": {
                "illusory_weapons": {
                    "name": "虚幻武器",
                    "stats_by_champion_id": {"63": {"winrate": 0.55, "pickrate": 0.12}},
                    "synergies": [{"hero_id": "63", "hero_name": "复仇焰魂", "rating": "S", "content": "测试"}],
                }
            },
        },
        context={"ok": True, "champion_id": "63", "champion_name": "复仇焰魂"},
    )

    assert model["stats"][0]["status_code"] == "READY"
    assert model["stats"][0]["status_text"] == ""
    assert model["synergies"][0]["data_status"] == "SYNERGY_DEGRADED"
    assert model["synergies"][0]["status_text"] == "联动数据为上一代"


def test_expired_synergy_source_marks_degraded_and_exposes_data_age() -> None:
    """回归：apex 数据过期时联动行必须降级并透出最旧 data_at，胜率行不受影响。"""

    row = _recommend(
        _view(
            source_status={
                "hextech": SourceStatusV2(freshness="fresh", data_status="fresh", run_id="hex-new"),
                "apex": SourceStatusV2(
                    freshness="fresh",
                    data_status="data_stale",
                    data_reason="source_data_expired",
                    data_at="2026-07-24T15:40:22+00:00",
                    stale_age_seconds=345600,
                ),
                "mayhem": SourceStatusV2(
                    freshness="fresh", data_status="fresh", data_at="2026-07-26T00:00:00+00:00"
                ),
            },
            degraded=False,
        )
    )

    assert row["status_code"] == "READY"
    assert row["synergy_data_status"] == "degraded"
    assert row["synergy_data_reason"] == "source_data_expired"
    # 取 apex/mayhem 中最旧的一侧，供渲染端现算真实年龄。
    assert row["synergy_data_at"] == "2026-07-24T15:40:22+00:00"


def test_synergy_stale_text_formats_age_by_hours_and_days() -> None:
    now = datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc)

    assert (
        _synergy_stale_text("source_data_expired", "2026-07-26T15:00:00+00:00", now=now)
        == "联动数据为 5 小时前"
    )
    assert (
        _synergy_stale_text("source_data_expired", "2026-07-23T15:00:00+00:00", now=now)
        == "联动数据为 3 天前"
    )
    # data_at 不可解析或原因非过期时回退通用文案，不虚构时间。
    assert _synergy_stale_text("source_data_expired", "not-a-time", now=now) == "联动数据为上一代"
    assert (
        _synergy_stale_text("candidate_rejected_last_good_preserved", "2026-07-23T15:00:00+00:00", now=now)
        == "联动数据为上一代"
    )


def test_renderer_shows_expired_synergy_age_text() -> None:
    data_at = (datetime.now(timezone.utc) - timedelta(hours=100, minutes=30)).isoformat(timespec="seconds")
    model = build_render_model(
        {
            "active": True,
            "source": {"generation_id": "g-freshness"},
            "slots": [{"slot": 0, "state": "ready", "augment_id": "illusory_weapons", "name": "虚幻武器"}],
        },
        hint_cache={
            "snapshot": {
                "state": "ready",
                "generation_id": "g-freshness",
                "source_status": {
                    "hextech": {"freshness": "fresh", "data_status": "fresh"},
                    "apex": {
                        "freshness": "fresh",
                        "data_status": "data_stale",
                        "data_reason": "source_data_expired",
                        "data_at": data_at,
                    },
                    "mayhem": {"freshness": "fresh", "data_status": "fresh"},
                },
            },
            "source": {"private_policy_stats_enabled": True},
            "hints": {
                "illusory_weapons": {
                    "name": "虚幻武器",
                    "stats_by_champion_id": {"63": {"winrate": 0.55, "pickrate": 0.12}},
                    "synergies": [{"hero_id": "63", "hero_name": "复仇焰魂", "rating": "S", "content": "测试"}],
                }
            },
        },
        context={"ok": True, "champion_id": "63", "champion_name": "复仇焰魂"},
    )

    assert model["stats"][0]["status_code"] == "READY"
    assert model["synergies"][0]["data_status"] == "SYNERGY_DEGRADED"
    # 100.5h 前 → 4 天前（100//24）；留 30 分钟余量避免小时边界抖动。
    assert model["synergies"][0]["status_text"] == "联动数据为 4 天前"

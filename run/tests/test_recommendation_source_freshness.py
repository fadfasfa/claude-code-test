"""验证 Overlay 数字统计与联动状态只受各自来源 freshness 影响。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hextech.contracts import (
    AugmentId,
    ChampionId,
    GameContext,
    SourceProvenance,
    SourceStatusV2,
    VisionEpoch,
    VisionSceneState,
    VisionSelection,
    VisionSlot,
    VisionSlotState,
)
from hextech.interfaces.overlay.renderer import _stats_stale_text, _synergy_stale_text, build_render_model
from hextech.modules.data.generation import DataSnapshotManifest, DataSnapshotView
from hextech.modules.recommendation import RecommendationService


@pytest.mark.parametrize("source", ("catalog", "hextech", "aramkit", "blitz", "apex", "mayhem"))
def test_frozen_snapshot_age_never_invents_upstream_change(source: str) -> None:
    data_at = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
    manifest = DataSnapshotManifest(
        schema_version=2,
        generation_id=f"g-{source}",
        created_at=data_at.isoformat(),
        content_fingerprint="f" * 64,
        source_files=(),
        champion_count=1,
        augment_count=1,
        stat_record_count=1,
        files=(),
        health="healthy",
        source_status={
            source: SourceStatusV2(
                freshness="fresh",
                data_status="fresh",
                data_at=data_at.isoformat(),
            )
        },
    )
    view = DataSnapshotView(manifest, {})
    before = manifest.to_dict()

    before_age = view.status(now=data_at + timedelta(hours=1))
    after_seven_days = view.status(now=data_at + timedelta(days=7))

    assert before_age == after_seven_days
    projected = after_seven_days["source_status"][source]
    assert projected["check_status"] == "unknown"
    assert projected["freshness"] == "unknown"
    assert projected["data_status"] == "fresh"
    assert projected["data_reason"] == "upstream_check_unknown"
    assert projected["stale_age_seconds"] == 0
    assert after_seven_days["effective_degraded_sources"] == ([] if source == "catalog" else [source])
    assert after_seven_days["degraded_sources"] == []
    assert after_seven_days["state"] == "ready"
    assert manifest.to_dict() == before


def test_bound_successful_check_keeps_old_data_current(tmp_path) -> None:
    data_at = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
    run_id = "aramkit-run"
    provenance = SourceProvenance(
        source="aramkit", artifact_role="hero_rankings", run_id=run_id,
        catalog_generation_id="catalog-1", artifact_sha256="a" * 64,
        manifest_sha256="b" * 64, record_count=1, content_schema_version=1,
    )
    manifest = DataSnapshotManifest(
        schema_version=3, generation_id="g-bound-check", created_at=data_at.isoformat(),
        content_fingerprint="f" * 64, source_files=(provenance,), champion_count=1,
        augment_count=1, stat_record_count=1, files=(),
        components={
            "ranking": {"run_id": run_id, "source_version": "data/16.18", "catalog_id": "catalog-1"},
            "champions": {"1": {"source_version": "data/16.18", "catalog_id": "catalog-1",
                                    "complete": False}},
        },
        source_status={"aramkit": SourceStatusV2(
            freshness="fresh", data_status="fresh", run_id=run_id, data_at=data_at.isoformat())},
    )
    schedule = tmp_path / "state" / "data-service" / "refresh_schedule.v1.json"
    schedule.parent.mkdir(parents=True)
    schedule.write_text(json.dumps({"sources": {"aramkit": {
        "current_run_id": run_id, "check_status": "up_to_date",
        "upstream_revision": "data/16.18", "applied_revision": "data/16.18",
        "last_checked_at": "2026-09-15T00:00:00+00:00", "check_interval_seconds": 14400,
    }}}), encoding="utf-8")

    projected = DataSnapshotView(manifest, {}, _runtime_root=tmp_path).status(
        now=data_at + timedelta(days=30))["source_status"]["aramkit"]

    assert projected["check_status"] == "up_to_date"
    assert projected["check_evidence_bound"] is True
    assert projected["freshness"] == "fresh"
    assert projected["data_status"] == "fresh"
    assert projected["data_at"] == data_at.isoformat()

    schedule.write_text(schedule.read_text(encoding="utf-8").replace(run_id, "other-run"), encoding="utf-8")
    mismatched = DataSnapshotView(manifest, {}, _runtime_root=tmp_path).status()["source_status"]["aramkit"]
    assert mismatched["check_status"] == "unknown"
    assert mismatched["check_evidence_bound"] is False
    assert mismatched["freshness"] == "unknown"


def test_snapshot_freshness_uses_created_at_only_when_data_at_is_missing() -> None:
    created_at = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
    manifest = DataSnapshotManifest(
        schema_version=2,
        generation_id="g-created-at-fallback",
        created_at=created_at.isoformat(),
        content_fingerprint="f" * 64,
        source_files=(),
        champion_count=1,
        augment_count=1,
        stat_record_count=1,
        files=(),
        source_status={"aramkit": SourceStatusV2(freshness="fresh", data_status="fresh")},
    )

    projected = DataSnapshotView(manifest, {}).status(now=created_at + timedelta(hours=6))["source_status"][
        "aramkit"
    ]

    assert projected["data_at"] == created_at.isoformat()
    assert projected["check_status"] == "unknown"
    assert projected["freshness"] == "unknown"
    assert projected["data_status"] == "fresh"
    assert projected["stale_age_seconds"] == 0


def test_legacy_age_only_stale_reason_becomes_unknown_without_check_evidence() -> None:
    manifest = DataSnapshotManifest(
        schema_version=2, generation_id="g-legacy-age", created_at="2026-08-19T00:00:00+00:00",
        content_fingerprint="f" * 64, source_files=(), champion_count=1, augment_count=1,
        stat_record_count=1, files=(), source_status={"aramkit": SourceStatusV2(
            freshness="last_good", data_status="data_stale", data_reason="source_data_expired",
            stale_age_seconds=3600, data_at="2026-08-18T00:00:00+00:00")},
    )

    projected = DataSnapshotView(manifest, {}).status()["source_status"]["aramkit"]

    assert projected["check_status"] == "unknown"
    assert projected["freshness"] == "unknown"
    assert projected["data_status"] == "fresh"
    assert projected["data_reason"] == "upstream_check_unknown"
    assert projected["stale_age_seconds"] == 0


def test_snapshot_freshness_preserves_specific_reason_and_invalid_time() -> None:
    observed_at = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    manifest = DataSnapshotManifest(
        schema_version=2,
        generation_id="g-reason-preserved",
        created_at=observed_at.isoformat(),
        content_fingerprint="f" * 64,
        source_files=(),
        champion_count=1,
        augment_count=1,
        stat_record_count=1,
        files=(),
        health="degraded",
        degraded_sources=("aramkit",),
        source_status={
            "aramkit": SourceStatusV2(
                freshness="last_good",
                data_status="data_stale",
                data_reason="candidate_rejected_last_good_preserved",
                data_at="2026-08-18T00:00:00+00:00",
            ),
            "blitz": SourceStatusV2(
                freshness="fresh",
                data_status="fresh",
                data_at="not-a-time",
            ),
        },
    )

    status = DataSnapshotView(manifest, {}).status(now=observed_at)

    assert status["source_status"]["aramkit"]["data_reason"] == "candidate_rejected_last_good_preserved"
    assert status["source_status"]["aramkit"]["stale_age_seconds"] == 0
    assert status["source_status"]["blitz"]["data_status"] == "fresh"
    assert status["source_status"]["blitz"]["check_status"] == "unknown"
    assert status["health"] == "degraded"
    assert status["degraded_sources"] == ["aramkit"]
    assert status["effective_degraded_sources"] == ["aramkit", "blitz"]


def _view(
    *,
    source_status: dict[str, SourceStatusV2] | None,
    degraded: bool = True,
    ranking_only: bool = False,
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
                    "augments": [
                        {"id": "100", "source_tier": 3, "champion_tier": 1, "stats_scope": "top_champion_tier"}
                        if ranking_only
                        else {"id": "100", "winrate": 0.55, "pickrate": 0.12}
                    ],
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


def _recommend(view: DataSnapshotView, *, bind_checks: bool = True) -> dict[str, object]:
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
    snapshot = view
    if bind_checks:
        class CheckedView:
            def status(self):
                status = view.status()
                status["source_status"] = {
                    source: {
                        **value.to_dict(),
                        "check_status": "up_to_date" if value.freshness == "fresh" else "failed",
                        "check_evidence_bound": True,
                        "upstream_revision": "revision-1",
                        "applied_revision": "revision-1",
                    }
                    for source, value in view.manifest.source_status.items()
                }
                return status

            def __getattr__(self, name):
                return getattr(view, name)

        snapshot = CheckedView()  # type: ignore[assignment]
    return dict(RecommendationService().build(context, snapshot, vision=vision).augment_slots[0])


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


def test_retired_blitz_is_not_used_when_aramkit_has_no_numeric_stats() -> None:
    row = _recommend(
        _view(
            source_status={
                "blitz": SourceStatusV2(freshness="fresh", data_status="fresh", run_id="blitz-new"),
                "aramkit": SourceStatusV2(freshness="last_good", data_status="data_stale", run_id="aram-old"),
            },
            ranking_only=True,
        )
    )

    assert row["status_code"] == "SOURCE_STAT_MISSING"
    assert not row["stats"]


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

    assert row["status_code"] == "STATS_STALE"
    assert row["data_reason"] == "candidate_rejected_last_good_preserved"
    assert row["synergy_data_status"] == "ready"


def test_old_manifest_without_source_status_falls_back_to_aggregate_health() -> None:
    row = _recommend(_view(source_status=None, degraded=True))

    assert row["status_code"] == "STATS_STALE"
    assert row["source_freshness"] == "unknown"


def test_present_but_unknown_hextech_status_never_claims_fresh_data() -> None:
    row = _recommend(
        _view(source_status={"hextech": SourceStatusV2()}, degraded=False),
        bind_checks=False,
    )

    assert row["status_code"] == "STATS_STALE"


def test_present_but_unknown_synergy_status_never_claims_ready() -> None:
    row = _recommend(
        _view(
            source_status={
                "hextech": SourceStatusV2(freshness="fresh", data_status="fresh"),
                "apex": SourceStatusV2(),
                "mayhem": SourceStatusV2(freshness="fresh", data_status="fresh"),
            },
            degraded=False,
        ),
        bind_checks=False,
    )

    assert row["synergy_data_status"] == "degraded"


def test_renderer_keeps_expired_numeric_stats_without_age_notice() -> None:
    data_at = (datetime.now(timezone.utc) - timedelta(hours=76, minutes=30)).isoformat(timespec="seconds")
    model = build_render_model(
        {
            "active": True,
            "source": {"generation_id": "g-stale"},
            "slots": [{"slot": 0, "state": "ready", "augment_id": "illusory_weapons", "name": "虚幻武器"}],
        },
        hint_cache={
            "snapshot": {
                "state": "degraded",
                "generation_id": "g-stale",
                "source_status": {
                    "hextech": {
                        "freshness": "last_good",
                        "data_status": "data_stale",
                        "data_reason": "source_data_expired",
                        "data_at": data_at,
                    }
                },
            },
            "source": {"private_policy_stats_enabled": True},
            "hints": {
                "illusory_weapons": {
                    "name": "虚幻武器",
                    "stats_by_champion_id": {"63": {"winrate": 0.55, "pickrate": 0.12}},
                }
            },
        },
        context={"ok": True, "champion_id": "63", "champion_name": "复仇焰魂"},
    )

    row = model["stats"][0]
    assert row["status_code"] == "GENERATION_DEGRADED"
    assert row["stats_text"] == "胜率 55.0% · 出场 12.0%"
    assert row["status_text"] == ""
    assert row["winrate_text"] == "55.0%"
    assert row["pickrate_text"] == "12.0%"
    assert model.get("data_notice") is None


def test_stats_stale_text_never_invents_age_for_invalid_or_future_time() -> None:
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)

    assert _stats_stale_text("source_data_expired", "invalid", now=now) == "统计数据暂非最新"
    assert _stats_stale_text("source_data_expired", "2026-08-24T00:00:00+00:00", now=now) == "统计数据暂非最新"
    assert _stats_stale_text("candidate_rejected_last_good_preserved", "2026-08-20T00:00:00+00:00", now=now) == "统计数据暂非最新"


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


def test_renderer_displays_ranking_without_inventing_percentages() -> None:
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
                "source_status": {"blitz": {"freshness": "fresh", "data_status": "fresh"}},
            },
            "source": {"private_policy_stats_enabled": True},
            "hints": {
                "illusory_weapons": {
                    "name": "虚幻武器",
                    "stats_by_champion_id": {"63": {"source_tier": 3, "champion_tier": 1}},
                }
            },
        },
        context={"ok": True, "champion_id": "63", "champion_name": "复仇焰魂"},
    )

    assert model["stats"][0]["status_code"] == "READY"
    assert model["stats"][0]["stats_text"] == "该英雄 T1 · 全局 T3"
    assert model["stats"][0]["winrate_text"] == ""
    assert model["stats"][0]["pickrate_text"] == ""


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

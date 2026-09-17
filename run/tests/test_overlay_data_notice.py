"""年龄/切代不出现在 Canvas，但真实缺失和损坏不被掩盖。"""
import pytest

from hextech.interfaces.overlay.data_notice import build_data_notice


@pytest.mark.parametrize("name", ["aramkit", "hextech", "blitz"])
def test_oldness_is_diagnostic_only(name):
    snapshot = {"source_status": {name: {
        "freshness": "last_good", "data_status": "data_stale",
        "data_reason": "source_data_expired", "data_at": "2020-01-01T00:00:00Z",
    }}}
    assert build_data_notice(snapshot, stats_scope={"new_generation_available": True}, rows=[{
        "stats_source": "aramkit", "data_status": "stale", "source_data_at": "2020-01-01T00:00:00Z",
    }]) is None


@pytest.mark.parametrize("reason", ["source_missing", "snapshot_corrupt", "payload_invalid", "source_unavailable"])
def test_actual_unavailable_notice_keeps_cause_without_age(reason):
    result = build_data_notice({"source_status": {"aramkit": {
        "data_status": "unavailable", "data_reason": reason, "data_at": "2020-01-01T00:00:00Z",
    }}})
    assert result["reason"] == reason
    assert result["state"] == "unavailable"
    assert result["age_seconds"] is None
    assert result["text"] == "统计暂不可用"


def test_verified_old_blitz_ranking_remains_visible_without_age_text():
    from hextech.interfaces.overlay.renderer import build_render_model
    model = build_render_model(
        {"active": True, "slots": [{"slot": 0, "state": "ready", "augment_id": "one", "name": "海克斯"}]},
        hint_cache={
            "snapshot": {"state": "degraded", "source_status": {"blitz": {
                "freshness": "last_good", "data_status": "data_stale",
                "data_reason": "source_data_expired", "data_at": "2020-01-01T00:00:00Z",
            }}},
            "source": {"private_policy_stats_enabled": True},
            "hints": {"one": {"name": "海克斯", "stats_by_champion_id": {
                "114": {"stats_source": "blitz", "champion_tier": 1, "source_tier": 3},
            }}},
        }, context={"ok": True, "champion_id": "114"},
    )
    row = model["stats"][0]
    assert row["stats_text"] == "该英雄 T1 · 全局 T3"
    assert row["status_code"] == "GENERATION_DEGRADED"
    assert row["status_text"] == ""
    assert not model.get("data_notice")

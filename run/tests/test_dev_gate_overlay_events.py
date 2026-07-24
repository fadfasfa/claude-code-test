"""overlay 域 pytest 开发门禁。"""

import pytest

from tests._dev_gate_support import (
    Path,
    RUN_DIR,
    TemporaryDirectory,
    time,
)

pytestmark = [pytest.mark.dev_gate, pytest.mark.overlay]

def test_overlay_event_channel_contract() -> None:
    """验证 overlay 本地事件通道可写、可读、可诊断，且固定为三槽位。"""
    import hextech.modules.vision.events as overlay_event_channel

    hint_cache = {
        "schema_version": 1,
        "generated_at": time.time(),
        "source": {"tag": "dev-check", "private_policy_stats_enabled": False},
        "hints": {
            "augment_001": {
                "augment_id": "augment_001",
                "name": "珠光护手",
                "tier": "Gold",
                "summary": "技能可以暴击。",
            }
        },
    }
    event = overlay_event_channel.build_overlay_event(
        [
            {
                "slot": 0,
                "augment_id": "augment_001",
                "acceptance_rule": "observed_name",
                "channels": {
                    "observed_name": {
                        "margin": 0.11,
                        "top_candidates": [
                            {"augment_id": "augment_001", "name": "珠光护手", "confidence": 0.97}
                        ],
                    }
                },
            }
        ],
        source_tag="dev-check",
        hint_cache=hint_cache,
    )
    event["_acceptance_rules"] = ["observed_name:2", "", ""]
    assert event["schema_version"] == overlay_event_channel.SCHEMA_VERSION
    assert event["source"]["tag"] == "dev-check"
    assert event["active"] is True
    assert event["selection_type"] == "hextech"
    assert len(event["slots"]) == 3
    assert event["slots"][0]["name"] == "珠光护手"
    assert event["slots"][0]["summary"] == "技能可以暴击。"
    assert event["slots"][1]["state"] == "empty"

    with TemporaryDirectory() as tmp_dir:
        event_path = Path(tmp_dir) / "overlay-event.json"
        overlay_event_channel.write_overlay_event(event, event_path)
        snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert snapshot["ok"] is True
        assert snapshot["visible"] is True
        assert snapshot["selection_type"] == "hextech"
        assert len(snapshot["slots"]) == 3
        assert snapshot["slots"][0]["name"] == "珠光护手"
        assert snapshot["slots"][0]["acceptance_rule"] == "observed_name"
        assert snapshot["slots"][0]["channels"]["observed_name"]["margin"] == 0.11
        assert snapshot["_acceptance_rules"] == ["observed_name:2", "", ""]

        zero_ready_event = overlay_event_channel.build_overlay_event(
            [
                {"slot": 0, "state": "detecting"},
                {"slot": 1, "state": "detecting"},
                {"slot": 2, "state": "detecting"},
            ],
            source_tag="dev-check",
            active=False,
        )
        zero_ready_event["source"].update(
            {
                "selection_window_active": True,
                "ready_slots": 0,
                "content_ready": False,
            }
        )
        overlay_event_channel.write_overlay_event(zero_ready_event, event_path)
        zero_ready_snapshot = overlay_event_channel.read_overlay_event(event_path)
        assert zero_ready_snapshot["ok"] is True
        assert zero_ready_snapshot["visible"] is False
        assert zero_ready_snapshot["active"] is False
        assert zero_ready_snapshot["source"]["selection_window_active"] is True
        assert zero_ready_snapshot["source"]["ready_slots"] == 0
        assert overlay_event_channel.EVENT_HEARTBEAT_SECONDS == 1.0
        assert overlay_event_channel.EVENT_STALE_HEARTBEAT_BUDGET == 2.5
        assert overlay_event_channel.EVENT_MAX_AGE_SECONDS == (
            overlay_event_channel.EVENT_HEARTBEAT_SECONDS
            * overlay_event_channel.EVENT_STALE_HEARTBEAT_BUDGET
        )

        fake_path = Path(tmp_dir) / "fake-detection.json"
        fake_written = overlay_event_channel.write_fake_detection_overlay_event(fake_path)
        assert fake_written == fake_path
        fake_snapshot = overlay_event_channel.read_overlay_event(fake_path)
        assert fake_snapshot["ok"] is True
        assert fake_snapshot["visible"] is True
        assert fake_snapshot["source"]["tag"] == "fake-detection"
        assert [slot["state"] for slot in fake_snapshot["slots"]] == ["ready", "ready", "ready"]
        assert fake_snapshot["slots"][0]["name"] == "假识别海克斯 A"

        body_shard_event = overlay_event_channel.build_overlay_event(
            [{"slot": 0, "name": "锻体样例", "summary": "锻体碎片选择使用同一通道，不复用海克斯文案。"}],
            source_tag="dev-check",
            selection_type="锻体碎片选择",
            active=True,
        )
        body_shard_path = Path(tmp_dir) / "body-shard.json"
        overlay_event_channel.write_overlay_event(body_shard_event, body_shard_path)
        body_shard_snapshot = overlay_event_channel.read_overlay_event(body_shard_path)
        assert body_shard_snapshot["ok"] is True
        assert body_shard_snapshot["visible"] is False
        assert body_shard_snapshot["selection_type"] == "body_shard"
        assert body_shard_snapshot["selection_label"] == "锻体碎片选择"

        inactive_path = Path(tmp_dir) / "inactive.json"
        overlay_event_channel.write_inactive_overlay_event(inactive_path)
        inactive_snapshot = overlay_event_channel.read_overlay_event(inactive_path)
        assert inactive_snapshot["ok"] is True
        assert inactive_snapshot["visible"] is False

        missing_snapshot = overlay_event_channel.read_overlay_event(Path(tmp_dir) / "missing.json")
        assert missing_snapshot["ok"] is False
        assert missing_snapshot["visible"] is False
        assert missing_snapshot["error"] == "event_missing"

        damaged_path = Path(tmp_dir) / "damaged.json"
        damaged_path.write_text("{bad-json", encoding="utf-8")
        damaged_snapshot = overlay_event_channel.read_overlay_event(damaged_path)
        assert damaged_snapshot["ok"] is False
        assert damaged_snapshot["error"] == "event_damaged"

        expired_event = dict(event)
        expired_event["generated_at"] = time.time() - overlay_event_channel.EVENT_MAX_AGE_SECONDS - 1
        expired_path = Path(tmp_dir) / "expired.json"
        overlay_event_channel.write_overlay_event(expired_event, expired_path)
        expired_snapshot = overlay_event_channel.read_overlay_event(expired_path)
        assert expired_snapshot["ok"] is False
        assert expired_snapshot["error"] == "event_expired"

        unknown_event = dict(event)
        unknown_event["selection_type"] = "legacy-unknown"
        unknown_path = Path(tmp_dir) / "unknown-selection.json"
        overlay_event_channel.write_overlay_event(unknown_event, unknown_path)
        unknown_snapshot = overlay_event_channel.read_overlay_event(unknown_path)
        assert unknown_snapshot["ok"] is False
        assert unknown_snapshot["visible"] is False
        assert unknown_snapshot["error"] == "selection_type_unknown"

    module_text = (RUN_DIR / "src" / "hextech" / "modules" / "vision" / "events.py").read_text(encoding="utf-8")
    assert "requests" not in module_text
    assert "cv2" not in module_text
    assert "from processing.runtime_store" not in module_text
    assert "import processing.runtime_store" not in module_text
    assert "from processing.overlay_hint_cache" not in module_text
    assert "from hextech.modules.vision.runtime_paths import overlay_runtime_state_path" in module_text
    assert "def _overlay_runtime_root_dir" not in module_text
    assert "def _overlay_runtime_state_path" not in module_text

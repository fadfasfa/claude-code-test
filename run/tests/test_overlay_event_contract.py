"""验证 Overlay v3 事件兼容、缺失原因和渲染文案。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hextech.interfaces.overlay.renderer import build_render_model
from hextech.modules.vision.events import (
    SCHEMA_VERSION,
    build_overlay_event,
    read_overlay_event,
    write_overlay_event,
)


@pytest.mark.parametrize(
    ("reason", "status_code", "status_text"),
    [
        ("recognition_missing", "RECOGNITION_MISSING", "识别未完成"),
        ("identity_unresolved", "IDENTITY_UNRESOLVED", "无法关联统计 ID"),
        ("source_stat_missing", "SOURCE_STAT_MISSING", "公开来源未提供此海克斯统计"),
        ("champion_stat_missing", "CHAMPION_STAT_MISSING", "该英雄暂无此海克斯样本"),
        ("context_missing", "CONTEXT_MISSING", "等待当前英雄"),
        ("snapshot_unavailable", "SNAPSHOT_UNAVAILABLE", "数据准备中"),
    ],
)
def test_explicit_data_reason_maps_to_specific_overlay_copy(
    reason: str,
    status_code: str,
    status_text: str,
) -> None:
    snapshot = build_overlay_event(
        [
            {
                "slot": 0,
                "state": "ready",
                "augment_id": "augment-a",
                "name": "测试海克斯",
                "data_reason": reason,
            }
        ],
        selection_type="hextech",
    )

    model = build_render_model(
        snapshot,
        hint_cache={},
        context={"ok": True, "champion_id": "1", "champion_name": "测试英雄"},
    )

    assert model["stats"][0]["status_code"] == status_code
    assert model["stats"][0]["status_text"] == status_text


def test_v2_event_reads_compatibly_and_writes_as_v3_with_identity_fields(tmp_path: Path) -> None:
    event_path = tmp_path / "legacy-event.json"
    legacy = {
        "schema_version": 2,
        "generated_at": time.time(),
        "source": {"tag": "legacy"},
        "active": True,
        "selection_type": "hextech",
        "slots": [
            {
                "slot": 0,
                "state": "ready",
                "augment_id": "legacy-a",
                "name": "旧海克斯",
                "status_code": "SOURCE_STATS_MISSING",
                "visual_variant_id": "vision-a",
            }
        ],
    }
    event_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    read = read_overlay_event(event_path)
    assert read["schema_version"] == 2
    assert read["slots"][0]["data_reason"] == "source_stat_missing"

    write_overlay_event(legacy, event_path)
    persisted = json.loads(event_path.read_text(encoding="utf-8"))
    slot = persisted["slots"][0]

    assert persisted["schema_version"] == SCHEMA_VERSION == 3
    assert persisted["timing"]["event_written_at"] > 0
    assert slot["data_reason"] == "source_stat_missing"
    assert {
        "data_status",
        "generation_id",
        "vision_id",
        "canonical_id",
        "champion_id",
    }.issubset(slot)
    assert slot["vision_id"] == "vision-a"


def test_v3_event_preserves_cursor_slots_timing_and_build_identity(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event = build_overlay_event([], selection_type="hextech")
    event["source"]["cursor_over_slots"] = [0, 2]
    event["timing"]["captured_at"] = 123.0

    write_overlay_event(event, event_path)
    read = read_overlay_event(event_path)

    assert read["source"]["cursor_over_slots"] == [0, 2]
    assert read["timing"]["captured_at"] == 123.0
    assert read["timing"]["event_written_at"] > 0
    assert read["build_id"]

"""验证真机 Overlay 结构化会话报告的落盘与轮转。"""

from __future__ import annotations

import json
from pathlib import Path


def _snapshot(index: int) -> dict:
    return {
        "schema_version": 3,
        "ok": True,
        "visible": False,
        "active": True,
        "error": "",
        "selection_type": "hextech",
        "source": {
            "tag": "fixture",
            "session_id": f"session-{index}",
            "generation_id": f"generation-{index}",
        },
        "slots": [
            {
                "slot": 0,
                "state": "ready",
                "name": "测试海克斯",
                "data_status": "NO_STATS",
                "data_reason": "champion_stat_missing",
                "generation_id": f"generation-{index}",
                "vision_id": "vision-a",
                "canonical_id": "augment-a",
                "champion_id": "1",
            }
        ],
    }


def test_overlay_sessions_keep_latest_and_only_twenty_structured_reports(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from hextech.interfaces.overlay import host_sync

    monkeypatch.setattr(host_sync, "get_var_dir", lambda: tmp_path)
    visibility: dict[str, object] = {}
    for index in range(21):
        path = host_sync._write_overlay_session_report(
            _snapshot(index),
            {"stats": [{"slot": 0, "status_code": "CHAMPION_STAT_MISSING", "status_text": "英雄暂无统计"}]},
            visibility,
            context={"ok": True, "champion_id": "1"},
            diagnostic=index == 20,
        )
        assert path is not None

    report_dir = tmp_path / "reports" / "overlay_sessions"
    reports = sorted(report_dir.glob("overlay-session-*.json"))
    latest = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))

    assert len(reports) == 20
    assert {
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in reports
    } == {f"session-{index}" for index in range(1, 21)}
    assert latest["diagnostic"] is True
    assert latest["slots"][0]["data_reason"] == "champion_stat_missing"
    assert latest["render"]["rows"][0]["status_code"] == "CHAMPION_STAT_MISSING"
    assert latest["screenshot"] == ""

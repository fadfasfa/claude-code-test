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
            "selection_epoch": index + 1,
            "selection_revision": 2,
            "scene_state": "active",
            "selection_window_active": True,
            "scene_temporal_state": "stable",
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


def test_overlay_sessions_keep_latest_and_bounded_structured_reports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay import report_writer as report_writer_module
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    # 真实上限必须容得下整局证据（一局实测约 58 份报告，200 ≥ 3 局）；
    # 修剪机制本身用小上限验证，避免测试写两百个文件。
    assert report_writer_module.OVERLAY_SESSION_REPORT_LIMIT == 200
    monkeypatch.setattr(report_writer_module, "OVERLAY_SESSION_REPORT_LIMIT", 20)

    writer = OverlayReportWriter(
        tmp_path / "reports" / "overlay_sessions",
        tmp_path / "state" / "session_evidence",
        max_queue=64,
    )
    writer.start()
    visibility: dict[str, object] = {"report_writer": writer}
    try:
        for index in range(21):
            accepted = host_sync._write_overlay_session_report(
                _snapshot(index),
                {"stats": [{"slot": 0, "status_code": "CHAMPION_STAT_MISSING", "status_text": "英雄暂无统计"}]},
                visibility,
                context={"ok": True, "champion_id": "1"},
                diagnostic=index == 20,
            )
            assert accepted
        assert writer.wait_empty()
    finally:
        writer.close()

    report_dir = tmp_path / "reports" / "overlay_sessions"
    reports = sorted(report_dir.glob("overlay-session-*.json"))
    latest = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))

    assert len(reports) == 20
    assert {
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in reports
    } == {f"session-{index}" for index in range(1, 21)}
    assert latest["diagnostic"] is True
    assert latest["schema_version"] == 2
    assert latest["build_id"]
    assert latest["slots"][0]["data_reason"] == "champion_stat_missing"
    assert latest["source"]["selection_epoch"] == 21
    assert latest["source"]["selection_revision"] == 2
    assert latest["source"]["scene_temporal_state"] == "stable"
    assert latest["render"]["rows"][0]["status_code"] == "CHAMPION_STAT_MISSING"
    assert latest["screenshot"] == ""
    assert latest["timing"]["report_written_at"] >= latest["timing"]["report_enqueued_at"]


def test_report_slots_fill_data_fields_from_model_and_context(tmp_path: Path) -> None:
    """生产 vision 槽从不携带数据层字段；报告必须从 model/context 兜底填充。"""

    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    snapshot = _snapshot(1)
    # 还原生产形态：vision 槽只有识别身份，没有任何数据层字段。
    snapshot["slots"] = [
        {
            "slot": 0,
            "state": "ready",
            "name": "测试海克斯",
            "augment_id": "vision-augment-a",
        }
    ]
    model = {
        "stats": [
            {
                "slot": 0,
                "status_code": "READY",
                "status_text": "",
                "hint_id": "canonical-augment-a",
            }
        ]
    }
    writer = OverlayReportWriter(tmp_path / "reports" / "overlay_sessions", tmp_path / "evidence")
    writer.start()
    visibility: dict[str, object] = {"report_writer": writer}
    try:
        assert host_sync._write_overlay_session_report(
            snapshot,
            model,
            visibility,
            context={"ok": True, "champion_id": "59"},
        )
        assert writer.wait_empty()
    finally:
        writer.close()

    latest = json.loads((tmp_path / "reports" / "overlay_sessions" / "latest.json").read_text(encoding="utf-8"))
    slot = latest["slots"][0]
    assert slot["vision_id"] == "vision-augment-a"
    assert slot["canonical_id"] == "canonical-augment-a"
    assert slot["champion_id"] == "59"
    assert slot["data_status"] == "READY"
    assert slot["generation_id"] == "generation-1"


def test_report_submission_does_not_write_on_calling_thread(tmp_path: Path) -> None:
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    report_dir = tmp_path / "reports" / "overlay_sessions"
    writer = OverlayReportWriter(report_dir, tmp_path / "evidence")
    visibility: dict[str, object] = {"report_writer": writer}

    assert host_sync._write_overlay_session_report(_snapshot(1), None, visibility)
    assert not report_dir.exists()

    writer.start()
    try:
        assert writer.wait_empty()
    finally:
        writer.close()
    assert (report_dir / "latest.json").is_file()


def test_report_signature_keeps_revision_changes_in_same_epoch(tmp_path: Path) -> None:
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    writer = OverlayReportWriter(tmp_path / "reports", tmp_path / "evidence")
    visibility: dict[str, object] = {"report_writer": writer}
    first = _snapshot(1)
    second = _snapshot(1)
    second["source"]["selection_revision"] = 3

    assert host_sync._write_overlay_session_report(first, None, visibility)
    assert host_sync._write_overlay_session_report(second, None, visibility)
    assert writer.status()["queue_depth"] == 2


def test_report_queue_coalesces_duplicates_and_keeps_latest_when_full(tmp_path: Path) -> None:
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    writer = OverlayReportWriter(tmp_path / "reports", tmp_path / "evidence", max_queue=2)
    assert writer.submit_session({"value": 1}, key="same")
    assert writer.submit_session({"value": 2}, key="same")
    assert writer.submit_session({"value": 3}, key="second")
    assert writer.submit_session({"value": 4}, key="latest")

    status = writer.status()
    assert status["queue_depth"] == 2
    assert status["coalesced_count"] == 1
    assert status["dropped_count"] == 1

    writer.start()
    try:
        assert writer.wait_empty()
    finally:
        writer.close()
    latest = json.loads((tmp_path / "reports" / "latest.json").read_text(encoding="utf-8"))
    assert latest["value"] == 4

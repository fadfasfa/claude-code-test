"""验证真机 Overlay 结构化会话报告的落盘与轮转。"""

from __future__ import annotations

import json
import time
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
            "vision_pool_generation_id": "vision-pool-1",
            "selection_epoch": index + 1,
            "selection_revision": 2,
            "scene_state": "active",
            "selection_window_active": True,
            "scene_temporal_state": "stable",
            "dpi_scale": 1.5,
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
                "slot_generation": 3,
                "temporal_state": "confirmed",
                "replacement_reason": "ocr_exact_transition",
                "rejection_reason": "",
                "diagnostic": "ocr_exact_fallback",
            }
        ],
    }


def test_overlay_sessions_keep_latest_and_bounded_structured_reports(
    tmp_path: Path,
) -> None:
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay import report_writer as report_writer_module
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    # 真实上限必须容得下整局证据（一局实测约 58 份报告，200 ≥ 3 局）。
    # writer 只 append；目录枚举与淘汰统一由 diagnostic_retention 执行。
    assert report_writer_module.OVERLAY_SESSION_REPORT_LIMIT == 200

    writer = OverlayReportWriter(
        tmp_path / "reports" / "overlay_sessions",
        tmp_path / "state" / "session_evidence",
        max_queue=64,
    )
    writer.start()
    visibility: dict[str, object] = {
        "report_writer": writer,
        "typography": {
            "dpi_scale": 1.5,
            "stats_pixel_size": 32,
            "synergy_title_px": 24,
        },
    }
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

    assert len(reports) == 21
    assert {
        json.loads(path.read_text(encoding="utf-8"))["session_id"]
        for path in reports
    } == {f"session-{index}" for index in range(21)}
    assert latest["diagnostic"] is True
    assert latest["schema_version"] == 2
    assert latest["build_id"]
    assert latest["slots"][0]["data_reason"] == "champion_stat_missing"
    assert latest["slots"][0]["slot_generation"] == 3
    assert latest["slots"][0]["temporal_state"] == "confirmed"
    assert latest["slots"][0]["replacement_reason"] == "ocr_exact_transition"
    assert latest["slots"][0]["diagnostic"] == "ocr_exact_fallback"
    assert latest["source"]["selection_epoch"] == 21
    assert latest["source"]["selection_revision"] == 2
    assert latest["source"]["scene_temporal_state"] == "stable"
    assert latest["source"]["dpi_scale"] == 1.5
    assert latest["render"]["rows"][0]["status_code"] == "CHAMPION_STAT_MISSING"
    assert latest["render"]["typography"]["synergy_title_px"] == 24
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
    assert slot["stats_generation_id"] == "generation-1"
    assert slot["vision_pool_generation_id"] == "vision-pool-1"


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


def test_report_keeps_stage_scope_sample_and_fallback_diagnostics(tmp_path: Path) -> None:
    from hextech.contracts import (
        GameSessionState,
        GenerationId,
        GameSessionId,
        OverlayVisibility,
        PresentationMode,
        RecommendationModel,
        SessionPhase,
    )
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    recommendation = RecommendationModel(
        generation_id=GenerationId("generation-1"),
        session_id=GameSessionId("session-1"),
        observed_at=1.0,
        augment_slots=(
            {
                "slot": 0,
                "state": "ready",
                "canonical_id": "100",
                "data_status": "ready",
                "stats_source": "aramkit",
                "stats_scope": "stage_3",
                "requested_stage": 3,
                "stats_fallback_reason": "",
                "stats": {"sample_count": 578, "sample_quality": "low"},
            },
        ),
    )
    state = GameSessionState(
        session_id=GameSessionId("session-1"),
        generation_id=GenerationId("generation-1"),
        observed_at=1.0,
        phase=SessionPhase.READY,
        visibility=OverlayVisibility(True, True, True, PresentationMode.CONTENT, "ready"),
        recommendation=recommendation,
    )
    writer = OverlayReportWriter(tmp_path / "reports", tmp_path / "evidence")
    writer.start()
    visibility: dict[str, object] = {
        "report_writer": writer,
        "pinned_stats_scope": {
            "status": "ready",
            "stage": 3,
            "aramkit_run_id": "aramkit-run",
            "stage_context": {"reason": "stage_context_conflict"},
        },
    }
    snapshot = _snapshot(0)
    snapshot["slots"] = [{"slot": 0, "state": "ready", "augment_id": "vision-100"}]
    try:
        assert host_sync._write_overlay_session_report(
            snapshot,
            {
                "stage_indicator": {"stage": 3, "label": "阶段 3"},
                "stats": [
                    {
                        "slot": 0,
                        "status_code": "READY",
                        "status_text": "",
                        "stats_tone": "low_sample",
                        "low_sample_outline": False,
                    }
                ],
            },
            visibility,
            context={"ok": True, "champion_id": "1", "player_level": 11},
            state=state,
        )
        assert writer.wait_empty()
    finally:
        writer.close()

    latest = json.loads((tmp_path / "reports" / "latest.json").read_text(encoding="utf-8"))
    row = latest["render"]["rows"][0]
    assert row["stats_source"] == "aramkit"
    assert row["stats_scope"] == "stage_3"
    assert row["requested_stage"] == 3
    assert row["sample_count"] == 578
    assert row["sample_quality"] == "low"
    assert row["stats_tone"] == "low_sample"
    assert row["low_sample_outline"] is False
    assert latest["render"]["stage_indicator"] == {"stage": 3, "label": "阶段 3"}
    assert latest["context"]["player_level"] == 11
    assert latest["stats_scope"]["stage_context"]["reason"] == "stage_context_conflict"


def test_captured_recognition_without_v2_timeline_reports_contract_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    snapshot = _snapshot(0)
    snapshot["build_id"] = "build-v4"
    snapshot["source"]["build_id"] = "build-v4"
    snapshot["source"]["sidecar_instance_id"] = "sidecar-v4"
    snapshot["timing"] = {
        "observation_kind": "recognition",
        "capture_status": "captured",
        "event_written_at": time.time() - 2.0,
    }
    monkeypatch.setattr(host_sync, "get_var_dir", lambda: tmp_path)
    report_dir = tmp_path / "reports"
    writer = OverlayReportWriter(report_dir, tmp_path / "evidence")
    writer.start()
    visibility: dict[str, object] = {"report_writer": writer}
    try:
        assert host_sync._write_overlay_session_report(snapshot, None, visibility)
        assert writer.wait_empty()
        report = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))
        assert report["diagnostic_contract_error"] == "timeline_missing"
    finally:
        writer.close()


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


def test_report_separates_draw_mapped_and_composed_timestamps(tmp_path: Path) -> None:
    from hextech.interfaces.overlay import host_sync
    from hextech.interfaces.overlay.report_writer import OverlayReportWriter

    report_dir = tmp_path / "reports"
    writer = OverlayReportWriter(report_dir, tmp_path / "evidence")
    writer.start()
    visibility: dict[str, object] = {
        "report_writer": writer,
        "window_visible": True,
        "draw_started_at": 10.0,
        "draw_completed_at": 11.0,
        "event_read_started_at": 9.6,
        "event_read_completed_at": 9.61,
        "context_requested_at": 9.62,
        "context_read_started_at": 9.63,
        "context_read_completed_at": 9.7,
        "context_input_sequence": 4,
        "context_input_error": "",
        "context_input_game_instance_id": "session-1",
        "context_input_age_seconds": .04,
        "context_gate_evaluated_at": 9.71,
        "context_confirmed_at": 9.71,
        "game_window_mode_status": "supported",
        "game_window_mode": "borderless",
        "game_window_mode_reason": "window_mode_borderless",
        "game_window_mode_source": "game_cfg",
        "game_window_mode_observed_at": 8.0,
        "capture_exclusion": {
            "status": "applied",
            "requested_affinity": 17,
            "applied_affinity": 17,
            "query_ok": True,
        },
        "presentation": {
            "state": "composed",
            "overlay_hwnd": 123,
            "ws_visible": True,
            "cloaked": False,
            "expected_client_rect": [0, 0, 1920, 1080],
            "actual_rect": [0, 0, 1920, 1080],
            "rect_matches": True,
            "style_checks": {"topmost": True},
            "host_surface_probe": {"state": "matched", "sample_count": 5},
            "composition_probe": {"state": "excluded", "sample_count": 0},
            "mapped_at": 12.0,
            "composition_checked_at": 13.0,
            "presented_at": 13.0,
            "draw_completed_at": 11.0,
            "event_written_at": 9.5,
            "event_session_id": "session-1",
            "event_selection_epoch": 2,
            "event_selection_revision": 1,
            "ready_frame": True,
            "probe_contract": "host_surface_and_capture_exclusion_v1",
        },
    }
    try:
        assert host_sync._write_overlay_session_report(_snapshot(1), None, visibility)
        assert writer.wait_empty()
        composed = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))
        assert composed["presentation"]["state"] == "composed"
        assert composed["timing"]["draw_completed_at"] == 11.0
        assert composed["timing"]["event_read_started_at"] == 9.6
        assert composed["timing"]["event_read_completed_at"] == 9.61
        assert composed["timing"]["context_requested_at"] == 9.62
        assert composed["timing"]["context_read_started_at"] == 9.63
        assert composed["timing"]["context_read_completed_at"] == 9.7
        assert composed["timing"]["context_input_sequence"] == 4
        assert composed["timing"]["context_input_game_instance_id"] == "session-1"
        assert composed["timing"]["context_input_age_seconds"] == .04
        assert composed["timing"]["context_gate_evaluated_at"] == 9.71
        assert composed["timing"]["context_confirmed_at"] == 9.71
        assert composed["timing"]["mapped_at"] == 12.0
        assert composed["timing"]["presented_at"] == 13.0
        assert composed["timing"]["presented_event_written_at"] == 9.5
        assert composed["presentation"]["event_selection_epoch"] == 2
        assert composed["presentation"]["ready_frame"] is True
        assert composed["presentation"]["probe_contract"] == "host_surface_and_capture_exclusion_v1"
        assert composed["presentation"]["capture_exclusion"]["status"] == "applied"
        assert composed["presentation"]["host_surface_probe"]["state"] == "matched"
        assert composed["game_window_mode"] == {
            "status": "supported",
            "mode": "borderless",
            "reason": "window_mode_borderless",
            "source": "game_cfg",
            "observed_at": 8.0,
        }

        presentation = visibility["presentation"]
        assert isinstance(presentation, dict)
        presentation.update(
            {
                "state": "mapped",
                "composition_probe": {"state": "unavailable", "reason": "screen_pixel_api_unavailable"},
                "composition_checked_at": 14.0,
                "presented_at": 0.0,
            }
        )
        assert host_sync._write_overlay_session_report(_snapshot(2), None, visibility)
        assert writer.wait_empty()
    finally:
        writer.close()

    unavailable = json.loads((report_dir / "latest.json").read_text(encoding="utf-8"))
    assert unavailable["presentation"]["state"] == "mapped"
    assert unavailable["presentation"]["composition_probe"]["state"] == "unavailable"
    assert unavailable["timing"]["composition_checked_at"] == 14.0
    assert unavailable["timing"]["presented_at"] == 0.0


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

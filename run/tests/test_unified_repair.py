"""Production-format and bounded evidence regressions for the unified repair."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_font_measurements_are_bounded_and_context_specific():
    from hextech.interfaces.overlay.text_metrics import TextMetrics

    metrics = TextMetrics(SimpleNamespace())
    font = SimpleNamespace(getlength=Mock(side_effect=lambda value: len(value)))
    metrics._fonts[(30, True)] = font
    metrics.bind_context((2560, 1440, 1.0, "anchors-v2"))
    assert metrics.width("胜率 55%", 30, True) == metrics.width("胜率 55%", 30, True)
    assert font.getlength.call_count == 1
    for value in range(2100):
        metrics.width(str(value), 30, True)
    assert len(metrics._widths) == 2048
    metrics.bind_context((1920, 1080, 1.5, "anchors-v2"))
    assert not metrics._widths
    metrics.width("胜率 55%", 30, True)
    assert font.getlength.call_count == 2102


@pytest.mark.parametrize("payload", [[], {}, {"entries": []}, {"entries": [None]}])
def test_mayhem_bad_catalog_cannot_become_empty_success(tmp_path, payload):
    from hextech.infrastructure.sources.mayhem.service import _merge_candidate

    (tmp_path / "海克斯资源目录.v1.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "英雄目录.v1.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="Catalog"):
        _merge_candidate({"items": []}, catalog_root=tmp_path, merge=None)


def test_refresh_attempts_are_bounded_and_do_not_invent_http_denominator(tmp_path):
    from hextech.infrastructure.observability.refresh_attempts import record_refresh_attempt

    for i in range(230):
        record_refresh_attempt(tmp_path, source="aramkit", checked_at=str(i),
            error="timeout" if i % 2 else "", result={}, generation_id="generation")
    path = tmp_path / "state" / "refresh_attempts.v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["records"]) == 200
    assert payload["records"][0]["checked_at"] == "30"
    assert all(row["http_denominator_complete"] is False for row in payload["records"])
    assert path.stat().st_size <= 128 * 1024


@pytest.mark.parametrize("state,expected", [
    ("pending", "SOURCE_UNAVAILABLE"), ("unavailable", "SOURCE_UNAVAILABLE"),
    ("confirmed_empty", "CONFIRMED_EMPTY"), ("fresh", "NO_MATCH"),
])
def test_synergy_absence_has_a_source_reason(state, expected):
    from hextech.interfaces.overlay.renderer import _synergy_status

    assert _synergy_status(matched=None, context={"ok": True}, snapshot_status={
        "state": "ready", "source_status": {
            "apex": {"data_status": state}, "mayhem": {"data_status": state},
        },
    }) == expected


def test_retired_blitz_does_not_create_overlay_failure_notice():
    from hextech.interfaces.overlay.data_notice import build_data_notice

    assert build_data_notice({"source_status": {"blitz": {
        "data_status": "unavailable", "data_reason": "source_missing",
    }}}) is None


def test_draw_phase_evidence_binds_to_draw_not_next_tick():
    from hextech.interfaces.overlay.host_presentation import mark_canvas_drawn, presentation_status

    visibility = {"draw_phases_ms": {"canvas_update": 4.5}}
    mark_canvas_drawn(visibility, completed_at=10)
    visibility["draw_phases_ms"] = {"canvas_update": 999}
    assert presentation_status(visibility)["bound_draw_phases_ms"] == {"canvas_update": 4.5}


def test_http_metrics_separate_retry_cache_and_success():
    from hextech.infrastructure.sources.aramkit.http_metrics import measure_http

    responses = iter(((503, "{}"), (200, "{}"), (304, "")))

    @measure_http(lambda *_args, **_kwargs: next(responses))
    def check(*, fetcher):
        fetcher("https://fixture/data")
        fetcher("https://fixture/data")
        fetcher("https://fixture/versions")
        fetcher.record_cache_hit()
        return {"success": True}

    summary = check()["http_summary"]
    assert summary == {"requests": 3, "failed_requests": 1, "retries": 1,
                       "not_modified": 1, "raw_cache_hits": 1, "repeated_url_requests": 1}


def test_failed_evidence_save_is_visible_and_repeated_logs_are_coalesced(tmp_path, monkeypatch):
    from hextech.infrastructure.vision import failure_evidence as module
    from test_selection_capture_cache import build_draft

    monkeypatch.setattr(module, "persist_selection_capture",
                        Mock(side_effect=ValueError("selection_cache_budget_exhausted")))
    writer = module.FailureEvidenceWriter(cache_root=tmp_path)
    try:
        for _ in range(3):
            assert writer.submit(build_draft())
            assert writer.wait_empty()
        status = writer.status()
        assert status["failed"] == 3
        assert status["suppressed_error_logs"] == 2
        assert status["selection_cache"]["save_state"] == "not_saved"
        assert status["selection_cache"]["unsaved_count"] == 3
        assert len(writer.drain_journal_events()) == 1
    finally:
        writer.close()


def test_equal_priority_anomaly_rotation_protects_manual_assets(tmp_path):
    from dataclasses import replace
    from uuid import uuid4
    from test_selection_capture_cache import build_draft
    from hextech.infrastructure.vision.selection_capture_persistence import persist_selection_capture

    first = replace(build_draft(), terminal={"reason": "evidence_starved"})
    persist_selection_capture(tmp_path, first, group_limit=1)
    second = replace(first, diagnostic_id=uuid4().hex)
    persist_selection_capture(tmp_path, second, group_limit=1)
    assert not (tmp_path / first.diagnostic_id).exists()
    manifest_path = tmp_path / second.diagnostic_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["labels"] = ["manual-truth"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()
    with pytest.raises(ValueError, match="budget_exhausted"):
        persist_selection_capture(tmp_path, replace(second, diagnostic_id=uuid4().hex), group_limit=1)
    assert manifest_path.read_bytes() == before

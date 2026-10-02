"""Real failure shape: pre-selection level 10 must not pin stage 2 at level 11."""
from types import SimpleNamespace

import pytest

from hextech.interfaces.overlay.stats_pin import SelectionStatsPin
from hextech.infrastructure.vision.selection_capture_cache import classify_selection_evidence


def event(epoch=1):
    return {"source": {"session_id": "sidecar", "game_instance_id": "game", "window_hwnd": 42,
                       "selection_epoch": epoch, "selection_window_active": True}}


def context(level=11, seq=1, start=101., publisher="broker"):
    return {"ok": True, "champion_id": "233", "game_instance_id": "game", "window_hwnd": 42,
            "source": "live-client-data", "player_level": level, "publisher_instance_id": publisher,
            "publication_seq": seq, "sample_started_at": start, "sample_completed_at": start + .01,
            "published_at": start + .02, "generated_at": start + .02}


def pin(clock):
    return SelectionStatsPin(now=lambda: clock[0], wall_now=lambda: clock[0],
        cache=SimpleNamespace(load=lambda *args: SimpleNamespace(available=True, view=None, run_id="unit", reason="")))


VIEW = SimpleNamespace(status=lambda: {"generation_id": "generation"})


@pytest.mark.parametrize("old,new,stage", [(10, 11, 3), (6, 7, 2), (14, 15, 4)])
def test_old_level_and_duplicate_publication_cannot_confirm(old, new, stage):
    clock = [100.]
    scope = pin(clock)
    def resolve(c):
        return scope.resolve(event(), c, VIEW, completed_selection_count=None)
    assert resolve(context(old, start=99.)).status == "preparing"
    clock[0] = 101.1
    assert resolve(context(new)).status == "preparing"
    assert resolve(context(new)).status == "preparing"
    clock[0] = 101.4
    result = resolve(context(new, seq=2, start=101.3))
    assert result.stage == stage and result.frozen
    assert resolve(context(18, seq=3, start=101.35)).stage == stage


def test_timeout_does_not_freeze_old_stage_or_all_stats():
    clock = [100.]
    scope = pin(clock)
    assert scope.resolve(event(), context(10, start=99.), VIEW, completed_selection_count=None).status == "preparing"
    clock[0] = 102.1
    result = scope.resolve(event(), context(10, start=99.), VIEW, completed_selection_count=None)
    assert result.status == "unavailable"
    assert result.reason == "stage_unconfirmed" and result.stage is None and result.scoped_view is None


def test_publisher_restart_and_stage_change_restart_confirmation():
    clock = [100.]
    scope = pin(clock)
    scope.resolve(event(), context(start=99.), VIEW, completed_selection_count=None)
    for c, t in [(context(10, 1, 100.1), 100.2), (context(11, 2, 100.3), 100.4),
                 (context(11, 1, 100.5, "new"), 100.6)]:
        clock[0] = t
        assert scope.resolve(event(), c, VIEW, completed_selection_count=None).status == "preparing"
    clock[0] = 100.9
    assert scope.resolve(event(), context(11, 2, 100.8, "new"), VIEW, completed_selection_count=None).stage == 3


def test_partial_ready_starvation_is_anomaly():
    frame = {"final_classification": {"scene_state": "active", "slots": [
        {"state": "ready"}, {"state": "ready"}, {"state": "detecting", "temporal_state": "evidence_starved"}]}}
    assert classify_selection_evidence([frame], {}) == "anomaly"


def test_never_fully_ready_slow_epoch_is_anomaly():
    frame = {"final_classification": {"scene_state": "active", "unconfirmed_elapsed_ms": 1500,
        "slots": [{"state": "ready"}, {"state": "detecting"}]}}
    assert classify_selection_evidence([frame], {}) == "anomaly"


@pytest.mark.parametrize("change", [{"game_instance_id": "other"}, {"window_hwnd": 43},
    {"source_conflict": True}, {"ok": False}, {"sample_started_at": 0},
    {"sample_completed_at": float("nan")}, {"published_at": 10000}])
def test_invalid_observations_cannot_confirm(change):
    clock = [100.]
    scope = pin(clock)
    scope.resolve(event(), {}, VIEW, completed_selection_count=None)
    clock[0] = 101.1
    assert scope.resolve(event(), {**context(), **change}, VIEW, completed_selection_count=None).status == "preparing"
    clock[0] = 101.4
    assert scope.resolve(event(), context(seq=2, start=101.3), VIEW, completed_selection_count=None).status == "preparing"


def test_bounded_sampling_and_no_retrigger_on_heartbeat_or_old_epoch():
    from hextech.interfaces.overlay.context_poll_schedule import SelectionContextCadence
    cadence = SelectionContextCadence(1.5)
    key = ("game", "sidecar", 5)
    assert cadence.due(10, key)
    cadence.completed(10)
    assert not cadence.due(10.1, key)
    assert cadence.due(10.25, key)
    cadence.completed(12.1)
    assert cadence.fast_until == 12
    assert not cadence.due(12.2, key)
    assert not cadence.due(12.2, ("game", "sidecar", 4))
    assert cadence.due(12.2, ("game", "sidecar", 6))


def test_confirmation_timeout_clears_all_percentages():
    from test_overlay_stage_projection import _state, _view, _record, _context
    from hextech.modules.recommendation.stage_projection import apply_scoped_stage_stats
    from hextech.interfaces.overlay.renderer import build_render_model_from_session
    state = apply_scoped_stage_stats(_state(), stage_context=_context(None),
        scoped_view=_view(stage_record=None, all_record=_record(2000)),
        scope_status="unavailable", scope_reason="stage_unconfirmed")
    row = build_render_model_from_session(state)["stats"][0]
    assert row["stats_text"] == "阶段未确认"
    assert state.recommendation.augment_slots[0]["stats"] == {}


def test_statistics_report_freezes_actual_draw_not_latest_context():
    from hextech.interfaces.overlay.draw_evidence import freeze_draw_statistics
    from test_overlay_stage_projection import _state
    model = {"stats": [{"stats_text": "胜率 53.5% · 出场 2.1%", "status_code": "READY"}]}
    row = {"canonical_id": "test", "stats": {"win_rate": .5349, "pick_rate": .0207, "sample_count": 9206},
           "requested_stage": 3, "source_run_id": "unit", "stats_generation_id": "generation"}
    state = _state()
    from dataclasses import replace
    state = replace(state, recommendation=replace(state.recommendation, augment_slots=(row,)))
    prepared = SimpleNamespace(state=state, model=model, scope={"stage": 3}, context={"publication_seq": 5},
                               event=event(5), input_timing={})
    evidence = freeze_draw_statistics(prepared, model)
    assert evidence["rows"][0]["win_rate"] == .5349
    assert evidence["context"]["publication_seq"] == 5
    other = freeze_draw_statistics(prepared, {"stats": [{"stats_text": "旧画面"}]})
    assert not other["rows"][0]["prepared_binding_valid"]
    assert other["rows"][0]["win_rate"] is None


def test_broker_sample_clocks_are_not_publication_clock(tmp_path):
    from hextech.interfaces.overlay.context_broker import OverlayContextBroker
    from hextech.modules.vision.window import WindowProbeResult
    clock = [100.]
    def lcu(**kwargs):
        clock[0] = 100.1
        return None, "unavailable"
    def live():
        clock[0] = 100.3
        return {"champion_id": "233", "player_level": 11, "source": "live-client-data", "game_time_seconds": 3}, ""
    broker = OverlayContextBroker(context_path=tmp_path / "context.json", now=lambda: clock[0],
        window_probe=lambda: WindowProbeResult(status="found", hwnd=42, game_instance_id="game", process_started_at=90),
        lcu_reader=lcu, live_reader=live)
    assert broker.poll_once()
    assert broker.last_publication["sample_started_at"] == 100.1
    assert broker.last_publication["sample_completed_at"] == 100.3
    assert broker.last_publication["published_at"] == 100.3
    broker.live_reader = lambda: (None, "unavailable")
    broker.poll_once()
    assert "sample_started_at" not in broker.last_publication


def test_mouse_diagnostic_counts_why_edges_were_not_queued():
    from hextech.infrastructure.vision.mouse_transition import MouseTransitionObserver
    down = [True]
    observer = MouseTransitionObserver(button_probe=lambda: down[0], cursor_probe=lambda: None)
    observer.poll_once()
    observer.update_context(window_hwnd=42, game_instance_id="game", selection_epoch=1, eligible=True)
    down[0] = False
    observer.poll_once()
    down[0] = True
    observer.poll_once()
    status = observer.status()
    assert status["edge_count"] == 2 and status["enqueued_count"] == 0
    assert status["drop_reasons"]["ineligible"] == 1
    assert status["drop_reasons"]["missing_cursor"] == 1


@pytest.mark.parametrize("stage,win,pick,samples,text", [
    (2, .5023, .0152, 7274, "胜率 50.2% · 出场 1.5%"),
    (3, .5349, .0207, 9206, "胜率 53.5% · 出场 2.1%"),
])
def test_champion_233_real_unit_percentages_reach_draw_evidence(stage, win, pick, samples, text):
    # Exact numeric rows from unit-a74b48fa7fd771e4661a16a98406fd654f524d51,
    # scoped_stats/champions/233.json, canonical 1322. No reconstructed image/truth.
    from dataclasses import replace
    from types import MappingProxyType
    from test_overlay_stage_projection import _state
    from hextech.modules.data.scoped_stats import ScopedStatsView
    from hextech.modules.game_context.stage_context import StageContextV1
    from hextech.modules.recommendation.stage_projection import apply_scoped_stage_stats
    from hextech.interfaces.overlay.renderer import build_render_model_from_session
    from hextech.interfaces.overlay.draw_evidence import freeze_draw_statistics
    state = _state()
    row = {**state.recommendation.augment_slots[0], "canonical_id": "1322", "canonical_augment_id": "1322"}
    state = replace(state, recommendation=replace(state.recommendation, augment_slots=(row,)))
    view = ScopedStatsView(generation_id="generation", run_id="unit-a74b48fa7fd771e4661a16a98406fd654f524d51",
        champion_id="233", version="16.19", data_path="data/16.19-20260927-05de64d31ef1",
        all_stats=MappingProxyType({}), stage_stats=MappingProxyType({stage: MappingProxyType({"1322": {
            "id": "1322", "sample_count": samples, "win_rate": win, "pick_rate": pick}})}))
    state = apply_scoped_stage_stats(state, stage_context=StageContextV1(champion_id="233", stage=stage, status="ready"),
                                    scoped_view=view, scope_status="ready", scope_reason="")
    model = build_render_model_from_session(state)
    assert model["stats"][0]["stats_text"] == text
    prepared = SimpleNamespace(state=state, model=model, scope={"stage": stage}, event=event())
    evidence = freeze_draw_statistics(prepared, model)["rows"][0]
    assert (evidence["canonical_id"], evidence["win_rate"], evidence["pick_rate"], evidence["sample_count"]) == (
        "1322", win, pick, samples)

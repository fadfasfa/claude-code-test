"""Overlay Host generation、语义键与逐槽 last-good 缓存测试。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest


def _snapshot() -> dict:
    return {
        "active": True,
        "visible": True,
        "revision": 7,
        "source": {
            "session_id": "session-1",
            "selection_epoch": 1,
            "selection_revision": 7,
            "selection_window_active": True,
            "layout_id": "layout-a",
            "dpi_scale": 1.0,
        },
        "slots": [
            {
                "slot_generation": 1,
                "state": "ready",
                "augment_id": f"augment-{index}",
                "name": f"海克斯 {index}",
                "tier": "gold",
            }
            for index in range(3)
        ],
    }


def _model() -> dict:
    return {
        "stage_indicator": {"stage": 1, "label": "阶段 1"},
        "stats": [
            {
                "slot": index,
                "state": "matched",
                "name": f"海克斯 {index}",
                "tier": "gold",
                "stats_text": f"统计 {index}",
                "status_code": "READY",
                "winrate_text": "55.0%",
                "pickrate_text": "3.0%",
                "status_text": "",
                "synergy_status": "SYNERGY_READY",
            }
            for index in range(3)
        ],
        "synergies": [
            {
                "slot": index,
                "augment_name": f"海克斯 {index}",
                "tier": "gold",
                "hero_name": "测试英雄",
                "rating": "S",
                "tag": "联动",
                "content": f"联动 {index}",
            }
            for index in range(3)
        ],
    }


def test_initial_placeholder_canvas_uses_bound_physical_client_rect():
    from hextech.interfaces.overlay.host_render_state import canvas_viewport

    canvas = SimpleNamespace(winfo_width=lambda: 1, winfo_height=lambda: 1)
    assert canvas_viewport(canvas, {}, target_rect=(-2560, -100, 0, 1500)) == (2560, 1600)
    with pytest.raises(ValueError, match="not ready"):
        canvas_viewport(canvas, {"width": 2560, "height": 1600})
    with pytest.raises(ValueError, match="not ready"):
        canvas_viewport(canvas, {}, target_rect=(0, 0, 0, 1440))


def test_first_frame_rejects_invalid_stat_geometry_instead_of_shrinking_to_eight_pixels():
    from hextech.interfaces.overlay.canvas_renderer import resolve_overlay_layout

    for viewport in ((1, 1), (0, 1440), (2560, 0)):
        with pytest.raises(ValueError, match="not ready"):
            resolve_overlay_layout(viewport)


def test_semantic_key_ignores_diagnostics_but_tracks_visible_inputs() -> None:
    from hextech.interfaces.overlay.host_render_state import render_semantic_key

    snapshot = _snapshot()
    baseline = render_semantic_key(
        snapshot,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
    )
    diagnostics_only = deepcopy(snapshot)
    diagnostics_only["captured_at"] = 999.0
    diagnostics_only["slots"][0]["confidence"] = 0.99
    diagnostics_only["slots"][0]["evidence_hits"] = 5
    assert render_semantic_key(
        diagnostics_only,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
    ) == baseline

    changed = deepcopy(snapshot)
    changed["source"]["selection_revision"] = 8
    assert render_semantic_key(
        changed,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
    ) != baseline

    dpi_changed = deepcopy(snapshot)
    dpi_changed["source"]["dpi_scale"] = 1.5
    assert render_semantic_key(
        dpi_changed,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
    ) != baseline

    transform_changed = deepcopy(snapshot)
    transform_changed["source"]["layout_transform"] = {
        "dx_ratio": 0.02,
        "dy_ratio": -0.01,
        "scale": 1.05,
    }
    assert render_semantic_key(
        transform_changed,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
    ) == baseline

    assert render_semantic_key(
        snapshot,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(2560, 1600),
    ) != baseline

    assert render_semantic_key(
        snapshot,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
        stats_scope_key=("stage", 2),
    ) != baseline

    assert render_semantic_key(
        snapshot,
        context_revision=3,
        generation_id="generation-1",
        display_mode="compact",
        viewport=(1920, 1080),
        data_notice_key=(("aramkit", "last_good", "data_stale", "source_data_expired", "t1"),),
    ) != baseline


def test_single_slot_shell_keeps_other_slots_last_good() -> None:
    from hextech.interfaces.overlay.host_render_state import SlotRenderCache

    cache = SlotRenderCache()
    snapshot = _snapshot()
    selection = ("session-1", 1)
    cache.merge(
        snapshot,
        _model(),
        current_selection_key=selection,
        context_revision=3,
        generation_id="generation-1",
    )

    replacement = deepcopy(snapshot)
    replacement["source"]["selection_revision"] = 8
    replacement["slots"][0] = {
        "slot_generation": 2,
        "state": "detecting",
        "augment_id": "",
        "name": "",
        "tier": "",
    }
    shell = cache.build_shell(replacement, current_selection_key=selection)

    assert shell["stats"][0]["state"] == "detecting"
    assert [row["name"] for row in shell["stats"][1:]] == ["海克斯 1", "海克斯 2"]
    assert [row["stats_text"] for row in shell["stats"][1:]] == ["统计 1", "统计 2"]
    assert [row["slot"] for row in shell["synergies"]] == [1, 2]


def test_hint_cache_reads_each_generation_once() -> None:
    from hextech.interfaces.overlay.host_render_state import HostGenerationHintCache

    class View:
        def __init__(self) -> None:
            self.reads = 0

        def status(self) -> dict:
            return {"generation_id": "generation-1"}

        def get_overlay_hints(self) -> dict:
            self.reads += 1
            return {"source": {}, "hints": {"a": {"name": "测试"}}, "name_index": {}}

    view = View()
    cache = HostGenerationHintCache()
    first = cache.resolve(view, lambda: {})
    second = cache.resolve(view, lambda: {})

    assert view.reads == 1
    assert first["hints"] is second["hints"]
    assert second["snapshot"]["generation_id"] == "generation-1"


def test_active_selection_uses_one_frame_event_poll_interval() -> None:
    from hextech.interfaces.overlay.host_platform import build_overlay_window_config
    from hextech.interfaces.overlay.host_render_state import resolve_event_render_delay_ms

    config = build_overlay_window_config()

    assert config["fast_event_poll_ms"] == 16
    assert resolve_event_render_delay_ms(
        config,
        {"selection_window_active": True},
    ) == 16


def test_host_polling_uses_250_idle_and_16_game_millisecond_tiers() -> None:
    from hextech.interfaces.overlay.host_platform import build_overlay_window_config
    from hextech.interfaces.overlay.host_render_state import resolve_event_render_delay_ms

    config = build_overlay_window_config()

    assert config["event_poll_ms"] == 250
    assert config["game_event_poll_ms"] == 16
    assert resolve_event_render_delay_ms(config, {}) == 250
    assert resolve_event_render_delay_ms(config, {"target_hwnd": 100}) == 16
    assert resolve_event_render_delay_ms(
        config,
        {"target_hwnd": 100, "selection_window_active": True},
    ) == 16


def test_stats_scope_change_refreshes_rows_without_vision_revision() -> None:
    from hextech.interfaces.overlay.host_render_state import SlotRenderCache

    cache = SlotRenderCache()
    snapshot = _snapshot()
    selection = ("session-1", 1)
    first = cache.merge(
        snapshot,
        _model(),
        current_selection_key=selection,
        context_revision=3,
        generation_id="generation-1",
        stats_scope_key=("stage", 1),
    )
    updated_model = _model()
    updated_model["stats"][0].update(
        stats_text="胜率 55.0% · 出场 3.0%",
        stats_tone="aggregate",
        low_sample_outline=True,
    )
    updated_model["stage_indicator"] = {"stage": 2, "label": "阶段 2"}
    second = cache.merge(
        snapshot,
        updated_model,
        current_selection_key=selection,
        context_revision=3,
        generation_id="generation-1",
        stats_scope_key=("stage", 2),
    )

    assert snapshot["source"]["selection_revision"] == 7
    assert first["stats"][0]["stats_text"] == "统计 0"
    assert second["stats"][0]["stats_text"] == "胜率 55.0% · 出场 3.0%"
    assert second["stats"][0]["stats_tone"] == "aggregate"
    assert second["stats"][0]["low_sample_outline"] is True
    assert second["stage_indicator"] == {"stage": 2, "label": "阶段 2"}

"""显示锚点不随识别资源变化；未有真机内框证据不授予校准资格。"""
import pytest

from hextech.interfaces.overlay.canvas_renderer import resolve_overlay_layout
from hextech.modules.vision import layout as vision_layout


def test_vision_roi_changes_cannot_move_display_anchors(monkeypatch):
    viewport = (2560, 1440)
    original = resolve_overlay_layout(viewport)
    monkeypatch.setattr(vision_layout, "CARD_PANELS_16_9", ((.1, .1, .3, .6),) * 3)
    assert resolve_overlay_layout(viewport) == original


@pytest.mark.parametrize("viewport,expected", [
    ((2560, 1440), [(557, 862, 933, 934), (1101, 862, 1477, 934), (1647, 862, 2023, 934)]),
    ((2560, 1600), [(557, 916, 933, 988), (1101, 916, 1477, 988), (1647, 916, 2023, 988)]),
    ((1920, 1080), [(417, 646, 699, 700), (825, 646, 1107, 700), (1235, 646, 1517, 700)]),
    ((1920, 1200), [(417, 687, 699, 741), (825, 687, 1107, 741), (1235, 687, 1517, 741)]),
])
def test_separating_geometry_preserves_uncalibrated_baseline(viewport, expected):
    assert resolve_overlay_layout(viewport)["stat_boxes"] == expected


def test_geometry_identity_is_deterministic_and_remains_unqualified():
    from hextech.interfaces.overlay.display_geometry import display_geometry_key, display_geometry_metadata
    assert display_geometry_key((2560, 1440)) == display_geometry_key((2560, 1440))
    assert display_geometry_key((2560, 1440)) != display_geometry_key((2560, 1600))
    for viewport in ((2560, 1440), (1920, 1200), (1280, 720), (3440, 1440)):
        meta = display_geometry_metadata(viewport)
        assert meta["anchor_qualification"] == "pending_real_device"
        assert not meta["calibration_evidence_sha256"]


def test_render_cache_tracks_host_window_context_not_only_vision_dpi():
    from hextech.interfaces.overlay.host_render_state import render_semantic_key
    arguments = dict(context_revision=1, generation_id="g", display_mode="compact", viewport=(2560, 1440))
    context = (42, (0, 0, 2560, 1440), "DISPLAY2", 1.0)
    baseline = render_semantic_key({}, **arguments, display_context_key=context)
    assert render_semantic_key({}, **arguments, display_context_key=context) == baseline
    for changed in ((43, context[1], "DISPLAY2", 1.0),
                    (42, (-2560, 0, 0, 1440), "DISPLAY1", 1.0),
                    (42, context[1], "DISPLAY2", 1.5)):
        assert render_semantic_key({}, **arguments, display_context_key=changed) != baseline

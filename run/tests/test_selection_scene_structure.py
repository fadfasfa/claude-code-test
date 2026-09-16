"""Structural scene regressions. Generated colours are not real-device acceptance."""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from hextech.modules.vision.layout import (
    LayoutTransform, _aligned_panel_score, _panel_score, apply_transform, detect_selection_scene, pick_card_panels,
)


def panel(color=(195, 195, 200), *, sides="ltrb", fill=(5, 15, 18)):
    image = Image.new("RGB", (200, 400), fill)
    draw = ImageDraw.Draw(image)
    for side, box in {"l": (0, 0, 8, 399), "r": (191, 0, 199, 399),
                      "t": (0, 0, 199, 8), "b": (0, 391, 199, 399)}.items():
        if side in sides:
            draw.rectangle(box, fill=color)
    return image


@pytest.mark.parametrize("color", [(195, 195, 200), (190, 160, 95), (135, 185, 240), (195, 130, 205)])
def test_border_hue_does_not_gate_scene(color):
    assert _panel_score(panel(color), (0, 0, 200, 400)) >= 0.58


@pytest.mark.parametrize("sides", ["", "l", "r", "t", "b", "lr", "tb", "lt", "rb"])
def test_dark_background_and_partial_edges_are_not_panels(sides):
    assert _panel_score(panel(sides=sides), (0, 0, 200, 400)) == 0


def test_uniform_bright_panel_and_low_contrast_border_rejected():
    assert _panel_score(panel(fill=(160, 160, 160)), (0, 0, 200, 400)) == 0
    assert _panel_score(panel(color=(24, 34, 38)), (0, 0, 200, 400)) == 0


def test_scattered_border_highlights_rejected():
    image = panel(sides="tb")
    draw = ImageDraw.Draw(image)
    for y in range(0, 400, 16):
        draw.rectangle((0, y, 8, y+3), fill="white")
        draw.rectangle((191, y, 199, y+3), fill="white")
    assert _panel_score(image, (0, 0, 200, 400)) == 0


@pytest.mark.parametrize("shift", [-24, 24])
def test_local_frame_alignment_does_not_depend_on_border_hue(shift):
    image = Image.new("RGB", (500, 500), (20, 25, 28))
    image.paste(panel(), (150+shift, 50))
    assert _aligned_panel_score(image, (.3, .1, .7, .9), LayoutTransform()) >= .58
    image.info.update(hextech_roi_origin=(160, 50), hextech_roi_size=(180, 400))
    assert _aligned_panel_score(image, (.3, .1, .7, .9), LayoutTransform()) == 0


@pytest.mark.parametrize("size", [(1920, 1080), (1920, 1200), (2560, 1440), (2560, 1600)])
@pytest.mark.parametrize("count,button,expected", [(3, True, True), (2, True, True),
                                                (1, True, False), (3, False, False)])
def test_scene_requires_button_and_multiple_aligned_panels(size, count, button, expected):
    image = Image.new("RGB", size, (20, 25, 28))
    if button:
        w, h = size
        ImageDraw.Draw(image).rectangle((round(.4445*w), round(.7725*h), round(.5555*w), round(.8155*h)),
                                       fill=(10, 140, 185))
    transform = detect_selection_scene(image, layout_id="fixture").transform if button else LayoutTransform()
    for definition in pick_card_panels(size)[:count]:
        box = apply_transform(definition, size, transform)
        image.paste(panel().resize((box[2]-box[0], box[3]-box[1])), box[:2])
    assert detect_selection_scene(image, layout_id="fixture").present is expected


@pytest.mark.parametrize("name", ["hextech_20260720", "hextech_20260720_e1", "hextech_20260720_e2",
                                  "hextech_20260720_e3", "hextech_20260720_e4"])
def test_existing_real_full_frame_scene_regressions(name):
    root = Path(__file__).parent / "fixtures/diagnostics/overlay_vision_fixtures"
    with Image.open(root / name / "frame.png") as image:
        assert detect_selection_scene(image, layout_id="historical_fixture").present

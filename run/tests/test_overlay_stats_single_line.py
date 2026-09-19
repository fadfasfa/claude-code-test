"""The real 2128/136 two-digit pick-rate case and numeric boundary layout contract."""
import tkinter as tk

import pytest

from hextech.interfaces.overlay.canvas_renderer import _format_percent, draw_overlay_frame
from test_overlay_display_modes import _model


@pytest.mark.parametrize("value,expected", [
    (0, "0.0%"), (.6293, "62.9%"), (.1119, "11.2%"), (.999, "99.9%"),
    (.9996, "100%"), (1.0, "100%"), (100.0, "100%"),
    (float("nan"), ""), (float("inf"), ""), (-.1, ""), (101, ""), (True, ""),
])
def test_percent_compaction_preserves_rounding_and_rejects_invalid(value, expected):
    assert _format_percent(value) == expected


@pytest.mark.parametrize("viewport", [(2560, 1440), (2560, 1600), (1920, 1080), (1920, 1200), (1280, 720)])
def test_native_single_line_real_case_and_boundaries(viewport, request):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    root = tk.Tk()
    root.withdraw()
    try:
        canvas = tk.Canvas(root)
        texts = ["胜率 62.9% · 出场 11.2%", "胜率 99.9% · 出场 99.9%",
                 "胜率 100.0% · 出场 100.0%", "胜率 0.0% · 出场 0.0%",
                 "胜率 99.9% · 出场数 99", "胜率 100.0% · 出场数 0"]
        for dpi in (1.0, 1.25, 1.5, 1.75, 2.0):
            root.tk.call("tk", "scaling", dpi * 96 / 72)
            for offset in range(len(texts)):
                model = _model()
                model["synergies"] = []
                for slot, row in enumerate(model["stats"]):
                    row["stats_text"] = texts[(slot + offset) % len(texts)]
                perf = {}
                layout = draw_overlay_frame(canvas, model, viewport_size=viewport, dpi_scale=dpi, perf_sink=perf)
                audits = perf["display_layout"]["stats_text"]
                assert len(audits) == 3 and all(row["line_count"] == 1 for row in audits)
                assert perf["typography"]["stats_pixel_size"] == int(30 * viewport[0] / 2560 + .5)
                if viewport == (2560, 1440):
                    assert all(row["padding_px"] == 5 for row in audits)
                else:
                    assert all(row["padding_px"] == max(2, int(6 * viewport[0] / 2560 + .5)) for row in audits)
                items = [item for item in canvas.find_all() if canvas.type(item) == "text"]
                assert len(items) == 3
                for item, box in zip(items, layout["stat_boxes"], strict=True):
                    text = canvas.itemcget(item, "text")
                    assert "\n" not in text and "100.0%" not in text
                    x0, y0, x1, y1 = canvas.bbox(item)
                    assert box[0] <= x0 < x1 <= box[2] and box[1] <= y0 < y1 <= box[3]
                    assert abs((x0 + x1) / 2 - (box[0] + box[2]) / 2) <= 1
    finally:
        root.destroy()

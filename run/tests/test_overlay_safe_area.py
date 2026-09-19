"""四规格的原生 Tk 安全区验收；不将模拟窗口模式当作真实 League。"""
from itertools import product

import pytest

from test_overlay_display_modes import _model, RecordingCanvas
from hextech.interfaces.overlay.canvas_renderer import draw_overlay_frame


def test_native_safe_area_matrix(request):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    try:
        canvas = tk.Canvas(root)
        for viewport, dpi, mode, expanded in product(
            [(1920,1080), (1920,1200), (2560,1440), (2560,1600)],
            [1, 1.25, 1.5, 1.75, 2], ["windowed", "borderless"], [False, True],
        ):
            root.tk.call("tk", "scaling", dpi*96/72)
            model = _model()
            model["stats"][0]["stats_text"] = "胜率 100.0% · 出场 100.0%"
            model["stats"][1]["stats_text"] = "胜率 100.0% · 出场数 99"
            perf = {}
            layout = draw_overlay_frame(canvas, model, viewport_size=viewport, dpi_scale=dpi, expanded=expanded, perf_sink=perf)
            expected_px = 30 if viewport[0] == 2560 else 23
            assert perf["typography"]["stats_pixel_size"] == expected_px, (viewport,dpi,mode)
            assert len(perf["display_layout"]["stats_text"]) == 3
            for entry, box in zip(perf["display_layout"]["stats_text"], layout["safe_boxes"]):
                left, top, right, bottom = entry["actual_bbox"]
                pad = entry["padding_px"]
                assert box[0]+pad <= left < right <= box[2]-pad
                assert box[1] <= top < bottom <= box[3]
                assert abs((left+right)/2-(box[0]+box[2])/2) <= 1
            assert perf["display_layout"]["stats_text"][0]["spacing"] in {"normal", "thin", "hair", "compact"}
            assert all(entry["line_count"] == 1 for entry in perf["display_layout"]["stats_text"])
            canvas.delete("all")
    finally:
        root.destroy()


def test_oversized_stats_fail_without_font_shrink_or_model_mutation():
    model = _model()
    original = "胜率 " + "100.0%"*20
    model["stats"][0]["stats_text"] = original
    canvas = RecordingCanvas(2560,1600)
    with pytest.raises(ValueError, match="stats_text_exceeds_safe_area"):
        draw_overlay_frame(canvas, model)
    assert model["stats"][0]["stats_text"] == original and canvas.draw_calls == 0

"""验证游戏内 Overlay 精简/展开与关键按钮禁入区。"""

from __future__ import annotations

from itertools import combinations
import tkinter as tk

import pytest

from hextech.interfaces.overlay.canvas_renderer import (
    OVERLAY_THEME,
    _tier_color,
    draw_overlay_frame,
    resolve_overlay_layout,
    resolve_overlay_typography,
)
from hextech.modules.vision.layout import LayoutTransform


class RecordingCanvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.draw_calls = 0
        self.text_calls: list[dict] = []
        self.polygon_calls: list[dict] = []

    def winfo_width(self) -> int:
        return self.width

    def winfo_height(self) -> int:
        return self.height

    def delete(self, *_args) -> None:
        return None

    def _draw(self, *_args, **_kwargs) -> None:
        self.draw_calls += 1

    def create_polygon(self, *args, **kwargs) -> None:
        self.draw_calls += 1
        self.polygon_calls.append({**kwargs, "_coords": tuple(args)})

    create_line = _draw
    create_rectangle = _draw

    def create_text(self, *args, **kwargs) -> None:
        self.draw_calls += 1
        self.text_calls.append({**kwargs, "_coords": tuple(args[:2])})


def _model() -> dict:
    stats = [
        {
            "slot": index,
            "state": "ready",
            "name": f"强化 {index}",
            "tier": "gold",
            "stats_text": "胜率 55.0% · 出场 3.0%",
            "status_code": "READY",
            "winrate_text": "55.0%",
            "pickrate_text": "3.0%",
            "status_text": "",
            "synergy_status": "READY",
        }
        for index in range(3)
    ]
    synergies = [
        {
            "slot": index,
            "augment_name": f"强化 {index}",
            "tier": "gold",
            "hero_name": "测试英雄",
            "rating": "S",
            "tag": "联动",
            "content": "这是一条用于布局验证的联动说明。" * 8,
        }
        for index in range(3)
    ]
    return {"stats": stats, "synergies": synergies}


def test_compact_and_expanded_keep_all_synergies_aligned_to_their_slots() -> None:
    compact = draw_overlay_frame(RecordingCanvas(1920, 1080), _model(), expanded=False)
    expanded = draw_overlay_frame(RecordingCanvas(1920, 1080), _model(), expanded=True)

    for layout in (compact, expanded):
        assert len(layout["synergy_boxes"]) == 3
        for synergy_box, card_box in zip(layout["synergy_boxes"], layout["card_boxes"]):
            assert synergy_box[0] == card_box[0]
            assert synergy_box[2] == card_box[2]
            assert synergy_box[3] < card_box[1]


def test_missing_synergy_slot_does_not_shift_other_slots() -> None:
    model = _model()
    model["synergies"] = [model["synergies"][1]]

    layout = draw_overlay_frame(RecordingCanvas(1920, 1080), model, expanded=False)

    assert len(layout["synergy_boxes"]) == 1
    assert layout["synergy_boxes"][0][0] == layout["card_boxes"][1][0]
    assert layout["synergy_boxes"][0][2] == layout["card_boxes"][1][2]


def test_insufficient_vertical_space_hides_synergies() -> None:
    layout = draw_overlay_frame(RecordingCanvas(640, 360), _model(), expanded=False)

    assert layout["synergy_boxes"] == []


def test_compact_and_expanded_keep_the_same_short_stats_text() -> None:
    expected = "胜率 55.0% · 出场 3.0%"
    for expanded in (False, True):
        canvas = RecordingCanvas(2560, 1600)
        draw_overlay_frame(canvas, _model(), expanded=expanded)

        visible_stats = [call.get("text") for call in canvas.text_calls if str(call.get("text", "")).startswith("胜率")]
        # 每段统计只保留一个 Canvas text item；两种模式都不得改写统计文案。
        assert len(visible_stats) == 3
        assert set(visible_stats) == {expected}


def test_each_visible_text_segment_creates_one_canvas_item() -> None:
    canvas = RecordingCanvas(2560, 1600)

    draw_overlay_frame(canvas, _model(), expanded=True)

    keys = [(call["_coords"], call.get("text")) for call in canvas.text_calls]
    assert len(keys) == len(set(keys))


def test_all_overlay_text_uses_physical_pixel_sizes() -> None:
    canvas = RecordingCanvas(2560, 1600)

    draw_overlay_frame(canvas, _model(), expanded=True)

    assert canvas.text_calls
    stats_calls = [call for call in canvas.text_calls if str(call.get("text", "")).startswith("胜率")]
    synergy_calls = [call for call in canvas.text_calls if call not in stats_calls]
    assert stats_calls
    assert all(int(call["font"][1]) == -30 for call in stats_calls)
    assert synergy_calls
    assert all(int(call["font"][1]) < 0 for call in synergy_calls)


def test_non_ready_card_status_uses_the_same_pixel_size() -> None:
    model = _model()
    model["stats"][0].update(
        status_code="SOURCE_STAT_MISSING",
        status_text="源站暂无统计",
        stats_text="源站暂无该组合统计",
    )
    canvas = RecordingCanvas(2560, 1600)

    draw_overlay_frame(canvas, model, expanded=False)

    status_calls = [call for call in canvas.text_calls if call.get("text") == "源站暂无统计"]
    assert len(status_calls) == 1
    assert all(int(call["font"][1]) == -24 for call in status_calls)


def test_low_resolution_card_text_scales_down_to_avoid_cross_card_overlap() -> None:
    low = RecordingCanvas(1280, 720)
    medium = RecordingCanvas(1920, 1080)
    high = RecordingCanvas(2560, 1600)

    draw_overlay_frame(low, _model())
    draw_overlay_frame(medium, _model())
    draw_overlay_frame(high, _model())

    def stats_size(canvas: RecordingCanvas) -> int:
        call = next(item for item in canvas.text_calls if str(item.get("text", "")).startswith("胜率"))
        return int(call["font"][1])

    assert abs(stats_size(low)) == 15
    assert abs(stats_size(medium)) == 23
    assert abs(stats_size(high)) == 30


def test_exclusion_zone_prevents_any_panel_from_drawing_over_critical_controls() -> None:
    canvas = RecordingCanvas(2560, 1440)
    draw_overlay_frame(
        canvas,
        _model(),
        expanded=True,
        exclusion_zones=((0, 0, 2560, 1440),),
    )

    assert canvas.draw_calls == 0


@pytest.mark.parametrize(
    ("tone", "expected_color"),
    [
        ("default", "#FFB23E"),
        ("low_sample", "#F87171"),
        ("aggregate", "#3FA9DC"),
    ],
)
def test_stats_tone_controls_card_text_color(tone: str, expected_color: str) -> None:
    model = _model()
    model["stats"][0]["stats_tone"] = tone
    canvas = RecordingCanvas(1920, 1080)

    draw_overlay_frame(canvas, model)

    row = next(call for call in canvas.text_calls if call.get("text") == "胜率 55.0% · 出场 3.0%")
    assert row["fill"] == expected_color


def test_stale_stat_panel_draws_only_age_text_without_percentages() -> None:
    model = _model()
    model["stats"][0].update(
        status_code="STATS_STALE",
        stats_text="统计数据为 4 天前",
        status_text="统计数据为 4 天前",
        winrate_text="",
        pickrate_text="",
    )
    canvas = RecordingCanvas(1920, 1080)

    draw_overlay_frame(canvas, model)

    visible = " ".join(str(call.get("text") or "") for call in canvas.text_calls)
    assert "统计数据为 4 天前" in visible
    # 其他两张 fresh 卡仍有百分比；第一张过期行不得把旧的 55%/3% 再画一次。
    assert sum(call.get("text") == "胜率 55.0% · 出场 3.0%" for call in canvas.text_calls) == 2


def test_generation_degraded_stat_panel_keeps_percentages_and_stage_notice() -> None:
    model = _model()
    model["stats"][0].update(status_code="GENERATION_DEGRADED")
    model["stage_indicator"] = {
        "stage": 1,
        "label": "阶段 1",
        "data_notice": {
            "text": "统计数据为 18 小时前",
            "source": "aramkit",
            "reason": "source_data_expired",
            "data_at": "2026-08-29T00:00:00+00:00",
            "state": "stale",
            "age_seconds": 64800,
        },
    }
    canvas = RecordingCanvas(1920, 1080)

    draw_overlay_frame(canvas, model)

    visible = " ".join(str(call.get("text") or "") for call in canvas.text_calls)
    assert visible.count("胜率 55.0% · 出场 3.0%") == 3
    assert "阶段 1" in visible
    assert "统计数据为 18 小时前" in visible


def test_aggregate_low_sample_adds_red_inner_outline() -> None:
    model = _model()
    model["stats"][0].update(stats_tone="aggregate", low_sample_outline=True)
    canvas = RecordingCanvas(1920, 1080)

    draw_overlay_frame(canvas, model)

    outlines = [call for call in canvas.polygon_calls if call.get("outline") == "#F87171"]
    assert len(outlines) == 1
    assert outlines[0].get("fill") == ""
    assert outlines[0].get("width") == 2


@pytest.mark.parametrize("viewport", [(1280, 720), (1920, 1080), (2560, 1600)])
def test_stage_indicator_is_one_line_and_anchored_inside_top_right(viewport: tuple[int, int]) -> None:
    model = _model()
    model["stage_indicator"] = {"stage": 3, "label": "阶段 3"}
    width, height = viewport
    canvas = RecordingCanvas(width, height)

    layout = draw_overlay_frame(canvas, model)

    calls = [call for call in canvas.text_calls if call.get("text") == "阶段 3"]
    assert len(calls) == 1
    assert "\n" not in calls[0]["text"]
    x0, y0, x1, y1 = layout["stage_indicator_box"]
    assert 0 <= x0 < x1 <= width
    assert 64 <= y0 <= 108
    assert 40 <= y1 - y0 <= 54
    assert x1 - x0 >= 112
    assert 28 <= width - x1 <= 40
    assert 16 <= abs(int(calls[0]["font"][1])) <= 24


def test_chinese_and_english_tier_aliases_share_colors() -> None:
    assert _tier_color("白银") == _tier_color("silver") == OVERLAY_THEME["silver"]
    assert _tier_color("黄金") == _tier_color("gold") == OVERLAY_THEME["gold"]
    assert _tier_color("棱彩") == _tier_color("Prismatic") == OVERLAY_THEME["prismatic"]


def _synergy_pixel_sizes(canvas: RecordingCanvas) -> list[int]:
    """收集联动文字像素字号，排除卡内统计行。"""

    return [
        abs(int(call["font"][1]))
        for call in canvas.text_calls
        if int(call["font"][1]) < 0
        and not str(call.get("text", "")).startswith("胜率")
    ]


def test_aoc_and_boe_use_same_card_relative_typography_despite_dpi() -> None:
    aoc = RecordingCanvas(2560, 1440)
    boe = RecordingCanvas(2560, 1600)
    draw_overlay_frame(aoc, _model(), dpi_scale=1.0, expanded=True)
    draw_overlay_frame(boe, _model(), dpi_scale=1.5, expanded=True)

    aoc_sizes = sorted(set(_synergy_pixel_sizes(aoc)))
    boe_sizes = sorted(set(_synergy_pixel_sizes(boe)))
    assert aoc_sizes == [18, 22, 24]
    assert boe_sizes == [18, 22, 24]

    aoc_metrics = resolve_overlay_typography(1.0)
    boe_metrics = resolve_overlay_typography(1.5)
    assert aoc_metrics == {
        "dpi_scale": 1.0,
        "geometry_scale": 1.0,
        "stats_pixel_size": 30,
        "synergy_title_px": 24,
        "synergy_body_px": 18,
        "compact_synergy_px": 18,
        "stage_px": 24,
        "stage_warning_px": 18,
        "expanded_panel_min_px": 135,
        "compact_panel_px": 96,
    }
    assert {key: value for key, value in boe_metrics.items() if key != "dpi_scale"} == {
        key: value for key, value in aoc_metrics.items() if key != "dpi_scale"
    }


def test_measured_and_legacy_profiles_keep_stats_inside_their_own_card_frame() -> None:
    layouts = [
        resolve_overlay_layout((2560, 1440), synergy_heights=[135] * 3),
        resolve_overlay_layout((2560, 1600), synergy_heights=[135] * 3),
    ]

    normalized: list[tuple[float, float, float, float]] = []
    for layout in layouts:
        card = layout["card_boxes"][0]
        stat = layout["stat_boxes"][0]
        synergy = layout["synergy_boxes"][0]
        card_width = card[2] - card[0]
        card_height = card[3] - card[1]
        normalized.append(
            (
                (stat[0] - card[0]) / card_width,
                (stat[1] - card[1]) / card_height,
                (stat[3] - stat[1]) / card_height,
                (card[1] - synergy[3]) / card_height,
            )
        )

    # Different aspect ratios cannot inherit a calibration from a single 1440p screenshot.
    for left, top, height, gap in normalized:
        assert 0 < left < .5 and 0 < top < top + height < 1 and gap > 0


def test_recognition_transform_never_moves_display_geometry() -> None:
    viewport = (2560, 1600)
    base = resolve_overlay_layout(viewport, synergy_heights=[135] * 3)
    transformed = resolve_overlay_layout(
        viewport,
        layout_transform=LayoutTransform(dx_ratio=0.02, dy_ratio=-0.01, scale=1.05),
        synergy_heights=[142] * 3,
    )

    assert transformed == base
    for card, stat in zip(base["card_boxes"], base["stat_boxes"]):
        assert abs((stat[0] + stat[2]) / 2 - (card[0] + card[2]) / 2) <= 1
        assert abs((stat[1] + stat[3]) / 2 - 952) <= 1


def test_long_synergy_content_reports_omission_without_silent_truncation() -> None:
    model = _model()
    for row in model["synergies"]:
        row["content"] = "很长的联动说明文本" * 30
    canvas = RecordingCanvas(2560, 1600)

    perf = {}
    layout = draw_overlay_frame(canvas, model, dpi_scale=1.5, expanded=True, perf_sink=perf)

    for synergy_box, card_box in zip(layout["synergy_boxes"], layout["card_boxes"]):
        # 面板顶部不得越过屏幕安全边距，底部保持在卡片上方。
        assert synergy_box[1] >= 8
        assert synergy_box[3] < card_box[1]
    assert not any("…" in str(call.get("text", "")) for call in canvas.text_calls)
    assert any("待审" in str(call.get("text", "")) for call in canvas.text_calls)
    assert all(row["content"] == "很长的联动说明文本" * 30 for row in model["synergies"])
    assert len(perf["synergy_render"]) == len(model["synergies"])
    assert all(entry["source_omitted"] and entry["reason"] for entry in perf["synergy_render"])


def test_expanded_rating_renders_as_standalone_badge_text() -> None:
    canvas = RecordingCanvas(2560, 1600)

    draw_overlay_frame(canvas, _model(), expanded=True)

    # 评级从标题内文本改为独立徽章文字；标题里不再拼接 "· S"。
    assert any(call.get("text") == "S" for call in canvas.text_calls)
    assert not any("· S" in str(call.get("text", "")) for call in canvas.text_calls)


def test_compact_synergy_line_keeps_same_card_relative_size_across_monitors() -> None:
    low = RecordingCanvas(2560, 1440)
    high = RecordingCanvas(2560, 1600)
    draw_overlay_frame(low, _model(), dpi_scale=1.0, expanded=False)
    draw_overlay_frame(high, _model(), dpi_scale=1.5, expanded=False)

    low_sizes = _synergy_pixel_sizes(low)
    high_sizes = _synergy_pixel_sizes(high)
    assert low_sizes and high_sizes
    assert set(low_sizes) == {18}
    assert set(high_sizes) == {18}


def test_draw_records_typography_metrics_for_session_evidence() -> None:
    perf: dict[str, object] = {}

    draw_overlay_frame(
        RecordingCanvas(2560, 1600),
        _model(),
        dpi_scale=1.5,
        perf_sink=perf,
    )

    layout = resolve_overlay_layout((2560, 1600))
    card = layout["card_boxes"][0]
    assert perf["typography"] == resolve_overlay_typography(
        1.5,
        card_size=(card[2] - card[0], card[3] - card[1]),
    )


@pytest.mark.parametrize("viewport", [(1280, 720), (1920, 1080), (1920, 1200), (2560, 1440), (2560, 1600)])
@pytest.mark.parametrize("expanded", [False, True])
def test_fixed_layout_matrix_ignores_dpi_text_and_recognition_noise(viewport, expanded) -> None:
    from hextech.interfaces.overlay.text_metrics import canvas_text_metrics

    baseline = None
    sizes = None
    for dpi in (1.0, 1.25, 1.5, 1.75, 2.0):
        canvas = RecordingCanvas(*viewport)
        model = _model()
        for row in model["synergies"]:
            row["content"] = "长联动 Mixed 123 测试" * int(25 * dpi)
        for row in model["stats"]:
            row["stats_text"] = "胜率 100.0% · 出场 100.0%"
        layout = draw_overlay_frame(canvas, model, viewport_size=viewport, dpi_scale=dpi,
                                    expanded=expanded, layout_transform=LayoutTransform(.02, -.01, .9))
        fonts = [call["font"] for call in canvas.text_calls if str(call.get("text", "")).startswith("胜率")]
        if baseline is None:
            baseline, sizes = layout, fonts
        assert layout == baseline
        assert fonts == sizes
        for call, box in zip([c for c in canvas.text_calls if str(c.get("text", "")).startswith("胜率")], layout["stat_boxes"]):
            assert max(canvas_text_metrics(canvas).width(line, abs(call["font"][1]), True)
                       for line in call["text"].splitlines()) <= box[2] - box[0] - 8


@pytest.mark.parametrize("viewport", [(1920, 1080), (1920, 1200), (2560, 1440), (2560, 1600)])
def test_native_tk_text_bounds_fit_fixed_panels(viewport, request) -> None:
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    root = tk.Tk()
    root.withdraw()
    try:
        canvas = tk.Canvas(root)
        for dpi in (1.0, 1.25, 1.5, 1.75, 2.0):
            root.tk.call("tk", "scaling", dpi * 96 / 72)
            model = _model()
            model["stats"][0].update(status_code="SOURCE_STAT_MISSING", status_text="公开来源未提供此海克斯统计")
            model["stats"][1]["stats_text"] = "胜率 100.0% · 出场 100.0%"
            layout = draw_overlay_frame(canvas, model, viewport_size=viewport, dpi_scale=dpi, expanded=True)
            boxes = layout["stat_boxes"] + layout["synergy_boxes"]
            for item in canvas.find_all():
                if canvas.type(item) != "text":
                    continue
                x0, y0, x1, y1 = canvas.bbox(item)
                assert any(b[0] <= x0 <= x1 <= b[2] and b[1] <= y0 <= y1 <= b[3] for b in boxes), (canvas.itemcget(item, "text"), (x0, y0, x1, y1))
            canvas.delete("all")
    finally:
        root.destroy()


def test_production_tk_stats_restore_reference_width_without_content_driven_font_changes(request) -> None:
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    from tkinter.font import Font

    root = tk.Tk()
    root.withdraw()
    try:
        canvas = tk.Canvas(root)
        model = _model()
        reference_texts = ("胜率 51.8% · 出场 20.6%", "胜率 50.4% · 出场 6.2%", "胜率 51.3% · 出场 1.4%")
        for row, text in zip(model["stats"], reference_texts):
            row["stats_text"] = text
        perf = {}
        layout = draw_overlay_frame(canvas, model, viewport_size=(2560, 1600), dpi_scale=1.5, perf_sink=perf)
        assert perf["typography"]["stats_pixel_size"] == 30
        font = Font(root=root, family="Microsoft YaHei UI", size=-30, weight="bold")
        # 30px 已确认说明样图的字体度量，不代表真实内框锚点已验收。
        assert all(abs(font.measure(text) - ink_width) <= 4 for text, ink_width in zip(reference_texts, (357, 338, 338)))
        assert font.measure("胜率100.0%·出场100.0%") + 2 <= min(b[2] - b[0] for b in layout["stat_boxes"]) - 12
        assert all(abs((b[1] + b[3]) / 2 - 952) <= 1 for b in layout["stat_boxes"])
    finally:
        root.destroy()


def test_native_tk_1440_stats_clear_measured_frame_without_shrinking(request) -> None:
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    root = tk.Tk()
    root.withdraw()
    try:
        canvas = tk.Canvas(root)
        for dpi in (1.0, 1.25, 1.5, 1.75, 2.0):
            root.tk.call("tk", "scaling", dpi * 96 / 72)
            for texts in (("胜率 48.5% · 出场 1.2%", "胜率 49.0% · 出场 2.0%", "胜率 48.9% · 出场 1.1%"),
                          ("胜率 100.0% · 出场 100.0%", "胜率 100.0% · 出场数 99", "胜率 0.0% · 出场 0.0%")):
                model = _model()
                model["synergies"] = []
                for row, text in zip(model["stats"], texts):
                    row["stats_text"] = text
                perf = {}
                draw_overlay_frame(canvas, model, viewport_size=(2560, 1440), dpi_scale=dpi, perf_sink=perf)
                assert perf["typography"]["stats_pixel_size"] == 30
                items = [item for item in canvas.find_all() if canvas.type(item) == "text"]
                assert len(items) == 3
                for item, (left, right) in zip(items, ((625, 970), (1115, 1460), (1605, 1950))):
                    x0, y0, x1, y1 = canvas.bbox(item)
                    assert left + 6 <= x0 < x1 <= right - 6
                    assert 780 <= y0 < y1 <= 876
                    assert abs((x0 + x1) / 2 - (left + right) / 2) <= 2
                    assert "-30" in canvas.itemcget(item, "font")
                if texts[0].startswith("胜率 48"):
                    assert all(entry["line_count"] == 1 for entry in perf["display_layout"]["stats_text"])
                else:
                    assert perf["display_layout"]["stats_text"][0]["line_count"] == 2
                canvas.delete("all")
    finally:
        root.destroy()


@pytest.mark.parametrize(
    ("viewport", "dpi_scale"),
    [((2560, 1440), 1.0), ((2560, 1600), 1.5)],
)
def test_real_tk_text_bboxes_stay_inside_viewport_without_overlap(
    viewport: tuple[int, int],
    dpi_scale: float,
    request,
) -> None:
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    root = tk.Tk()
    root.withdraw()
    width, height = viewport
    canvas = tk.Canvas(root, width=width, height=height, highlightthickness=0, bd=0)
    canvas.pack()
    try:
        draw_overlay_frame(
            canvas,
            _model(),
            viewport_size=viewport,
            dpi_scale=dpi_scale,
            expanded=True,
        )
        root.update_idletasks()
        text_boxes = [canvas.bbox(item) for item in canvas.find_all() if canvas.type(item) == "text"]
        assert text_boxes and all(box is not None for box in text_boxes)
        concrete_boxes = [box for box in text_boxes if box is not None]
        assert all(0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height for x0, y0, x1, y1 in concrete_boxes)
        for left, right in combinations(concrete_boxes, 2):
            overlap_width = min(left[2], right[2]) - max(left[0], right[0])
            overlap_height = min(left[3], right[3]) - max(left[1], right[1])
            assert overlap_width <= 0 or overlap_height <= 0
    finally:
        root.destroy()

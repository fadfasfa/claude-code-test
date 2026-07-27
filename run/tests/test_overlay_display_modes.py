"""验证游戏内 Overlay 精简/展开与关键按钮禁入区。"""

from __future__ import annotations

from itertools import combinations
import tkinter as tk

import pytest

from hextech.interfaces.overlay.canvas_renderer import (
    CARD_TEXT_POINT_SIZE,
    OVERLAY_THEME,
    _tier_color,
    draw_overlay_frame,
)


class RecordingCanvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.draw_calls = 0
        self.text_calls: list[dict] = []

    def winfo_width(self) -> int:
        return self.width

    def winfo_height(self) -> int:
        return self.height

    def delete(self, *_args) -> None:
        return None

    def _draw(self, *_args, **_kwargs) -> None:
        self.draw_calls += 1

    create_polygon = _draw
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


def test_card_text_uses_16_points_while_synergies_keep_pixel_sizes() -> None:
    canvas = RecordingCanvas(2560, 1600)

    draw_overlay_frame(canvas, _model(), expanded=True)

    assert canvas.text_calls
    stats_calls = [call for call in canvas.text_calls if str(call.get("text", "")).startswith("胜率")]
    synergy_calls = [call for call in canvas.text_calls if call not in stats_calls]
    assert stats_calls
    assert all(int(call["font"][1]) == CARD_TEXT_POINT_SIZE for call in stats_calls)
    assert synergy_calls
    assert all(int(call["font"][1]) < 0 for call in synergy_calls)


def test_non_ready_card_status_uses_the_same_16_point_size() -> None:
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
    assert all(int(call["font"][1]) == CARD_TEXT_POINT_SIZE for call in status_calls)


def test_exclusion_zone_prevents_any_panel_from_drawing_over_critical_controls() -> None:
    canvas = RecordingCanvas(2560, 1440)
    draw_overlay_frame(
        canvas,
        _model(),
        expanded=True,
        exclusion_zones=((0, 0, 2560, 1440),),
    )

    assert canvas.draw_calls == 0


def test_chinese_and_english_tier_aliases_share_colors() -> None:
    assert _tier_color("白银") == _tier_color("silver") == OVERLAY_THEME["silver"]
    assert _tier_color("黄金") == _tier_color("gold") == OVERLAY_THEME["gold"]
    assert _tier_color("棱彩") == _tier_color("Prismatic") == OVERLAY_THEME["prismatic"]


def _synergy_pixel_sizes(canvas: RecordingCanvas) -> list[int]:
    """收集负像素字号的绝对值；正数点字号（统计行）不计入。"""

    return [abs(int(call["font"][1])) for call in canvas.text_calls if int(call["font"][1]) < 0]


def test_synergy_text_scales_up_with_viewport_resolution() -> None:
    low = RecordingCanvas(1920, 1080)
    high = RecordingCanvas(2560, 1600)
    draw_overlay_frame(low, _model(), expanded=True)
    draw_overlay_frame(high, _model(), expanded=True)

    low_sizes = _synergy_pixel_sizes(low)
    high_sizes = _synergy_pixel_sizes(high)
    assert low_sizes and high_sizes
    # 1600p 的标题/正文都必须比 1080p 更大，不能再被固定上限钉死在 15/12px。
    assert max(high_sizes) > max(low_sizes)
    assert min(high_sizes) > min(low_sizes)
    # 1600p 正文需与卡下 16pt 统计行同量级（≥18px），保证真机可读。
    assert max(high_sizes) >= 24
    assert sorted(set(high_sizes))[0] >= 17


def test_long_synergy_content_truncates_inside_available_space() -> None:
    model = _model()
    for row in model["synergies"]:
        row["content"] = "很长的联动说明文本" * 30
    canvas = RecordingCanvas(2560, 1600)

    layout = draw_overlay_frame(canvas, model, expanded=True)

    for synergy_box, card_box in zip(layout["synergy_boxes"], layout["card_boxes"]):
        # 面板顶部不得越过屏幕安全边距，底部保持在卡片上方。
        assert synergy_box[1] >= 8
        assert synergy_box[3] < card_box[1]
    assert any("…" in str(call.get("text", "")) for call in canvas.text_calls)


def test_expanded_rating_renders_as_standalone_badge_text() -> None:
    canvas = RecordingCanvas(2560, 1600)

    draw_overlay_frame(canvas, _model(), expanded=True)

    # 评级从标题内文本改为独立徽章文字；标题里不再拼接 "· S"。
    assert any(call.get("text") == "S" for call in canvas.text_calls)
    assert not any("· S" in str(call.get("text", "")) for call in canvas.text_calls)


def test_compact_synergy_line_scales_with_viewport() -> None:
    low = RecordingCanvas(1920, 1080)
    high = RecordingCanvas(2560, 1600)
    draw_overlay_frame(low, _model(), expanded=False)
    draw_overlay_frame(high, _model(), expanded=False)

    low_sizes = _synergy_pixel_sizes(low)
    high_sizes = _synergy_pixel_sizes(high)
    assert low_sizes and high_sizes
    assert max(high_sizes) > max(low_sizes)


def test_real_tk_text_bboxes_stay_inside_viewport_without_overlap() -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    root.withdraw()
    canvas = tk.Canvas(root, width=2560, height=1600, highlightthickness=0, bd=0)
    canvas.pack()
    try:
        draw_overlay_frame(canvas, _model(), viewport_size=(2560, 1600), expanded=True)
        root.update_idletasks()
        text_boxes = [canvas.bbox(item) for item in canvas.find_all() if canvas.type(item) == "text"]
        assert text_boxes and all(box is not None for box in text_boxes)
        concrete_boxes = [box for box in text_boxes if box is not None]
        assert all(0 <= x0 < x1 <= 2560 and 0 <= y0 < y1 <= 1600 for x0, y0, x1, y1 in concrete_boxes)
        for left, right in combinations(concrete_boxes, 2):
            overlap_width = min(left[2], right[2]) - max(left[0], right[0])
            overlap_height = min(left[3], right[3]) - max(left[1], right[1])
            assert overlap_width <= 0 or overlap_height <= 0
    finally:
        root.destroy()

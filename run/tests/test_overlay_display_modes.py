"""验证游戏内 Overlay 精简/展开与关键按钮禁入区。"""

from __future__ import annotations

from hextech.interfaces.overlay.canvas_renderer import OVERLAY_THEME, _tier_color, draw_overlay_frame


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

    def create_text(self, *_args, **kwargs) -> None:
        self.draw_calls += 1
        self.text_calls.append(kwargs)


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
        # 每段文字各绘制一次阴影和一次正文；两种模式都不得改写统计文案。
        assert len(visible_stats) == 6
        assert set(visible_stats) == {expected}


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

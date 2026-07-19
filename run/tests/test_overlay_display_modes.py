"""验证游戏内 Overlay 精简/展开与关键按钮禁入区。"""

from __future__ import annotations

from hextech.interfaces.overlay.canvas_renderer import draw_overlay_frame


class RecordingCanvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.draw_calls = 0

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
    create_text = _draw


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


def test_compact_shows_one_short_synergy_and_expanded_shows_all() -> None:
    compact = draw_overlay_frame(RecordingCanvas(1920, 1080), _model(), expanded=False)
    expanded = draw_overlay_frame(RecordingCanvas(1920, 1080), _model(), expanded=True)

    assert len(compact["synergy_boxes"]) == 1
    assert len(expanded["synergy_boxes"]) == 3


def test_exclusion_zone_prevents_any_panel_from_drawing_over_critical_controls() -> None:
    canvas = RecordingCanvas(2560, 1440)
    draw_overlay_frame(
        canvas,
        _model(),
        expanded=True,
        exclusion_zones=((0, 0, 2560, 1440),),
    )

    assert canvas.draw_calls == 0

"""桌面单行状态栏收敛层、磅值字体与卡片纯逻辑回归。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.app_controls、app_shared。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from hextech.interfaces.desktop import app_controls as desktop_controls
from hextech.interfaces.desktop.app import HextechUI, UI_COLORS
from hextech.interfaces.desktop.app_shared import (
    format_data_age_suffix,
    parse_generation_created_ts,
    ui_font,
)


class _Widget:
    """轻量 widget 伪对象：记录 config 调用，兼容 Label/Frame/Canvas 用法。"""

    def __init__(self, text: str = ""):
        self.text = text
        self.fg = ""
        self.kwargs: dict = {}

    def winfo_exists(self):
        return True

    def cget(self, key):
        return self.text if key == "text" else self.kwargs.get(key, "")

    def config(self, **kwargs):
        self.kwargs.update(kwargs)
        self.text = kwargs.get("text", self.text)
        self.fg = kwargs.get("fg", self.fg)


def _make_ui(monkeypatch, *, monotonic: float = 100.0, wall: float = 2_000_000.0):
    monkeypatch.setattr(
        desktop_controls,
        "_time",
        SimpleNamespace(monotonic=lambda: monotonic, time=lambda: wall),
    )
    ui = object.__new__(HextechUI)
    ui._status_channels = {
        "service": {"text": "", "color": UI_COLORS["muted"], "at": 0.0},
        "overlay": {"text": "", "color": UI_COLORS["muted"], "at": 0.0},
    }
    ui._data_created_ts = 0.0
    ui.status_line_label = _Widget()
    return ui


def test_ui_font_uses_positive_point_sizes() -> None:
    """回归：负数字号是像素语义会绕过 Tk 的 DPI 换算——字体过小的历史根因。"""

    assert ui_font(12) == ("Microsoft YaHei", 9)
    assert ui_font(16, bold=True) == ("Microsoft YaHei", 12, "bold")
    assert ui_font(11) == ("Microsoft YaHei", 8)
    assert ui_font(17, bold=True) == ("Microsoft YaHei", 13, "bold")
    assert all(font[1] > 0 for font in (ui_font(1), ui_font(40)))


def test_parse_generation_created_ts_handles_iso_and_garbage() -> None:
    expected = datetime(2026, 7, 26, 10, 28, 3, tzinfo=timezone.utc).timestamp()

    assert parse_generation_created_ts("2026-07-26T10:28:03+00:00") == expected
    assert parse_generation_created_ts("2026-07-26T10:28:03Z") == expected
    assert parse_generation_created_ts("") == 0.0
    assert parse_generation_created_ts("not-a-time") == 0.0
    assert parse_generation_created_ts(None) == 0.0


def test_format_data_age_suffix_granularity() -> None:
    base = 1_000_000.0

    assert format_data_age_suffix(0.0, base) == ""
    assert format_data_age_suffix(base - 1800, base) == " · 数据刚更新"
    assert format_data_age_suffix(base - 3 * 3600, base) == " · 数据 3 小时前"
    assert format_data_age_suffix(base - 26 * 3600, base) == " · 数据 1 天前"


def test_fresh_service_message_wins_over_overlay(monkeypatch) -> None:
    ui = _make_ui(monkeypatch, monotonic=100.0)

    ui._set_overlay_status_summary("识别就绪 · 等待实际对局", UI_COLORS["green"])
    ui._set_status("正在导出诊断包...", UI_COLORS["warn"])

    # service 消息在新鲜窗口内优先，保证按钮操作反馈可见。
    assert ui.status_line_label.text == "正在导出诊断包..."
    assert ui.status_line_label.fg == UI_COLORS["warn"]


def test_aged_service_message_yields_to_overlay(monkeypatch) -> None:
    ui = _make_ui(monkeypatch, monotonic=200.0)
    ui._status_channels["service"] = {"text": "实时数据已挂载", "color": UI_COLORS["green"], "at": 100.0}

    ui._set_overlay_status_summary("游戏内显示中", UI_COLORS["green"])

    assert ui.status_line_label.text == "游戏内显示中"


def test_error_service_message_pins_over_overlay(monkeypatch) -> None:
    ui = _make_ui(monkeypatch, monotonic=200.0)
    ui._status_channels["service"] = {"text": "诊断导出失败: boom", "color": UI_COLORS["error"], "at": 0.0}

    ui._set_overlay_status_summary("游戏内显示中", UI_COLORS["green"])

    # error 置顶且不追加时效后缀。
    assert ui.status_line_label.text.startswith("诊断导出失败")
    assert ui.status_line_label.fg == UI_COLORS["error"]


def test_data_age_suffix_appends_only_when_line_fits(monkeypatch) -> None:
    wall = 2_000_000.0
    ui = _make_ui(monkeypatch, monotonic=200.0, wall=wall)
    ui._data_created_ts = wall - 3 * 3600

    ui._set_overlay_status_summary("游戏内显示中", UI_COLORS["green"])
    assert ui.status_line_label.text == "游戏内显示中 · 数据 3 小时前"

    # 长短语放不下后缀时优先保住主状态，整行不得截掉状态本体。
    ui._set_overlay_status_summary("识别就绪 · 等待实际对局", UI_COLORS["green"])
    assert ui.status_line_label.text == "识别就绪 · 等待实际对局"


def test_status_line_truncates_over_budget_text(monkeypatch) -> None:
    ui = _make_ui(monkeypatch, monotonic=200.0)

    ui._set_overlay_status_summary("游戏内显示启动请求已提交(accepted)", UI_COLORS["warn"])

    text = ui.status_line_label.text
    assert text.endswith("…")
    assert len(text) == desktop_controls.STATUS_LINE_MAX_CHARS
    # 完整文案仍保留在通道镜像里，供测试与回显消费。
    assert ui._overlay_status_text == "游戏内显示启动请求已提交(accepted)"


def test_card_update_sets_big_win_label_and_weak_pick_label() -> None:
    ui = object.__new__(HextechUI)
    row = {
        "id": "1",
        "name": "旧名",
        "tier": "T3",
        "win": None,
        "pick": None,
        "tier_badge": None,
        "name_label": _Widget(),
        "win_label": _Widget(),
        "pick_label": _Widget(),
        "ribbon": _Widget(),
    }

    HextechUI._update_candidate_card(ui, row, {"id": "1", "name": "逆羽", "tier": "T3", "win": 0.525, "pick": 0.007}, 1.0)

    assert row["win_label"].text == "52.5%"
    assert row["win_label"].fg == UI_COLORS["green"]
    assert row["pick_label"].text == "出场 0.7%"
    # 英雄名不再拼接称号，横向空间让给右侧大号胜率列。
    assert row["name_label"].text == "逆羽"
    assert row["ribbon"].kwargs.get("bg") == UI_COLORS["green"]

    HextechUI._update_candidate_card(ui, row, {"id": "1", "name": "逆羽", "tier": "T3", "win": 0.48, "pick": 0.007}, 1.0)

    assert row["win_label"].text == "48.0%"
    assert row["win_label"].fg == UI_COLORS["red"]
    assert row["ribbon"].kwargs.get("bg") == UI_COLORS["red"]


class _PackWidget:
    """记录 pack/pack_forget 的伪 widget，模拟 Tk 的 mapped 状态。"""

    def __init__(self):
        self.mapped = False
        self.pack_calls: list[dict] = []

    def winfo_ismapped(self):
        return self.mapped

    def pack(self, **kwargs):
        self.mapped = True
        self.pack_calls.append(kwargs)

    def pack_forget(self):
        self.mapped = False


def test_selected_badge_toggles_with_selection_role() -> None:
    """回归：己方已选英雄要有明确标识，且 bench→self 跃迁原地切换不重建卡片。"""

    ui = object.__new__(HextechUI)
    badge = _PackWidget()
    row = {
        "id": "1",
        "name": "",
        "tier": "T4",
        "win": None,
        "pick": None,
        "tier_badge": None,
        "name_label": _Widget(),
        "win_label": _Widget(),
        "pick_label": _Widget(),
        "ribbon": _Widget(),
        "selected_badge": badge,
        "selection_role": "",
    }

    HextechUI._update_candidate_card(
        ui, row, {"id": "1", "name": "潮汐海灵", "tier": "T4", "win": 0.502, "pick": 0.006, "selection_role": "self"}, 1.0
    )
    assert badge.mapped is True

    HextechUI._update_candidate_card(
        ui, row, {"id": "1", "name": "潮汐海灵", "tier": "T4", "win": 0.502, "pick": 0.006, "selection_role": "bench"}, 1.0
    )
    assert badge.mapped is False


def _scroll_ui(content_height: int, viewport_height: int, *, collapsed: bool = False):
    ui = object.__new__(HextechUI)
    ui._collapsed = collapsed
    ui.list_scrollbar = _PackWidget()
    ui.list_frame = SimpleNamespace(winfo_reqheight=lambda: content_height)
    moves: list[float] = []
    ui.canvas = SimpleNamespace(
        winfo_height=lambda: viewport_height,
        yview_moveto=lambda value: moves.append(value),
    )
    ui._yview_moves = moves
    return ui


def test_list_scrollbar_only_appears_on_overflow() -> None:
    """回归：英雄数量未溢出可视高度时不得出现滚动条（真机观感反馈）。"""

    fits = _scroll_ui(500, 700)
    HextechUI._sync_list_scrollbar(fits)
    assert fits.list_scrollbar.mapped is False
    assert fits._yview_moves == [0.0]

    overflow = _scroll_ui(900, 700)
    HextechUI._sync_list_scrollbar(overflow)
    assert overflow.list_scrollbar.mapped is True
    assert overflow.list_scrollbar.pack_calls[0].get("before") is overflow.canvas

    # 首次布局前 viewport 高度为 1：不做判定，保持现状等下一次 <Configure>。
    unmeasured = _scroll_ui(500, 1)
    HextechUI._sync_list_scrollbar(unmeasured)
    assert unmeasured.list_scrollbar.mapped is False
    assert unmeasured._yview_moves == []

    collapsed = _scroll_ui(900, 700, collapsed=True)
    collapsed.list_scrollbar.mapped = True
    HextechUI._sync_list_scrollbar(collapsed)
    assert collapsed.list_scrollbar.mapped is False

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
    format_data_refresh_status,
    parse_generation_created_ts,
    snapshot_data_timestamp,
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
        "refresh": {
            "text": "",
            "color": UI_COLORS["muted"],
            "state": "idle",
            "at": 0.0,
            "signature": (),
        },
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


def test_snapshot_data_timestamp_prefers_aramkit_data_at_and_falls_back_when_missing() -> None:
    created_at = "2026-07-26T10:28:03+00:00"
    aramkit_at = "2026-07-26T08:00:00+00:00"

    assert snapshot_data_timestamp(
        {
            "created_at": created_at,
            "source_status": {"aramkit": {"data_at": aramkit_at}},
        }
    ) == parse_generation_created_ts(aramkit_at)
    assert snapshot_data_timestamp(
        {"created_at": created_at, "source_status": {"aramkit": {"data_at": ""}}}
    ) == parse_generation_created_ts(created_at)


def test_refresh_status_copy_covers_running_deferred_and_terminal_states() -> None:
    assert format_data_refresh_status({"state": "running", "scope": "core"}) == (
        "正在检测核心数据",
        UI_COLORS["warn"],
    )
    assert format_data_refresh_status(
        {"state": "running", "scope": "core", "reason_code": "resumed_after_game"}
    )[0] == "赛后继续检测"
    assert format_data_refresh_status({"state": "deferred"})[0] == "对局中暂停，赛后继续"
    assert format_data_refresh_status({"state": "completed"})[0] == "数据处理完成，更新状态未知"
    assert format_data_refresh_status({"state": "unchanged"})[0] == "数据已检查，无变化"
    assert format_data_refresh_status({"state": "failed"})[0] == "检测失败，继续使用已验证数据"


def test_refresh_running_is_sticky_and_old_terminal_yields_to_overlay(monkeypatch) -> None:
    wall = 2_000_000.0
    ui = _make_ui(monkeypatch, monotonic=200.0, wall=wall)
    ui._set_overlay_status_summary("游戏内显示中", UI_COLORS["green"])

    ui._set_data_refresh_status(
        {
            "state": "running",
            "scope": "core",
            "phase": "core",
            "reason_code": "refresh_running",
            "started_at": wall - 100,
            "completed_at": 0,
        }
    )
    assert ui.status_line_label.text == "正在检测核心数据"

    ui._set_data_refresh_status(
        {
            "state": "completed",
            "scope": "core",
            "phase": "complete",
            "reason_code": "core_cohort_promoted",
            "started_at": wall - 20,
            "completed_at": wall - 7,
        }
    )
    assert ui.status_line_label.text == "游戏内显示中"


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
    }

    HextechUI._update_candidate_card(ui, row, {"id": "1", "name": "逆羽", "tier": "T3", "win": 0.525, "pick": 0.007}, 1.0)

    assert row["win_label"].text == "52.5%"
    assert row["win_label"].fg == UI_COLORS["green"]
    assert row["pick_label"].text == "出场 0.7%"
    # 英雄名不再拼接称号，横向空间让给右侧大号胜率列。
    assert row["name_label"].text == "逆羽"

    HextechUI._update_candidate_card(ui, row, {"id": "1", "name": "逆羽", "tier": "T3", "win": 0.48, "pick": 0.007}, 1.0)

    assert row["win_label"].text == "48.0%"
    assert row["win_label"].fg == UI_COLORS["red"]


class _PackWidget:
    """记录 pack/pack_forget/config 的伪 widget，模拟 Tk 的 mapped 状态。"""

    def __init__(self):
        self.mapped = False
        self.pack_calls: list[dict] = []
        self.kwargs: dict = {}

    def winfo_ismapped(self):
        return self.mapped

    def pack(self, **kwargs):
        self.mapped = True
        self.pack_calls.append(kwargs)

    def pack_forget(self):
        self.mapped = False

    def config(self, **kwargs):
        self.kwargs.update(kwargs)


def test_selected_badge_toggles_with_selection_role() -> None:
    """角色状态位固定在右栏，bench 跃迁只改内容与颜色，不改变卡片高度。"""

    ui = object.__new__(HextechUI)
    badge = _PackWidget()
    badge.mapped = True
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
        "selected_badge": badge,
        "selection_role": "",
    }

    HextechUI._update_candidate_card(
        ui, row, {"id": "1", "name": "潮汐海灵", "tier": "T4", "win": 0.502, "pick": 0.006, "selection_role": "self"}, 1.0
    )
    assert badge.kwargs.get("text") == "已选"
    assert badge.kwargs.get("bg") == UI_COLORS["selected"]

    # teammate 同样要有明确标识，且与 self 视觉可区分（文本与配色都不同）。
    HextechUI._update_candidate_card(
        ui, row, {"id": "1", "name": "潮汐海灵", "tier": "T4", "win": 0.502, "pick": 0.006, "selection_role": "teammate"}, 1.0
    )
    assert badge.kwargs.get("text") == "队友"
    assert badge.kwargs.get("bg") == UI_COLORS["teammate"]

    HextechUI._update_candidate_card(
        ui, row, {"id": "1", "name": "潮汐海灵", "tier": "T4", "win": 0.502, "pick": 0.006, "selection_role": "bench"}, 1.0
    )
    assert badge.mapped is True
    assert badge.kwargs.get("text") == ""
    assert badge.kwargs.get("bg") == UI_COLORS["surface"]


def test_tier_change_updates_badge_and_full_height_strength_bar() -> None:
    """同一卡片评级变化时，T 徽章与左侧强度色条必须同步换色。"""

    ui = object.__new__(HextechUI)
    tier_badge = _Widget()
    strength_bar = _Widget()
    row = {
        "id": "1",
        "name": "",
        "tier": "T4",
        "win": None,
        "pick": None,
        "tier_badge": tier_badge,
        "strength_bar": strength_bar,
        "name_label": _Widget(),
        "win_label": _Widget(),
        "pick_label": _Widget(),
    }

    HextechUI._update_candidate_card(
        ui, row, {"id": "1", "name": "潮汐海灵", "tier": "T1", "win": 0.502, "pick": 0.006}, 1.0
    )

    assert tier_badge.kwargs["text"] == "T1"
    assert tier_badge.kwargs["bg"] == "#F2C94C"
    assert strength_bar.kwargs["bg"] == "#F2C94C"


def test_real_tk_compact_layout_keeps_long_labels_inside_columns(request, monkeypatch) -> None:
    """真实 Tk 字体度量下，最长常见英雄名和角色徽章不得互相挤压或裁字。"""
    from native_tk_runner import run_native_tk_case

    if run_native_tk_case(request.node.nodeid):
        return

    monkeypatch.setattr(HextechUI, "_initialize_background_runtime", lambda self: None)
    monkeypatch.setattr(HextechUI, "_start_desktop_tray", lambda self: None)
    monkeypatch.setattr(HextechUI, "_schedule_post_visible_bootstrap", lambda self: None)
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_services.initialize_window_threads", lambda ui: None)
    monkeypatch.setattr(HextechUI, "_load_and_set_img", lambda self, _champion_id, _label: None)

    ui = HextechUI()
    try:
        # 本例只测卡片排版；生命周期/显隐由独立 owner 集成回归覆盖，不接真实 LCU。
        ui._desktop_window_presentation.close()
        ui.root.attributes("-alpha", 0.0)
        from hextech.modules.vision.window import root_window_hwnd
        import win32gui
        ui.root.geometry("320x740+10000+10000")
        hwnd = root_window_hwnd(ui.root.winfo_id())
        win32gui.SetWindowLong(hwnd, -20, win32gui.GetWindowLong(hwnd, -20) | 0x08000000)
        ui.root.deiconify()
        ui.root.update()
        ui._ensure_card_state()
        row = ui._build_candidate_card(
            {
                "id": "53",
                "name": "蒸汽机器人",
                "tier": "T1",
                "win": 0.519,
                "pick": 0.005,
                "selection_role": "self",
            },
            1.0,
        )
        ui.root.update()

        for toggle in (ui.web_frontend_check, ui.game_overlay_check, ui.private_stats_check):
            label = toggle.winfo_children()[1]
            assert label.winfo_width() >= label.winfo_reqwidth()
        assert ui.private_stats_check.winfo_rootx() + ui.private_stats_check.winfo_width() <= (
            ui.feature_frame.winfo_rootx() + ui.feature_frame.winfo_width()
        )

        name_label = row["name_label"]
        metric = row["win_label"].master
        assert name_label.winfo_width() >= name_label.winfo_reqwidth()
        assert name_label.winfo_rootx() + name_label.winfo_width() < metric.winfo_rootx()
        assert row["selected_badge"].winfo_width() <= metric.winfo_width()
        assert row["win_label"].winfo_width() <= metric.winfo_width()
        assert row["img_label"].winfo_width() == 50
        assert metric.winfo_width() >= max(row["win_label"].winfo_reqwidth(), row["selected_badge"].winfo_reqwidth())
    finally:
        ui.root.destroy()


def _scroll_ui(content_height: int, viewport_height: int):
    ui = object.__new__(HextechUI)
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


def test_unchanged_core_keeps_independent_optional_failure_visible():
    from hextech.interfaces.desktop.app_shared import format_data_refresh_status
    text, _ = format_data_refresh_status({"state": "unchanged", "reason_code": "no_content_change",
        "checked_at": 100.0, "data_at": "2026-08-01T00:00:00Z",
        "optional_sources": {"apex": {"state": "failed"}, "mayhem": {"state": "completed"}}})
    assert "无变化" in text and "可选来源待重试" in text
    assert "已更新" not in text


def test_authoritative_refresh_copy_distinguishes_content_catalog_and_availability():
    base = {"checked": True, "content_changed": False, "catalog_changed": False}
    assert format_data_refresh_status({"state": "unchanged", **base})[0] == "已检查，与上游一致"
    assert format_data_refresh_status({"state": "completed", **base, "catalog_changed": True})[0] == "识别目录已更新"
    assert format_data_refresh_status({"state": "completed", **base, "content_changed": True})[0] == "数据已更新"
    text, color = format_data_refresh_status({"state": "unchanged", **base, "source_outcomes": {
        "blitz": {"state": "confirmed_empty", "availability": "confirmed_empty", "checked": True},
    }})
    assert text == "已检来源与上游一致 · Blitz 已确认无记录"
    assert color == UI_COLORS["green"]
    text, color = format_data_refresh_status({"state": "unchanged", **base,
        "reason_code": "core_complete_optional_failed", "source_outcomes": {
            "aramkit": {"state": "unchanged", "checked": True},
            "blitz": {"state": "unavailable", "availability": "unavailable"},
        }})
    assert text == "已检来源与上游一致 · Blitz 暂不可用"
    assert color == UI_COLORS["warn"]
    text, color = format_data_refresh_status({"state": "unchanged", **base,
        "catalog_state": "deferred"})
    assert text == "已检查，与上游一致 · 识别资源赛后更新"
    assert color == UI_COLORS["warn"]
    inconsistent = {"state": "unchanged", **base, "checked": False}
    assert format_data_refresh_status(inconsistent)[0] == "刷新未完成"


def test_failed_refresh_only_claims_last_good_when_a_valid_source_was_preserved():
    unavailable = {"state": "failed", "checked": False, "content_changed": False,
                   "catalog_changed": False, "source_outcomes": {
                       "blitz": {"state": "unavailable", "used_last_good": False}}}
    assert format_data_refresh_status(unavailable)[0] == "检测失败，暂无可用数据"
    preserved = {**unavailable, "source_outcomes": {
        "aramkit": {"state": "last_good", "used_last_good": True}}}
    assert format_data_refresh_status(preserved)[0] == "检测失败，继续使用已验证数据"
    legacy_preserved = {**unavailable, "last_good_available": True, "source_outcomes": {}}
    assert format_data_refresh_status(legacy_preserved)[0] == "检测失败，继续使用已验证数据"


def test_not_due_and_partial_check_copy_never_claims_every_source_was_checked():
    base = {"checked": False, "content_changed": False, "catalog_changed": False}
    not_due = {**base, "state": "unchanged", "source_outcomes": {
        "aramkit": {"state": "not_due"}, "apex": {"state": "not_due"},
    }}
    assert format_data_refresh_status(not_due)[0] == "尚未到检测时间"

    partial = {**base, "checked": True, "state": "unchanged", "source_outcomes": {
        "aramkit": {"state": "unchanged", "checked": True, "check_status": "up_to_date"},
        "apex": {"state": "not_due"},
    }}
    assert format_data_refresh_status(partial)[0] == "已检来源与上游一致"


def test_snapshot_generation_watch_reloads_without_inventing_refresh_copy():
    from threading import RLock

    class StopAfterOne:
        calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    view = SimpleNamespace(
        status=lambda: {"generation_id": "generation-new", "created_at": "2026-09-15T00:00:00Z"},
        get_champions=lambda: [{"id": "1", "name": "测试英雄"}],
    )
    client = SimpleNamespace(status=view.status, open_view=lambda: view)
    ui = object.__new__(HextechUI)
    ui.stop_event = StopAfterOne()
    ui._snapshot_client = client
    ui.data_service = None
    ui._snapshot_generation_id = "generation-old"
    ui._champions_lock = RLock()
    ui.champions = []
    ui.current_candidate_groups = {}
    ui._data_created_ts = 0.0
    rendered = []
    ui._run_on_ui_thread = lambda callback: callback() or True
    ui.update_ui = lambda groups: rendered.append(groups)
    ui._set_status = lambda *_args: (_ for _ in ()).throw(AssertionError("watcher invented refresh copy"))

    ui._snapshot_watch_loop()

    assert ui._snapshot_generation_id == "generation-new"
    assert ui.champions == [{"id": "1", "name": "测试英雄"}]
    assert rendered == [{}]

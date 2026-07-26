"""runtime 域 pytest 开发门禁。"""

import os
import subprocess

import pytest

from tests._dev_gate_support import (
    Any,
    RUN_DIR,
    _probe_clean_import,
    _probe_module_import,
    _top_level_import_names,
    patch,
    threading,
)

pytestmark = pytest.mark.dev_gate


def test_desktop_ui_feature_switch_contract() -> None:
    """验证桌面 UI 不再初始化时无条件启动 Web 服务。"""
    import hextech.interfaces.desktop.runtime as ui_runtime
    import hextech.interfaces.desktop.runtime_services as ui_runtime_services
    import hextech.interfaces.desktop.app as desktop_app

    desktop_dir = RUN_DIR / "src" / "hextech" / "interfaces" / "desktop"
    app_entry_text = (desktop_dir / "app.py").read_text(encoding="utf-8")
    ui_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(desktop_dir.glob("app*.py"))
    )
    runtime_text = (RUN_DIR / "src" / "hextech" / "interfaces" / "desktop" / "runtime.py").read_text(encoding="utf-8")
    assert "ui._df_lock" not in runtime_text
    assert "ui.df" not in runtime_text
    init_start = app_entry_text.index("    def __init__(self):")
    init_end = app_entry_text.index("\ndef run_desktop", init_start)
    init_body = app_entry_text[init_start:init_end]

    assert "ServiceManager" in ui_text
    assert "Web 前端" in ui_text
    assert "游戏内显示" in ui_text
    assert "低频监听" not in ui_text
    assert "tk.Checkbutton" not in ui_text
    assert "feature_status_label" not in ui_text
    assert "Web:" not in ui_text
    assert " / sidecar" not in ui_text
    assert 'root.attributes("-alpha", 1.0' in ui_text
    assert "_build_feature_toggle" in ui_text
    assert 'sticky="ew"' in ui_text
    assert "self._refresh_feature_toggle_styles()\n            command()" in ui_text
    assert "_feature_toggle_busy" in ui_text
    assert "_closing" in ui_text
    assert "_overlay_operation_lock" in ui_text
    assert "_game_overlay_desired_enabled" in ui_text
    assert "StartupTimingProbe" in ui_text
    assert "_schedule_post_visible_bootstrap" in ui_text
    assert "hextech-post-visible-bootstrap" in ui_text
    assert "self.root.after(50, self._schedule_post_visible_bootstrap)" in ui_text
    assert "self._start_runtime_supervisor()" not in init_body
    assert "ServiceManager(" not in init_body
    assert "start_low_frequency_listener()" not in init_body
    assert "_start_tracked_thread" in ui_text
    assert 'self._feature_toggle_is_busy("游戏内显示")' in ui_text
    assert "set_game_overlay_enabled" in ui_text
    assert "components.get(\"game_overlay\")" in ui_text
    assert "hextech-toggle-web" in ui_text
    assert "hextech-toggle-overlay" in ui_text
    assert "hextech-overlay-watchdog" in ui_text
    assert "hextech-toggle-private-stats" in ui_text
    assert "正在切换 Web 前端" in ui_text
    assert "_set_overlay_status_summary" in ui_text
    assert "游戏内显示: 正在提交启动请求" in ui_text
    assert "游戏内显示启动请求已提交" in ui_text
    assert "WINDOW_EXPANDED_WIDTH = 320" in ui_text
    assert "WINDOW_COLLAPSED_WIDTH = 112" in ui_text
    assert "WINDOW_BASE_HEIGHT = 740" in ui_text
    # 几何锁 1.0 维持基线"狭长"观感（窗宽/头像/padding 不乘 DPI）；
    # 字体与几何解耦：ui_font 用正磅值由 Tk 按系统 DPI 隐式放大。
    assert "self._ui_scale = 1.0" in ui_text
    assert "def ui_font(size_px" in ui_text
    assert "round(size_px * 0.75)" in ui_text
    # 禁负像素字体回潮：那条路线曾在锁几何时把 DPI 补偿一并抹掉。
    assert "-scaled(size_px" not in ui_text
    # 构建号与长路径不再拼进状态文案；底部收敛为单行状态栏。
    assert "build_suffix" not in ui_text
    assert "status_line_label" in ui_text
    assert "_render_status_line" in ui_text
    assert "overlay_status_label" not in ui_text
    assert "self.status_label" not in ui_text
    assert "resolve_overlay_follow_height" in ui_text
    assert "manage_overlay_runtime=False" in ui_text
    assert "overlay_controller=GameOverlayController(" not in ui_text
    assert "start_vision_sidecar_process" not in ui_text
    assert "self._start_web_server()" not in init_body

    root_entry_imports = _top_level_import_names(RUN_DIR / "src" / "hextech" / "bootstrap" / "desktop.py")
    assert not any(name.startswith("display") for name in root_entry_imports)

    assert _probe_clean_import("src/hextech/bootstrap/desktop.py") == set()
    assert _probe_module_import("hextech.interfaces.overlay.host") == set()

    captured: dict[str, Any] = {}

    class DummyProcess:
        def poll(self):
            return None

    def fake_popen(command, startupinfo=None, cwd=None, env=None, creationflags=0):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = dict(env or {})
        captured["creationflags"] = creationflags
        return DummyProcess()

    with (
        patch.object(ui_runtime.subprocess, "Popen", side_effect=fake_popen),
            patch.object(ui_runtime_services, "_clear_web_readiness_files", return_value=None),
            patch.object(ui_runtime_services, "_wait_for_web_startup", return_value=None),
    ):
        ui_runtime.start_web_server_process("unused-port-file.txt", auto_open_browser=True)
    assert captured["env"]["HEXTECH_OPEN_BROWSER"] == "0"
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW

    assert hasattr(ui_runtime, "open_companion_browser")
    assert hasattr(ui_runtime, "close_companion_browser")
    private_toggle_body = ui_text.split("    def _toggle_private_policy_stats", 1)[1].split("    def _toggle_low_frequency_listener", 1)[0]
    assert "self.data_service.set_private_stats" in private_toggle_body
    assert "build_overlay_hint_cache_from_precomputed" not in private_toggle_body
    assert "desired_private_stats = bool(self.private_stats_var.get())" in private_toggle_body
    assert "self._persist_feature_flags_from_controls()" in private_toggle_body
    assert "save_ui_feature_flags(desired_flags)" not in private_toggle_body
    assert "if not _web_frontend_available(ui):\n            time.sleep(3)\n            continue" not in runtime_text

    class DummyBoolVar:
        def __init__(self, value: bool):
            self.value = value

        def get(self) -> bool:
            return self.value

        def set(self, value: bool) -> None:
            self.value = bool(value)

    class OverlayStartProbe:
        def __init__(self):
            self.start_calls = 0

        def start_game_overlay(self) -> None:
            self.start_calls += 1

        def stop_game_overlay(self) -> None:
            return None

        def is_game_overlay_running(self) -> bool:
            return False

    # 退出标记可能在 toggle 等待 operation lock 时发生；释放后不得再启动 overlay。
    lifecycle_ui = object.__new__(desktop_app.HextechUI)
    lifecycle_ui.game_overlay_var = DummyBoolVar(True)
    lifecycle_ui.service_manager = OverlayStartProbe()
    lifecycle_ui._overlay_operation_lock = threading.Lock()
    lifecycle_ui._overlay_operation_lock.acquire()
    lifecycle_ui._closing = False
    lifecycle_ui._game_overlay_desired_enabled = False
    lifecycle_ui._set_feature_toggle_busy = lambda *_args: None
    lifecycle_ui._set_status = lambda *_args: None
    lifecycle_ui._persist_feature_flags_from_controls = lambda: None
    lifecycle_ui._try_persist_feature_flags_from_controls = lambda: None
    lifecycle_ui._run_on_ui_thread = lambda command: command()
    lifecycle_ui._raise_if_service_error = lambda _name: None
    toggle_threads: list[threading.Thread] = []

    def start_toggle_thread(target, *, name: str):
        thread = threading.Thread(target=target, name=name, daemon=True)
        toggle_threads.append(thread)
        thread.start()
        return thread

    lifecycle_ui._start_tracked_thread = start_toggle_thread
    lifecycle_ui._toggle_game_overlay()
    assert toggle_threads and toggle_threads[0].is_alive()
    lifecycle_ui._closing = True
    lifecycle_ui._overlay_operation_lock.release()
    toggle_threads[0].join(timeout=2)
    assert not toggle_threads[0].is_alive()
    assert lifecycle_ui.service_manager.start_calls == 0

    candidate_groups = {
        "selected_champion_ids": ["1", "2", "3", "4", "5"],
        "bench_champion_ids": ["6", "7", "8", "9", "10"],
        "local_champion_id": "2",
        "teammate_champion_ids": ["1", "3", "4", "5"],
    }
    candidate_champions = [
        {"id": str(index), "name": f"英雄{index}", "英雄评级": f"T{index}", "英雄胜率": win, "英雄出场率": 0.01}
        for index, win in (
            (1, 0.41),
            (2, 0.55),
            (3, 0.49),
            (4, 0.52),
            (5, 0.47),
            (6, 0.60),
            (7, 0.46),
            (8, 0.58),
            (9, 0.50),
            (10, 0.44),
        )
    ]
    dummy_ui = type("DummyDesktopUI", (), {})()
    dummy_ui._candidate_groups_from_input = desktop_app.HextechUI._candidate_groups_from_input.__get__(dummy_ui)
    display_list = desktop_app.HextechUI._build_candidate_display_list(dummy_ui, candidate_groups, candidate_champions)
    assert [item["id"] for item in display_list] == ["6", "8", "2", "4", "9", "3", "5", "7", "10", "1"]
    assert next(item for item in display_list if item["id"] == "2")["selection_role"] == "self"
    assert next(item for item in display_list if item["id"] == "4")["selection_role"] == "teammate"
    assert next(item for item in display_list if item["id"] == "6")["selection_role"] == "bench"
    assert "_group_rank" not in display_list[0]
    equal_win_champions = [
        {"id": "2", "name": "英雄2", "英雄评级": "T1", "英雄胜率": 0.5, "英雄出场率": 0.01},
        {"id": "1", "name": "英雄1", "英雄评级": "T1", "英雄胜率": 0.5, "英雄出场率": 0.01},
    ]
    equal_win_groups = {
        "selected_champion_ids": ["2", "1"],
        "bench_champion_ids": [],
        "local_champion_id": "1",
        "teammate_champion_ids": ["2"],
    }
    equal_win_list = desktop_app.HextechUI._build_candidate_display_list(dummy_ui, equal_win_groups, equal_win_champions)
    assert [item["id"] for item in equal_win_list] == ["2", "1"]


def test_overlay_follow_height_never_exceeds_client_bottom() -> None:
    """跟随高度以 740 为基值；客户端更矮时压缩到客户端底缘，极矮时保住可用下限。"""

    from hextech.interfaces.desktop.app_shared import (
        WINDOW_BASE_HEIGHT,
        WINDOW_MIN_FOLLOW_HEIGHT,
        resolve_overlay_follow_height,
    )

    # 客户端足够高：维持基值
    assert resolve_overlay_follow_height(100, 1200) == WINDOW_BASE_HEIGHT
    # 1280x720 小客户端（窗口顶 y=80、内容区底缘 800）：下端不越过底缘
    assert resolve_overlay_follow_height(80, 800) == 720
    # 工作区底缘低于客户端底缘时取更小者（客户端伸到任务栏后面）
    assert resolve_overlay_follow_height(100, 1200, workarea_bottom=700) == 600
    # 极矮客户端：宁可轻微越界也保住窗口可用
    assert resolve_overlay_follow_height(500, 560) == WINDOW_MIN_FOLLOW_HEIGHT
    assert WINDOW_MIN_FOLLOW_HEIGHT < WINDOW_BASE_HEIGHT


def test_collapsed_width_budget_fits_avatar_and_tier_badge() -> None:
    """折叠态核心信息（头像 + T 级徽章）必须在基宽预算内完整可见。

    审查用真实 Tk 实测：80px 基宽叠加滚动条后头像被裁半、徽章完全不渲染。
    预算按 scale=1 常量推导（缩放已锁 1.0）。
    """

    from hextech.interfaces.desktop.app_shared import WINDOW_COLLAPSED_WIDTH

    left_padding = 10          # list_shell 左侧 padding（折叠态滚动条已隐藏）
    ribbon = 3 + 5             # 胜率色条及其右间距
    avatar = 48 + 2 * 1        # 头像位图 + 统一 1px 边框（高亮描边已删除）
    avatar_gap = 8
    # 徽章按 9pt bold（12px 基准）随 DPI 放大：150% 下"T1"+padx 约 30px。
    # 已知代价：200%+ 缩放下徽章可能被裁 2-6px（窗宽固定而磅值字体无上限），
    # 本轮接受，不为极端缩放扩大窗宽。
    badge_min = 30
    assert WINDOW_COLLAPSED_WIDTH >= left_padding + ribbon + avatar + avatar_gap + badge_min

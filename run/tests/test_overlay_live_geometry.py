"""事件仍新鲜时移动游戏窗口，Host 也必须使用当前客户区。"""
from unittest.mock import Mock

import pytest

from hextech.interfaces.overlay import host_geometry, host_sync
from hextech.modules.vision import window
from support.host_input import PreloadedInputObserver


@pytest.fixture(autouse=True)
def preloaded_input(monkeypatch):
    from hextech.interfaces.overlay import host_runner
    monkeypatch.setattr(host_runner, "HostInputObserver", PreloadedInputObserver)


def test_translation_invalidates_queued_mapping_without_hiding_canvas(monkeypatch):
    from hextech.interfaces.overlay import host_presentation
    root = Mock()
    old = (0, 0, 1920, 1080)
    new = (100, 50, 2020, 1130)
    visibility = {"target_hwnd": 200, "target_rect": old, "window_visible": True,
                  "prepared_shell_key": "keep", "render_semantic_key": "keep"}
    state = host_presentation._presentation_state(visibility)
    state.update(_token=7, _scheduled=True)
    monkeypatch.setattr(host_geometry, "is_window_renderable", lambda _: True)
    monkeypatch.setattr(host_geometry, "_window_client_rect", lambda *_a, **_kw: new)
    monkeypatch.setattr(host_geometry, "_apply_overlay_rect", lambda *_a: True)
    monkeypatch.setattr(host_geometry, "_ensure_overlay_window_styles", lambda *_a: None)
    host_geometry.refresh_bound_geometry(root, {}, visibility, (200, old))
    assert state["_token"] != 7
    assert not state["_scheduled"]
    assert visibility["window_visible"] is True
    assert visibility["prepared_shell_key"] == "keep"
    root.withdraw.assert_not_called()


def test_dpi_change_with_identical_rect_invalidates_old_surface():
    root = Mock()
    visibility = {"window_visible": True, "prepared_shell_key": "old", "render_semantic_key": "old"}
    host_geometry.refresh_display_geometry(root, visibility, {"dpi_scale": 1.0, "monitor_device": "left"})
    root.withdraw.assert_not_called()
    host_geometry.refresh_display_geometry(root, visibility, {"dpi_scale": 1.5, "monitor_device": "left"})
    root.withdraw.assert_called_once()
    assert visibility["window_visible"] is False
    assert "prepared_shell_key" not in visibility
    assert "render_semantic_key" not in visibility


def test_same_dpi_monitor_change_keeps_current_surface():
    from hextech.interfaces.overlay import host_presentation
    root = Mock()
    visibility = {"window_visible": True, "prepared_shell_key": "keep", "render_semantic_key": "keep"}
    host_geometry.refresh_display_geometry(root, visibility, {"dpi_scale": 1, "monitor_device": "left"})
    state = host_presentation._presentation_state(visibility)
    state.update(_token=10, _scheduled=True)
    host_geometry.refresh_display_geometry(root, visibility, {"dpi_scale": 1, "monitor_device": "right"})
    assert state["_token"] != 10 and not state["_scheduled"]
    assert visibility["window_visible"]
    assert visibility["prepared_shell_key"] == "keep"
    root.withdraw.assert_not_called()


@pytest.mark.parametrize("rect", [(-2560, 0, 0, 1600), (0, 0, 2560, 1440), (40, 50, 1960, 1130)])
def test_same_event_tracks_live_client_rect_before_drawing(monkeypatch, rect):
    root = Mock()
    snapshot = {"ok": True, "source": {"window_hwnd": 200, "client_rect": [0, 0, 1920, 1080]}}
    visibility = {}
    monkeypatch.setattr(host_sync, "is_window_renderable", lambda hwnd: hwnd == 200)
    monkeypatch.setattr(host_geometry, "is_window_renderable", lambda hwnd: hwnd == 200)
    monkeypatch.setattr(window.win32gui, "GetClientRect", lambda hwnd: (0, 0, rect[2]-rect[0], rect[3]-rect[1]))
    monkeypatch.setattr(window.win32gui, "ClientToScreen", lambda hwnd, point: rect[:2])
    apply_rect = Mock()
    monkeypatch.setattr(host_geometry, "_apply_overlay_rect", apply_rect)
    monkeypatch.setattr(host_geometry, "_ensure_overlay_window_styles", lambda *args: None)
    host_sync._refresh_target_window(root, {}, visibility, snapshot)
    assert visibility["target_rect"] == rect
    apply_rect.assert_called_once_with(root, rect)
    assert visibility["display_context_refresh"] is True
    assert visibility["geometry_source"] == "live_client_rect"


def test_failed_live_client_query_does_not_present_stale_or_outer_rect(monkeypatch):
    root = Mock()
    snapshot = {"ok": True, "source": {"window_hwnd": 200, "client_rect": [0, 0, 1920, 1080]}}
    visibility = {"target_hwnd": 200, "target_rect": (0, 0, 1920, 1080)}
    monkeypatch.setattr(host_sync, "is_window_renderable", lambda hwnd: True)
    monkeypatch.setattr(host_geometry, "is_window_renderable", lambda hwnd: True)
    monkeypatch.setattr(window.win32gui, "GetClientRect", lambda hwnd: (0, 0, 0, 0))
    monkeypatch.setattr(window.win32gui, "ClientToScreen", lambda hwnd, point: (0, 0))
    monkeypatch.setattr(window.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 1940, 1120))
    monkeypatch.setattr(host_geometry, "_apply_overlay_rect", lambda *args: None)
    monkeypatch.setattr(host_geometry, "_ensure_overlay_window_styles", lambda *args: None)
    host_sync._refresh_target_window(root, {}, visibility, snapshot)
    assert visibility["target_rect"] is None
    assert visibility["target_hwnd"] is None
    root.geometry.assert_not_called()


def test_native_same_snapshot_follows_moved_window(request, monkeypatch):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import tkinter as tk
    import win32gui
    from hextech.interfaces.desktop.window_activation import suppress_desktop_activation
    window.configure_process_dpi_awareness()
    with suppress_desktop_activation():
        game = tk.Tk()
        game.withdraw()
        game.overrideredirect(True)
        game.attributes("-alpha", 0.0)
        game.geometry("640x480+10000+10000")
        game.update_idletasks()
    hwnd = window.root_window_hwnd(game.winfo_id())
    root = Mock()
    visibility = {}
    snapshot = {"ok": True, "source": {"window_hwnd": hwnd, "client_rect": [10000, 10000, 10640, 10480]}}
    monkeypatch.setattr(host_sync, "is_window_renderable", lambda target: target == hwnd)
    monkeypatch.setattr(host_geometry, "is_window_renderable", lambda target: target == hwnd)
    monkeypatch.setattr(host_geometry, "_apply_overlay_rect", lambda *args: None)
    monkeypatch.setattr(host_geometry, "_ensure_overlay_window_styles", lambda *args: None)
    try:
        for x, y, width, height in ((-2200, 100, 800, 600), (120, 50, 960, 540)):
            win32gui.SetWindowPos(hwnd, 0, x, y, width, height, 0x0010 | 0x0004)
            host_sync._refresh_target_window(root, {}, visibility, snapshot)
            assert visibility["target_rect"] == (x, y, x+width, y+height)
    finally:
        game.destroy()


@pytest.mark.parametrize(
    "target_hwnd,live_rect,replace_surface",
    [
        pytest.param(200, (-2560, 0, 0, 1440), True, id="viewport-resize"),
        pytest.param(201, (0, 0, 1920, 1080), True, id="bound-hwnd-change"),
        pytest.param(200, (-1920, 40, 0, 1120), False, id="translation-only"),
    ],
)
def test_full_render_tick_never_presents_old_canvas_after_surface_change(
    monkeypatch, target_hwnd, live_rect, replace_surface,
):
    """同一选择已显示旧帧；新数据尚未准备时也须先撤旧面，再画新 viewport shell。

    执行真实 refresh/visibility/shell/present-model 调度，仅替换 Win32、Canvas
    绘制和后台准备边界；纯平移作为不应隐藏或重画 shell 的对照。
    """
    from queue import Queue
    from types import SimpleNamespace

    from hextech.interfaces.overlay import host_render_state, host_runner

    event = {
        "ok": True, "active": True, "visible": True, "selection_type": "hextech",
        "source": {
            "session_id": "session-1", "game_instance_id": "game-1",
            "selection_epoch": 1, "selection_revision": 7,
            "selection_window_active": True, "scene_state": "active",
            "window_hwnd": target_hwnd, "client_rect": [0, 0, 1920, 1080],
        },
        "slots": [
            {"state": "ready", "slot_generation": 1, "augment_id": f"augment-{index}"}
            for index in range(3)
        ],
    }
    shell_key = (host_runner.selection_key(event), host_runner.snapshot_slot_generations(event))
    old_render_key = ("already-drawn-1920-layout",)
    visibility = {
        "user_enabled": True, "window_visible": True,
        "target_hwnd": 200, "target_rect": (0, 0, 1920, 1080),
        "applied_target_hwnd": 200, "applied_geometry": "1920x1080+0+0",
        "prepared_shell_key": shell_key, "render_semantic_key": old_render_key,
    }
    trace = []
    surface = {"visible": True, "viewport": (1920, 1080), "hwnd": 200}
    scheduled = []
    root = Mock()

    def withdraw():
        trace.append(("hide",))
        surface["visible"] = False

    def geometry(value):
        trace.append(("geometry", surface["visible"],
                      visibility.get("prepared_shell_key"), visibility.get("render_semantic_key")))

    def draw(_canvas, _model, *, viewport_size, **_kwargs):
        surface.update(viewport=viewport_size, hwnd=visibility["target_hwnd"])
        trace.append(("draw", viewport_size))

    def present(*_args):
        trace.append(("present", surface["viewport"], surface["hwnd"]))
        surface["visible"] = True

    root.withdraw.side_effect = withdraw
    root.geometry.side_effect = geometry
    root.update_idletasks.side_effect = lambda: trace.append(("flush",))
    canvas = SimpleNamespace(
        winfo_width=lambda: 1920, winfo_height=lambda: 1080,
        update_idletasks=lambda: None,
        after=lambda delay, callback: scheduled.append((delay, callback)) or "scheduled",
    )
    preparation = SimpleNamespace(
        request=Mock(return_value="new-request"), status=lambda: {},
        poll=Mock(return_value=None),
    )
    gate = SimpleNamespace(evaluate=lambda *_a, **_k: SimpleNamespace(
        state="confirmed", reason="context_confirmed", context_revision=1, held=False,
        payload={"ok": True, "champion_id": "4"},
    ))
    monkeypatch.setattr(host_runner, "ContextRenderGate", lambda: gate)
    monkeypatch.setattr(host_sync, "is_window_renderable", lambda hwnd: True)
    monkeypatch.setattr(host_geometry, "is_window_renderable", lambda hwnd: True)
    monkeypatch.setattr(host_geometry, "_window_client_rect", lambda *_a, **_k: live_rect)
    monkeypatch.setattr(host_geometry, "_apply_overlay_rect", lambda *_a: True)
    monkeypatch.setattr(host_geometry, "_ensure_overlay_window_styles", lambda *_a: None)
    monkeypatch.setattr(host_sync, "_root_hwnd", lambda _root: 300)
    monkeypatch.setattr(host_sync, "_is_game_window_foreground", lambda *_a, **_k: True)
    monkeypatch.setattr(host_sync, "_refresh_gameflow_in_progress", lambda *_a: True)
    monkeypatch.setattr(host_sync, "_log_visibility_diagnostic", lambda *_a, **_k: None)
    monkeypatch.setattr(host_sync, "ensure_overlay_presentation", present)
    display_context = Mock(return_value={"status": "available", "monitor_device": "target", "dpi_scale": 1.5})
    monkeypatch.setattr(host_runner, "window_display_context", display_context)
    monkeypatch.setattr(host_runner, "is_scoreboard_key_down", lambda: False)
    monkeypatch.setattr(host_render_state, "draw_overlay_frame", draw)
    for module in (host_sync, host_runner):
        monkeypatch.setattr(module, "_write_host_visibility_status", lambda *_a, **_k: None)
    monkeypatch.setattr(host_runner, "_write_overlay_session_report", lambda *_a, **_k: None)

    host_runner._schedule_event_render(
        root, canvas, {}, visibility, Queue(),
        data_source=SimpleNamespace(read_event=lambda: event, read_context=lambda: {}),
        data_preparation=preparation,
    )

    viewport = (live_rect[2] - live_rect[0], live_rect[3] - live_rect[1])
    assert visibility["consecutive_render_failures"] == 0, trace
    display_context.assert_called_once_with(target_hwnd, force=True)
    assert scheduled, "完整 tick 必须继续调度，而非异常退出"
    operations = [item[0] for item in trace]
    if replace_surface:
        assert "hide" in operations, trace
        assert operations.index("hide") < operations.index("geometry") < operations.index("draw"), trace
        assert next(item for item in trace if item[0] == "geometry") == ("geometry", False, None, None)
        assert operations.index("draw") < operations.index("present"), trace
        assert [item for item in trace if item[0] == "present"] == [("present", viewport, target_hwnd)]
        assert visibility["prepared_shell_key"] == shell_key
        assert "render_semantic_key" not in visibility
        preparation.poll.assert_not_called()  # 新 shell 不得等待后台数据。
        preparation.request.assert_not_called()
    else:
        assert "hide" not in operations and "draw" not in operations, trace
        assert visibility["prepared_shell_key"] == shell_key
        assert visibility["render_semantic_key"] == old_render_key
        assert [item for item in trace if item[0] == "present"] == [("present", viewport, target_hwnd)]
        preparation.poll.assert_called_once_with("new-request")
    if replace_surface:
        scheduled[0][1]()  # 首帧映射之后才进入数据准备。
    assert preparation.request.call_args.kwargs["viewport_size"] == viewport

"""桌面多屏几何合同；不启动正式服务或修改显示设置。"""
from itertools import product

import pytest

from hextech.interfaces.desktop.responsive_layout import (
    DesktopMonitor,
    drag_origin,
    layout_mode,
    resolve_auto_desktop_layout,
    resolve_desktop_layout,
    resolve_manual_desktop_layout,
)


MIXED_MONITORS = (
    DesktopMonitor("left-150", (-2560, 0, 0, 1528), 1.5),
    DesktopMonitor("right-100", (0, 0, 2560, 1392), 1.0),
)


@pytest.mark.parametrize("screen,dpi,cw,placement", list(product(
    [(1920, 1080), (1920, 1200), (2560, 1440), (2560, 1600)],
    [1, 1.25, 1.5, 1.75, 2], [1024, 1280, 1600, 1920], ["center", "edge", "exact", "short"],
)))
def test_monitor_client_matrix(screen, dpi, cw, placement):
    width, height = screen
    remaining = {"center": (width-cw)//2, "edge": 0, "exact": int(200*dpi), "short": int(200*dpi)-1}[placement]
    right = width - remaining
    client = (right-cw, 30, right, min(height-60, 30+cw*9//16))
    work = (0, 0, width, height-40)
    result = resolve_desktop_layout(client, work, dpi_scale=dpi)
    assert result.font(8)[1] <= -int(12*dpi)
    if remaining < 200*dpi:
        assert result.rect is None and result.reason == "right_width_insufficient"
    else:
        assert result.rect is not None
        x, y, end, bottom = result.rect
        assert x == right and x < end <= width and 0 <= y < bottom <= client[3]
        assert (end-x)/dpi >= 200


def test_high_dpi_centered_client_uses_available_width():
    result = resolve_desktop_layout((320, 200, 2240, 1280), (0, 0, 2560, 1552), dpi_scale=1.5)
    assert result.rect == (2240, 200, 2560, 1280)
    assert result.mode == "minimum" and result.client_scale == 1


def test_hysteresis_only_delays_expansion():
    assert layout_mode(229, "normal") == "minimum"
    assert layout_mode(235, "minimum") == "minimum"
    assert layout_mode(238, "minimum") == "narrow"
    assert layout_mode(305, "narrow") == "narrow"
    assert layout_mode(308, "narrow") == "normal"
    assert layout_mode(299, "normal") == "narrow"


def test_negative_origin_and_invalid_geometry():
    result = resolve_desktop_layout((-2200, -800, -920, -80), (-2560, -1000, 0, 0))
    assert result.rect == (-920, -800, -600, -80)
    assert resolve_desktop_layout((0, 0, 1, 1), (0, 20, 1000, 1000)).reason == "right_height_insufficient"
    assert resolve_desktop_layout(None, None, dpi_scale=float("nan")).reason == "display_context_unavailable"


def test_activation_guard_releases_on_exception():
    from hextech.interfaces.desktop.window_activation import suppress_desktop_activation, _retained_hooks
    with pytest.raises(ValueError, match="fixture"):
        with suppress_desktop_activation():
            assert len(_retained_hooks) == 1
            raise ValueError("fixture")
    assert not _retained_hooks


def test_monitor_transition_keeps_auto_dock_despite_drag_and_restore_requests():
    from test_desktop_window_presentation import controller, observation
    ui, owner = controller()
    owner.apply_latest(now=10.1)
    owner.begin_manual_drag((1600, 120))
    owner.drag_to_cursor((1100, 220))
    assert owner.manual_rect is None
    owner.publish_window(observation(observed_at=10.2, monitor_device="new-monitor",
                                     client_rect=(200, 100, 1480, 820)))
    owner.apply_latest(now=10.2)
    assert owner.position_mode == "auto"
    assert owner.manual_rect is None
    assert ui.root.rect == (1480, 100, 1800, 820)
    assert ui._auto_follow_enabled

    owner.request_restore_auto_docking(reason="test_restore")
    owner.apply_latest(now=10.3)
    assert owner.position_mode == "auto"
    assert ui._auto_follow_enabled


def test_legacy_adjacent_layout_math_does_not_authorize_production_docking():
    result = resolve_auto_desktop_layout(
        (-1920, 100, 0, 900),
        MIXED_MONITORS,
        client_monitor_device="left-150",
        client_dpi_scale=1.5,
    )
    assert result.rect == (0, 100, 320, 840)
    assert result.monitor_device == "right-100"
    assert result.dpi_scale == 1.0
    assert "left-150:right_width_insufficient" in result.candidate_rejections
    # 兼容纯几何 helper 仍有调用方；生产只解析客户端同屏 workarea。
    from test_desktop_window_presentation import decide, observation
    decision = decide(observation(
        client_rect=(-1920, 100, 0, 900), workarea=MIXED_MONITORS[0].workarea,
        monitors=MIXED_MONITORS, monitor_device="left-150", dpi_scale=1.5,
    ))
    assert not decision.visible and decision.reason == "right_width_insufficient"
    assert decision.rect is None


def test_auto_dock_rejects_blank_gap_and_short_vertical_intersection():
    gap = resolve_auto_desktop_layout(
        (-1920, 100, 0, 900),
        (
            DesktopMonitor("left", (-2560, 0, 0, 1528), 1.5),
            DesktopMonitor("gap", (100, 0, 2660, 1392), 1.0),
        ),
        client_monitor_device="left",
        client_dpi_scale=1.5,
    )
    assert gap.rect is None and gap.reason == "no_valid_dock_target"
    assert "gap:non_adjacent_blank_gap" in gap.candidate_rejections

    short = resolve_auto_desktop_layout(
        (-1920, 1300, 0, 1500),
        MIXED_MONITORS,
        client_monitor_device="left-150",
        client_dpi_scale=1.5,
    )
    assert short.rect is None and short.reason == "no_valid_dock_target"
    assert "right-100:vertical_intersection_insufficient" in short.candidate_rejections


def test_manual_layout_uses_actual_screen_and_settles_inside_valid_workarea():
    moving = resolve_manual_desktop_layout(
        (-100, 100, 380, 1180),
        MIXED_MONITORS,
        source_dpi_scale=1.5,
        client_scale=1.0,
        cursor=(50, 120),
        settle=False,
    )
    assert moving.rect == (-100, 100, 220, 820)
    assert moving.monitor_device == "right-100"

    settled = resolve_manual_desktop_layout(
        (2500, 1350, 2820, 2070),
        MIXED_MONITORS,
        source_dpi_scale=1.0,
        client_scale=1.0,
        cursor=(2600, 1400),
        settle=True,
    )
    assert settled.rect == (2240, 672, 2560, 1392)
    from test_desktop_window_presentation import decide
    decision = decide(position_mode="manual", manual_rect=settled.rect, manual_open=True)
    assert decision.visible and decision.rect == (1580, 100, 1900, 820)
    assert not decision.topmost


def test_cross_dpi_drag_scales_physical_anchor_to_keep_same_logical_grab_point():
    assert drag_origin((100, 250), (240, 90), source_dpi_scale=1.5, target_dpi_scale=1.0) == (-60, 190)


def test_monitor_topology_keeps_last_complete_probe_but_accepts_real_disconnect(monkeypatch):
    import win32api
    from hextech.interfaces.desktop import runtime_window

    previous = MIXED_MONITORS
    assert runtime_window._select_desktop_monitor_topology(previous, None) == previous
    assert runtime_window._select_desktop_monitor_topology(previous, previous[:1]) == previous[:1]

    handles = [(1, None, None), (2, None, None)]
    monkeypatch.setattr(win32api, "EnumDisplayMonitors", lambda: handles)
    monkeypatch.setattr(win32api, "GetMonitorInfo", lambda handle: {
        "Device": f"screen-{handle}", "Work": (0, 0, 100, 100), "Monitor": (0, 0, 100, 100),
    })
    monkeypatch.setattr(runtime_window, "_monitor_dpi",
                        lambda handle: 1.0 if handle == 1 else (_ for _ in ()).throw(ValueError("gone")))
    assert runtime_window._desktop_monitors() is None


def test_real_pywintypes_errors_fail_closed_without_discarding_topology(monkeypatch):
    import pywintypes
    import win32api
    from hextech.interfaces.desktop import runtime_window

    error = pywintypes.error(0, "display-probe", "temporarily unavailable")
    monkeypatch.setattr(win32api, "EnumDisplayMonitors", lambda: (_ for _ in ()).throw(error))
    assert runtime_window._desktop_monitors() is None

    monkeypatch.setattr(win32api, "EnumDisplayMonitors", lambda: [(1, None, None)])
    monkeypatch.setattr(win32api, "GetMonitorInfo", lambda _monitor: (_ for _ in ()).throw(error))
    assert runtime_window._desktop_monitors() is None

    monkeypatch.setattr(runtime_window, "window_display_context",
                        lambda *_args, **_kwargs: {"dpi_scale": 1.0, "status": "available"})
    monkeypatch.setattr(win32api, "MonitorFromWindow", lambda *_args: (_ for _ in ()).throw(error))
    assert runtime_window._client_display_context(999)["status"] == "unavailable"


def test_desktop_production_modules_stay_within_line_budget():
    from pathlib import Path
    from hextech.interfaces.desktop import app_view

    assert len(Path(app_view.__file__).read_text(encoding="utf-8").splitlines()) <= 800


def test_external_client_dpi_awareness_does_not_override_monitor(monkeypatch):
    import win32api
    from hextech.interfaces.desktop import runtime_window
    monkeypatch.setattr(runtime_window, "window_display_context", lambda *a, **k: {"dpi_scale": 1, "status": "available"})
    monkeypatch.setattr(win32api, "MonitorFromWindow", lambda *args: 123)
    def query(handle, kind, x, y):
        x._obj.value = y._obj.value = 144
        return 0
    monkeypatch.setattr(runtime_window.ctypes.windll.shcore, "GetDpiForMonitor", query)
    assert runtime_window._client_display_context(999)["dpi_scale"] == 1.5


def test_failed_monitor_dpi_does_not_guess_primary_screen(monkeypatch):
    import win32api
    from hextech.interfaces.desktop import runtime_window
    monkeypatch.setattr(runtime_window, "window_display_context", lambda *a, **k: {"dpi_scale": 1})
    monkeypatch.setattr(win32api, "MonitorFromWindow", lambda *args: 123)
    monkeypatch.setattr(runtime_window.ctypes.windll.shcore, "GetDpiForMonitor", lambda *args: -1)
    assert runtime_window._client_display_context(999)["dpi_scale"] == 0


def test_drag_roundtrip_across_mixed_dpi_cannot_move_production_dock():
    from test_desktop_window_presentation import controller, observation
    ui, owner = controller()
    owner.publish_window(observation(
        client_rect=(-2500, 100, -580, 900), workarea=MIXED_MONITORS[0].workarea,
        monitors=MIXED_MONITORS, monitor_device="left-150", dpi_scale=1.5,
    ))
    initial = owner.apply_latest(now=10.1)
    # FakeUI 不执行真实控件重排；补上生产 _apply_desktop_layout 保存的布局。
    ui._desktop_layout = owner.apply_latest(now=10.1).layout
    # 当前同屏外贴：480px、150%；拖动兼容入口必须保持无效。
    assert initial.rect == (-580, 100, -100, 900)
    owner.begin_manual_drag((-340, 120))
    owner.drag_to_cursor((-1000, 200))
    left = owner.apply_latest(now=10.1).rect
    assert owner.manual_rect is None
    assert left is not None and left[2]-left[0] == 480
    assert left[:2] == (-580, 100)
    owner.drag_to_cursor((1000, 200))
    right = owner.apply_latest(now=10.1).rect
    assert right is not None and right[2]-right[0] == 480
    assert right[:2] == (-580, 100)
    owner.end_manual_drag()
    assert owner.position_mode == "auto"
    # 超过旧 8 秒回吸期限；只有客户端实际换屏能改变停靠位置和 DPI。
    owner.publish_window(observation(observed_at=30, monitors=MIXED_MONITORS))
    owner.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                        client_hwnd=100, observed_at=30)
    moved = owner.apply_latest(now=30.1)
    assert moved.rect == (1580, 100, 1900, 820)
    assert moved.layout.dpi_scale == 1.0
    assert owner.manual_rect is None
    assert ui._auto_follow_enabled
    owner.close()

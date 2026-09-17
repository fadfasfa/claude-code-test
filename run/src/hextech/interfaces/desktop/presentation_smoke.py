"""显式冻结包桌面呈现烟测：只创建/采样自有窗口，不接触真实客户端。"""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
import json
import os
import socket
import subprocess
import time
from unittest.mock import patch


RUNTIME_CONTRACT = "desktop-independent-panel-v1"
FOOTPRINT_MARKER = "hextech-desktop-presentation-smoke-v1"


def _require(condition: bool, message: str) -> None:
    # Frozen executables can use optimization; acceptance must not depend on assert.
    if not condition:
        raise RuntimeError(message)


def _pump(root) -> None:
    import _tkinter
    deadline = time.monotonic() + .1
    for _ in range(256):
        if time.monotonic() >= deadline or not root.tk.dooneevent(
            _tkinter.WINDOW_EVENTS | _tkinter.IDLE_EVENTS | _tkinter.DONT_WAIT
        ):
            break


def _above(win32gui, upper: int, lower: int) -> bool:
    """Compare owned fixture windows; hidden top-level windows also consume Z-order."""
    if upper == lower:
        return False
    windows: set[int] = set()

    def collect(hwnd, _context):
        windows.add(int(hwnd))
        return True

    # The old fixed 128/256 limit returned false with hundreds of unrelated
    # hidden windows between a topmost panel and a normal fixture blocker.
    win32gui.EnumWindows(collect, None)
    if upper not in windows or lower not in windows:
        raise RuntimeError("fixture window missing from Z-order snapshot")
    visited: set[int] = set()
    current = win32gui.GetWindow(lower, 3)
    for _ in range(len(windows)):
        if not current:
            return False
        if current in visited:
            raise RuntimeError("fixture Z-order traversal cycle")
        visited.add(current)
        if current == upper:
            return True
        current = win32gui.GetWindow(current, 3)
    if not current:
        return False
    raise RuntimeError("fixture Z-order changed beyond snapshot budget")


def _control_pixels(ui, hwnd: int) -> dict:
    """只读本窗口两个控件的背景点，不截桌面、不保存图片。"""
    import win32gui
    controls = {"canvas": ui.canvas, "title_frame": ui.title_frame}
    samples = {}
    for name, widget in controls.items():
        # 左下角避开标题文字，canvas靠右背景避开内嵌列表。
        local_x = widget.winfo_width() - 3
        local_y = widget.winfo_height() - 3
        x, y = widget.winfo_rootx() + local_x, widget.winfo_rooty() + local_y
        pointed = win32gui.WindowFromPoint((x, y))
        _require(pointed == hwnd or bool(win32gui.IsChild(hwnd, pointed)),
                 f"pixel point not covered by own panel: {name}")
        dc = win32gui.GetDC(widget.winfo_id())
        try:
            color = win32gui.GetPixel(dc, local_x, local_y)
        finally:
            win32gui.ReleaseDC(widget.winfo_id(), dc)
        _require(color != -1, f"GetPixel failed: {name}")
        actual = (color & 255, (color >> 8) & 255, (color >> 16) & 255)
        expected = tuple(component // 257 for component in widget.winfo_rgb(widget.cget("bg")))
        _require(actual == expected, f"opaque background mismatch: {name}: {actual} != {expected}")
        samples[name] = {"rgb": actual, "expected_rgb": expected, "point": (x, y)}
    return samples


def run_desktop_presentation_smoke() -> dict:
    """真实HextechUI/controller/native层全链；只替换外部I/O与前台输入。

    fixture不是League进程：真实前台从未切换。查询前台的外部输入被显式注入为
    已知自有fixture HWND，真实窗口操作、owner检查、布局、map与租约不被mock。
    入口应由独立子进程调用；任何失败均抛异常，使冻结包门禁失败。
    """
    import win32api
    import win32gui
    import win32process
    from . import app, runtime_services
    from .client_layer import desktop_wrapper_hwnd
    from .responsive_layout import DesktopMonitor, resolve_desktop_layout
    from .window_activation import suppress_desktop_activation
    from .window_presentation import DesktopWindowObservation
    from hextech.modules.session.build_identity import current_build_id
    from hextech.modules.vision.window import configure_process_dpi_awareness

    configure_process_dpi_awareness()
    native_foreground = win32gui.GetForegroundWindow
    original_foreground = native_foreground()
    started = time.perf_counter()
    windows: list[int] = []
    ui = None
    fixture_class = f"RCLIENT.HextechDesktopSmoke.{os.getpid()}.{time.time_ns()}"
    instance = win32api.GetModuleHandle(None)
    brush = win32gui.CreateSolidBrush(0x00FF00FF)
    atom = 0
    report: dict = {}

    class SmokeUI(app.HextechUI):
        def _start_desktop_tray(self):
            pass

        def _schedule_post_visible_bootstrap(self):
            pass

        def _request_avatar(self, row):
            # Layout/card drawing is real; avatar network/cache I/O is out of scope.
            pass

    def new_window(rect, *, topmost=False):
        left, top, right, bottom = rect
        hwnd = win32gui.CreateWindowEx(
            0x08000000 | 0x80 | (8 if topmost else 0), atom,
            FOOTPRINT_MARKER, 0x80000000, left, top, right-left, bottom-top,
            0, 0, instance, None,
        )
        windows.append(hwnd)
        win32gui.SetWindowPos(hwnd, -1 if topmost else 0, left, top, right-left, bottom-top,
                              0x0010 | 0x0040 | 0x0200)
        _require(win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid(), "fixture not owned")
        return hwnd

    try:
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = instance
        window_class.lpszClassName = fixture_class
        window_class.lpfnWndProc = win32gui.DefWindowProc
        window_class.hbrBackground = brush
        atom = win32gui.RegisterClass(window_class)
        monitor_info = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), 2))
        workarea = tuple(int(value) for value in monitor_info["Work"])
        left, top, right, bottom = workarea
        _require(right-left >= 850 and bottom-top >= 600, "smoke requires 850x600 workarea")
        client_rect = (left+20, top+20, left+532, min(bottom-20, top+820))
        client = new_window(client_rect)
        client_rect = win32gui.GetWindowRect(client)
        layout = resolve_desktop_layout(client_rect, workarea, dpi_scale=1.0)
        _require(layout.rect is not None, "fixture has no dock space")
        panel_rect = layout.rect
        backdrop = new_window(panel_rect, topmost=True)
        foreground_input = {"hwnd": client}
        with ExitStack() as isolation:
            # No config reads, background workers, services or genuine client polling.
            isolation.enter_context(patch.object(app, "load_ui_feature_flags", return_value={
                "web_frontend_enabled": False, "game_overlay_enabled": False,
                "private_policy_stats_enabled": False, "low_frequency_listener_enabled": False,
            }))
            isolation.enter_context(patch.object(runtime_services, "initialize_window_threads", lambda _ui: None))
            isolation.enter_context(patch.object(socket.socket, "connect", side_effect=RuntimeError("smoke forbids network")))
            isolation.enter_context(patch.object(subprocess, "Popen", side_effect=RuntimeError("smoke forbids service processes")))
            isolation.enter_context(patch.object(win32gui, "GetForegroundWindow", lambda: foreground_input["hwnd"]))
            with suppress_desktop_activation():
                # Retain the partially initialized instance for cleanup even if __init__ fails.
                ui = SmokeUI.__new__(SmokeUI)
                SmokeUI.__init__(ui)
            controller = ui._desktop_window_presentation
            if controller._after_id is not None:
                ui.root.after_cancel(controller._after_id)
                controller._after_id = None
            ui.root.attributes("-alpha", 1.0)
            ui._ensure_card_state()
            ui._render_candidate_cards([
                {"id": "smoke-fixture", "name": "独立面板烟测", "tier": "T1",
                 "win": .525, "pick": .151, "selection_role": "self"},
            ])
            observation = DesktopWindowObservation(
                client_hwnd=client, foreground_hwnd=client, client_rect=client_rect,
                workarea=workarea, client_visible=True, client_active=True,
                observed_at=time.time(), dpi_scale=1.0, monitor_device="owned-smoke-fixture",
                monitors=(DesktopMonitor("owned-smoke-fixture", workarea, 1.0, workarea),),
            )
            controller.publish_window(observation)
            first_show_started = time.perf_counter()
            controller.apply_latest()
            _pump(ui.root)
            hwnd = desktop_wrapper_hwnd(ui.root)
            _require(win32gui.GetWindow(hwnd, 4) == 0, "no external owner contract violated")
            _require(bool(ui.root.winfo_ismapped()) and bool(win32gui.IsWindowVisible(hwnd)), "panel not mapped")
            _require(win32gui.GetWindowRect(hwnd) == panel_rect, "native panel rect differs from target")
            _require(controller.status()["visible"], "controller did not confirm visibility")
            _require(controller.status()["layer"]["desired_topmost"], "lease not granted")
            controls = {}
            for name in ("title_bar", "refresh_button", "diagnostics_button", "canvas", "status_line_label"):
                widget = getattr(ui, name)
                _require(bool(widget.winfo_ismapped()), f"key control not mapped: {name}")
                controls[name] = {"mapped": True, "width": widget.winfo_width(), "height": widget.winfo_height()}
            _require(float(ui.root.attributes("-alpha")) == 1.0, "panel is not opaque")
            ctypes.windll.dwmapi.DwmFlush()
            pixels = _control_pixels(ui, hwnd)
            first_show_ms = (time.perf_counter()-first_show_started)*1000
            _require(first_show_ms <= 300, f"first opaque display exceeded 300ms: {first_show_ms:.3f}")
            _require(native_foreground() == original_foreground, "smoke changed real foreground")
            _require(not ui.threads, "smoke unexpectedly started background threads")
            initial_status = controller.status()
            blockers = []
            for topmost in (False, True):
                blocker = new_window(panel_rect, topmost=topmost)
                win32gui.SetWindowPos(blocker, -1 if topmost else 0, 0, 0, 0, 0,
                                      0x0010 | 0x0001 | 0x0002 | 0x0200)
                before = {"layer": controller.status().get("layer"), "panel_above": _above(win32gui, hwnd, blocker)}
                # Wait the production 100ms probe interval; do not mutate lease state.
                time.sleep(.11)
                recovery_started = time.perf_counter()
                controller.apply_latest()
                _pump(ui.root)
                above = _above(win32gui, hwnd, blocker)
                after = controller.status()
                occluder = ((after.get("layer") or {}).get("occlusion") or {}).get("occluder_hwnd", 0)
                failure_detail = {
                    "blocker_topmost": topmost, "blocker_hwnd": blocker,
                    "wrapper_hwnd": hwnd, "current_wrapper_hwnd": desktop_wrapper_hwnd(ui.root),
                    "inner_hwnd": int(ui.root.winfo_id()), "before": before,
                    "after": after, "panel_above": above,
                    "occluder_scope": "none" if not occluder else "owned-fixture" if occluder in windows else "outside-fixture",
                    "recovery_ms": (time.perf_counter()-recovery_started)*1000,
                    "foreground_unchanged": native_foreground() == original_foreground,
                }
                _require(above, "panel remained below owned blocker: " + json.dumps(failure_detail, ensure_ascii=False))
                _require(win32gui.GetWindow(hwnd, 4) == 0, "blocker recovery introduced owner")
                blockers.append({"topmost": topmost, "panel_above": True})
                win32gui.DestroyWindow(blocker)
                windows.remove(blocker)
            foreground_input["hwnd"] = 0
            hide_started = time.perf_counter()
            controller.apply_latest()
            hidden_ms = (time.perf_counter()-hide_started)*1000
            _require(not win32gui.IsWindowVisible(hwnd) and not ui.root.winfo_ismapped(), "lost foreground did not hide panel")
            _require(not win32gui.GetWindowLong(hwnd, -20) & 8, "lost foreground did not revoke topmost")
            _require(hidden_ms <= 100, f"hide exceeded 100ms: {hidden_ms:.3f}")
            _require(native_foreground() == original_foreground, "smoke changed real foreground")
            report = {
                "state": "ok", "build_id": current_build_id(), "runtime_contract": RUNTIME_CONTRACT,
                "footprint_marker": FOOTPRINT_MARKER, "owner_hwnd": 0, "no_external_owner": True,
                "wrapper_hwnd": hwnd, "inner_hwnd": int(ui.root.winfo_id()), "mapped": True,
                "actual_rect": panel_rect, "alpha": 1.0, "controls": controls, "pixels": pixels,
                "layer": initial_status["layer"], "blockers": blockers, "hide_ms": hidden_ms,
                "first_show_ms": first_show_ms,
                "foreground_unchanged": True,
                "fixture": {"kind": "self_owned_RCLIENT_like", "hwnd": client,
                            "process_id": os.getpid(), "class": fixture_class,
                            "observation": "native_fixture_rect_with_synthetic_foreground_and_dpi",
                            "league_process_identity_verified": False, "real_league_acceptance": False},
                "footprint": {"network_requests": 0, "service_processes": 0, "avatar_cache_writes": 0,
                              "screenshots_saved": 0, "external_window_mutations": 0,
                              "backdrop_hwnd": backdrop},
            }
    finally:
        try:
            if ui is not None and getattr(ui, "root", None) is not None:
                ui.root.destroy()
        finally:
            try:
                for hwnd in reversed(windows):
                    if win32gui.IsWindow(hwnd):
                        win32gui.DestroyWindow(hwnd)
                if atom:
                    win32gui.UnregisterClass(fixture_class, instance)
            finally:
                # Registered class background brushes are deleted by UnregisterClass.
                if not atom:
                    win32gui.DeleteObject(brush)
    report["resources_closed"] = all(not win32gui.IsWindow(hwnd) for hwnd in
                                     [*windows, report["wrapper_hwnd"], report["inner_hwnd"]])
    _require(report["resources_closed"], "fixture leaked native resources")
    report["elapsed_ms"] = (time.perf_counter()-started)*1000
    return report

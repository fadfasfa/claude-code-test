"""原生控件边界与状态保持；完整桌面在隔离子进程运行。"""
from dataclasses import replace

from hextech.interfaces.desktop.responsive_layout import DesktopLayout


def test_native_responsive_controls(request, monkeypatch):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import tkinter as tk
    from tkinter.font import Font
    from hextech.interfaces.desktop.app import HextechUI

    monkeypatch.setattr(HextechUI, "_start_desktop_tray", lambda self: None)
    monkeypatch.setattr(HextechUI, "_schedule_post_visible_bootstrap", lambda self: None)
    monkeypatch.setattr(HextechUI, "_load_and_set_img", lambda *args: None)
    monkeypatch.setattr(HextechUI, "_request_avatar", lambda *args: None)
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_services.initialize_window_threads", lambda ui: None)
    ui = HextechUI()
    ui._desktop_window_presentation.close()
    ui.root.attributes("-alpha", 0.0)
    ui.root.geometry("320x1000+10000+10000")
    from hextech.modules.vision.window import root_window_hwnd
    import win32gui
    hwnd = root_window_hwnd(ui.root.winfo_id())
    win32gui.SetWindowLong(hwnd, -20, win32gui.GetWindowLong(hwnd, -20) | 0x08000000)
    ui.root.deiconify()
    ui.root.update()
    ui._ensure_card_state()
    rows = [{"id": str(i), "name": "荣耀行刑官德莱文测试超长名字", "tier": "T1",
             "win": 1.0, "pick": .999, "selection_role": "self"} for i in range(12)]
    try:
        ui._render_candidate_cards(rows)
        identity = ui._card_rows["0"]["card"]
        enabled = ui.game_overlay_var.get()
        from itertools import product
        for dpi, (width, mode), client_scale in product(
            (1, 1.25, 1.5, 1.75, 2), ((320, "normal"), (260, "narrow"), (200, "minimum")), (.8, 1, 1.25),
        ):
            layout = DesktopLayout((10000, 10000, 10000+round(width*dpi), 10000+round(700*dpi)),
                                   dpi, client_scale, width, mode, "")
            minimum = ui._apply_desktop_layout(layout)
            ui.root.update_idletasks()
            assert minimum < 700*dpi
            assert ui._card_rows["0"]["card"] is identity and ui.game_overlay_var.get() == enabled
            assert {int(t["frame"].grid_info()["row"]) for t in ui._feature_toggle_widgets} == {
                "normal": {0}, "narrow": {0, 1}, "minimum": {0, 1, 2}}[mode]
            widgets = [ui.title_bar, ui.exit_button, ui.refresh_button, ui.diagnostics_button, ui.status_line_label]
            widgets += [t["label"] for t in ui._feature_toggle_widgets]
            row = ui._card_rows["0"]
            widgets += [row[k] for k in ("img_label", "name_label", "tier_badge", "win_label", "pick_label", "selected_badge")]
            for widget in widgets:
                assert widget.winfo_ismapped(), (mode, str(widget))
                left = widget.winfo_rootx()-ui.root.winfo_rootx()
                assert 0 <= left and left+widget.winfo_width() <= ui.root.winfo_width(), (mode,dpi,widget,left,widget.winfo_width())
                if isinstance(widget, (tk.Label, tk.Button)) and widget.cget("text"):
                    f = Font(root=ui.root, font=widget.cget("font"))
                    assert abs(f.actual("size")) > 0
                    assert all(f.measure(line) <= widget.winfo_width() for line in widget.cget("text").splitlines()), (mode,dpi,widget.cget("text"),widget.winfo_width())
            ui._status_channels["service"].update(text="这是一段必须按实际字体测量并换成两行的状态信息"*3, color="#F38BA8")
            ui._render_status_line()
            assert len(ui.status_line_label.cget("text").splitlines()) <= 2
            import os
            from pathlib import Path
            qa_root = os.environ.get("HEXTECH_UI_QA_DIR")
            if qa_root and dpi == 1 and client_scale == 1:
                from native_ui_capture import capture_test_window
                ui.canvas.yview_moveto(0)
                capture_test_window(ui.root, Path(qa_root) / f"desktop-{mode}.png")
            ui.canvas.yview_moveto(.5)
            original_first = ui.canvas.canvasy(0)
            ui._apply_desktop_layout(replace(layout, mode="narrow" if mode=="normal" else mode, logical_width=width-1))
            assert ui.canvas.canvasy(0) > 0 or original_first <= 0
    finally:
        ui.root.destroy()


def test_native_mixed_dpi_dock_ignores_drag_and_follows_client_display_geometry(request, monkeypatch):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import time
    import win32gui
    from hextech.interfaces.desktop.app import HextechUI
    from hextech.interfaces.desktop.responsive_layout import DesktopMonitor
    from test_desktop_window_presentation import observation

    # HWND 100 是 synthetic geometry 输入；不对它执行真实 owner/foreground API。
    monkeypatch.setattr(HextechUI, "_bind_client_layer", lambda self, hwnd: None)
    monkeypatch.setattr(HextechUI, "_desktop_client_is_foreground", lambda self, hwnd: hwnd == 100)
    monkeypatch.setattr(HextechUI, "_maintain_foreground_layer", lambda self, hwnd, eligible:
                        {"desired_topmost": eligible, "actual_topmost": False})
    monkeypatch.setattr(HextechUI, "_start_desktop_tray", lambda self: None)
    monkeypatch.setattr(HextechUI, "_schedule_post_visible_bootstrap", lambda self: None)
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_services.initialize_window_threads", lambda ui: None)
    foreground = win32gui.GetForegroundWindow()
    ui = HextechUI()
    ui.root.attributes("-alpha", 0.0)
    monitors = (
        DesktopMonitor("left-150", (-2560, 0, 0, 1528), 1.5, (-2560, 0, 0, 1600)),
        DesktopMonitor("right-100", (0, 0, 2560, 1392), 1.0, (0, 0, 2560, 1440)),
    )
    try:
        owner = ui._desktop_window_presentation
        now = time.time()
        owner.publish_window(observation(
            client_rect=(-2500, 100, -580, 900), workarea=monitors[0].workarea,
            dpi_scale=1.5, monitor_device="left-150", monitors=monitors, observed_at=now,
        ))
        owner.publish_phase({"context_phase": "champ_select", "context_connection_state": "connected"},
                            client_hwnd=100, observed_at=now)
        owner.apply_latest(now=now)
        ui.root.update()
        assert owner.status()["actual_rect"] == (-580, 100, -100, 900)
        assert ui._desktop_layout.dpi_scale == 1.5
        assert not ui._window_topmost

        owner.begin_manual_drag((20, 120))
        owner.drag_to_cursor((-100, 200))
        owner.end_manual_drag()
        owner.apply_latest(now=now)
        ui.root.update()
        assert owner.status()["actual_rect"] == (-580, 100, -100, 900)
        assert owner.manual_rect is None

        owner.publish_window(observation(
            client_rect=(300, 100, 1580, 820), workarea=monitors[1].workarea,
            dpi_scale=1.0, monitor_device="right-100", monitors=monitors, observed_at=now,
        ))
        owner.apply_latest(now=now)
        ui.root.update()
        assert owner.status()["actual_rect"] == (1580, 100, 1900, 820)
        assert ui._desktop_layout.dpi_scale == 1.0
        assert owner.position_mode == "auto" and owner.manual_rect is None
        from hextech.modules.vision.window import root_window_hwnd
        import win32process
        active = win32gui.GetForegroundWindow()
        assert active == foreground, {
            "initial_foreground": foreground,
            "actual_foreground": active,
            "test_hwnd": root_window_hwnd(ui.root.winfo_id()),
            "foreground_thread_process": win32process.GetWindowThreadProcessId(active) if active else None,
        }
    finally:
        ui.root.destroy()

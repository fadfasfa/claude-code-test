"""真实不透明 Tk 子控件与 owner 绑定像素 QA；仅捕获测试自建窗口。"""
import os
from pathlib import Path


def test_native_opaque_relayout_keeps_one_widget_and_complete_content(request, monkeypatch, tmp_path):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import tkinter as tk
    import win32gui
    from PIL import ImageGrab
    from hextech.interfaces.desktop.app import HextechUI
    from hextech.interfaces.desktop.responsive_layout import DesktopLayout
    from hextech.interfaces.desktop.client_layer import desktop_wrapper_hwnd
    from hextech.interfaces.desktop.window_activation import suppress_desktop_activation
    from hextech.modules.vision.window import root_window_hwnd
    from test_desktop_client_layer import _bounded_native_owner
    def pump(instance):
        import _tkinter
        import time
        deadline = time.monotonic() + .1
        for _ in range(128):
            if time.monotonic() >= deadline or not instance.root.tk.dooneevent(
                    _tkinter.WINDOW_EVENTS | _tkinter.IDLE_EVENTS | _tkinter.DONT_WAIT):
                break
    monkeypatch.setattr(HextechUI, "_start_desktop_tray", lambda self: None)
    monkeypatch.setattr(HextechUI, "_schedule_post_visible_bootstrap", lambda self: None)
    monkeypatch.setattr("hextech.interfaces.desktop.runtime_services.initialize_window_threads", lambda ui: None)
    ui = HextechUI()
    ui._desktop_window_presentation.close()
    ui.root.attributes("-alpha", 1.0)
    ui._ensure_card_state()
    from hextech.modules.data.ports.paths import CHAMPION_ASSET_DIR
    from hextech.interfaces.desktop import runtime_window
    seed_path = Path(CHAMPION_ASSET_DIR)
    for champion_id in ("901", "96", "53"):
        assert (seed_path / f"{champion_id}.png").is_file()
    # 强制只读既有seed，不使用正式runtime缓存或网络。
    monkeypatch.setattr(runtime_window, "var_path", lambda *parts: tmp_path.joinpath(*parts))
    ui.session = type("NoNetwork", (), {"get": lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no network"))})()
    rows = [{"id": champion_id, "name": name, "tier": "T1", "win": .525, "pick": .151, "selection_role": "self"}
            for champion_id, name in zip(("901", "96", "53"), ("炽炎雏龙", "深渊巨口", "蒸汽机器人"))]
    ui._render_candidate_cards(rows)
    identity = tuple(str(w) for w in ui.title_frame.winfo_children())
    if os.environ.get("HEXTECH_R10_ABLATE_REPAINT") == "1":
        native_redraw = win32gui.RedrawWindow
        monkeypatch.setattr(win32gui, "RedrawWindow", lambda hwnd, rect, region, flags:
            None if flags == (0x0001 | 0x0004 | 0x0080 | 0x0100 | 0x0400)
            else native_redraw(hwnd, rect, region, flags))
    with suppress_desktop_activation():
        backing = tk.Toplevel(ui.root)
        backing.withdraw()
        backing.overrideredirect(True)
        backing.configure(bg="#FF00FF")
        backing.geometry("520x940+20+20")
        backing.update_idletasks()
        backing_hwnd = desktop_wrapper_hwnd(backing)
        win32gui.SetWindowLong(backing_hwnd, -20, win32gui.GetWindowLong(backing_hwnd, -20) | 0x08000000)
        backing.deiconify()
        win32gui.SetWindowPos(backing_hwnd, -1, 20, 20, 520, 940, 0x0010 | 0x0040)
    try:
        with _bounded_native_owner() as (owner, _send, _receive):
            ui._bind_client_layer(owner)
            for width, dpi, mode in ((320, 1, "normal"), (480, 1.5, "normal"),
                                     (260, 1, "narrow"), (320, 1, "normal")):
                layout = DesktopLayout((30, 30, 30+width, 930), dpi, 1, width/dpi, mode)
                for _ in range(12):
                    ui._apply_desktop_layout(layout)
                    ui._move_overlay_to(30, 30, height=900)
                    ui._show_overlay()
                    ui._render_candidate_cards(rows)
                    pump(ui)
                    ui._drain_ui_callbacks()
                import time
                avatar_deadline = time.monotonic() + 3
                while not all(getattr(row["img_label"], "_hextech_avatar_loaded", False) for row in ui._card_rows.values()):
                    assert time.monotonic() < avatar_deadline, "seed avatar publication exceeded 3s"
                    ui._drain_ui_callbacks()
                    pump(ui)
                    time.sleep(.01)
                for row in ui._card_rows.values():
                    assert row["img_label"]._hextech_avatar_loaded_key == (str(row["id"]), round(48*dpi), ui._avatar_revision)
                assert tuple(str(w) for w in ui.title_frame.winfo_children()) == identity
                assert sum(w.cget("text") == "刷新" for w in ui.title_frame.winfo_children()) == 1
                assert sum(w.cget("text") == "诊断" for w in ui.title_frame.winfo_children()) == 1
                for w in (ui.title_bar, ui.refresh_button, ui.diagnostics_button, ui.canvas, ui.status_line_label):
                    assert w.winfo_ismapped(), {"widget": str(w), "size": (w.winfo_width(), w.winfo_height()),
                        "root": (ui.root.winfo_ismapped(), ui.root.state(), ui.root.winfo_width(), ui.root.winfo_height()),
                        "title_frame": (ui.title_frame.winfo_ismapped(), ui.title_frame.winfo_width(), ui.title_frame.winfo_height()),
                        "layout": (width, dpi, mode), "grid": w.grid_info()}
                destination = os.environ.get("HEXTECH_R10_QA_DIR") or str(tmp_path)
                if destination:
                    hwnd = root_window_hwnd(ui.root.winfo_id())
                    print({"capture_hwnd": hwnd, "owner_hwnd": owner, "inner_hwnd": ui.root.winfo_id(),
                           "native_rect": win32gui.GetWindowRect(hwnd), "tk_rect": (ui.root.winfo_width(), ui.root.winfo_height()),
                           "class": win32gui.GetClassName(hwnd)}, flush=True)
                    assert hwnd != owner, "capture must never target even the fixture's external owner"
                    win32gui.SetWindowPos(hwnd, -1, 30, 30, width, 900, 0x0010 | 0x0040)
                    pump(ui)
                    import ctypes
                    ctypes.windll.dwmapi.DwmFlush()
                    assert win32gui.GetWindowRect(hwnd) == (30, 30, 30+width, 930)
                    assert desktop_wrapper_hwnd(ui.root) == hwnd
                    # 两层均为本测试创建；只抓面板客户区，漏绘只会露出纯色测试背板。
                    image = ImageGrab.grab(bbox=(30, 30, 30+width, 930), all_screens=True)
                    target = Path(destination)
                    target.mkdir(parents=True, exist_ok=True)
                    image.save(target / f"desktop-{width}-{dpi}-{mode}.png")
                    assert image.getpixel((width//2, 800)) == (9, 20, 40)
                    assert image.getpixel((width//2, 800)) != (255, 0, 255)
                    # 刷新青色只允许在本次唯一按钮bbox内；重复绘制残影会落在旧bbox。
                    button = ui.refresh_button
                    bx, by = button.winfo_rootx()-30, button.winfo_rooty()-30
                    bw, bh = button.winfo_width(), button.winfo_height()
                    ghost = [(x, y) for y in range(min(ui.title_frame.winfo_height(), image.height))
                             for x in range(image.width)
                             if image.getpixel((x, y))[0] < 60 and image.getpixel((x, y))[1] > 170
                             and image.getpixel((x, y))[2] > 150
                             and not (bx-1 <= x < bx+bw+1 and by-1 <= y < by+bh+1)]
                    assert len(ghost) == 0, {"ghost_cyan_pixels": len(ghost), "button_bbox": (bx, by, bw, bh)}
                    win32gui.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002)
    finally:
        backing.destroy()
        ui.root.destroy()

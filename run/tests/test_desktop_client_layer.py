"""真实 Win32 owner/region/非激活合同，只操作自身隔离透明窗口。"""
from contextlib import contextmanager
from functools import partial

import pytest


def test_native_independent_layer_keeps_mouse_activation(request):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import tkinter as tk
    import win32gui
    from hextech.interfaces.desktop.client_layer import bind_client_layer, _hooks, _regions
    from hextech.interfaces.desktop.window_activation import suppress_desktop_activation
    from hextech.modules.vision.window import root_window_hwnd, configure_process_dpi_awareness
    configure_process_dpi_awareness()
    foreground = win32gui.GetForegroundWindow()
    with suppress_desktop_activation():
        root = tk.Tk()
        root.withdraw()
        client = tk.Toplevel(root)
        panel = tk.Toplevel(root)
        for widget in (client, panel):
            widget.withdraw()
            widget.overrideredirect(True)
            widget.attributes("-alpha", 0.0)
            widget.geometry("320x600+10000+10000")
        root.update_idletasks()
    owner = root_window_hwnd(client.winfo_id())
    hwnd = root_window_hwnd(panel.winfo_id())
    clicked = []
    button = tk.Button(panel, command=lambda: clicked.append(True))
    try:
        win32gui.SetWindowPos(owner, 0, 10000, 10000, 1280, 720, 0x0010 | 0x0004)
        win32gui.SetWindowPos(hwnd, 0, 11200, 10000, 320, 600, 0x0010 | 0x0004)
        bind_client_layer(hwnd, owner)
        assert win32gui.GetWindow(hwnd, 4) == 0
        style = win32gui.GetWindowLong(hwnd, -20)
        assert style & 0x08000000 and not style & 0x8
        assert win32gui.SendMessage(hwnd, 0x21, 0, 0) == 3
        button.invoke()
        assert clicked == [True]
        assert win32gui.GetForegroundWindow() == foreground
        region = win32gui.CreateRectRgnIndirect((0, 0, 0, 0))
        try:
            assert win32gui.GetWindowRgn(hwnd, region) != 0
            assert not win32gui.PtInRegion(region, 10, 10)  # overlaps client
            assert win32gui.PtInRegion(region, 100, 10)     # uncovered right portion
        finally:
            win32gui.DeleteObject(region)
        win32gui.SetWindowPos(hwnd, 0, 11280, 10000, 320, 600, 0x0010 | 0x0004)
        bind_client_layer(hwnd, owner)
        assert win32gui.GetForegroundWindow() == foreground
    finally:
        panel.destroy()
        client.destroy()
        root.destroy()
    assert hwnd not in _hooks and hwnd not in _regions


# 产品目标为 <=100ms；共享 Windows 测试机允许 250ms 调度容差。
# owner 每轮只挂起 1s，因此旧同步调用会失败，但不会永久挂住测试或 GUI。
_OWNER_STALL_SECONDS = 1.0
_GUI_CALL_LIMIT_SECONDS = 0.250
_HUNG_OWNER_SCRIPT = r'''
import queue
import sys
import threading
import time
import win32gui

commands = queue.Queue()
def read_commands():
    for line in sys.stdin:
        commands.put(line.strip())
    commands.put("stop")
threading.Thread(target=read_commands, daemon=True).start()
hwnd = win32gui.CreateWindowEx(
    0x08080080, "Static", "hextech-test-bounded-owner", 0x80000000,
    10000, 10000, 1280, 720, 0, 0, 0, None,
)
win32gui.SetLayeredWindowAttributes(hwnd, 0, 0, 2)
win32gui.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE; transparent and far off-screen.
print(hwnd, flush=True)
deadline = time.monotonic() + 20.0
try:
    while time.monotonic() < deadline:
        try:
            command = commands.get_nowait()
        except queue.Empty:
            command = ""
        if command == "stop":
            break
        if command == "freeze":
            print("frozen", flush=True)
            # 明确停止 owner 所属线程的消息泵；不是暂停另一个无关线程。
            time.sleep(float(sys.argv[1]))
            print("thawed", flush=True)
        win32gui.PumpWaitingMessages()
        time.sleep(.002)
finally:
    win32gui.DestroyWindow(hwnd)
'''


def _add_native_layout_controls(ui):
    """真实 Tk 控件覆盖完整重排/geometry 路径，不启动应用后台服务或头像线程。"""
    import tkinter as tk

    ui.image_cache = {}
    ui._card_rows = {}
    ui._card_order = []
    ui._ensure_card_state = lambda: None
    ui._refresh_feature_toggle_styles = lambda: None
    ui._render_status_line = lambda: None
    ui.title_frame = tk.Frame(ui.root)
    ui.title_frame.pack(fill=tk.X)
    for name in ("title_bar", "exit_button", "refresh_button", "diagnostics_button"):
        widget = tk.Label(ui.title_frame, text=name)
        widget.pack(side=tk.LEFT)
        setattr(ui, name, widget)
    ui.feature_frame = tk.Frame(ui.root)
    ui.feature_frame.pack(fill=tk.X)
    ui._feature_toggle_widgets = []
    for index in range(3):
        frame = tk.Frame(ui.feature_frame)
        frame.grid(row=0, column=index)
        label = tk.Label(frame, text=f"feature {index}")
        label.pack(side=tk.LEFT)
        dot = tk.Canvas(frame, width=13, height=13)
        dot.pack(side=tk.LEFT)
        ui._feature_toggle_widgets.append(dict(frame=frame, label=label, dot=dot))
    ui.list_shell = tk.Frame(ui.root)
    ui.list_shell.pack(fill=tk.BOTH, expand=True)
    ui.canvas = tk.Canvas(ui.list_shell)
    ui.canvas.pack(fill=tk.BOTH, expand=True)
    ui.list_frame = tk.Frame(ui.canvas)
    ui.canvas.create_window((0, 0), window=ui.list_frame, anchor="nw")
    ui.list_scrollbar = tk.Scrollbar(ui.list_shell)
    ui.status_line_label = tk.Label(ui.root, text="isolated layout")
    ui.status_line_label.pack()
    ui.root.update_idletasks()


@contextmanager
def _bounded_native_owner():
    """独立进程、不显示窗口；每次握手和最终退出均有截止时间。"""
    import queue
    import subprocess
    import sys
    import threading

    process = subprocess.Popen(
        [sys.executable, "-B", "-u", "-c", _HUNG_OWNER_SCRIPT, str(_OWNER_STALL_SECONDS)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    replies = queue.Queue()

    def read_replies():
        for line in process.stdout:
            replies.put(line.strip())
        replies.put("<owner exited>")

    reader = threading.Thread(target=read_replies, daemon=True)
    reader.start()

    def receive():
        try:
            return replies.get(timeout=5)
        except queue.Empty:
            pytest.fail("isolated owner handshake exceeded 5s")

    def send(command):
        process.stdin.write(command + "\n")
        process.stdin.flush()

    try:
        hwnd = int(receive())
        yield hwnd, send, receive
    finally:
        try:
            if process.poll() is None:
                try:
                    send("stop")
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # 只终止本测试创建并持有句柄的 owner，绝不扫描真实客户端。
                    process.kill()
                    process.wait(timeout=3)
        finally:
            process.stdin.close()
            reader.join(timeout=2)
            process.stdout.close()
        assert process.poll() is not None, "isolated owner was not reaped"


@pytest.mark.parametrize("operation", [
    "first_bind", "repeat_bind", "clear_topmost", "move_hidden", "move_visible", "first_show", "reshow", "hide_visible",
    "layout_visible",
])
def test_native_hung_owner_does_not_block_gui(request, monkeypatch, operation):
    from native_tk_runner import run_native_tk_case
    if run_native_tk_case(request.node.nodeid):
        return
    import time
    import tkinter as tk
    import win32gui
    from hextech.interfaces.desktop.client_layer import bind_client_layer, _hooks, _regions
    from hextech.interfaces.desktop.window_activation import suppress_desktop_activation
    from hextech.modules.vision.window import root_window_hwnd, configure_process_dpi_awareness

    configure_process_dpi_awareness()
    foreground = win32gui.GetForegroundWindow()
    with suppress_desktop_activation():
        panel = tk.Tk()
        panel.withdraw()
        panel.overrideredirect(True)
        panel.attributes("-alpha", 0.0)
        panel.geometry("320x600+11280+10000")
        panel.update_idletasks()
    hwnd = root_window_hwnd(panel.winfo_id())
    try:
        with _bounded_native_owner() as (owner, send, receive):
            assert win32gui.IsWindowVisible(owner) and not win32gui.IsIconic(owner)
            original_owner_style = win32gui.GetWindowLong(owner, -20)
            if operation not in {"first_bind", "move_visible", "reshow", "hide_visible", "layout_visible"}:
                bind_client_layer(hwnd, owner)
            if operation == "clear_topmost":
                win32gui.SetWindowPos(
                    hwnd, -1, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002 | 0x0200 | 0x4000,
                )
                assert win32gui.GetWindowLong(hwnd, -20) & 0x8, "precondition: real TOPMOST required"
            if operation in {"move_hidden", "move_visible", "first_show", "reshow", "hide_visible", "layout_visible"}:
                from hextech.interfaces.desktop.app_view import DesktopViewMixin
                from hextech.interfaces.desktop.responsive_view import DesktopResponsiveMixin
                # 只组装真实 mixin；不启动 HextechUI、托盘、网络或服务。
                class ProbeUI(DesktopViewMixin, DesktopResponsiveMixin):
                    pass
                ui = ProbeUI()
                ui.root = panel
                ui._overlay_pixel_width = 320
                ui._window_height_px = 600
                ui._window_visible = False
                ui._window_topmost = False
                ui._desktop_owner_hwnd = owner
                if operation == "layout_visible":
                    _add_native_layout_controls(ui)
                if operation in {"move_visible", "reshow", "hide_visible", "layout_visible"}:
                    # 先建立已映射 Tk 状态，再附外部 owner；这里不调用无界 root.update()。
                    ui._desktop_owner_hwnd = 0
                    win32gui.SetWindowLong(hwnd, -20, win32gui.GetWindowLong(hwnd, -20) | 0x08000000)
                    ui._show_overlay(topmost=False)
                    assert panel.winfo_ismapped()
                    if operation == "reshow":
                        # 只建立“曾映射、现已隐藏”的前置状态；实测的是重新绑定后的 show。
                        ui._hide_overlay()
                        assert not panel.winfo_ismapped()
                    ui._bind_client_layer(owner)
                if operation.startswith("move_"):
                    action = partial(ui._move_overlay_to, 11290, 10010, height=610)
                elif operation == "hide_visible":
                    action = ui._hide_overlay
                elif operation == "layout_visible":
                    from hextech.interfaces.desktop.responsive_layout import DesktopLayout
                    layout = DesktopLayout((11280, 10000, 11520, 10600), 1.0, 1.0, 240, "narrow")
                    action = partial(ui._apply_desktop_layout, layout)
                else:
                    action = partial(ui._show_overlay, topmost=False)
            else:
                action = partial(bind_client_layer, hwnd, owner, topmost=False if operation == "clear_topmost" else None)

            calls = []
            timings = []
            native_set_window_pos = win32gui.SetWindowPos

            def record_window_pos(*args):
                if args[0] == hwnd:
                    calls.append(args)
                started = time.perf_counter()
                try:
                    return native_set_window_pos(*args)
                finally:
                    timings.append(("SetWindowPos", time.perf_counter() - started))

            def time_tk_method(name, method):
                def timed(*args, **kwargs):
                    started = time.perf_counter()
                    try:
                        return method(*args, **kwargs)
                    finally:
                        timings.append((name, time.perf_counter() - started))
                return timed

            monkeypatch.setattr(win32gui, "SetWindowPos", record_window_pos)
            for name in ("geometry", "update_idletasks", "deiconify", "attributes", "withdraw"):
                monkeypatch.setattr(panel, name, time_tk_method(name, getattr(panel, name)))
            send("freeze")
            assert receive() == "frozen"
            started = time.perf_counter()
            action_result = action()
            elapsed = time.perf_counter() - started
            # 先读取原生结果，不能等 owner 恢复后再声称 ASYNC 已经生效。
            style = win32gui.GetWindowLong(hwnd, -20)
            actual_owner = win32gui.GetWindow(hwnd, 4)
            actual_rect = win32gui.GetWindowRect(hwnd)
            native_visible = bool(win32gui.IsWindowVisible(hwnd))
            owner_style = win32gui.GetWindowLong(owner, -20)
            assert receive() == "thawed"
            map_detail = ""
            if operation in {"first_show", "reshow"}:
                import _tkinter
                before_pump = (panel.winfo_ismapped(), panel.state(), int(panel.winfo_id()),
                               root_window_hwnd(panel.winfo_id()))
                pump_started = time.perf_counter()
                events = 0
                for _ in range(256):
                    if panel.winfo_ismapped() and panel.state() == "normal":
                        break
                    if time.perf_counter() - pump_started >= .250:
                        break
                    if not panel.tk.dooneevent(_tkinter.ALL_EVENTS | _tkinter.DONT_WAIT):
                        break
                    events += 1
                after_pump = (panel.winfo_ismapped(), panel.state(), int(panel.winfo_id()),
                              root_window_hwnd(panel.winfo_id()))
                map_detail = (f"; Tk pre/post pump={before_pump}/{after_pump}, events={events}, "
                              f"inner_visible={bool(win32gui.IsWindowVisible(panel.winfo_id()))}, "
                              f"wrapper_visible={bool(win32gui.IsWindowVisible(after_pump[3]))}, "
                              f"owner_visible={bool(win32gui.IsWindowVisible(owner))}")
            detail = ", ".join(f"{name}={duration * 1000:.3f}ms" for name, duration in timings)
            print(f"{operation}: {elapsed * 1000:.3f}ms (target <=100ms, machine tolerance <=250ms); {detail}{map_detail}")
            assert elapsed <= _GUI_CALL_LIMIT_SECONDS, (
                f"{operation} blocked GUI for {elapsed:.3f}s; target <=100ms, tolerance <=250ms; {detail}"
            )
            assert actual_owner == 0
            assert style & 0x08000000 and not style & 0x8
            assert owner_style == original_owner_style, "must not modify external owner styles"
            assert win32gui.SendMessage(hwnd, 0x21, 0, 0) == 3
            assert win32gui.GetForegroundWindow() == foreground
            demotions = [args for args in calls if args[1] == -2]
            if operation in {"first_bind", "repeat_bind"}:
                assert not demotions, "ordinary non-topmost bind must not reorder every tick"
            elif operation == "clear_topmost":
                assert len(demotions) == 1
                required = 0x0010 | 0x0200 | 0x4000  # NOACTIVATE | NOOWNERZORDER | ASYNCWINDOWPOS
                assert demotions[0][-1] & required == required
            elif operation.startswith("move_"):
                assert actual_rect == (11290, 10010, 11610, 10620)
                assert calls and all(args[-1] & 0x0200 for args in calls)
            elif operation == "hide_visible":
                assert not native_visible and not ui._window_visible
            elif operation == "layout_visible":
                assert action_result > 0 and ui._desktop_layout_key == (240, 1.0, 1.0, "narrow")
                assert actual_rect[2] - actual_rect[0] == 240 and native_visible
            else:
                assert native_visible, "native popup must be visible before owner recovery"
                assert panel.winfo_ismapped() and ui._window_visible, (
                    f"Tk mapped={panel.winfo_ismapped()}, native_visible={native_visible}, "
                    f"Tk state={panel.state()}, ui_visible={ui._window_visible}, rect={actual_rect}{map_detail}"
                )
                assert win32gui.IsWindowVisible(panel.winfo_id()), "Tk content must be visible after event pump"
                assert root_window_hwnd(panel.winfo_id()) == hwnd, "verify the same owned HWND, not a stale wrapper"
    finally:
        panel.destroy()
    assert hwnd not in _hooks and hwnd not in _regions

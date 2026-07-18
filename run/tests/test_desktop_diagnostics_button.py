"""测试 桌面诊断按钮。

调用方: pytest; 关键依赖: hextech.interfaces.desktop.app。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace


def test_diagnostics_button_is_created_in_title_frame(monkeypatch):
    import hextech.interfaces.desktop.app as desktop_app

    class Variable:
        def __init__(self, value=False):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Widget:
        def __init__(self, parent=None, **kwargs):
            self.parent = parent
            self.kwargs = dict(kwargs)
            self.pack_options = None
            self.grid_options = None

        def pack(self, **kwargs):
            self.pack_options = dict(kwargs)

        def grid(self, **kwargs):
            self.grid_options = dict(kwargs)

        def bind(self, *_args, **_kwargs):
            return None

        def bind_all(self, *_args, **_kwargs):
            return None

        def grid_columnconfigure(self, *_args, **_kwargs):
            return None

        def create_window(self, *_args, **_kwargs):
            return None

        def configure(self, **kwargs):
            self.kwargs.update(kwargs)

        config = configure

        def bbox(self, *_args):
            return (0, 0, 0, 0)

        def cget(self, key):
            return self.kwargs.get(key)

        def delete(self, *_args):
            return None

        def create_oval(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(desktop_app.tk, "Frame", Widget)
    monkeypatch.setattr(desktop_app.tk, "Label", Widget)
    monkeypatch.setattr(desktop_app.tk, "Button", Widget)
    monkeypatch.setattr(desktop_app.tk, "Canvas", Widget)
    monkeypatch.setattr(desktop_app.tk, "BooleanVar", Variable)

    dummy = object.__new__(desktop_app.HextechUI)
    dummy.root = Widget()
    dummy.feature_flags = {
        "web_frontend_enabled": True,
        "game_overlay_enabled": True,
        "private_policy_stats_enabled": False,
        "low_frequency_listener_enabled": True,
    }
    dummy._feature_toggle_lock = threading.Lock()
    dummy._feature_toggle_busy = set()
    dummy._runtime_services_ready = True

    desktop_app.HextechUI._build_ui(dummy)

    assert dummy.exit_button.parent is dummy.title_frame
    assert dummy.exit_button.kwargs["text"] == "×"
    assert dummy.exit_button.kwargs["command"] == dummy.on_close
    assert dummy.exit_button.pack_options["side"] == desktop_app.tk.RIGHT
    assert dummy.exit_button.kwargs["activebackground"] == desktop_app.UI_COLORS["red"]
    assert dummy.diagnostics_button.parent is dummy.title_frame
    assert dummy.diagnostics_button.kwargs["text"] == "诊断"
    assert dummy.diagnostics_button.kwargs["command"] == dummy._start_user_diagnostics_export
    assert dummy.diagnostics_button.pack_options["side"] == desktop_app.tk.RIGHT
    assert dummy.diagnostics_button.grid_options is None


def test_diagnostics_export_callback_restores_button_on_error(monkeypatch, tmp_path):
    import hextech.interfaces.desktop.app as desktop_app
    import hextech.interfaces.desktop.app_controls as desktop_controls

    states: list[str] = []
    statuses: list[tuple[str, str]] = []

    class Button:
        def config(self, **kwargs):
            if "state" in kwargs:
                states.append(kwargs["state"])

    class Root:
        def after(self, _delay, callback):
            callback()

    dummy = object.__new__(desktop_app.HextechUI)
    dummy.root = Root()
    dummy.diagnostics_button = Button()
    dummy._set_status = lambda text, color: statuses.append((text, color))

    def run_now(target, *, name: str):
        assert name == "hextech-user-diagnostics-export"
        target()

    dummy._start_tracked_thread = run_now

    def fail_export():
        raise RuntimeError("boom")

    monkeypatch.setattr(desktop_controls, "export_user_diagnostics", fail_export)

    desktop_app.HextechUI._start_user_diagnostics_export(dummy)

    assert states[0] == desktop_app.tk.DISABLED
    assert states[-1] == desktop_app.tk.NORMAL
    assert statuses
    assert "诊断导出失败" in statuses[-1][0]


def test_diagnostics_export_runs_async_callback_and_reports_zip(monkeypatch, tmp_path):
    import hextech.interfaces.desktop.app as desktop_app
    import hextech.interfaces.desktop.app_controls as desktop_controls

    states: list[str] = []
    statuses: list[tuple[str, str]] = []

    class Button:
        def config(self, **kwargs):
            if "state" in kwargs:
                states.append(kwargs["state"])

    class Root:
        def after(self, _delay, callback):
            callback()

    dummy = object.__new__(desktop_app.HextechUI)
    dummy.root = Root()
    dummy.diagnostics_button = Button()
    dummy._set_status = lambda text, color: statuses.append((text, color))

    def run_now(target, *, name: str):
        assert name == "hextech-user-diagnostics-export"
        target()

    dummy._start_tracked_thread = run_now
    zip_path = tmp_path / "diagnostics.zip"
    monkeypatch.setattr(
        desktop_controls,
        "export_user_diagnostics",
        lambda: SimpleNamespace(zip_path=zip_path),
    )

    desktop_app.HextechUI._start_user_diagnostics_export(dummy)

    assert states == [desktop_app.tk.DISABLED, desktop_app.tk.NORMAL]
    assert statuses[-1][0] == f"诊断已导出: {zip_path}"

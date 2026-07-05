from __future__ import annotations

import inspect
from pathlib import Path


def test_diagnostics_button_is_in_title_frame_not_feature_or_list():
    import hextech.display.desktop.app as desktop_app

    source = inspect.getsource(desktop_app.HextechUI._build_ui)
    assert 'text="诊断"' in source
    assert "self.diagnostics_button" in source
    assert "self.title_frame" in source
    assert "self.diagnostics_button.pack(side=tk.RIGHT" in source
    assert "self.diagnostics_button.grid" not in source

    title_index = source.index("self.diagnostics_button")
    feature_index = source.index("self.feature_frame")
    canvas_index = source.index("self.canvas")
    assert title_index < feature_index < canvas_index


def test_diagnostics_button_uses_async_export_and_status_label():
    import hextech.display.desktop.app as desktop_app

    source = inspect.getsource(desktop_app.HextechUI._start_user_diagnostics_export)
    assert "export_user_diagnostics" in source
    assert '_start_tracked_thread(worker, name="hextech-user-diagnostics-export")' in source
    assert "self.diagnostics_button.config(state=tk.DISABLED)" in source
    assert "self.diagnostics_button.config(state=tk.NORMAL)" in source
    assert "zip_path" in source
    assert "_set_status" in source
    assert "logger.exception" in source


def test_diagnostics_export_callback_restores_button_on_error(monkeypatch, tmp_path):
    import hextech.display.desktop.app as desktop_app

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

    monkeypatch.setattr(desktop_app, "export_user_diagnostics", fail_export)

    desktop_app.HextechUI._start_user_diagnostics_export(dummy)

    assert states[0] == desktop_app.tk.DISABLED
    assert states[-1] == desktop_app.tk.NORMAL
    assert statuses
    assert "诊断导出失败" in statuses[-1][0]

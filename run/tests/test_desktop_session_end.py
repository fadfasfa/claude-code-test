from __future__ import annotations

import threading
from types import SimpleNamespace

from hextech.interfaces.desktop import client_layer
from hextech.interfaces.desktop.app import HextechUI
from hextech.interfaces.desktop.service_manager import ServiceManager


def test_session_query_cancel_does_not_exit_and_reopens_service_gate() -> None:
    class Services:
        def __init__(self) -> None:
            self.requests: list[bool] = []

        def set_shutdown_requested(self, requested: bool) -> None:
            self.requests.append(requested)

    ui = HextechUI.__new__(HextechUI)
    ui.root = SimpleNamespace()
    ui.service_manager = Services()
    ui._windows_session_end_pending = False
    ui._windows_session_end_epoch = 0
    exits: list[bool] = []
    ui.exit_application = lambda: exits.append(True)

    assert ui._on_windows_query_end_session() is True
    ui._on_windows_end_session(False)
    ui._finish_windows_session_end(1)

    assert exits == []
    assert ui._windows_session_end_pending is False
    assert ui.service_manager.requests == [True, False]


def test_session_end_true_runs_real_exit_application_once(monkeypatch) -> None:
    class Root:
        def __init__(self) -> None:
            self.destroy_calls = 0

        def destroy(self) -> None:
            self.destroy_calls += 1

    class FakeThread:
        created = 0

        def __init__(self, *, target, name, daemon) -> None:
            del target, name, daemon
            type(self).created += 1

        def start(self) -> None:
            return

    root = Root()
    ui = HextechUI.__new__(HextechUI)
    ui.root = root
    ui._closing = False
    ui._windows_session_end_pending = False
    ui._windows_session_end_epoch = 0
    ui._desktop_window_presentation = SimpleNamespace(close=lambda: None)
    ui.stop_background_runtime = lambda: None
    ui.stop_event = threading.Event()
    ui._overlay_status_after_id = None
    ui._hide_overlay = lambda: None
    ui._close_pending_process_jobs = lambda: None
    ui._threads_lock = threading.Lock()
    ui.threads = []
    ui._service_manager_lock = threading.Lock()
    ui._service_manager_shutdown_in_progress = None
    ui._service_manager_shutdown_completed = None
    ui._take_service_manager_for_shutdown = lambda: None
    ui.runtime_owner = None
    ui.data_service = None
    ui._shutdown_done_event = threading.Event()

    monkeypatch.setattr("hextech.interfaces.desktop.avatar_loading.close_avatar_loader", lambda _ui: None)
    monkeypatch.setattr("hextech.interfaces.desktop.app_view.threading.Thread", FakeThread)

    ui._on_windows_query_end_session()
    ui._on_windows_end_session(True)
    ui._on_windows_end_session(True)

    assert root.destroy_calls == 1
    assert FakeThread.created == 1


def test_session_handler_survives_tk_wrapper_rebuild(monkeypatch) -> None:
    old_hwnd, new_hwnd = 101, 202

    class Root:
        hwnd = old_hwnd

        def winfo_exists(self) -> bool:
            return True

    ui = SimpleNamespace(root=Root(), _desktop_window_operation_depth=0)
    handler = (lambda: True, lambda _ending: None)
    client_layer._hooks.clear()
    client_layer._regions.clear()
    client_layer._session_end_handlers.clear()
    client_layer._hooks[old_hwnd] = (object(), object())
    client_layer._session_end_handlers[old_hwnd] = handler
    installed: list[int] = []

    monkeypatch.setattr(client_layer, "desktop_wrapper_hwnd", lambda root: root.hwnd)
    monkeypatch.setattr(client_layer, "_install_input_hook", lambda hwnd: installed.append(hwnd))
    monkeypatch.setattr(client_layer, "checked_set_window_long", lambda *_args: None)
    monkeypatch.setattr(client_layer.win32gui, "GetWindowLong", lambda *_args: 0x08000000)
    monkeypatch.setattr(client_layer.win32gui, "RedrawWindow", lambda *_args: None)

    @client_layer.unowned_tk_operation
    def rebuild(self):
        # Simulate the old hooked HWND receiving WM_NCDESTROY during Tk rebuild.
        client_layer._hooks.pop(old_hwnd, None)
        client_layer._session_end_handlers.pop(old_hwnd, None)
        self.root.hwnd = new_hwnd

    rebuild(ui)

    assert client_layer._session_end_handlers.get(new_hwnd) == handler
    assert installed == [new_hwnd]
    client_layer._hooks.clear()
    client_layer._regions.clear()
    client_layer._session_end_handlers.clear()


def test_session_query_gate_does_not_wait_for_service_lock() -> None:
    manager = ServiceManager.__new__(ServiceManager)
    manager._lock = threading.Lock()
    manager._shutdown_requested = False
    manager._session_end_requested = threading.Event()
    manager._lock.acquire()
    try:
        manager.set_shutdown_requested(True)
    finally:
        manager._lock.release()
    assert manager._session_end_requested.is_set()

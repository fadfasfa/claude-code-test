"""验证托盘待机、进程探针和识别恢复状态机。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from hextech.interfaces.desktop.background_runtime import (
    DesktopBackgroundRuntimeMixin,
    probe_league_process_state,
    resolve_background_runtime_action,
)


def _state(*, client: bool = False, game: bool = False, ok: bool = True) -> dict[str, bool]:
    return {"probe_ok": ok, "client_running": client, "game_running": game}


def test_idle_timeout_is_exactly_five_minutes() -> None:
    action, idle = resolve_background_runtime_action(
        runtime_state="running",
        idle_started_at=100.0,
        now=399.0,
        manual_visible_until=0.0,
        process_state=_state(),
    )
    assert (action, idle) == ("none", 100.0)

    action, idle = resolve_background_runtime_action(
        runtime_state="running",
        idle_started_at=100.0,
        now=400.0,
        manual_visible_until=0.0,
        process_state=_state(),
    )
    assert (action, idle) == ("suspend", 100.0)


def test_client_or_game_process_prevents_suspend_and_resumes() -> None:
    for process_state in (_state(client=True), _state(game=True)):
        action, idle = resolve_background_runtime_action(
            runtime_state="running",
            idle_started_at=100.0,
            now=1000.0,
            manual_visible_until=0.0,
            process_state=process_state,
        )
        assert action == "none"
        assert idle == 1000.0

        action, _idle = resolve_background_runtime_action(
            runtime_state="suspended",
            idle_started_at=100.0,
            now=1000.0,
            manual_visible_until=0.0,
            process_state=process_state,
        )
        assert action == "resume"


def test_manual_wake_starts_one_new_five_minute_window() -> None:
    action, idle = resolve_background_runtime_action(
        runtime_state="running",
        idle_started_at=100.0,
        now=399.0,
        manual_visible_until=400.0,
        process_state=_state(),
    )
    assert (action, idle) == ("none", 100.0)

    action, _idle = resolve_background_runtime_action(
        runtime_state="running",
        idle_started_at=100.0,
        now=400.0,
        manual_visible_until=400.0,
        process_state=_state(),
    )
    assert action == "suspend"


def test_probe_ignores_riot_launcher_but_detects_minimized_league_processes() -> None:
    processes = [
        SimpleNamespace(info={"name": "RiotClientServices.exe"}),
        SimpleNamespace(info={"name": "LeagueClientUx.exe"}),
        SimpleNamespace(info={"name": "League of Legends.exe"}),
    ]
    state = probe_league_process_state(lambda _fields: processes)
    assert state == {"probe_ok": True, "client_running": True, "game_running": True}

    launcher_only = probe_league_process_state(
        lambda _fields: [SimpleNamespace(info={"name": "RiotClientServices.exe"})]
    )
    assert launcher_only == {"probe_ok": True, "client_running": False, "game_running": False}


class _FakeManager:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.web = SimpleNamespace(process="web-handle")

    def stop_web(self) -> None:
        self.calls.append("stop_web")

    def stop_data_service(self) -> None:
        self.calls.append("stop_data")

    def start_data_service(self):
        self.calls.append("start_data")
        return "data-handle"

    def start_web(self) -> None:
        self.calls.append("start_web")


class _FakeUI(DesktopBackgroundRuntimeMixin):
    def __init__(self) -> None:
        self._closing = False
        self.pause_event = threading.Event()
        self._overlay_operation_lock = threading.Lock()
        self._supervisor_lease_stop = threading.Event()
        self._supervisor_lease_thread = None
        self.runtime_supervisor = SimpleNamespace(name="supervisor")
        self.calls: list[str] = []
        self.service_manager = _FakeManager(self.calls)
        self.feature_flags = {"web_frontend_enabled": True}
        self.web_process = "web"
        self.data_service = "data"
        self._initialize_background_runtime()

    def _run_on_ui_thread(self, callback):
        callback()
        return True

    def _set_overlay_status_summary(self, text: str, color: str) -> None:
        del text, color

    def _start_runtime_supervisor(self, *, restore_persisted_game_overlay: bool = True) -> None:
        assert restore_persisted_game_overlay is True
        self.calls.append("start_supervisor")
        self.runtime_supervisor = SimpleNamespace(name="resumed")


def test_suspend_and_resume_stop_and_restore_heavy_services(monkeypatch) -> None:
    ui = _FakeUI()

    def stop_supervisor(handle) -> None:
        assert handle.name == "supervisor"
        ui.calls.append("stop_supervisor")

    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.ui_runtime.stop_runtime_supervisor_process",
        stop_supervisor,
    )
    ui._suspend_background_runtime()

    assert ui._background_runtime_state == "suspended"
    assert ui.pause_event.is_set()
    assert ui.calls == ["stop_web", "stop_data", "stop_supervisor"]
    assert ui.runtime_supervisor is None

    ui._resume_background_runtime(reason="test")
    assert ui._background_runtime_state == "running"
    assert not ui.pause_event.is_set()
    assert ui.calls[-3:] == ["start_data", "start_supervisor", "start_web"]
    assert ui.data_service == "data-handle"


def test_hide_to_tray_never_exits_process() -> None:
    ui = _FakeUI()
    ui._tray_controller = SimpleNamespace(refresh=lambda: ui.calls.append("tray_refresh"))
    ui._hide_overlay = lambda: ui.calls.append("hide")
    ui._manual_window_visible_until = 123.0

    ui.hide_to_tray()

    assert ui.calls == ["hide", "tray_refresh"]
    assert ui._manual_window_visible_until == 0.0
    assert ui._closing is False

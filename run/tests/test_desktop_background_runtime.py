"""验证托盘待机、进程探针和识别恢复状态机。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from hextech.interfaces.desktop.background_runtime import (
    BACKGROUND_PROCESS_PROBE_SECONDS,
    BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS,
    BACKGROUND_RESUME_RETRY_SECONDS,
    DesktopBackgroundRuntimeMixin,
    probe_league_process_state,
    resolve_background_runtime_action,
)


def _state(*, client: bool = False, game: bool = False, ok: bool = True) -> dict[str, object]:
    return {
        "probe_ok": ok,
        "client_running": client,
        "game_running": game,
        "matched_processes": [],
    }


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


def test_league_auto_wake_budget_leaves_scheduler_headroom() -> None:
    """最坏一轮探测加完整恢复预算仍必须小于 15 秒验收上限。"""

    assert BACKGROUND_PROCESS_PROBE_SECONDS + BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS <= 14.0
    assert BACKGROUND_RESUME_RETRY_SECONDS == 5.0


def test_probe_interval_uses_fast_tier_only_while_waiting_for_league() -> None:
    """1 秒档只服务于待机唤醒；运行期探针只用于空闲判定，5 秒即可。"""

    from hextech.interfaces.desktop.background_runtime import (
        BACKGROUND_PROCESS_PROBE_RUNNING_SECONDS,
        resolve_background_probe_interval,
    )

    for state in ("suspended", "resume_failed", "resume_cleanup_pending"):
        assert resolve_background_probe_interval(state) == BACKGROUND_PROCESS_PROBE_SECONDS
    for state in ("running", "suspending", "resuming", "restart_in_progress", ""):
        assert resolve_background_probe_interval(state) == BACKGROUND_PROCESS_PROBE_RUNNING_SECONDS
    # 唤醒预算只由 1 秒档参与，慢档不得进入 15 秒验收路径。
    assert BACKGROUND_PROCESS_PROBE_RUNNING_SECONDS > BACKGROUND_PROCESS_PROBE_SECONDS


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
    assert state == {
        "probe_ok": True,
        "client_running": True,
        "game_running": True,
        "matched_processes": ["league of legends.exe", "leagueclientux.exe"],
    }

    launcher_only = probe_league_process_state(
        lambda _fields: [SimpleNamespace(info={"name": "RiotClientServices.exe"})]
    )
    assert launcher_only == {
        "probe_ok": True,
        "client_running": False,
        "game_running": False,
        "matched_processes": [],
    }


class _FakeManager:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.web = SimpleNamespace(process="web-handle")

    def stop_web(self) -> None:
        self.calls.append("stop_web")

    def stop_data_service(self) -> None:
        self.calls.append("stop_data")

    def start_data_service(self, *, timeout: float | None = None):
        del timeout
        self.calls.append("start_data")
        return "data-handle"

    def start_web(self, *, timeout: float | None = None) -> None:
        del timeout
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
        self.supervisor_restore_arguments: list[bool] = []
        self.service_manager = _FakeManager(self.calls)
        self.feature_flags = {"web_frontend_enabled": True, "game_overlay_enabled": True}
        self.web_process = "web"
        self.data_service = "data"
        self._initialize_background_runtime()

    def _run_on_ui_thread(self, callback):
        callback()
        return True

    def _set_overlay_status_summary(self, text: str, color: str) -> None:
        del text, color

    def _start_runtime_supervisor(
        self,
        *,
        restore_persisted_game_overlay: bool = True,
        startup_timeout: float | None = None,
    ) -> None:
        del startup_timeout
        self.supervisor_restore_arguments.append(restore_persisted_game_overlay)
        self.calls.append("start_supervisor")
        self.runtime_supervisor = SimpleNamespace(
            name="resumed",
            set_game_overlay_enabled=lambda enabled: self.calls.append(f"restore_overlay:{enabled}")
            or {"status": "accepted"},
        )


def test_suspend_and_resume_stop_and_restore_heavy_services(monkeypatch) -> None:
    ui = _FakeUI()
    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.probe_league_process_state",
        lambda: _state(),
    )

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
    assert ui.calls[-4:] == ["start_data", "start_supervisor", "restore_overlay:True", "start_web"]
    assert ui.supervisor_restore_arguments == [False]
    assert ui.data_service == "data-handle"


def test_background_health_requires_current_host_and_sidecar_build_ids(monkeypatch) -> None:
    ui = _FakeUI()
    monkeypatch.setattr("hextech.interfaces.desktop.background_runtime.current_build_id", lambda: "build-current")

    def status(*, host_build: str, sidecar_build: str) -> dict[str, object]:
        return {
            "components": {
                "game_overlay": {
                    "status": "running",
                    "host_pid": 123,
                    "build_id": "build-current",
                    "build_ids": {"host": host_build, "sidecar": sidecar_build},
                    "sidecar_liveness": {"status": "running"},
                }
            }
        }

    ui.runtime_supervisor = SimpleNamespace(get_status=lambda: status(host_build="", sidecar_build=""))
    missing_identity = ui._background_runtime_health()
    assert missing_identity["overlay"] == "starting"
    assert missing_identity["sidecar"] == "starting"

    ui.runtime_supervisor = SimpleNamespace(
        get_status=lambda: status(host_build="build-current", sidecar_build="build-current")
    )
    verified_identity = ui._background_runtime_health()
    assert verified_identity["overlay"] == "ready"
    assert verified_identity["sidecar"] == "ready"


def test_background_health_rejects_exited_web_handle_but_allows_unverified_fake() -> None:
    ui = _FakeUI()

    unverified = ui._background_runtime_health()
    assert unverified["web"] == "unverified"

    ui.service_manager.web = SimpleNamespace(process="stale-handle", is_running=lambda: False)
    stale_handle = ui._background_runtime_health()
    assert stale_handle["web"] == "starting"


def test_hide_to_tray_never_exits_process() -> None:
    ui = _FakeUI()
    ui._tray_controller = SimpleNamespace(refresh=lambda: ui.calls.append("tray_refresh"))
    ui._hide_overlay = lambda: ui.calls.append("hide")
    ui._manual_window_visible_until = 123.0

    ui.hide_to_tray()

    assert ui.calls == ["hide", "tray_refresh"]
    assert ui._manual_window_visible_until == 0.0
    assert ui._closing is False


def test_suspend_rechecks_league_before_stopping_services(monkeypatch) -> None:
    ui = _FakeUI()
    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.probe_league_process_state",
        lambda: {
            "probe_ok": True,
            "client_running": True,
            "game_running": False,
            "matched_processes": ["leagueclient.exe"],
        },
    )

    ui._suspend_background_runtime()

    assert ui._background_runtime_state == "running"
    assert ui._background_runtime_reason == "league_detected_before_suspend"
    assert ui.calls == []
    assert not ui.pause_event.is_set()


def test_resume_failed_is_retried_while_league_is_still_running() -> None:
    for state in ("resume_failed", "resume_cleanup_pending"):
        action, _idle = resolve_background_runtime_action(
            runtime_state=state,
            idle_started_at=100.0,
            now=105.0,
            manual_visible_until=0.0,
            process_state=_state(client=True),
        )

        assert action == "resume"


def test_complete_exit_never_queues_automatic_resume() -> None:
    ui = _FakeUI()
    ui._closing = True
    starts: list[str] = []
    ui._start_tracked_thread = lambda _target, *, name: starts.append(name)  # type: ignore[method-assign]

    ui.request_runtime_resume(reason="league_process_detected", show_window=False)

    assert starts == []


def test_resume_failure_cleans_partial_services_before_next_probe(monkeypatch) -> None:
    ui = _FakeUI()
    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.probe_league_process_state",
        lambda: _state(),
    )
    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.ui_runtime.stop_runtime_supervisor_process",
        lambda _handle: ui.calls.append("stop_supervisor"),
    )
    ui._suspend_background_runtime()
    ui.calls.clear()
    ui._wait_for_background_runtime_health = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sidecar timeout"))  # type: ignore[method-assign]

    ui._resume_background_runtime(reason="league_process_detected")

    assert ui._background_runtime_state == "resume_failed"
    assert ui.pause_event.is_set()
    assert ui.runtime_supervisor is None
    assert ui.data_service is None
    assert ui.web_process is None
    assert ui.calls == [
        "start_data",
        "start_supervisor",
        "restore_overlay:True",
        "start_web",
        "stop_web",
        "stop_data",
        "stop_supervisor",
    ]


def test_auto_resume_does_not_start_late_supervisor_after_bootstrap_budget_expires(monkeypatch) -> None:
    """慢 DataService 必须耗尽同一恢复 deadline，而不是再给 Supervisor 15 秒。"""

    ui = _FakeUI()
    ui._background_runtime_state = "suspended"
    # ``_FakeUI`` 默认模拟运行态；这里必须与真实轻量待机一致，不能留下旧 Supervisor。
    ui.runtime_supervisor = None
    ui.data_service = None
    ui.web_process = None
    clock = {"now": 100.0}
    data_timeouts: list[float] = []

    def slow_start_data(*, timeout: float | None = None):
        data_timeouts.append(float(timeout or 0.0))
        clock["now"] = 113.1
        ui.calls.append("start_data")
        return "data-handle"

    ui.service_manager.start_data_service = slow_start_data  # type: ignore[method-assign]
    monkeypatch.setattr("hextech.interfaces.desktop.background_runtime.time.monotonic", lambda: clock["now"])

    ui._resume_background_runtime(reason="league_process_detected")

    assert data_timeouts == [BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS]
    assert ui._background_runtime_state == "resume_failed"
    assert "start_supervisor" not in ui.calls


def test_auto_resume_passes_remaining_deadline_to_web_before_health_check(monkeypatch) -> None:
    """DataService、Supervisor、Web 必须共享一次恢复预算，不能各自重置超时。"""

    ui = _FakeUI()
    ui._background_runtime_state = "suspended"
    ui.runtime_supervisor = None
    ui.data_service = None
    ui.web_process = None
    clock = {"now": 100.0}
    data_timeouts: list[float] = []
    supervisor_timeouts: list[float] = []
    web_timeouts: list[float] = []

    def start_data(*, timeout: float | None = None):
        data_timeouts.append(float(timeout or 0.0))
        clock["now"] = 104.0
        ui.calls.append("start_data")
        return "data-handle"

    def start_supervisor(*, restore_persisted_game_overlay: bool, startup_timeout: float | None = None) -> None:
        assert restore_persisted_game_overlay is False
        supervisor_timeouts.append(float(startup_timeout or 0.0))
        clock["now"] = 108.0
        ui.calls.append("start_supervisor")
        ui.runtime_supervisor = SimpleNamespace(
            name="resumed",
            set_game_overlay_enabled=lambda _enabled: {"status": "accepted"},
            stop=lambda: None,
        )

    def start_web(*, timeout: float | None = None) -> None:
        web_timeouts.append(float(timeout or 0.0))
        clock["now"] = 113.1
        ui.calls.append("start_web")

    ui.service_manager.start_data_service = start_data  # type: ignore[method-assign]
    ui.service_manager.start_web = start_web  # type: ignore[method-assign]
    ui._start_runtime_supervisor = start_supervisor  # type: ignore[method-assign]
    monkeypatch.setattr("hextech.interfaces.desktop.background_runtime.time.monotonic", lambda: clock["now"])

    ui._resume_background_runtime(reason="league_process_detected")

    assert data_timeouts == [BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS]
    assert supervisor_timeouts == [BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS - 4.0]
    assert web_timeouts == [BACKGROUND_RESUME_HEALTH_TIMEOUT_SECONDS - 8.0]
    assert ui._background_runtime_state == "resume_failed"


def test_complete_exit_cancels_inflight_resume_before_supervisor_start(monkeypatch) -> None:
    """托盘“退出 Hextech”撞上恢复时，DataService 后不得再拉起 Supervisor。"""

    ui = _FakeUI()
    ui._background_runtime_state = "suspended"
    ui.runtime_supervisor = None
    ui.data_service = None
    ui.web_process = None
    data_started = threading.Event()
    release_data_start = threading.Event()

    def start_data(*, timeout: float | None = None):
        del timeout
        ui.calls.append("start_data")
        data_started.set()
        assert release_data_start.wait(timeout=1.0)
        return "data-handle"

    ui.service_manager.start_data_service = start_data  # type: ignore[method-assign]
    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.probe_league_process_state",
        lambda: _state(),
    )
    worker = threading.Thread(
        target=lambda: ui._resume_background_runtime(reason="league_process_detected"),
        daemon=True,
    )
    worker.start()
    assert data_started.wait(timeout=1.0)

    ui._closing = True
    ui.stop_background_runtime()
    release_data_start.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert "start_supervisor" not in ui.calls
    assert ui.runtime_supervisor is None
    assert ui.data_service is None
    assert ui.pause_event.is_set()


def test_background_health_caps_supervisor_status_request_to_remaining_deadline(monkeypatch) -> None:
    """最后一次健康轮询不能再获得固定两秒，避免穿透 13 秒总预算。"""

    ui = _FakeUI()
    clock = {"now": 100.0}
    captured: list[float] = []
    monkeypatch.setattr("hextech.interfaces.desktop.background_runtime.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr("hextech.interfaces.desktop.background_runtime.current_build_id", lambda: "build-current")

    def get_status(*, timeout: float) -> dict[str, object]:
        captured.append(timeout)
        return {
            "components": {
                "game_overlay": {
                    "status": "running",
                    "host_pid": 123,
                    "build_id": "build-current",
                    "build_ids": {"host": "build-current", "sidecar": "build-current"},
                    "sidecar_liveness": {"status": "running"},
                }
            }
        }

    ui.runtime_supervisor = SimpleNamespace(get_status=get_status, is_running=lambda: True)

    health = ui._background_runtime_health(deadline=100.25)

    assert captured == [0.25]
    assert health["overlay"] == "ready"


def test_background_resume_caps_overlay_enable_request_to_remaining_deadline(monkeypatch) -> None:
    ui = _FakeUI()
    clock = {"now": 100.0}
    captured: list[float] = []
    monkeypatch.setattr("hextech.interfaces.desktop.background_runtime.time.monotonic", lambda: clock["now"])

    def set_enabled(_enabled: bool, *, timeout: float) -> dict[str, str]:
        captured.append(timeout)
        return {"status": "accepted"}

    ui.runtime_supervisor = SimpleNamespace(set_game_overlay_enabled=set_enabled)

    ui._restore_game_overlay_after_background_resume(deadline=100.125)

    assert captured == [0.125]


def test_failed_supervisor_cleanup_blocks_next_resume_until_stopped(monkeypatch) -> None:
    ui = _FakeUI()
    stale_supervisor = ui.runtime_supervisor
    ui._background_runtime_state = "resume_cleanup_pending"
    monkeypatch.setattr(
        "hextech.interfaces.desktop.background_runtime.ui_runtime.stop_runtime_supervisor_process",
        lambda _handle: (_ for _ in ()).throw(OSError("still running")),
    )

    ui._resume_background_runtime(reason="league_process_detected")

    assert ui._background_runtime_state == "resume_cleanup_pending"
    assert ui.runtime_supervisor is stale_supervisor
    assert "start_supervisor" not in ui.calls


def test_restart_recognition_routes_cleanup_pending_through_resume_gate() -> None:
    ui = _FakeUI()
    ui._background_runtime_state = "resume_cleanup_pending"
    resumes: list[dict[str, object]] = []
    ui.request_runtime_resume = lambda **kwargs: resumes.append(kwargs)  # type: ignore[method-assign]

    ui.restart_recognition()

    assert resumes == [{"reason": "tray_restart_recognition", "show_window": False}]
    assert "start_supervisor" not in ui.calls

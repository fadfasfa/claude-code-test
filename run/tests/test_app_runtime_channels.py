"""新生产链的管道、身份栅栏和单所有者验证。所有产物均在tmp_path。"""
import json
import os
import subprocess
import sys
import threading
import time
from multiprocessing import Pipe
from pathlib import Path
from types import SimpleNamespace

import pytest

from hextech.modules.session import runtime_channel as transport


def test_channel_duplex_and_payload_bound():
    left, right = Pipe()
    a, b = transport.RuntimeChannel(left), transport.RuntimeChannel(right)
    try:
        a.publish("event", {"sequence": 1})
        b.publish("context", {"publication_seq": 2})
        assert b.wait("event", 2) == {"sequence": 1}
        assert a.wait("context", 2) == {"publication_seq": 2}
        with pytest.raises(ValueError, match="too_large"):
            a.publish("event", {"value": "x" * transport.MAX_MESSAGE_BYTES})
        with pytest.raises(ValueError, match="kind"):
            a.publish("arbitrary", {})
    finally:
        a.close()
        b.close()


def test_pending_messages_coalesce_while_sender_is_blocked():
    class BlockedConnection:
        def __init__(self):
            self.gate = threading.Event()
            self.entered = threading.Event()
        def recv_bytes(self, _limit):
            self.gate.wait(5)
            raise EOFError
        def send_bytes(self, _encoded):
            self.entered.set()
            self.gate.wait(5)
        def close(self):
            self.gate.set()
    connection = BlockedConnection()
    channel = transport.RuntimeChannel(connection)
    try:
        channel.publish("event", {"seq": 0})
        assert connection.entered.wait(1)
        for i in range(1000):
            channel.publish("event", {"seq": i})
        assert len(channel._pending) == 1
        assert json.loads(channel._pending["event"])["payload"]["seq"] == 999
    finally:
        channel.close()


def test_real_named_pipe_ready_and_shutdown():
    """Only an isolated protocol probe is started, never the installed application."""
    pipe = transport.ChildPipe("probe")
    env = os.environ.copy()
    env.update(pipe.environment())
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    code = """
import time
from hextech.modules.session.runtime_channel import child_channel, signal_ready, shutdown_requested
signal_ready(probe=True)
while not shutdown_requested(): time.sleep(.01)
"""
    child = subprocess.Popen([sys.executable, "-c", code], env=env,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    from hextech.interfaces.desktop.runtime_process_windows import WindowsJobObject
    job = WindowsJobObject(child)
    try:
        assert job.attached
        assert pipe.wait_ready(child, 5)["probe"] is True
        assert pipe.publish("shutdown", {"requested": True})
        assert child.wait(timeout=5) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        pipe.close()
        job.close()


def test_app_transport_keeps_context_and_event_in_memory(tmp_path, monkeypatch):
    from hextech.modules.vision import events
    from hextech.interfaces.overlay.context import write_overlay_context
    from hextech.modules.game_context.overlay_context import read_overlay_context
    sent = []
    monkeypatch.setattr(events, "OVERLAY_EVENT_FILE", tmp_path / "event.json")
    transport.install_app_transport(lambda kind, payload: sent.append((kind, payload)))
    try:
        event = events.build_inactive_overlay_event()
        events.write_overlay_event(event)
        assert events.read_overlay_event()["generated_at"] == event["generated_at"]
        context = {"schema_version": 1, "generated_at": time.time(), "champion_id": "1"}
        write_overlay_context(context)
        assert read_overlay_context()["champion_id"] == "1"
        assert not (tmp_path / "event.json").exists()
        assert [kind for kind, _ in sent] == ["event", "context"]
    finally:
        transport.install_app_transport(None)


def test_retired_vision_pipe_cannot_publish_into_new_session(tmp_path):
    from hextech.bootstrap.runtime_owner import AppRuntimeOwner
    from hextech.modules.session.build_identity import current_build_id
    runtime = SimpleNamespace(snapshot=lambda: {}, shutdown=lambda reason: None)
    owner = AppRuntimeOwner(overlay_runtime=runtime, event_log_path=tmp_path / "events.jsonl",
                            retention_worker=SimpleNamespace(request=lambda: None), monitor=False)
    old = SimpleNamespace(close=lambda: None, publish=lambda *_args: True)
    current = SimpleNamespace(close=lambda: None, publish=lambda *_args: True)
    try:
        owner.attach_pipe("vision", old)
        owner.attach_pipe("vision", current)
        owner.receive("vision", old, "event", {"build_id": current_build_id(), "seq": 1})
        assert transport.runtime_payload("event") == {}
        owner.receive("vision", current, "event", {"build_id": "old-build", "seq": 2})
        assert transport.runtime_payload("event") == {}
        owner.receive("vision", current, "event", {"build_id": current_build_id(), "seq": 3})
        assert transport.runtime_payload("event")["seq"] == 3
    finally:
        owner.stop(timeout=2)


def test_ui_status_reads_cached_memory_only(tmp_path):
    from hextech.bootstrap.runtime_owner import AppRuntimeOwner
    runtime=SimpleNamespace(snapshot=lambda:{"status":"starting"},shutdown=lambda reason:None)
    owner=AppRuntimeOwner(overlay_runtime=runtime,event_log_path=tmp_path/"events",monitor=False,
                           retention_worker=SimpleNamespace(request=lambda:None))
    try:
        runtime.snapshot=lambda: (_ for _ in ()).throw(AssertionError("GUI must not read files"))
        assert owner.get_status()["components"]["game_overlay"]["status"]=="starting"
    finally:
        owner.stop(timeout=2)


def test_failed_child_cleanup_retains_owner_and_blocks_replacement(tmp_path):
    from hextech.bootstrap.runtime_owner import AppRuntimeOwner
    runtime=SimpleNamespace(snapshot=lambda:{},shutdown=lambda reason:(_ for _ in ()).throw(OSError("child remains")))
    owner=AppRuntimeOwner(overlay_runtime=runtime,event_log_path=tmp_path/"events",monitor=False,
                           retention_worker=SimpleNamespace(request=lambda:None))
    try:
        with pytest.raises(RuntimeError,match="shutdown_failed"):
            owner.stop(timeout=2)
        assert owner.is_running()
        with pytest.raises(RuntimeError,match="already exists"):
            AppRuntimeOwner(overlay_runtime=runtime,event_log_path=tmp_path/"second",monitor=False)
    finally:
        runtime.shutdown=lambda reason:None
        owner.stop(timeout=2)


@pytest.mark.parametrize("role,frozen,debug", [("host", False, False), ("host", True, False),
                                              ("vision", False, True), ("vision", True, False)])
def test_production_spawn_owns_job_and_pipe_without_readiness_files(monkeypatch, role, frozen, debug):
    from hextech.interfaces.overlay import lifecycle
    from hextech.interfaces.desktop import runtime_process_windows
    from support.process_fakes import FakeProcess
    seen = {}
    registered = []
    class PipeProbe:
        def __init__(self, pipe_role, **kwargs):
            assert pipe_role == role
        def environment(self):
            return {transport.ADDRESS_ENV: "isolated-test-pipe"}
        def wait_ready(self, process, timeout, **kwargs):
            seen["timeout"] = timeout
            return {"startup_profile": {}}
        def publish(self, kind, payload):
            seen["last_message"] = kind
            return True
        def close(self):
            seen["pipe_closed"] = True
    owner = SimpleNamespace(is_running=lambda: True, _shutdown_requested=threading.Event(),
                            attach_pipe=lambda *args: None,
                            register_child=lambda *args: registered.append(args), receive=lambda *args: None, retire_pipe=lambda *args:None)
    def spawn(command, **kwargs):
        seen.update(command=command, env=kwargs["env"], creationflags=kwargs["creationflags"])
        return FakeProcess(4321)
    monkeypatch.setattr(transport, "active_owner", lambda: owner)
    monkeypatch.setattr(transport, "ChildPipe", PipeProbe)
    monkeypatch.setattr(runtime_process_windows, "WindowsJobObject", lambda process: SimpleNamespace(attached=True, close=lambda:None))
    monkeypatch.setattr(lifecycle.subprocess, "Popen", spawn)
    monkeypatch.setattr(lifecycle.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(lifecycle, "resolve_roi_dump_enabled", lambda value=None: debug)
    process = lifecycle.start_host_process() if role == "host" else lifecycle.start_sidecar_process()
    assert seen["creationflags"] == (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert registered == [(role, process)]
    assert transport.ADDRESS_ENV in seen["env"]
    assert lifecycle.OVERLAY_READY_FILE_ENV not in seen["env"]
    assert lifecycle.OVERLAY_SIDECAR_READY_FILE_ENV not in seen["env"]
    if frozen:
        assert ("--game-overlay" if role == "host" else "--overlay-sidecar") in seen["command"]
    else:
        assert ("hextech.bootstrap.overlay" if role == "host" else "hextech.infrastructure.vision.sidecar") in seen["command"]
    if role == "host":
        assert seen["timeout"] == (20.0 if frozen else 5.0)
    if debug:
        assert "--debug-dump" in seen["command"]
    assert lifecycle.stop_process(process)
    assert seen["last_message"] == "shutdown"
    assert seen["pipe_closed"]


def test_cancelled_pipe_start_does_not_wait_for_ready():
    pipe = transport.ChildPipe("probe")
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            pipe.wait_ready(SimpleNamespace(poll=lambda:None), 5, cancel_event=cancel)
    finally:
        pipe.close()


def test_failed_bootstrap_keeps_error_type_and_retry_classification():
    pipe = transport.ChildPipe("probe")
    pipe.channel = SimpleNamespace(latest=lambda kind: {"state":"failed", "error_type":"PermissionError",
                                                       "error_message_sanitized":"resource access denied"}, close=lambda:None)
    try:
        with pytest.raises(transport.RuntimeBootstrapError) as error:
            pipe.wait_ready(SimpleNamespace(poll=lambda:None), 5)
        assert error.value.error_type == "PermissionError"
        assert error.value.retryable is False
    finally:
        pipe.close()


def test_legacy_runtime_never_auto_refreshes_through_embedded_factory(monkeypatch):
    from hextech.bootstrap import data_service_runtime
    from hextech.infrastructure.persistence import release_seed
    from hextech.infrastructure.sources import refresh_service
    monkeypatch.setattr(data_service_runtime, "DataSnapshotPublisher", lambda: object())
    monkeypatch.setattr(release_seed, "initialize_snapshot", lambda publisher: {"state":"failed", "reason_code":"migration_required"})
    monkeypatch.setattr(refresh_service, "IncrementalRefreshService", lambda **kwargs: pytest.fail("must not write/refresh the legacy runtime"))
    with pytest.raises(RuntimeError, match="migration_required"):
        data_service_runtime.create_embedded_data_service()


def test_worker_keeps_catalog_http_failure_and_retry_after():
    from hextech.bootstrap.acquisition_worker import _unexpected_failure_payload
    error=RuntimeError("catalog unavailable")
    error.reason="http_5xx"
    error.response=SimpleNamespace(failure_kind="http_5xx",response_headers={"Retry-After":"123"})
    payload=_unexpected_failure_payload(error)
    assert payload["reason_code"]=="http_5xx"
    assert payload["failure_stage"]=="fetch"
    assert payload["source_result"]["failure_kind"]=="http_5xx"
    assert payload["source_result"]["retry_after_seconds"]==123


def test_closed_real_pipe_cannot_restore_active_event_after_disable(tmp_path):
    from multiprocessing.connection import Client
    from hextech.bootstrap.runtime_owner import AppRuntimeOwner
    from hextech.modules.session.build_identity import current_build_id
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    runtime=SimpleNamespace(snapshot=lambda:{}, shutdown=lambda reason:None)
    owner=AppRuntimeOwner(overlay_runtime=runtime,event_log_path=tmp_path/"events",monitor=False,
                          retention_worker=SimpleNamespace(request=lambda:None))
    def dispatch(kind, payload):
        entered.set()
        assert release.wait(3)
        owner.receive("vision", pipe, kind, payload)
        completed.set()
    pipe=transport.ChildPipe("vision",on_message=dispatch,on_close=lambda:owner.retire_pipe("vision",pipe))
    owner.attach_pipe("vision",pipe)
    sending=transport.RuntimeChannel(Client(pipe.address,family="AF_PIPE",authkey=pipe.key))
    try:
        sending.publish("event",{"build_id":current_build_id(),"active":True,"seq":1})
        assert entered.wait(2)
        pipe.close()
        assert pipe.closed and "vision" not in owner._pipes
        transport.publish_runtime("event",{"build_id":current_build_id(),"active":False,"seq":2})
        release.set()
        assert completed.wait(2)
        assert transport.runtime_payload("event")["seq"]==2
        assert transport.runtime_payload("event")["active"] is False
    finally:
        release.set()
        sending.close()
        pipe.close()
        owner.stop(timeout=2)


def test_ready_replay_requires_current_verified_pipe(tmp_path):
    from hextech.bootstrap.runtime_owner import AppRuntimeOwner
    owner=AppRuntimeOwner(overlay_runtime=SimpleNamespace(snapshot=lambda:{},shutdown=lambda reason:None),
                          event_log_path=tmp_path/"events",monitor=False,
                          retention_worker=SimpleNamespace(request=lambda:None))
    sent=[]
    old=SimpleNamespace(closed=False,ready_verified=True,close=lambda:None,publish=lambda *args:sent.append(("old",args)))
    current=SimpleNamespace(closed=False,ready_verified=False,close=lambda:None,publish=lambda *args:sent.append(("current",args)))
    try:
        transport.publish_runtime("context",{"publication_seq":3})
        transport.publish_runtime("event",{"seq":4})
        owner.attach_pipe("host",old)
        owner.attach_pipe("host",current)
        owner.receive("host",old,"ready",{})
        owner.receive("host",current,"ready",{})
        assert sent==[]
        current.ready_verified=True
        owner.receive("host",current,"ready",{})
        assert [(role,item[0]) for role,item in sent]==[("current","context"),("current","event")]
    finally:
        owner.stop(timeout=2)


@pytest.mark.parametrize("argument", ["--web-server", "--data-service", "--runtime-supervisor"])
def test_retired_role_does_not_accidentally_start_app(argument):
    from hextech.bootstrap.desktop import _frozen_role
    with pytest.raises(SystemExit,match="unsupported runtime role"):
        _frozen_role([argument])


def test_frozen_app_rejects_bad_build_seed_before_starting_any_runtime(tmp_path, monkeypatch):
    from hashlib import sha256
    from hextech.bootstrap import desktop
    from hextech.interfaces.desktop.app import HextechUI
    from hextech.interfaces.desktop import startup_failure
    from hextech.modules.data.ports import paths
    from hextech.modules.data.generation import DataSnapshotClient
    from support.v4_release import make_release

    target, seed = tmp_path / "existing-runtime", tmp_path / "new-build-seed"
    make_release(target, monkeypatch)
    make_release(seed, monkeypatch)
    assert DataSnapshotClient(target / "snapshots").open_view().get_champions()
    image = next(seed.rglob("*.png"))
    image.write_bytes(b"broken new Build baseline")
    def inventory():
        return {str(path.relative_to(target)): sha256(path.read_bytes()).hexdigest()
                for path in target.rglob("*") if path.is_file()}
    before = inventory()
    monkeypatch.setattr(desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths, "resource_path", lambda *parts: seed if parts == ("seeds", "runtime.v4") else pytest.fail("unexpected resource"))
    monkeypatch.setattr(paths, "get_var_dir", lambda: target)
    # Call the actual frozen entry hook, not a tooling-only preflight.
    with pytest.raises((ValueError, OSError)):
        desktop._initialize_packaged_release()
    assert inventory() == before

    ui = HextechUI.__new__(HextechUI)
    ui._closing = False
    ui._windows_session_end_pending = False
    ui.service_manager = None
    ui._desktop_window_presentation = SimpleNamespace(status=lambda: {})
    ui.startup_timing = SimpleNamespace(mark=lambda *args, **kwargs: None)
    ui._assert_no_foreign_runtime_roles = lambda: None
    ui._seed_initializer = desktop._initialize_packaged_release
    ui._data_runtime_factory = lambda: pytest.fail("DataService must not start after bad seed")
    ui._runtime_owner_factory = lambda: pytest.fail("Overlay/Vision owner must not start after bad seed")
    ui._shutdown_failed_bootstrap_service_manager = lambda manager: None
    ui._run_on_ui_thread = lambda callback: callback()
    notices = []
    monkeypatch.setattr(startup_failure, "show_startup_failure", lambda *args: notices.append(args))
    statuses = []
    ui._set_status = lambda text, color: statuses.append(text)
    ui._post_visible_bootstrap()
    assert ui.service_manager is None
    assert any("启动失败" in text for text in statuses)
    assert len(notices) == 1
    assert inventory() == before
    assert DataSnapshotClient(target / "snapshots").open_view().get_champions()

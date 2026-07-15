from __future__ import annotations

import io
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
import pandas as pd

from hextech.data_service import (
    DATA_SERVICE_NONCE_HEADER,
    DataBuildResult,
    DataServiceApplication,
    DataServiceCore,
    DataServiceInstanceLock,
    build_snapshot_from_runtime,
)
from hextech.data_snapshot import DataSnapshotClient, DataSnapshotPublisher


def _build_payload(private_stats_enabled: bool) -> dict[str, object]:
    return {
        "champions": [{"id": 1, "name": "hero"}],
        "champion_hextech": {
            "hero": {"hero_id": 1, "augments": [{"id": "augment-1", "win_rate": 0.51}]}
        },
        "overlay_hints": {
            "source": {"private_policy_stats_enabled": private_stats_enabled},
            "augments": {"augment-1": {"name": "augment"}},
        },
        "identities": {"champions": {"1": "hero"}, "augments": {"augment-1": "augment"}},
    }


def test_refresh_and_policy_actions_are_serialized(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    observed: list[bool] = []

    def builder(private_stats_enabled: bool) -> DataBuildResult:
        observed.append(private_stats_enabled)
        entered.set()
        release.wait(timeout=2)
        return DataBuildResult(_build_payload(private_stats_enabled))

    service = DataServiceCore(publisher=publisher, builder=builder, private_stats_enabled=False)
    refresh_thread = threading.Thread(target=service.refresh)
    refresh_thread.start()
    assert entered.wait(timeout=1)

    policy_thread = threading.Thread(target=service.set_private_stats, args=(True,))
    policy_thread.start()
    release.set()
    refresh_thread.join(timeout=2)
    policy_thread.join(timeout=2)

    assert observed == [False, True]
    client = DataSnapshotClient(tmp_path)
    assert client.load_manifest().private_stats_enabled is True
    assert client.get_overlay_hints()["source"]["private_policy_stats_enabled"] is True


def test_failed_refresh_preserves_last_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    service = DataServiceCore(
        publisher=publisher,
        builder=lambda private: DataBuildResult(_build_payload(private)),
        private_stats_enabled=True,
    )
    first = service.refresh()
    service.builder = lambda private: (_ for _ in ()).throw(RuntimeError("remote failed"))

    failed = service.refresh()

    assert first["state"] == "ready"
    assert failed["state"] == "degraded"
    assert failed["reason_code"] == "refresh_failed_last_good_preserved"
    assert DataSnapshotClient(tmp_path).status()["generation_id"] == first["generation_id"]


def test_service_publishes_source_summary(tmp_path: Path) -> None:
    source = {"name": "remote.csv", "size": 3, "sha256": "a" * 64, "record_count": 1}
    service = DataServiceCore(
        publisher=DataSnapshotPublisher(tmp_path),
        builder=lambda private: DataBuildResult(_build_payload(private), (source,)),
        private_stats_enabled=True,
    )

    assert service.refresh()["state"] == "ready"
    assert DataSnapshotClient(tmp_path).load_manifest().source_files == (source,)


def test_failed_private_stats_action_rolls_back_desired_policy(tmp_path: Path) -> None:
    service = DataServiceCore(
        publisher=DataSnapshotPublisher(tmp_path),
        builder=lambda private: DataBuildResult(_build_payload(private)),
        private_stats_enabled=False,
    )
    service.refresh()
    service.builder = lambda private: (_ for _ in ()).throw(RuntimeError("remote failed"))

    result = service.set_private_stats(True)

    assert result["state"] == "failed"
    assert result["reason_code"] == "private_stats_update_failed"
    assert service.status()["desired_private_stats_enabled"] is False
    assert DataSnapshotClient(tmp_path).load_manifest().private_stats_enabled is False


def test_control_plane_queues_actions_and_requires_nonce(tmp_path: Path) -> None:
    service = DataServiceCore(
        publisher=DataSnapshotPublisher(tmp_path),
        builder=lambda private: DataBuildResult(_build_payload(private)),
        private_stats_enabled=False,
    )
    application = DataServiceApplication(core=service, parent_pid=1, nonce="test-nonce")
    server = ThreadingHTTPServer(("127.0.0.1", 0), application.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Host": "127.0.0.1", DATA_SERVICE_NONCE_HEADER: "test-nonce"}
    try:
        assert requests.get(f"{base_url}/v1/status", timeout=2).status_code == 403
        response = requests.post(f"{base_url}/v1/actions/refresh", headers=headers, timeout=2)
        assert response.status_code == 202
        action_id = response.json()["action_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = requests.get(f"{base_url}/v1/status", headers=headers, timeout=2).json()
            if action_id in status.get("actions", {}):
                break
            time.sleep(0.02)
        else:
            pytest.fail("DataService action 未在期限内完成")
        assert status["actions"][action_id]["status"] == "completed"
        assert status["snapshot"]["state"] == "ready"
    finally:
        application.shutdown_requested.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_private_stats_handle_tracks_the_same_action_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.display.desktop import runtime

    class Process:
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"accepted": True, "action_id": "policy-1"}

    handle = runtime.DataServiceHandle(Process(), 52001, "nonce", 4321)
    statuses = iter(
        [
            {"actions": {}, "last_action": None},
            {
                "actions": {
                    "policy-1": {
                        "action_id": "policy-1",
                        "status": "completed",
                        "result": {"state": "ready", "private_stats_enabled": True},
                    }
                }
            },
        ]
    )
    monkeypatch.setattr(requests, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(handle, "get_status", lambda: next(statuses))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    result = handle.set_private_stats(True)

    assert result == {"state": "ready", "private_stats_enabled": True}


def test_refresh_action_is_singleflight_while_running(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def builder(private: bool) -> DataBuildResult:
        entered.set()
        release.wait(timeout=2)
        return DataBuildResult(_build_payload(private))

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            builder=builder,
            private_stats_enabled=False,
        ),
        parent_pid=1,
    )
    first = application.submit_action("refresh")
    assert entered.wait(timeout=1)
    second = application.submit_action("refresh")
    release.set()
    application.shutdown_requested.set()

    assert first["accepted"] is True
    assert second == {"accepted": False, "reason_code": "already_queued"}


def test_data_service_instance_lock_is_exclusive(tmp_path: Path) -> None:
    first = DataServiceInstanceLock(tmp_path / "data-service.lock")
    second = DataServiceInstanceLock(tmp_path / "data-service.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_runtime_builder_preserves_real_csv_ids_stats_and_synergy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.catalog import precomputed_cache, runtime_store
    import hextech.overlay.hints as overlay_hints

    csv_path = tmp_path / "Hextech_Data_2026-07-15.csv"
    dataframe = pd.DataFrame(
        [
            {
                "英雄ID": "266",
                "英雄名称": "暗裔剑魔",
                "英雄评级": "S",
                "英雄胜率": 0.52,
                "英雄出场率": 0.08,
                "海克斯ID": "1322",
                "海克斯名称": "测试强化",
                "海克斯阶级": "Gold",
                "海克斯胜率": 0.61,
                "海克斯出场率": 0.04,
                "胜率差": 0.09,
                "综合得分": 1.2,
            }
        ]
    )
    dataframe.to_csv(csv_path, index=False, encoding="utf-8-sig")
    synergy_path = tmp_path / "Champion_Synergy_Cleaned.json"
    synergy_path.write_text(
        json.dumps({"266": {"synergy_items": [{"content": "同代联动"}]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_store, "get_latest_valid_csv", lambda: str(csv_path))
    monkeypatch.setattr(runtime_store, "load_runtime_csv", lambda _path: dataframe.copy())
    monkeypatch.setattr(runtime_store, "build_synergy_data_path", lambda: str(synergy_path))
    monkeypatch.setattr(overlay_hints, "build_synergy_data_path", lambda: str(synergy_path))
    monkeypatch.setattr(
        precomputed_cache,
        "load_precomputed_champion_list",
        lambda: [
            {
                "英雄 ID": "266",
                "英雄名称": "暗裔剑魔",
                "英雄胜率": 0.52,
                "英雄出场率": 0.08,
            }
        ],
    )
    monkeypatch.setattr(
        precomputed_cache,
        "load_precomputed_hextech_map",
        lambda: {
            "暗裔剑魔": {
                "comprehensive": [
                    {
                        "海克斯ID": "1322",
                        "海克斯名称": "测试强化",
                        "海克斯阶级": "Gold",
                        "海克斯胜率": 0.61,
                        "海克斯出场率": 0.04,
                        "胜率差": 0.09,
                    }
                ]
            }
        },
    )

    build = build_snapshot_from_runtime(True)
    detail = build.payloads["champion_hextech"]["暗裔剑魔"]

    assert build.payloads["champions"][0]["id"] == "266"
    assert detail["hero_id"] == "266"
    assert detail["augments"][0]["id"] == "1322"
    assert detail["augments"][0]["海克斯胜率"] == pytest.approx(0.61)
    assert detail["synergy"]["synergy_items"][0]["content"] == "同代联动"
    assert [source["record_count"] for source in build.source_files] == [1, 1]


def test_service_manager_owns_data_service_lifecycle() -> None:
    from hextech.display.desktop.service_manager import ServiceManager

    class Handle:
        pid = 4321

        def __init__(self) -> None:
            self.stopped = False

    handle = Handle()
    manager = ServiceManager(
        start_web_func=lambda: object(),
        start_data_service_func=lambda: handle,
        stop_data_service_func=lambda value: setattr(value, "stopped", True),
    )

    assert manager.start_data_service() is handle
    assert manager.get_status_snapshot()["data_service"]["pid"] == 4321
    manager.shutdown()

    assert handle.stopped is True
    assert manager.get_status_snapshot()["data_service"]["status"] == "stopped"


def test_service_manager_restarts_data_service_after_child_exit() -> None:
    from hextech.display.desktop.runtime import DataServiceHandle
    from hextech.display.desktop.service_manager import ServiceManager

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None
            self.stdout = None
            self.stderr = None

        def poll(self) -> int | None:
            return self.returncode

    class JobObject:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    first_process = Process(4321)
    second_process = Process(4322)
    first_job = JobObject()
    handles = [
        DataServiceHandle(first_process, 52001, "first", first_process.pid, first_job),
        DataServiceHandle(second_process, 52002, "second", second_process.pid),
    ]
    manager = ServiceManager(start_web_func=lambda: object(), start_data_service_func=lambda: handles.pop(0))

    first = manager.start_data_service()
    first_process.returncode = 7
    stopped_status = manager.get_status_snapshot()["data_service"]
    second = manager.start_data_service()

    assert stopped_status["status"] == "stopped"
    assert stopped_status["pid"] is None
    assert first is not second
    assert second.process is second_process
    assert first_job.closed is True
    assert first.job_object is None
    status = manager.get_status_snapshot()["data_service"]
    assert status["status"] == "running"
    assert status["pid"] == 4322


def test_data_service_bootstrap_timeout_is_not_blocked_by_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.display.desktop import runtime

    class BlockingStream:
        def __init__(self) -> None:
            self.release = threading.Event()

        def readline(self) -> str:
            self.release.wait(timeout=1)
            return ""

        def read(self) -> str:
            return ""

        def close(self) -> None:
            self.release.set()

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BlockingStream()
            self.stderr = BlockingStream()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1
            self.stdout.close()
            self.stderr.close()

        def kill(self) -> None:
            self.terminate()

        def wait(self, timeout=None):
            return self.returncode

    fake_process = FakeProcess()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: fake_process)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="bootstrap 超时"):
        runtime.start_data_service_process(timeout=0.05)

    assert time.monotonic() - started < 0.5


def test_data_service_bootstrap_keeps_draining_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.display.desktop import runtime

    stdout_text = json.dumps({"port": 52001, "session_nonce": "nonce", "pid": 4321}) + "\nextra stdout\n"
    stderr_text = "refresh diagnostic\n" * 10000

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO(stdout_text)
            self.stderr = io.StringIO(stderr_text)
            self.returncode: int | None = None
            self.pid = 4321

        def poll(self) -> int | None:
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(runtime, "_WindowsJobObject", lambda _process: None)

    handle = runtime.start_data_service_process(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and (
        process.stdout.tell() < len(stdout_text) or process.stderr.tell() < len(stderr_text)
    ):
        time.sleep(0.01)

    assert handle.pid == 4321
    assert process.stdout.tell() == len(stdout_text)
    assert process.stderr.tell() == len(stderr_text)
    process.returncode = 0
    assert handle.close_exited_resources() is True

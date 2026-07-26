from __future__ import annotations

import io
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
import pandas as pd

from hextech.bootstrap.data_service_runtime import (
    build_snapshot_from_runtime,
)
from hextech.contracts import SourceProvenance
from hextech.modules.recommendation.hints import (
    enrich_overlay_hint_cache_with_catalog,
    enrich_overlay_hint_cache_with_synergy,
)


def _provenance(marker: str, *, source: str = "hextech", role: str = "stats") -> SourceProvenance:
    return SourceProvenance(
        source=source,  # type: ignore[arg-type]
        run_id=f"run-{marker}",
        catalog_generation_id="catalog-test",
        artifact_role=role,
        artifact_sha256=hashlib.sha256(f"artifact:{marker}".encode()).hexdigest(),
        record_count=1,
        manifest_sha256=hashlib.sha256(f"manifest:{marker}".encode()).hexdigest(),
        content_schema_version=2,
    )


def _complete_provenance(marker: str) -> tuple[SourceProvenance, ...]:
    return tuple(
        _provenance(f"{marker}-{source}-{role}", source=source, role=role)
        for source, role in (
            ("catalog", "champions"),
            ("catalog", "augments"),
            ("catalog", "versions"),
            ("hextech", "stats"),
            ("apex", "synergy"),
            ("mayhem", "combos"),
        )
    )


def test_brand_synergy_projects_all_known_names_and_catalog_only_hint() -> None:
    """复仇焰魂的 11 条方案必须附着到最终 hint，而不是停在 generation 原始数据。"""

    names = ["虚幻武器", "你肩上的恶魔", *[f"联动海克斯{i}" for i in range(1, 13)]]
    cache = {
        "schema_version": 1,
        "source": {"private_policy_stats_enabled": True},
        "hints": {
            "100": {
                "augment_id": "100",
                "name": "虚幻武器",
                "stats_by_champion_id": {"63": {"winrate": 0.55, "pickrate": 0.12}},
            }
        },
        "name_index": {"虚幻武器": "100"},
    }
    catalog = [
        {"augment_name_id": "illusory_weapons", "name": "虚幻武器", "tier": "Gold"},
        {"augment_name_id": "devil_on_your_shoulder", "name": "你肩上的恶魔", "tier": "Prismatic"},
        *[
            {"augment_name_id": f"synergy_{index}", "name": name, "tier": "Gold"}
            for index, name in enumerate(names[2:], start=1)
        ],
    ]
    enrich_overlay_hint_cache_with_catalog(cache, catalog)
    items = [
        {
            "augment_names": names[index : index + 2],
            "rating": "S" if index == 0 else "A",
            "content": f"复仇焰魂方案 {index + 1}",
        }
        for index in range(0, 11)
    ]
    # 保证测试口径与真机证据一致：11 条方案、14 个唯一名称。
    items[-1]["augment_names"] = names[10:]

    report = enrich_overlay_hint_cache_with_synergy(
        cache,
        {"63": {"name": "复仇焰魂", "synergy_items": items}},
    )

    assert report["hero_count"] == 1
    assert report["item_count"] == 11
    assert report["unique_augment_name_count"] == 14
    assert report["catalog_resolvable_name_count"] == 14
    assert report["projected_name_count"] == 14
    assert report["projection_coverage"] == 1.0
    virtual = cache["hints"][cache["name_index"]["虚幻武器"]]
    shoulder = cache["hints"][cache["name_index"]["你肩上的恶魔"]]
    assert virtual["synergies"][0]["hero_id"] == "63"
    assert shoulder["synergies"][0]["hero_name"] == "复仇焰魂"
    assert not shoulder.get("stats_by_champion_id")
    assert report["catalog_only_hints_with_synergy_count"] >= 1


def test_synergy_projection_rejects_empty_or_unresolvable_input() -> None:
    cache = {"hints": {"known": {"augment_id": "known", "name": "已知"}}, "name_index": {"已知": "known"}}

    with pytest.raises(ValueError, match="输入为空"):
        enrich_overlay_hint_cache_with_synergy(cache, {"63": {"synergy_items": []}})
    with pytest.raises(ValueError, match="没有可关联 Catalog"):
        enrich_overlay_hint_cache_with_synergy(
            cache,
            {"63": {"name": "复仇焰魂", "synergy_items": [{"augment_names": ["污染名称"]}]}},
        )

def test_runtime_builder_preserves_real_csv_ids_stats_and_synergy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from hextech.bootstrap import data_service_runtime
    from hextech.modules.acquisition.mayhem import merge as mayhem_merge
    from hextech.modules.data.catalog import runtime_store, version_catalog
    from hextech.modules.data.catalog import versioned as catalog_versioned
    from hextech.modules.data import source_runs

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
    synergy_path = tmp_path / "synergy.json"
    synergy_path.write_text(
        json.dumps(
            {
                "266": {
                    "name": "暗裔剑魔",
                    "synergy_items": [
                        {"augment_names": ["测试强化"], "rating": "S", "content": "同代联动"}
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    mayhem_path = tmp_path / "mayhem.json"
    mayhem_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr(runtime_store, "load_runtime_csv", lambda _path: dataframe.copy())
    monkeypatch.setattr(
        runtime_store,
        "get_latest_valid_csv",
        lambda: pytest.fail("contribution builder 不得读取全局 Hextech current"),
    )
    monkeypatch.setattr(
        runtime_store,
        "build_synergy_data_path",
        lambda: pytest.fail("contribution builder 不得读取全局 Apex current"),
    )
    monkeypatch.setattr(
        source_runs,
        "resolve_current_artifact",
        lambda _source: pytest.fail("contribution builder 不得解析全局 source current"),
    )
    artifacts = {"hextech": csv_path, "apex": synergy_path, "mayhem": mayhem_path}
    monkeypatch.setattr(
        data_service_runtime,
        "_validated_source_artifact",
        lambda source, _pointer, expected_role: artifacts[source],
    )
    monkeypatch.setattr(
        mayhem_merge,
        "merge_mayhem_combos",
        lambda **_kwargs: {"merged_payload": json.loads(synergy_path.read_text(encoding="utf-8"))},
    )
    monkeypatch.setattr(
        version_catalog,
        "load_champion_core_data",
        lambda _root=None: {"266": {"name": "暗裔剑魔", "en_name": "Aatrox"}},
    )
    monkeypatch.setattr(
        version_catalog,
        "load_augment_manifest_entries",
        lambda _root=None: [{"name": "测试强化", "augment_name_id": "test", "tier": "Gold"}],
    )
    catalog_sources = tuple(
        _provenance(f"catalog-{role}", source="catalog", role=role)
        for role in ("champions", "augments", "versions")
    )
    catalog_files = tuple(
        SimpleNamespace(role=role, relative_path=f"{role}.json")
        for role in ("champions", "augments", "versions")
    )
    monkeypatch.setattr(
        catalog_versioned,
        "load_active_catalog",
        lambda: type(
            "Catalog",
            (),
                {
                    "generation_id": "catalog-test",
                    "content_sha256": "c" * 64,
                    "root": tmp_path,
                    "manifest": SimpleNamespace(files=catalog_files),
                    "provenance": lambda self: catalog_sources,
                },
        )(),
    )

    def source_pointer(source: str) -> dict[str, object]:
        role = {"hextech": "stats", "apex": "synergy", "mayhem": "combos"}[source]
        artifact_hash = hashlib.sha256(f"source:{source}".encode()).hexdigest()
        return {
            "schema_version": 2,
            "source": source,
            "run_id": f"run-{source}",
            "catalog_generation_id": "catalog-test",
            "catalog_sha256": "c" * 64,
            "manifest_sha256": hashlib.sha256(f"manifest:{source}".encode()).hexdigest(),
            "artifact": {
                "role": role,
                "relative_path": f"{source}.json",
                "sha256": artifact_hash,
                "record_count": 1,
                "content_schema_version": 2,
                "size": 1,
            },
            "completed_at": "2026-07-17T00:00:00+00:00",
            "last_success_at": "2026-07-17T00:00:00+00:00",
        }

    monkeypatch.setattr(source_runs, "load_source_current", lambda source, verify_hash=True: source_pointer(source))
    build = build_snapshot_from_runtime()
    detail = build.payloads["champion_hextech"]["暗裔剑魔"]

    assert build.payloads["champions"][0]["id"] == "266"
    assert detail["hero_id"] == "266"
    assert detail["augments"][0]["id"] == "1322"
    assert detail["augments"][0]["海克斯胜率"] == pytest.approx(0.61)
    assert detail["synergy"]["synergy_items"][0]["content"] == "同代联动"
    hint = build.payloads["overlay_hints"]["hints"]["1322"]
    assert hint["synergies"][0]["hero_id"] == "266"
    assert build.payloads["overlay_hints"]["source"]["synergy_projection"]["projection_coverage"] == 1.0
    assert [source.record_count for source in build.source_files] == [1, 1, 1, 1, 1, 1]


def test_service_manager_owns_data_service_lifecycle() -> None:
    from hextech.interfaces.desktop.service_manager import ServiceManager

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
    from hextech.interfaces.desktop.runtime import DataServiceHandle
    from hextech.interfaces.desktop.service_manager import ServiceManager

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


def test_data_service_handle_keeps_live_child_when_venv_launcher_has_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.interfaces.desktop import runtime, runtime_processes

    class ExitedLauncher:
        pid = 101
        returncode = 0
        stdout = None
        stderr = None

        @staticmethod
        def poll() -> int:
            return 0

    class JobObject:
        closed = False

        def close(self) -> None:
            self.closed = True

    job = JobObject()
    handle = runtime.DataServiceHandle(ExitedLauncher(), 52001, "nonce", 202, job)
    child_alive = {"value": True}
    monkeypatch.setattr(runtime_processes, "_pid_is_running", lambda _pid: child_alive["value"])

    assert handle.poll() is None
    assert handle.close_exited_resources() is False

    child_alive["value"] = False
    assert handle.poll() == 0
    assert handle.close_exited_resources() is True
    assert job.closed is True


def test_data_service_bootstrap_timeout_is_not_blocked_by_readline(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.interfaces.desktop import runtime

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
    from hextech.interfaces.desktop import runtime

    stdout_text = "extra stdout\n"
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

    def fake_popen(*args, **kwargs):
        del args
        env = kwargs["env"]
        bootstrap_path = Path(env["HEXTECH_PROCESS_BOOTSTRAP_FILE"])
        bootstrap_path.write_text(
            json.dumps(
                {
                    "port": 52001,
                    "session_nonce": "nonce",
                    "pid": 4321,
                    "token": env["HEXTECH_PROCESS_BOOTSTRAP_TOKEN"],
                }
            ),
            encoding="utf-8",
        )
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
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

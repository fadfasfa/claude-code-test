from __future__ import annotations

import json
import hashlib
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil
import pytest

from hextech.bootstrap.data_service_runtime import DataBuildResult
from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator
from hextech.contracts import SourceProvenance
from hextech.infrastructure.processes import IsolatedProcessResult, run_isolated_process
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json


def _arg(command: list[str], name: str) -> Path:
    return Path(command[command.index(name) + 1])


class FakeWorkerRunner:
    def __init__(self, *, fail_source: str = "") -> None:
        self.fail_source = fail_source
        self.calls: list[str] = []
        self.runtime_roots: list[Path] = []
        self.round = 0

    def __call__(self, command, **_kwargs) -> IsolatedProcessResult:
        source = command[command.index("--source") + 1]
        self.calls.append(source)
        self.runtime_roots.append(Path(_kwargs["env"]["HEXTECH_VAR_DIR"]))
        pointer_path = _arg(command, "--pointer-output")
        result_path = _arg(command, "--result-output")
        if source == self.fail_source:
            atomic_write_json(
                result_path,
                {"state": "failed", "source": source, "error_type": "FixtureFailure"},
            )
            return IsolatedProcessResult(2, 0.01, False, "", "fixture failure")
        if source == "catalog":
            pointer = {
                "schema_version": 2,
                "catalog_generation_id": "catalog-test",
                "content_sha256": "c" * 64,
                "manifest_sha256": "d" * 64,
                "completed_at": "2026-01-01T00:00:00+00:00",
                "last_success_at": "2026-01-01T00:00:00+00:00",
            }
        else:
            role = {"hextech": "stats", "apex": "synergy", "mayhem": "combos"}[source]
            filename = {"hextech": "stats.csv", "apex": "synergy.json", "mayhem": "combos.json"}[source]
            pointer = {
                "schema_version": 2,
                "source": source,
                "run_id": f"{source}-run-{self.round}",
                "catalog_generation_id": "catalog-test",
                "catalog_sha256": "c" * 64,
                "manifest_sha256": {"hextech": "1", "apex": "2", "mayhem": "3"}[source] * 64,
                "artifact": {
                    "role": role,
                    "relative_path": filename,
                    "sha256": {"hextech": "4", "apex": "5", "mayhem": "6"}[source] * 64,
                    "record_count": 1,
                    "content_schema_version": 2,
                    "size": 1,
                },
                "completed_at": "2026-01-01T00:00:00+00:00",
                "last_success_at": "2026-01-01T00:00:00+00:00",
            }
        atomic_write_json(pointer_path, pointer)
        atomic_write_json(result_path, {"state": "ready", "source": source, "pointer": pointer})
        return IsolatedProcessResult(0, 0.01, False, "", "")


def _builder(root: Path) -> DataBuildResult:
    pointers = {
        source: json.loads((root / "sources" / source / "current.v2.json").read_text(encoding="utf-8"))
        for source in ("hextech", "apex", "mayhem")
    }
    catalog = json.loads((root / "catalog" / "current.v2.json").read_text(encoding="utf-8"))
    provenance = [
        SourceProvenance(
            source="catalog",
            run_id="catalog-test",
            catalog_generation_id="catalog-test",
            artifact_role=role,
            artifact_sha256=value * 64,
            record_count=1,
            manifest_sha256=catalog["manifest_sha256"],
            content_schema_version=2,
        )
        for role, value in (("champions", "7"), ("augments", "8"), ("versions", "9"))
    ]
    for source in ("hextech", "apex", "mayhem"):
        pointer = pointers[source]
        artifact = pointer["artifact"]
        provenance.append(
            SourceProvenance(
                source=source,  # type: ignore[arg-type]
                run_id=pointer["run_id"],
                catalog_generation_id=pointer["catalog_generation_id"],
                artifact_role=artifact["role"],
                artifact_sha256=artifact["sha256"],
                record_count=artifact["record_count"],
                manifest_sha256=pointer["manifest_sha256"],
                content_schema_version=artifact["content_schema_version"],
            )
        )
    return DataBuildResult(
        payloads={
            "champions": [{"id": "1", "name": "测试英雄"}],
            "champion_hextech": {
                "测试英雄": {"hero_id": "1", "augments": [{"id": "10", "name": "测试海克斯"}]}
            },
            "overlay_hints": {"augments": {"10": "测试海克斯"}, "hints": {}},
            "identities": {
                "schema_version": 2,
                "champions": {"1": "测试英雄"},
                "augments": {"10": "测试海克斯"},
            },
        },
        source_files=tuple(provenance),
    )


def test_cohort_promotes_only_after_all_candidates_succeed(tmp_path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    assert runner.calls == ["catalog", "hextech", "apex", "mayhem"]
    assert runner.runtime_roots == [tmp_path.resolve()] * 4
    assert DataSnapshotClient(tmp_path / "snapshots").status()["state"] == "ready"
    assert not coordinator.promotion.journal_path.exists()


def test_failed_candidate_never_changes_formal_pointers(tmp_path) -> None:
    runner = FakeWorkerRunner(fail_source="apex")
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "failed"
    assert not (tmp_path / "catalog" / "current.v2.json").exists()
    assert not (tmp_path / "sources" / "hextech" / "current.v2.json").exists()
    assert not (tmp_path / "snapshots" / "current.v2.json").exists()


def test_same_content_new_runs_update_sources_without_replacing_generation(tmp_path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    runner.round = 1

    second = coordinator.refresh(force=True)

    assert second["generation_id"] == first["generation_id"]
    hextech = json.loads((tmp_path / "sources" / "hextech" / "current.v2.json").read_text(encoding="utf-8"))
    assert hextech["run_id"] == "hextech-run-1"
    manifest = DataSnapshotClient(tmp_path / "snapshots").load_manifest()
    old_hextech = next(item for item in manifest.source_files if item.source == "hextech")
    assert old_hextech.run_id == "hextech-run-0"


def test_isolated_process_timeout_returns_without_worker_hang(tmp_path) -> None:
    started = time.monotonic()
    result = run_isolated_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=0.2,
        cancel_grace_seconds=0.1,
        cancel_file=tmp_path / "worker.cancel",
    )

    assert result.timed_out is True
    assert time.monotonic() - started < 4
    assert not (tmp_path / "worker.cancel").exists()


def test_coordinator_reads_only_v2_hash_verified_pointer_from_its_runtime_root(tmp_path) -> None:
    run_root = tmp_path / "sources" / "hextech" / "runs" / "run-test"
    run_root.mkdir(parents=True)
    manifest_path = run_root / "manifest.json"
    artifact_path = run_root / "stats.csv"
    manifest_path.write_text('{"schema_version":2}', encoding="utf-8")
    artifact_path.write_text("英雄ID,海克斯ID\n1,10\n", encoding="utf-8")
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    pointer = {
        "schema_version": 2,
        "source": "hextech",
        "run_id": "run-test",
        "catalog_generation_id": "catalog-test",
        "catalog_sha256": "c" * 64,
        "manifest_sha256": digest(manifest_path),
        "artifact": {
            "role": "stats",
            "relative_path": "stats.csv",
            "sha256": digest(artifact_path),
            "record_count": 1,
            "content_schema_version": 2,
            "size": artifact_path.stat().st_size,
        },
        "completed_at": "2026-01-01T00:00:00+00:00",
        "last_success_at": "2026-01-01T00:00:00+00:00",
    }
    atomic_write_json(tmp_path / "sources" / "hextech" / "current.v2.json", pointer)
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=FakeWorkerRunner(),
    )

    assert coordinator._current_pointer("hextech")["run_id"] == "run-test"
    artifact_path.write_text("tampered", encoding="utf-8")
    assert coordinator._current_pointer("hextech") == {}
    pointer["schema_version"] = 1
    atomic_write_json(tmp_path / "sources" / "hextech" / "current.v2.json", pointer)
    assert coordinator._current_pointer("hextech") == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object contract")
def test_isolated_process_timeout_kills_spawned_process_tree(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = "\n".join(
        (
            "import pathlib, subprocess, sys, time",
            "time.sleep(0.3)",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii')",
            "time.sleep(30)",
        )
    )
    child_pid = 0
    try:
        result = run_isolated_process(
            [sys.executable, "-c", script, str(child_pid_path)],
            timeout_seconds=1.0,
            cancel_grace_seconds=0.1,
            cancel_file=tmp_path / "tree.cancel",
        )
        assert result.timed_out is True
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        deadline = time.monotonic() + 3.0
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(child_pid)
    finally:
        if child_pid and psutil.pid_exists(child_pid):
            process = psutil.Process(child_pid)
            process.kill()
            process.wait(timeout=3)

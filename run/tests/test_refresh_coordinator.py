"""验证刷新 singleflight、candidate promotion 与隔离 worker 的停止边界。"""

from __future__ import annotations

import json
import hashlib
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import pytest

from hextech.bootstrap.data_service_runtime import DataBuildResult
from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator, SOURCE_INTERVALS
from hextech.contracts import CatalogManifestV2, RefreshSourceState, SourceProvenance
from hextech.infrastructure.processes import IsolatedProcessResult, run_isolated_process
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.catalog.versioned import (
    CATALOG_FILES,
    build_catalog_manifest,
    canonical_json_sha256,
    sha256_file,
)
from hextech.modules.data.ports.atomic import atomic_write_json


def test_apex_and_mayhem_share_72_hour_refresh_interval() -> None:
    assert SOURCE_INTERVALS["apex"].total_seconds() == 72 * 60 * 60
    assert SOURCE_INTERVALS["mayhem"].total_seconds() == 72 * 60 * 60


def test_missing_source_pointer_still_honors_failure_backoff(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=FakeWorkerRunner(),
        now=lambda: now,
    )
    state = RefreshSourceState(
        next_due_at=(now + timedelta(hours=6)).isoformat(),
        failure_kind="http_403",
        state="backoff",
    )

    assert coordinator._due("apex", state, {}, force=False) is False
    assert coordinator._due("apex", state, {}, force=True) is True


def _arg(command: list[str], name: str) -> Path:
    return Path(command[command.index(name) + 1])


class FakeWorkerRunner:
    def __init__(self, *, fail_source: str = "") -> None:
        self.fail_source = fail_source
        self.calls: list[str] = []
        self.runtime_roots: list[Path] = []
        self.round = 0
        self.catalog_id = ""
        self.catalog_sha256 = ""
        self.catalog_manifest_sha256 = ""

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
            catalog_root = self.runtime_roots[-1] / "catalog" / "fixture"
            catalog_root.mkdir(parents=True, exist_ok=True)
            resource_catalog = Path(__file__).resolve().parents[1] / "resources" / "catalog"
            for _role, filename, _list_key in CATALOG_FILES:
                shutil.copy2(resource_catalog / filename, catalog_root / filename)
            manifest = build_catalog_manifest(catalog_root, created_at="2026-01-01T00:00:00+00:00")
            generation_root = self.runtime_roots[-1] / "catalog" / "generations" / manifest.catalog_generation_id
            generation_root.mkdir(parents=True, exist_ok=True)
            for _role, filename, _list_key in CATALOG_FILES:
                shutil.copy2(catalog_root / filename, generation_root / filename)
            manifest_path = generation_root / "manifest.json"
            atomic_write_json(manifest_path, manifest.to_dict())
            self.catalog_id = manifest.catalog_generation_id
            self.catalog_sha256 = manifest.content_sha256
            self.catalog_manifest_sha256 = sha256_file(manifest_path)
            pointer = {
                "schema_version": 2,
                "catalog_generation_id": self.catalog_id,
                "content_sha256": self.catalog_sha256,
                "manifest_sha256": self.catalog_manifest_sha256,
                "completed_at": "2026-01-01T00:00:00+00:00",
                "last_success_at": "2026-01-01T00:00:00+00:00",
            }
        else:
            role = {"hextech": "stats", "apex": "synergy", "mayhem": "combos"}[source]
            filename = {"hextech": "stats.csv", "apex": "synergy.json", "mayhem": "combos.json"}[source]
            run_id = f"{source}-run-{self.round}"
            run_root = self.runtime_roots[-1] / "sources" / source / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            artifact_path = run_root / filename
            artifact_path.write_text(f"fixture-{source}\n", encoding="utf-8")
            manifest_path = run_root / "manifest.json"
            atomic_write_json(manifest_path, {"schema_version": 2, "source": source, "run_id": run_id})
            pointer = {
                "schema_version": 2,
                "source": source,
                "run_id": run_id,
                "catalog_generation_id": self.catalog_id,
                "catalog_sha256": self.catalog_sha256,
                "manifest_sha256": sha256_file(manifest_path),
                "artifact": {
                    "role": role,
                    "relative_path": filename,
                    "sha256": sha256_file(artifact_path),
                    "record_count": 1,
                    "content_schema_version": 2,
                    "size": artifact_path.stat().st_size,
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
    catalog_id = str(catalog["catalog_generation_id"])
    catalog_manifest = CatalogManifestV2.from_mapping(
        json.loads(
            (root / "catalog" / "generations" / catalog_id / "manifest.json").read_text(encoding="utf-8")
        )
    )
    provenance = [
        SourceProvenance(
            source="catalog",
            run_id=catalog_id,
            catalog_generation_id=catalog_id,
            artifact_role=item.role,
            artifact_sha256=item.sha256,
            record_count=item.record_count,
            manifest_sha256=catalog["manifest_sha256"],
            content_schema_version=item.content_schema_version,
        )
        for item in catalog_manifest.files
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
            "overlay_hints": {
                "hints": {"10": {"augment_id": "10", "name": "测试海克斯"}},
                "name_index": {"10": "10", "测试海克斯": "10"},
            },
            "identities": {
                "schema_version": 2,
                "champions": {"1": "测试英雄"},
                "augments": {"10": "测试海克斯"},
            },
        },
        source_files=tuple(provenance),
    )


@pytest.mark.parametrize("alter_catalog_artifact", (False, True))
def test_baseline_recovery_normalizes_manifest_hash_but_rejects_different_catalog_artifacts(
    tmp_path: Path, alter_catalog_artifact: bool
) -> None:
    runner = FakeWorkerRunner()
    work = tmp_path / "catalog-work"
    work.mkdir()
    pointer_path = work / "catalog.pointer.json"
    result_path = work / "catalog.result.json"
    runner(
        [
            "fixture",
            "--source",
            "catalog",
            "--pointer-output",
            str(pointer_path),
            "--result-output",
            str(result_path),
        ],
        env={"HEXTECH_VAR_DIR": str(tmp_path)},
    )
    catalog_pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    catalog_id = str(catalog_pointer["catalog_generation_id"])
    catalog_manifest = CatalogManifestV2.from_mapping(
        json.loads(
            (tmp_path / "catalog" / "generations" / catalog_id / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
    )
    canonical_manifest_sha256 = canonical_json_sha256(catalog_manifest.to_dict())
    assert canonical_manifest_sha256 != catalog_pointer["manifest_sha256"]
    catalog_provenance = [
        SourceProvenance(
            source="catalog",
            run_id=catalog_id,
            catalog_generation_id=catalog_id,
            artifact_role=item.role,
            artifact_sha256=(
                "f" * 64 if alter_catalog_artifact and item.role == "champions" else item.sha256
            ),
            record_count=item.record_count,
            manifest_sha256=canonical_manifest_sha256,
            content_schema_version=item.content_schema_version,
        )
        for item in catalog_manifest.files
    ]
    catalog_provenance.append(
        SourceProvenance(
            source="hextech",
            run_id="hextech-origin",
            catalog_generation_id=catalog_id,
            artifact_role="stats",
            artifact_sha256="a" * 64,
            record_count=1,
            manifest_sha256="b" * 64,
            content_schema_version=2,
        )
    )
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    publisher.publish(
        {
            "champions": [{"id": "1", "name": "测试英雄"}],
            "champion_hextech": {
                "测试英雄": {"hero_id": "1", "augments": [{"id": "10", "name": "测试海克斯"}]}
            },
            "overlay_hints": {
                "hints": {"10": {"augment_id": "10", "name": "测试海克斯"}},
                "name_index": {"10": "10", "测试海克斯": "10"},
            },
            "identities": {
                "schema_version": 2,
                "champions": {"1": "测试英雄"},
                "augments": {"10": "测试海克斯"},
            },
        },
        source_files=tuple(catalog_provenance),
    )
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
    )

    baseline = coordinator._baseline_contributions(catalog_pointer)
    assert set(baseline) == (set() if alter_catalog_artifact else {"hextech"})


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


def test_failed_source_reuses_same_catalog_last_good_and_publishes_degraded(tmp_path) -> None:
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
    runner.fail_source = "apex"

    second = coordinator.refresh(force=True)

    assert first["state"] == "ready"
    assert second["state"] == "degraded"
    assert set(second["degraded_sources"]) == {"apex", "mayhem"}
    assert "hextech" in second["refreshed_sources"]
    status = DataSnapshotClient(tmp_path / "snapshots").status()
    assert status["health"] == "degraded"
    assert set(status["degraded_sources"]) == {"apex", "mayhem"}


def test_same_content_new_runs_publish_generation_with_matching_provenance(tmp_path) -> None:
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

    assert second["generation_id"] != first["generation_id"]
    hextech = json.loads((tmp_path / "sources" / "hextech" / "current.v2.json").read_text(encoding="utf-8"))
    assert hextech["run_id"] == "hextech-run-1"
    manifest = DataSnapshotClient(tmp_path / "snapshots").load_manifest()
    current_hextech = next(item for item in manifest.source_files if item.source == "hextech")
    assert current_hextech.run_id == "hextech-run-1"


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


def test_isolated_process_preserves_cancel_signal_published_before_spawn(tmp_path: Path) -> None:
    cancel_file = tmp_path / "worker.cancel"
    cancel_file.touch()

    result = run_isolated_process(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; raise SystemExit(0 if pathlib.Path(sys.argv[1]).exists() else 7)",
            str(cancel_file),
        ],
        timeout_seconds=3.0,
        cancel_file=cancel_file,
    )

    assert result.returncode == 0
    assert not cancel_file.exists()


def test_stop_before_active_cancel_registration_reaches_worker(tmp_path: Path) -> None:
    delegate = FakeWorkerRunner()
    observed = False

    def runner(command, **kwargs):
        nonlocal observed
        observed = Path(kwargs["cancel_file"]).is_file()
        return delegate(command, **kwargs)

    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
    )
    work = tmp_path / "work-before-registration"
    work.mkdir()
    coordinator.request_stop()

    coordinator._run_source("catalog", work, None, force=True)

    assert observed is True


def test_stop_after_worker_spawn_publishes_active_cancel(tmp_path: Path) -> None:
    delegate = FakeWorkerRunner()
    entered = threading.Event()
    saw_cancel = threading.Event()

    def runner(command, **kwargs):
        entered.set()
        cancel_file = Path(kwargs["cancel_file"])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if cancel_file.is_file():
                saw_cancel.set()
                break
            time.sleep(0.01)
        return delegate(command, **kwargs)

    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
    )
    work = tmp_path / "work-after-spawn"
    work.mkdir()
    errors: list[BaseException] = []

    def run_source() -> None:
        try:
            coordinator._run_source("catalog", work, None, force=True)
        except BaseException as exc:  # pragma: no cover - 断言线程异常可见
            errors.append(exc)

    thread = threading.Thread(target=run_source)
    thread.start()
    assert entered.wait(timeout=1.0)
    coordinator.request_stop()
    thread.join(timeout=3.0)

    assert not thread.is_alive()
    assert not errors
    assert saw_cancel.is_set()


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
    with pytest.raises(RuntimeError, match="current 无效"):
        coordinator._current_pointer("hextech")
    pointer["schema_version"] = 1
    atomic_write_json(tmp_path / "sources" / "hextech" / "current.v2.json", pointer)
    with pytest.raises(RuntimeError, match="current 无效"):
        coordinator._current_pointer("hextech")


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

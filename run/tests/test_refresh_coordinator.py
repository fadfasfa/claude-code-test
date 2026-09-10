"""验证刷新 singleflight、candidate promotion 与隔离 worker 的停止边界。"""

from __future__ import annotations

import json
import hashlib
import shutil
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil
import pytest

from hextech.bootstrap.data_service_runtime import DataBuildResult
from hextech.bootstrap.refresh_coordinator import CohortRefreshCoordinator, SOURCE_INTERVALS
from hextech.contracts import CatalogManifestV2, RefreshSourceState, SourceProvenance
from hextech.infrastructure.processes import IsolatedProcessResult, run_isolated_process
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.catalog.versioned import (
    CATALOG_FILES,
    build_catalog_manifest,
    canonical_json_sha256,
    sha256_file,
)


def test_production_game_probe_is_host_independent_and_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    from hextech.bootstrap.game_refresh_gate import probe_production_game_in_progress
    from hextech.modules.vision.gameflow import GameflowState

    monkeypatch.setattr(
        "hextech.modules.vision.gameflow.probe_gameflow_state",
        lambda: GameflowState.UNKNOWN,
    )
    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda _fields: [type("Proc", (), {"info": {"name": "League of Legends.exe"}})()],
    )
    monkeypatch.setattr("hextech.modules.vision.window.find_lol_game_window", lambda: None)
    assert probe_production_game_in_progress() is True

    monkeypatch.setattr(psutil, "process_iter", lambda _fields: [])
    monkeypatch.setattr(
        "hextech.modules.vision.gameflow.probe_gameflow_state",
        lambda: GameflowState.NOT_IN_PROGRESS,
    )
    assert probe_production_game_in_progress() is False


def test_apex_and_mayhem_share_72_hour_refresh_interval() -> None:
    assert SOURCE_INTERVALS["apex"].total_seconds() == 72 * 60 * 60
    assert SOURCE_INTERVALS["mayhem"].total_seconds() == 72 * 60 * 60


def test_core_scope_forces_only_core_and_keeps_due_optional_sources(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    runner = FakeWorkerRunner()
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: now,
    )
    coordinator.refresh(force=True, scope="core")
    runner.round = 1
    runner.calls.clear()
    runner.force_flags.clear()

    result = coordinator.refresh(force=True, scope="core")

    assert result["state"] == "ready"
    assert runner.calls == ["aramkit", "blitz"]
    assert runner.force_flags == [("aramkit", True), ("blitz", True)]

    schedule = coordinator.schedule_store.load()
    states = dict(schedule.sources)
    for source in ("apex", "mayhem"):
        states[source] = replace(states[source], next_due_at=(now - timedelta(seconds=1)).isoformat())
    coordinator.schedule_store.save(
        type(schedule)(
            updated_at=schedule.updated_at,
            generation_id=schedule.generation_id,
            sources=states,
        )
    )
    runner.round = 2
    runner.calls.clear()
    runner.force_flags.clear()

    coordinator.refresh(force=True, scope="core")

    assert runner.calls == ["aramkit", "blitz", "apex", "mayhem"]
    assert runner.force_flags == [
        ("aramkit", True),
        ("blitz", True),
        ("apex", False),
        ("mayhem", False),
    ]


def test_missing_source_pointer_still_honors_failure_backoff(tmp_path: Path) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
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


def test_expired_blitz_pointer_overrides_a_falsely_delayed_schedule(tmp_path: Path) -> None:
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=FakeWorkerRunner(),
        now=lambda: now,
    )
    pointer = {"last_success_at": (now - timedelta(hours=2, minutes=31)).isoformat()}
    state = RefreshSourceState(
        last_success_at=pointer["last_success_at"],
        next_due_at=(now + timedelta(hours=2)).isoformat(),
        state="ready",
    )

    assert coordinator._due("blitz", state, pointer, force=False) is True


def test_recent_successful_check_of_stale_pointer_obeys_fixed_cadence(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 1, 10, 47, tzinfo=timezone.utc)]
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=FakeWorkerRunner(),
        now=lambda: clock[0],
    )
    old_success = (clock[0] - timedelta(days=12)).isoformat()
    pointer = {
        "run_id": "mayhem-old-last-good",
        "last_success_at": old_success,
    }
    state = RefreshSourceState(
        last_attempt_at=clock[0].isoformat(),
        last_success_at=old_success,
        next_due_at=(clock[0] + timedelta(hours=72)).isoformat(),
        current_run_id=pointer["run_id"],
        state="ready",
    )

    assert coordinator._due("mayhem", state, pointer, force=False) is False
    clock[0] += timedelta(hours=72, seconds=1)
    assert coordinator._due("mayhem", state, pointer, force=False) is True


def test_source_failure_reason_code_is_preserved_in_schedule(tmp_path: Path) -> None:
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=FakeWorkerRunner(),
    )

    state = coordinator._failure_state(
        RefreshSourceState(),
        {
            "error_type": "SourceRefreshFailed",
            "reason_code": "schema_changed",
            "fallback_used": True,
            "last_good_available": True,
        },
    )

    assert state.state == "backoff"
    assert state.failure_kind == "schema_changed"


def test_refresh_checkpoint_keeps_only_bounded_pending_failure_evidence(tmp_path: Path) -> None:
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=FakeWorkerRunner(),
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    coordinator._save_refresh_checkpoint(
        cycle_id="cycle-fixture",
        created_at="2026-01-01T00:00:00+00:00",
        force=True,
        phase="core",
        state="in_progress",
        catalog={"catalog_generation_id": "catalog-fixture"},
        completed_sources={"aramkit": {"run_id": "aramkit-complete"}},
        pending_sources={"aramkit", "blitz", "apex"},
        failures={
            "aramkit": {"reason_code": "completed_must_disappear"},
            "blitz": {
                "reason_code": "catalog_binding_failed",
                "failure_stage": "validation",
                "error_type": "SourceRefreshFailed",
                "fallback_used": True,
                "last_good_available": True,
                "error": "完整异常文本不得进入 checkpoint",
                "diagnostics": {
                    "unknown_augment_count": 2,
                    "unknown_augment_ids": ["2141", "2157"],
                    "proxy_url": "https://proxy.invalid",
                    "api_token": "sensitive",
                    "command_line": ["worker", "--secret"],
                    "full_traceback": "sensitive stack",
                },
            },
            "mayhem": {"reason_code": "not_pending"},
        },
    )

    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["pending_sources"] == ["blitz", "apex"]
    assert checkpoint["failures"] == {
        "blitz": {
            "reason_code": "catalog_binding_failed",
            "failure_stage": "validation",
            "error_type": "SourceRefreshFailed",
            "fallback_used": True,
            "last_good_available": True,
            "diagnostics": {
                "unknown_augment_count": 2,
                "unknown_augment_ids": ["2141", "2157"],
            },
        }
    }


def _arg(command: list[str], name: str) -> Path:
    return Path(command[command.index(name) + 1])


class FakeWorkerRunner:
    def __init__(self, *, fail_source: str = "") -> None:
        self.fail_source = fail_source
        self.calls: list[str] = []
        self.commands: list[list[str]] = []
        self.force_flags: list[tuple[str, bool]] = []
        self.runtime_roots: list[Path] = []
        self.round = 0
        self.catalog_id = ""
        self.catalog_sha256 = ""
        self.catalog_manifest_sha256 = ""
        self.catalog_variant = ""
        # 按来源覆盖 pointer 成功时间；默认沿用固定日期，供数据时效用例控制 data_at。
        self.success_at_overrides: dict[str, str] = {}

    def __call__(self, command, **_kwargs) -> IsolatedProcessResult:
        source = command[command.index("--source") + 1]
        self.calls.append(source)
        self.commands.append(list(command))
        self.force_flags.append((source, "--force" in command))
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
            if self.catalog_variant:
                (catalog_root / "hero_version.txt").write_text(self.catalog_variant, encoding="utf-8")
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
            success_at = self.success_at_overrides.get(source, "2026-01-01T00:00:00+00:00")
            pointer = {
                "schema_version": 2,
                "catalog_generation_id": self.catalog_id,
                "content_sha256": self.catalog_sha256,
                "manifest_sha256": self.catalog_manifest_sha256,
                "completed_at": success_at,
                "last_success_at": success_at,
            }
        else:
            role = {"aramkit": "scoped_stats", "blitz": "augment_ranking", "apex": "synergy", "mayhem": "combos"}[source]
            filename = {"aramkit": "scoped_stats.json", "blitz": "augment_ranking.json", "apex": "synergy.json", "mayhem": "combos.json"}[source]
            catalog_pointer = json.loads(_arg(command, "--catalog-pointer").read_text(encoding="utf-8"))
            run_id = f"{source}-run-{self.round}"
            run_root = self.runtime_roots[-1] / "sources" / source / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=True)
            artifact_path = run_root / filename
            artifact_path.write_text(f"fixture-{source}\n", encoding="utf-8")
            manifest_path = run_root / "manifest.json"
            atomic_write_json(manifest_path, {"schema_version": 2, "source": source, "run_id": run_id})
            success_at = self.success_at_overrides.get(source, "2026-01-01T00:00:00+00:00")
            pointer = {
                "schema_version": 2,
                "source": source,
                "run_id": run_id,
                "catalog_generation_id": catalog_pointer["catalog_generation_id"],
                "catalog_sha256": catalog_pointer["content_sha256"],
                "manifest_sha256": sha256_file(manifest_path),
                "artifact": {
                    "role": role,
                    "relative_path": filename,
                    "sha256": sha256_file(artifact_path),
                    "record_count": 1,
                    "content_schema_version": 2,
                    "size": artifact_path.stat().st_size,
                },
                "completed_at": success_at,
                "last_success_at": success_at,
            }
        atomic_write_json(pointer_path, pointer)
        atomic_write_json(result_path, {"state": "ready", "source": source, "pointer": pointer})
        return IsolatedProcessResult(0, 0.01, False, "", "")


def _builder(root: Path) -> DataBuildResult:
    pointers = {
        source: json.loads((root / "sources" / source / "current.v2.json").read_text(encoding="utf-8"))
        for source in ("aramkit", "blitz", "apex", "mayhem")
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
    for source in ("aramkit", "blitz", "apex", "mayhem"):
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
            source="aramkit",
            run_id="aramkit-origin",
            catalog_generation_id=catalog_id,
            artifact_role="scoped_stats",
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
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
    )

    baseline = coordinator._baseline_contributions(catalog_pointer)
    assert set(baseline) == (set() if alter_catalog_artifact else {"aramkit"})


def test_cohort_promotes_only_after_all_candidates_succeed(tmp_path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    assert runner.calls == ["catalog", "aramkit", "blitz", "apex", "mayhem"]
    assert runner.runtime_roots == [tmp_path.resolve()] * 5
    assert DataSnapshotClient(tmp_path / "snapshots").status()["state"] == "ready"
    assert not coordinator.promotion.journal_path.exists()


def test_new_catalog_forces_all_dependent_sources_into_same_cohort(tmp_path) -> None:
    runner = FakeWorkerRunner()
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    result = coordinator.refresh()

    assert result["state"] == "ready"
    assert runner.force_flags == [
        ("catalog", False),
        ("aramkit", True),
        ("blitz", True),
        ("apex", True),
        ("mayhem", True),
    ]


def test_due_aramkit_with_changed_metadata_marker_refreshes_full_cohort(tmp_path) -> None:
    runner = FakeWorkerRunner()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: now,
        upstream_marker_probe=lambda: {
            "version": "16.15",
            "dataPath": "data/new",
            "buildTimeUnixMs": 2,
            "allMatches": 200,
        },
    )
    first = coordinator.refresh(force=True)
    runner.calls.clear()
    runner.force_flags.clear()
    coordinator._source_metadata = lambda source, _pointer: (
        {
            "marker": {
                "version": "16.15",
                "dataPath": "data/old",
                "buildTimeUnixMs": 1,
                "allMatches": 100,
            }
        }
        if source == "aramkit"
        else {}
    )
    schedule = coordinator.schedule_store.load()
    states = dict(schedule.sources)
    states["aramkit"] = replace(states["aramkit"], next_due_at=now.isoformat())
    coordinator.schedule_store.save(
        type(schedule)(
            updated_at=schedule.updated_at,
            generation_id=schedule.generation_id,
            sources=states,
        )
    )

    second = coordinator.refresh(force=False)

    assert first["state"] == "ready"
    assert second["state"] == "ready"
    assert runner.calls == ["catalog", "aramkit", "blitz", "apex", "mayhem"]
    assert runner.force_flags == [
        ("catalog", False),
        ("aramkit", True),
        ("blitz", True),
        ("apex", True),
        ("mayhem", True),
    ]


def test_failed_candidate_never_changes_formal_pointers(tmp_path) -> None:
    runner = FakeWorkerRunner(fail_source="apex")
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "failed"
    assert not (tmp_path / "catalog" / "current.v2.json").exists()
    assert not (tmp_path / "sources" / "aramkit" / "current.v2.json").exists()
    assert not (tmp_path / "snapshots" / "current.v2.json").exists()


def test_optional_failure_keeps_new_core_generation_and_checkpoint(tmp_path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
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
    assert second["reason_code"] == "optional_refresh_deferred"
    assert second["generation_id"] != first["generation_id"]
    assert second["core_generation_id"] == second["generation_id"]
    assert second["pending_sources"] == ["apex"]
    status = DataSnapshotClient(tmp_path / "snapshots").status(
        now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert status["health"] == "healthy"
    assert status["degraded_sources"] == []
    for source in ("apex", "mayhem"):
        assert status["source_status"][source]["freshness"] == "last_good"
        assert status["source_status"][source]["data_reason"] == "refresh_pending_last_good_preserved"
    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(encoding="utf-8")
    )
    assert checkpoint["state"] == "in_progress"
    assert checkpoint["refresh_phase"] == "optional"
    assert checkpoint["pending_sources"] == ["apex"]
    assert set(checkpoint["completed_sources"]) == {"catalog", "aramkit", "blitz", "mayhem"}
    assert checkpoint["failures"] == {
        "apex": {
            "error_type": "FixtureFailure",
            "fallback_used": True,
            "last_good_available": True,
        }
    }


def test_optional_checkpoint_resume_only_reruns_missing_source(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: clock[0],
    )
    assert coordinator.refresh(force=True)["state"] == "ready"
    runner.round = 1
    runner.fail_source = "apex"
    deferred = coordinator.refresh(force=True)
    calls_before_resume = list(runner.calls)
    runner.fail_source = ""

    waiting = coordinator.refresh()

    assert deferred["refresh_phase"] == "optional"
    assert runner.calls[len(calls_before_resume) :] == []
    assert waiting["state"] == "degraded"
    assert waiting["reason_code"] == "refresh_backoff_pending"
    assert waiting["pending_sources"] == ["apex"]
    schedule_path = tmp_path / "state" / "data-service" / "refresh_schedule.v1.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    original_next_due = schedule["sources"]["apex"]["next_due_at"]
    assert json.loads(schedule_path.read_text(encoding="utf-8"))["sources"]["apex"][
        "next_due_at"
    ] == original_next_due

    clock[0] = datetime.fromisoformat(original_next_due) + timedelta(seconds=1)
    resumed = coordinator.refresh()

    assert runner.calls[len(calls_before_resume) :] == ["apex"]
    assert resumed["state"] == "ready"
    assert resumed["resumed_from_checkpoint"] is True
    assert resumed["refresh_phase"] == "complete"
    assert resumed["pending_sources"] == []
    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(encoding="utf-8")
    )
    assert checkpoint["state"] == "complete"
    assert checkpoint["failures"] == {}


def test_completed_checkpoint_resumes_promotion_without_rerunning_workers(tmp_path: Path) -> None:
    """最后一个 worker 后崩溃时，重启必须提交 candidates 而不是误报 not_stale。"""

    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    current = {
        "catalog": json.loads(
            (tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8")
        ),
        **{
            source: json.loads(
                (tmp_path / "sources" / source / "current.v2.json").read_text(
                    encoding="utf-8"
                )
            )
            for source in ("aramkit", "blitz", "apex", "mayhem")
        },
    }
    runner.round = 1
    work = tmp_path / "snapshots" / "staging" / "refresh-crash-window"
    work.mkdir(parents=True)
    blitz_candidate, _result = coordinator._run_source(
        "blitz",
        work,
        tmp_path / "catalog" / "current.v2.json",
        force=True,
        coverage_policy="active_partial",
    )
    coordinator._save_candidate("blitz", blitz_candidate)
    completed = {source: dict(pointer) for source, pointer in current.items()}
    completed["blitz"] = blitz_candidate
    coordinator._save_refresh_checkpoint(
        cycle_id="crash-window",
        created_at="2026-01-01T00:00:00+00:00",
        force=False,
        phase="core",
        state="in_progress",
        catalog=current["catalog"],
        completed_sources=completed,
        pending_sources=set(),
        failures={},
    )
    calls_before_resume = list(runner.calls)

    resumed = coordinator.refresh(force=False)

    assert runner.calls == calls_before_resume
    assert resumed["state"] == "ready"
    assert resumed["resumed_from_checkpoint"] is True
    assert resumed["refresh_phase"] == "complete"
    assert resumed["generation_id"] != first["generation_id"]
    blitz_current = json.loads(
        (tmp_path / "sources" / "blitz" / "current.v2.json").read_text(encoding="utf-8")
    )
    assert blitz_current["run_id"] == blitz_candidate["run_id"]
    schedule = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_schedule.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert schedule["sources"]["blitz"]["state"] == "ready"
    assert schedule["sources"]["blitz"]["failure_kind"] == ""
    assert schedule["sources"]["blitz"]["current_run_id"] == blitz_candidate["run_id"]
    assert schedule["sources"]["blitz"]["last_success_at"] == blitz_candidate["last_success_at"]
    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint["state"] == "complete"
    assert checkpoint["pending_sources"] == []
    assert checkpoint["failures"] == {}


def test_invalid_checkpoint_catalog_hash_is_ignored(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert coordinator.refresh(force=True)["state"] == "ready"
    runner.round = 1
    runner.fail_source = "apex"
    assert coordinator.refresh(force=True)["refresh_phase"] == "optional"
    checkpoint_path = tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["catalog"]["content_sha256"] = "0" * 64
    atomic_write_json(checkpoint_path, checkpoint)
    calls_before = len(runner.calls)
    runner.fail_source = ""
    runner.round = 2

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    assert result["resumed_from_checkpoint"] is False
    assert runner.calls[calls_before:] == ["catalog", "aramkit", "blitz", "apex", "mayhem"]


def test_semantic_noop_returns_before_opening_promotion_journal(tmp_path, monkeypatch) -> None:
    from hextech.bootstrap import refresh_promotion
    from hextech.infrastructure.persistence.cohort_recovery import CohortCandidate

    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    targets = {
        "catalog": json.loads((tmp_path / "catalog/current.v2.json").read_text(encoding="utf-8")),
        **{
            source: json.loads(
                (tmp_path / "sources" / source / "current.v2.json").read_text(encoding="utf-8")
            )
            for source in ("aramkit", "blitz", "apex", "mayhem")
        },
    }
    current_manifest = publisher.current_generation_id()
    manifest = DataSnapshotClient(tmp_path / "snapshots").open_generation(current_manifest).manifest
    monkeypatch.setattr(
        refresh_promotion,
        "validate_generation_cohort",
        lambda _root, generation_id: CohortCandidate(
            generation_id=generation_id,
            generation_created_at=manifest.created_at,
            manifest_health=manifest.health,
            pointers=targets,
            production_pool_id="pool-test",
            production_pool_count=1,
        ),
    )
    monkeypatch.setattr(
        coordinator.promotion,
        "begin",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("journal must stay closed")),
    )

    manifest, retention = coordinator._promote_targets(
        targets,
        degraded_sources=set(),
        pending_sources=set(),
        refreshed_sources=["aramkit"],
    )

    assert manifest.generation_id == first["generation_id"]
    assert retention["promotion_disposition"] == "unchanged"
    assert not (tmp_path / "state/data-service/promotion_journal.v1.json").exists()


def test_refresh_promotion_allows_legacy_publisher_without_semantic_matcher() -> None:
    from hextech.bootstrap.refresh_promotion import _matching_current_manifest

    assert (
        _matching_current_manifest(
            object(),
            {},
            source_files=(),
            health="healthy",
            degraded_sources=[],
            source_status={},
        )
        is None
    )


def test_changed_catalog_never_publishes_mixed_generation_and_resumes(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    first_catalog = json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8"))
    runner.round = 1
    runner.catalog_variant = "fixture-catalog-v2"
    runner.fail_source = "apex"

    blocked = coordinator.refresh(force=True)

    assert blocked["state"] == "degraded"
    assert blocked["refresh_phase"] == "full_catalog_rebind"
    assert blocked["generation_id"] == first["generation_id"]
    assert json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8")) == first_catalog
    for source in ("aramkit", "blitz", "apex", "mayhem"):
        current_pointer = json.loads(
            (tmp_path / "sources" / source / "current.v2.json").read_text(encoding="utf-8")
        )
        assert current_pointer["catalog_generation_id"] == first_catalog["catalog_generation_id"]

    runner.fail_source = "blitz"
    calls_before_active_refresh = len(runner.calls)
    commands_before_active_refresh = len(runner.commands)
    active_refresh = coordinator.refresh(force=True)

    assert runner.calls[calls_before_active_refresh:] == ["aramkit", "blitz", "apex", "mayhem"]
    active_commands = runner.commands[commands_before_active_refresh:]
    for command in active_commands:
        source = command[command.index("--source") + 1]
        assert ("--catalog-compatibility-pointer" in command) is (source in {"aramkit", "blitz"})
        if source == "blitz":
            assert command[command.index("--coverage-policy") + 1] == "active_partial"
    assert active_refresh["state"] == "degraded"
    assert active_refresh["reason_code"] == "optional_source_stale"
    assert active_refresh["data_status"] == "fresh"
    assert active_refresh["generation_id"] != first["generation_id"]
    assert json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8")) == first_catalog
    active_view = DataSnapshotClient(tmp_path / "snapshots").open_view()
    assert {
        item.catalog_generation_id for item in active_view.manifest.source_files
    } == {first_catalog["catalog_generation_id"]}
    assert active_view.status()["source_status"]["blitz"]["freshness"] == "last_good"

    calls_before_resume = len(runner.calls)
    runner.fail_source = ""
    resumed = coordinator.refresh()

    assert runner.calls[calls_before_resume:] == []
    assert resumed["state"] == "ready"
    adoption = json.loads(
        (tmp_path / "state" / "data-service" / "catalog_adoption_checkpoint.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert adoption["state"] == "blocked"
    assert adoption["pending_sources"] == ["apex"]
    new_catalog = json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8"))
    assert new_catalog == first_catalog
    for source in ("aramkit", "blitz", "apex", "mayhem"):
        current_pointer = json.loads(
            (tmp_path / "sources" / source / "current.v2.json").read_text(encoding="utf-8")
        )
        assert current_pointer["catalog_generation_id"] == first_catalog["catalog_generation_id"]


def test_foreign_catalog_migration_marker_change_preserves_blitz_backoff(
    tmp_path: Path,
) -> None:
    runner = FakeWorkerRunner()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: now,
        upstream_marker_probe=lambda: {
            "version": "16.15",
            "dataPath": "data/new",
            "buildTimeUnixMs": 2,
            "allMatches": 200,
        },
    )
    assert coordinator.refresh(force=True)["state"] == "ready"
    runner.round = 1
    runner.catalog_variant = "fixture-catalog-v2"
    runner.fail_source = "blitz"
    blocked = coordinator.refresh(force=True)
    schedule_path = tmp_path / "state" / "data-service" / "refresh_schedule.v1.json"
    schedule_before = json.loads(schedule_path.read_text(encoding="utf-8"))
    blitz_due_before = schedule_before["sources"]["blitz"]["next_due_at"]
    calls_before_resume = len(runner.calls)
    runner.fail_source = ""
    coordinator._source_metadata = lambda source, _pointer: (
        {
            "marker": {
                "version": "16.15",
                "dataPath": "data/old",
                "buildTimeUnixMs": 1,
                "allMatches": 100,
            }
        }
        if source == "aramkit"
        else {}
    )

    resumed = coordinator.refresh(force=False)

    assert blocked["refresh_phase"] == "full_catalog_rebind"
    assert schedule_before["sources"]["blitz"]["state"] == "backoff"
    assert datetime.fromisoformat(blitz_due_before) > now
    assert runner.calls[calls_before_resume:] == ["aramkit", "apex", "mayhem"]
    assert resumed["state"] == "ready"
    schedule_after = json.loads(schedule_path.read_text(encoding="utf-8"))
    assert schedule_after["sources"]["blitz"]["state"] == "backoff"
    assert schedule_after["sources"]["blitz"]["next_due_at"] == blitz_due_before
    adoption = json.loads(
        (tmp_path / "state" / "data-service" / "catalog_adoption_checkpoint.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert adoption["state"] == "blocked"


def test_blitz_failure_promotes_fresh_aramkit_and_keeps_fallback_pending(tmp_path: Path) -> None:
    """真实 worker 失败时，Blitz fallback 不能把失败洗成 completed/healthy。"""

    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    runner.round = 1
    runner.fail_source = "blitz"

    result = coordinator.refresh(force=True)
    status = DataSnapshotClient(tmp_path / "snapshots").status(
        now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(
            encoding="utf-8"
        )
    )
    schedule = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_schedule.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert result["state"] == "degraded"
    assert result["reason_code"] == "optional_source_stale"
    assert result["data_status"] == "fresh"
    assert result["generation_id"] != first["generation_id"]
    assert result["degraded_sources"] == ["blitz"]
    assert "aramkit" in result["refreshed_sources"]
    assert "blitz" not in result["refreshed_sources"]
    assert status["health"] == "degraded"
    assert status["degraded_sources"] == ["blitz"]
    assert status["source_status"]["aramkit"]["data_status"] == "fresh"
    assert status["source_status"]["blitz"]["freshness"] == "last_good"
    assert status["source_status"]["blitz"]["data_status"] == "data_stale"
    assert checkpoint["state"] == "in_progress"
    assert checkpoint["pending_sources"] == ["blitz"]
    assert "blitz" not in checkpoint["completed_sources"]
    assert checkpoint["failures"] == {
        "blitz": {
            "error_type": "FixtureFailure",
            "fallback_used": True,
            "last_good_available": True,
        }
    }
    assert schedule["sources"]["blitz"]["state"] == "backoff"
    assert schedule["sources"]["blitz"]["failure_kind"]


def test_catalog_schedule_drift_is_reconciled_to_active_pointer(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    coordinator.refresh(force=True)
    current_catalog = json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8"))
    current_blitz = json.loads(
        (tmp_path / "sources" / "blitz" / "current.v2.json").read_text(encoding="utf-8")
    )
    schedule_path = tmp_path / "state" / "data-service" / "refresh_schedule.v1.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["sources"]["catalog"]["current_run_id"] = "catalog-nonexistent"
    schedule["sources"]["catalog"]["last_success_at"] = "2025-01-01T00:00:00+00:00"
    schedule["sources"]["blitz"]["current_run_id"] = "blitz-nonexistent"
    schedule["sources"]["blitz"]["last_success_at"] = ""
    schedule["sources"]["blitz"]["last_attempt_at"] = "2026-01-01T00:00:00+00:00"
    schedule["sources"]["blitz"]["next_due_at"] = "2026-01-01T06:00:00+00:00"
    schedule["sources"]["blitz"]["failure_kind"] = "production_coverage_insufficient"
    schedule["sources"]["blitz"]["state"] = "backoff"
    atomic_write_json(schedule_path, schedule)
    calls_before_reconcile = list(runner.calls)

    coordinator.refresh()

    repaired = json.loads(schedule_path.read_text(encoding="utf-8"))
    assert runner.calls == calls_before_reconcile
    assert repaired["sources"]["catalog"]["current_run_id"] == current_catalog["catalog_generation_id"]
    assert repaired["sources"]["catalog"]["last_success_at"] == current_catalog["last_success_at"]
    assert repaired["sources"]["blitz"]["current_run_id"] == current_blitz["run_id"]
    assert repaired["sources"]["blitz"]["last_success_at"] == current_blitz["last_success_at"]
    assert repaired["sources"]["blitz"]["last_attempt_at"] == "2026-01-01T00:00:00+00:00"
    assert repaired["sources"]["blitz"]["next_due_at"] == "2026-01-01T06:00:00+00:00"
    assert repaired["sources"]["blitz"]["failure_kind"] == "production_coverage_insufficient"
    assert repaired["sources"]["blitz"]["state"] == "backoff"

def test_catalog_refresh_failure_uses_verified_current_catalog_for_core(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    current_catalog = json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8"))
    runner.round = 1
    runner.fail_source = "catalog"

    result = coordinator.refresh(force=True)

    assert result["state"] == "degraded"
    assert result["generation_id"] != first["generation_id"]
    assert result["degraded_sources"] == ["catalog"]
    assert json.loads((tmp_path / "catalog" / "current.v2.json").read_text(encoding="utf-8")) == current_catalog
    assert json.loads((tmp_path / "sources" / "aramkit" / "current.v2.json").read_text(encoding="utf-8"))[
        "run_id"
    ] == "aramkit-run-1"


def test_expired_data_at_publishes_data_stale_without_degrading_cohort(tmp_path: Path) -> None:
    """回归：数据冻结超过 interval×1.25 时必须如实标注过期，此前恒报 fresh。"""

    runner = FakeWorkerRunner()
    # catalog/aramkit 数据保持新鲜，apex/mayhem 的 data_at 停在 96h 前（阈值 90h）。
    runner.success_at_overrides = {
        "catalog": "2026-01-05T00:00:00+00:00",
        "aramkit": "2026-01-05T00:00:00+00:00",
    }
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 5, tzinfo=timezone.utc),
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    status = DataSnapshotClient(tmp_path / "snapshots").status(
        now=datetime(2026, 1, 5, tzinfo=timezone.utc)
    )
    # 过期只写展示字段，不得污染 cohort 健康度与降级集合。
    assert status["health"] == "healthy"
    assert status["degraded_sources"] == []
    for source in ("apex", "mayhem"):
        source_status = status["source_status"][source]
        assert source_status["freshness"] == "fresh"
        assert source_status["data_status"] == "data_stale"
        assert source_status["data_reason"] == "source_data_expired"
        assert source_status["stale_age_seconds"] == 96 * 3600
    for source in ("catalog", "aramkit"):
        source_status = status["source_status"][source]
        assert source_status["data_status"] == "fresh"
        assert source_status["data_reason"] == ""
        assert source_status["stale_age_seconds"] == 0


def test_data_age_within_normal_cadence_stays_fresh(tmp_path: Path) -> None:
    """真机口径：apex 53h（< 72h×1.25=90h）属正常节奏，不得标过期。"""

    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 3, 5, tzinfo=timezone.utc),
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    status = DataSnapshotClient(tmp_path / "snapshots").status(
        now=datetime(2026, 1, 3, 5, tzinfo=timezone.utc)
    )
    apex_status = status["source_status"]["apex"]
    assert apex_status["data_status"] == "fresh"
    assert apex_status["stale_age_seconds"] == 0
    # aramkit 阈值 4h×1.25=5h，53h 已过期——同一时钟下正反两个方向都被覆盖。
    assert status["source_status"]["aramkit"]["data_status"] == "data_stale"


def test_saved_candidate_reuse_does_not_hide_expired_age(tmp_path: Path) -> None:
    """回归：失败被已保存候选"洗白"后 freshness 仍为 fresh，但过期必须依旧可见。"""

    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: clock["now"],
    )
    assert coordinator.refresh(force=True)["state"] == "ready"
    # 第二轮 mayhem 失败：apex 的 run-1 候选被保存，但 cohort 连坐回 last-good（run-0）。
    runner.round = 1
    runner.fail_source = "mayhem"
    assert coordinator.refresh(force=True)["state"] == "degraded"
    # 第三轮 120h 后 apex 失败：saved candidate run-1 与 current run-0 不同 → 洗白路径生效。
    clock["now"] = datetime(2026, 1, 6, tzinfo=timezone.utc)
    runner.round = 2
    runner.fail_source = "apex"
    runner.success_at_overrides = {
        "catalog": "2026-01-06T00:00:00+00:00",
        "aramkit": "2026-01-06T00:00:00+00:00",
        "mayhem": "2026-01-06T00:00:00+00:00",
    }

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    status = DataSnapshotClient(tmp_path / "snapshots").status(now=clock["now"])
    apex_status = status["source_status"]["apex"]
    # 洗白路径让 freshness 保持 fresh，但 data_at 停在 2026-01-01 → 120h 过期必须暴露。
    assert apex_status["freshness"] == "fresh"
    assert apex_status["data_status"] == "data_stale"
    assert apex_status["data_reason"] == "source_data_expired"
    assert apex_status["stale_age_seconds"] == 120 * 3600


def test_recovered_apex_promotes_saved_mayhem_candidate_as_fresh_cohort(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert coordinator.refresh(force=True)["state"] == "ready"

    # 第二轮 Apex 失败时，健康 Mayhem 只能保存在 candidate，正式 cohort 继续使用旧值。
    runner.round = 1
    runner.fail_source = "apex"
    assert coordinator.refresh(force=True)["state"] == "degraded"
    current_mayhem = json.loads(
        (tmp_path / "sources" / "mayhem" / "current.v2.json").read_text(encoding="utf-8")
    )
    assert current_mayhem["run_id"] == "mayhem-run-0"

    # Apex 恢复后，即使本轮 Mayhem 抓取失败，也应复用同 Catalog 的健康 candidate 原子晋升。
    runner.round = 2
    runner.fail_source = "mayhem"
    result = coordinator.refresh(force=True)
    status = DataSnapshotClient(tmp_path / "snapshots").status()
    current_apex = json.loads(
        (tmp_path / "sources" / "apex" / "current.v2.json").read_text(encoding="utf-8")
    )
    current_mayhem = json.loads(
        (tmp_path / "sources" / "mayhem" / "current.v2.json").read_text(encoding="utf-8")
    )

    assert result["state"] == "ready"
    assert current_apex["run_id"] == "apex-run-2"
    assert current_mayhem["run_id"] == "mayhem-run-1"
    assert status["source_status"]["apex"]["freshness"] == "fresh"
    assert status["source_status"]["mayhem"]["freshness"] == "fresh"


def test_rejected_aramkit_candidate_keeps_last_good_and_marks_data_stale(tmp_path: Path) -> None:
    """覆盖门禁等候选失败时，消费面必须能识别正在使用的 last-good。"""

    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    runner.round = 1
    runner.fail_source = "aramkit"

    second = coordinator.refresh(force=True)
    status = DataSnapshotClient(tmp_path / "snapshots").status()
    current_aramkit = json.loads((tmp_path / "sources" / "aramkit" / "current.v2.json").read_text(encoding="utf-8"))
    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(
            encoding="utf-8"
        )
    )
    schedule = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_schedule.v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert first["state"] == "ready"
    assert second["state"] == "degraded"
    assert second["reason_code"] == "aramkit_refresh_failed"
    assert second["generation_id"] == first["generation_id"]
    assert current_aramkit["run_id"] == "aramkit-run-0"
    assert status["generation_id"] == first["generation_id"]
    assert checkpoint["state"] == "in_progress"
    assert "aramkit" in checkpoint["pending_sources"]
    assert schedule["sources"]["aramkit"]["state"] == "backoff"
    assert schedule["sources"]["aramkit"]["failure_kind"]


def test_game_in_progress_defers_refresh_without_marking_data_stale(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    game = {"active": False}
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        game_state_probe=lambda: game["active"],
    )
    first = coordinator.refresh(force=True)
    calls_before = list(runner.calls)
    game["active"] = True

    deferred = coordinator.refresh(force=True)

    assert first["state"] == "ready"
    assert deferred == {
        "state": "ready",
            "refresh_state": "deferred",
            "refresh_scope": "due",
        "deferred_reason": "game_in_progress",
        "reason_code": "game_in_progress",
        "generation_id": first["generation_id"],
        "refresh_phase": "core",
        "core_generation_id": "",
        "pending_sources": [],
        "resumed_from_checkpoint": False,
    }
    assert "data_stale" not in json.dumps(deferred)
    assert runner.calls == calls_before


def test_game_in_progress_does_not_block_cold_start_without_snapshot(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    coordinator = CohortRefreshCoordinator(
        publisher=DataSnapshotPublisher(tmp_path / "snapshots"),
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        game_state_probe=lambda: True,
    )

    result = coordinator.refresh(force=True)

    assert result["state"] == "ready"
    assert result.get("refresh_state") != "deferred"
    assert runner.calls == ["catalog", "aramkit", "blitz", "apex", "mayhem"]


def test_active_worker_cancelled_by_game_is_deferred_without_backoff(tmp_path: Path) -> None:
    initial_runner = FakeWorkerRunner()
    game = {"active": False}
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=initial_runner,
        game_state_probe=lambda: game["active"],
    )
    coordinator.refresh(force=True)
    entered = threading.Event()
    saw_cancel = threading.Event()

    def blocking_runner(_command, **kwargs):
        entered.set()
        cancel_file = Path(kwargs["cancel_file"])
        if cancel_file.exists() or cancel_file.parent.exists():
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not cancel_file.exists():
                time.sleep(0.01)
        if cancel_file.exists():
            saw_cancel.set()
        return IsolatedProcessResult(2, 0.01, False, "", "cancelled")

    coordinator.process_runner = blocking_runner
    result_holder: dict[str, object] = {}
    thread = threading.Thread(target=lambda: result_holder.update(coordinator.refresh(force=True)))
    thread.start()
    assert entered.wait(2.0)
    game["active"] = True
    coordinator.poll_deferred_refresh()
    thread.join(3.0)

    assert saw_cancel.is_set()
    assert result_holder["refresh_state"] == "deferred"
    schedule = coordinator.schedule_store.load()
    assert all(state.state == "ready" for state in schedule.sources.values())


def test_optional_worker_cancel_keeps_core_and_resumes_from_checkpoint(tmp_path: Path) -> None:
    delegate = FakeWorkerRunner()
    game = {"active": False}
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=delegate,
        game_state_probe=lambda: game["active"],
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    delegate.round = 1
    entered_optional = threading.Event()
    saw_cancel = threading.Event()

    def blocking_optional(command, **kwargs):
        source = command[command.index("--source") + 1]
        if source != "apex":
            return delegate(command, **kwargs)
        delegate.calls.append(source)
        entered_optional.set()
        cancel_file = Path(kwargs["cancel_file"])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not cancel_file.exists():
            time.sleep(0.01)
        if cancel_file.exists():
            saw_cancel.set()
        return IsolatedProcessResult(2, 0.01, False, "", "cancelled")

    coordinator.process_runner = blocking_optional
    result_holder: dict[str, object] = {}
    thread = threading.Thread(target=lambda: result_holder.update(coordinator.refresh(force=True)))
    thread.start()
    assert entered_optional.wait(3.0)
    game["active"] = True
    coordinator.poll_deferred_refresh()
    thread.join(4.0)

    assert saw_cancel.is_set()
    assert result_holder["refresh_phase"] == "optional"
    assert result_holder["generation_id"] != first["generation_id"]
    core_generation_id = str(result_holder["core_generation_id"])
    assert core_generation_id == result_holder["generation_id"]
    checkpoint = json.loads(
        (tmp_path / "state" / "data-service" / "refresh_checkpoint.v1.json").read_text(encoding="utf-8")
    )
    assert checkpoint["core_generation_id"] == core_generation_id
    assert checkpoint["pending_sources"] == ["apex", "mayhem"]

    calls_before_resume = len(delegate.calls)
    coordinator.process_runner = delegate
    game["active"] = False
    resumed = coordinator.refresh()

    assert delegate.calls[calls_before_resume:] == ["apex", "mayhem"]
    assert resumed["state"] == "ready"
    assert resumed["resumed_from_checkpoint"] is True
    assert resumed["refresh_phase"] == "complete"


def test_deferred_refresh_resumes_once_thirty_seconds_after_game(tmp_path: Path) -> None:
    runner = FakeWorkerRunner()
    game = {"active": False}
    clock = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        game_state_probe=lambda: game["active"],
        now=lambda: clock["now"],
    )
    coordinator.refresh(force=True)
    game["active"] = True
    coordinator.refresh(force=True, scope="core")
    assert coordinator.poll_deferred_refresh() is None

    game["active"] = False
    assert coordinator.poll_deferred_refresh() is None
    clock["now"] += timedelta(seconds=29)
    assert coordinator.poll_deferred_refresh() is None
    clock["now"] += timedelta(seconds=2)
    assert coordinator.poll_deferred_refresh() == {"force": True, "scope": "core"}
    assert coordinator.poll_deferred_refresh() is None


def test_same_content_new_runs_publish_generation_with_matching_provenance(tmp_path) -> None:
    runner = FakeWorkerRunner()
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    coordinator = CohortRefreshCoordinator(
        publisher=publisher,
        builder=lambda _targets: _builder(tmp_path),
        root=tmp_path,
        process_runner=runner,
        now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    first = coordinator.refresh(force=True)
    runner.round = 1

    second = coordinator.refresh(force=True)

    assert second["generation_id"] != first["generation_id"]
    aramkit = json.loads((tmp_path / "sources" / "aramkit" / "current.v2.json").read_text(encoding="utf-8"))
    assert aramkit["run_id"] == "aramkit-run-1"
    manifest = DataSnapshotClient(tmp_path / "snapshots").load_manifest()
    current_aramkit = next(item for item in manifest.source_files if item.source == "aramkit")
    assert current_aramkit.run_id == "aramkit-run-1"


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


def test_isolated_process_observes_runtime_cancel_and_reaps_uncooperative_worker(tmp_path: Path) -> None:
    cancel_file = tmp_path / "worker.cancel"
    result_holder: dict[str, IsolatedProcessResult] = {}

    thread = threading.Thread(
        target=lambda: result_holder.update(
            result=run_isolated_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=20.0,
                cancel_grace_seconds=0.1,
                cancel_file=cancel_file,
            )
        )
    )
    started = time.monotonic()
    thread.start()
    time.sleep(0.15)
    cancel_file.write_text("game_in_progress", encoding="utf-8")
    thread.join(3.0)

    assert not thread.is_alive()
    assert time.monotonic() - started < 3.0
    assert result_holder["result"].cancelled is True
    assert result_holder["result"].cancel_reason == "game_in_progress"
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
        builder=lambda _targets: _builder(tmp_path),
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
        builder=lambda _targets: _builder(tmp_path),
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
        builder=lambda _targets: _builder(tmp_path),
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

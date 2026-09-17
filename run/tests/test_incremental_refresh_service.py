import json
import itertools
from datetime import datetime
from pathlib import Path
import threading
import time
from threading import Event
from types import SimpleNamespace

import pytest

from hextech.infrastructure.sources.refresh_service import IncrementalRefreshService
from hextech.infrastructure.sources.aramkit.catalog_binding import CatalogBinding
from hextech.infrastructure.sources.aramkit.schema import (
    normalize_detail,
    normalize_rankings,
    resolve_version,
    version_marker,
)
from hextech.infrastructure.sources.aramkit.units import publish_unit
from hextech.infrastructure.processes import IsolatedProcessResult
from hextech.interfaces.desktop.app_shared import format_data_refresh_status
from hextech.modules.data import source_runs
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from test_cohort_seed import _write_catalog
from test_aramkit_source import _ranking, _detail, _version
import hextech.infrastructure.sources.refresh_service as refresh_module
import hextech.infrastructure.sources.refresh_service_lifecycle as refresh_lifecycle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(source_runs, "var_path", lambda *parts: root.joinpath(*parts))
    _, catalog, manifest = _write_catalog(root)
    atomic_write_json(root / "catalog" / "current.v2.json", catalog)
    binding = CatalogBinding(manifest.catalog_generation_id, manifest.content_sha256,
                             frozenset({"1"}), frozenset({1001}))
    return root, catalog, binding


def test_missing_catalog_capabilities_retry_without_claiming_source_failure(runtime):
    from datetime import datetime

    root, _, _ = runtime
    service = IncrementalRefreshService(publisher=DataSnapshotPublisher(root / "snapshots"), root=root,
        game_state_probe=lambda: False, champion_probe=lambda: "")
    service._catalog_capability_pending = 2
    service._mark_source("catalog")
    state = service.schedule_store.load().sources["catalog"]
    assert state.state == "ready" and not state.failure_kind
    assert (datetime.fromisoformat(state.next_due_at)-datetime.fromisoformat(state.last_attempt_at)).total_seconds() == 300


def test_real_unit_to_v3_publish_without_optional(runtime):
    root, catalog, binding = runtime
    publisher = DataSnapshotPublisher(root / "snapshots")
    stages = []
    core_ready_copy = []
    calls = []
    version = resolve_version(_version())
    rows = normalize_rankings({"rows": [_ranking("1")]})

    def process(command, *, observe, **kwargs):
        source = command[command.index("--source") + 1]
        calls.append(source)
        output = command[command.index("--result-output") + 1]
        if source == "catalog":
            pointer = catalog
            source_result = {
                "check_status": "up_to_date",
                "upstream_revision": catalog["catalog_generation_id"],
                "applied_revision": catalog["catalog_generation_id"],
            }
        elif source == "aramkit":
            from pathlib import Path
            units = Path(output).parent / "units"
            publish_unit(version, rows, binding=binding, pointer_output=units / "ranking.pointer.json")
            observe()
            view = DataSnapshotClient(publisher.root).open_view()
            stages.append(view.is_champion_complete("1"))
            pointer = publish_unit(version, normalize_detail(_detail(_ranking("1"), 1001), rows[0]),
                                   binding=binding, champion_id="1", pointer_output=units / "hero-1.pointer.json")
            time.sleep(0.21)
            observe()
            core_ready_copy.append(format_data_refresh_status(service.progress())[0])
            source_result = {
                "check_status": "changed",
                "upstream_revision": version["dataPath"],
                "applied_revision": version["dataPath"],
            }
        else:
            raise RuntimeError("optional_unavailable")
        from pathlib import Path
        atomic_write_json(
            Path(output),
            {"state": "ready", "pointer": pointer, "source_result": source_result},
        )
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = IncrementalRefreshService(publisher=publisher, root=root, game_state_probe=lambda: False,
                                        champion_probe=lambda: "1", process_runner=process)
    result = service.refresh(force=True, scope="core")
    service.wait_optional()
    view = DataSnapshotClient(publisher.root).open_view()
    assert view.manifest.schema_version == 3
    assert stages == [False]
    assert core_ready_copy == ["核心数据已就绪，后台补齐其他英雄"]
    assert view.is_champion_complete("1")
    assert result["generation_id"] == view.manifest.generation_id
    assert result["checked"] is True
    assert result["content_changed"] is True
    assert result["catalog_changed"] is False
    state = service.schedule_store.load().sources["aramkit"]
    assert state.check_status == "up_to_date"
    assert state.upstream_revision == version["dataPath"]
    assert result["source_outcomes"]["aramkit"]["check_status"] == "up_to_date"
    assert view.manifest.components["ranking"]["source_version"] == version["dataPath"]
    progress = service.progress()
    assert progress["core_published_at"] > 0
    assert progress["source_outcomes"]["apex"]["state"] == "unavailable"
    assert progress["source_outcomes"]["apex"]["reason_code"] == "optional_unavailable"
    assert progress["source_outcomes"]["mayhem"]["state"] == "unavailable"
    assert progress["optional_sources"]["apex"]["checked"] is False
    assert format_data_refresh_status(progress)[0].startswith("数据已更新")
    assert not (root / "state" / "data-service" / "promotion_journal.v1.json").exists()
    assert set(calls) == {"catalog", "aramkit", "blitz", "apex", "mayhem"}


def test_game_context_updates_never_write_cancel(runtime):
    root, _, _ = runtime
    service = IncrementalRefreshService(publisher=DataSnapshotPublisher(root / "snapshots"), root=root,
                                        game_state_probe=lambda: True, champion_probe=lambda: "1")
    cancel = root / "test.cancel"
    service._cancels.add(cancel)
    service.poll_context()
    assert not cancel.exists()
    state = json.loads((root / "state" / "data-service" / "download_context.v1.json").read_text())
    assert state["in_game"] is True and state["champion_id"] == "1"
    service.request_stop()
    assert cancel.read_text() == "shutdown_requested"


def _service(runtime, monkeypatch, process):
    root, _, _ = runtime
    ticks = itertools.count(1)
    monkeypatch.setattr(refresh_module, "time", SimpleNamespace(time=time.time, monotonic=lambda: next(ticks)))
    return IncrementalRefreshService(publisher=DataSnapshotPublisher(root / "snapshots"), root=root,
                                     game_state_probe=lambda: True, champion_probe=lambda: "1", process_runner=process)


def _units(binding, directory):
    version = resolve_version(_version())
    rows = normalize_rankings({"rows": [_ranking("1")]})
    ranking = publish_unit(version, rows, binding=binding, pointer_output=directory / "ranking.pointer.json")
    hero = publish_unit(version, normalize_detail(_detail(_ranking("1"), 1001), rows[0]),
                        binding=binding, champion_id="1")
    return ranking, hero


def test_bad_hero_publish_rolls_back_memory_then_replacement_is_accepted(runtime, monkeypatch):
    root, _, binding = runtime
    accepted = {}

    def process(command, *, observe, **kwargs):
        output = Path(command[command.index("--result-output") + 1])
        directory = output.parent / "units"
        ranking, hero = _units(binding, directory)
        observe()
        baseline = DataSnapshotClient(root / "snapshots").open_view()
        assert not baseline.is_champion_complete("1")
        atomic_write_json(directory / "hero-1.pointer.json", {**hero, "manifest_sha256": "f" * 64})
        observe()
        assert service._heroes == {}
        assert DataSnapshotClient(root / "snapshots").load_manifest().generation_id == baseline.manifest.generation_id
        assert not (root / "state/data-service/promotion_journal.v1.json").exists()
        atomic_write_json(directory / "hero-1.pointer.json", hero)
        accepted.update(hero)
        atomic_write_json(output, {"state": "ready", "pointer": ranking})
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    service._run("aramkit", root / "work")
    assert service._heroes == {"1": accepted}
    assert DataSnapshotClient(root / "snapshots").open_view().is_champion_complete("1")


def test_transient_publish_failure_same_unit_retry_does_not_fail_worker(runtime, monkeypatch):
    root, _, binding = runtime

    def process(command, *, observe, **kwargs):
        output = Path(command[command.index("--result-output") + 1])
        ranking, _ = _units(binding, output.parent / "units")
        observe()
        assert service._ranking == {}
        assert not (root / "snapshots/current.v2.json").exists()
        assert not (root / "state/data-service/promotion_journal.v1.json").exists()
        atomic_write_json(output, {"state": "ready", "pointer": ranking})
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    original = service.publisher.publish
    attempts = []

    def fail_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("injected transient publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.publisher, "publish", fail_once)
    pointer = service._run("aramkit", root / "work")
    assert len(attempts) == 2
    view = DataSnapshotClient(root / "snapshots").open_view()
    assert view.manifest.components["ranking"]["run_id"] == pointer["run_id"]
    assert service._ranking == pointer


def test_optional_lane_starts_while_core_worker_remains_active(runtime, monkeypatch):
    root, _, binding = runtime
    core_active, optional_active, observed_overlap = Event(), Event(), Event()

    def process(command, *, observe, **kwargs):
        source = command[command.index("--source") + 1]
        if source == "apex":
            optional_active.set()
            if core_active.wait(2):
                observed_overlap.set()
            raise RuntimeError("optional_unavailable")
        if source != "aramkit":
            raise RuntimeError("optional_unavailable")
        core_active.set()
        assert optional_active.wait(2)
        assert observed_overlap.wait(2)
        output = Path(command[command.index("--result-output") + 1])
        ranking, _ = _units(binding, output.parent / "units")
        observe()
        atomic_write_json(output, {"state": "ready", "pointer": ranking})
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    result = service.refresh(force=True)
    service.wait_optional()
    assert observed_overlap.is_set()
    assert result["generation_id"] == DataSnapshotClient(root / "snapshots").load_manifest().generation_id


def test_cold_ingame_without_catalog_is_failed_not_completed(tmp_path):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(args)
        return IsolatedProcessResult(1, 0.1, False, "", "")
    service = IncrementalRefreshService(publisher=DataSnapshotPublisher(tmp_path / "snapshots"), root=tmp_path,
                                       game_state_probe=lambda: True, champion_probe=lambda: "1",
                                       process_runner=unavailable)
    result = service.refresh()
    assert result["state"] == "failed" and result["reason_code"] == "catalog_unavailable"
    assert service.progress()["state"] != "completed"
    assert len(calls) == 1  # Recognition checks continue during a game; failure preserves no-data.
    assert not (tmp_path / "snapshots/current.v2.json").exists()


def test_stop_during_worker_handoff_cannot_start_new_process(runtime, monkeypatch):
    root, _, _ = runtime
    calls = []

    def process(*args, **kwargs):
        calls.append(args)
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)

    class StopOnRegistration(set):
        def add(self, path):
            super().add(path)
            service.request_stop()

    service._cancels = StopOnRegistration()
    with pytest.raises(RuntimeError, match="shutdown_requested"):
        service._run("aramkit", root / "work")
    assert calls == []
    assert not service._cancels


def test_unchanged_publish_preserves_previous_and_recovery_previous(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking = ranking
    first = service._publish()
    service._heroes = {"1": hero}
    second = service._publish()
    previous_path = root / "snapshots/previous.v2.json"
    before = previous_path.read_bytes()
    assert json.loads(before)["generation_id"] == first
    assert first != second
    assert service._publish() == second
    assert previous_path.read_bytes() == before
    point = json.loads((root / "state/data-service/cohort_recovery_point.v1.json").read_text())
    assert point["pointers"]["generation"]["previous"]["generation_id"] == first
    assert DataSnapshotClient(root / "snapshots").open_view().is_champion_complete("1")


def test_postcommit_retention_waits_for_core_optional_and_process_workers(runtime, monkeypatch):
    root, _, _ = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    calls = []

    def retention(_root, *, workers_idle):
        calls.append(workers_idle)
        return {
            "disposition": "completed" if workers_idle else "skipped_workers_active",
            "reason": "",
        }

    monkeypatch.setattr(refresh_lifecycle, "apply_cohort_retention", retention)
    service._retention_pending = True
    service._refresh_active = True
    service._try_retention()
    service._refresh_active = False
    service._cancels.add(root / "active.cancel")
    service._try_retention()
    service._cancels.clear()
    service._optional_active = True
    service._optional_thread = SimpleNamespace(is_alive=lambda: True)
    service._try_retention()
    assert service._retention_pending is True

    service._optional_active = False
    service._optional_thread = None
    service._try_retention()

    assert calls == [False, False, False, True]
    assert service._retention_pending is False


def test_retention_failure_does_not_undo_published_generation(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, _ = _units(binding, root / "units")
    service._ranking = ranking
    generation_id = service._publish()
    monkeypatch.setattr(
        refresh_lifecycle,
        "apply_cohort_retention",
        lambda *_args, **_kwargs: {"disposition": "failed", "reason": "disk busy"},
    )

    service._try_retention()

    assert service.publisher.current_generation_id() == generation_id
    assert service._retention_pending is True
    assert service._last_retention_result == {"disposition": "failed", "reason": "disk busy"}


def test_unexpected_retention_exception_cannot_fail_committed_refresh(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, _ = _units(binding, root / "units")
    service._ranking = ranking
    generation_id = service._publish()
    monkeypatch.setattr(
        refresh_lifecycle,
        "apply_cohort_retention",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )

    service._try_retention()

    assert service.publisher.current_generation_id() == generation_id
    assert service._retention_pending is True
    assert service._last_retention_result == {
        "disposition": "failed",
        "reason": "RuntimeError:unexpected",
    }


def test_optional_worker_remains_active_until_all_optional_sources_finish(runtime, monkeypatch):
    root, _, _ = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(
        refresh_lifecycle,
        "apply_cohort_retention",
        lambda _root, *, workers_idle: calls.append(workers_idle) or {
            "disposition": "completed" if workers_idle else "skipped_workers_active",
            "reason": "",
        },
    )
    service._retention_pending = True
    service._optional_active = True
    service._optional_thread = None
    service._refresh_optional_sources = service._try_retention

    refresh_lifecycle.run_optional_refresh(service)

    assert calls == [False, True]
    assert service._retention_pending is False


def test_new_generation_identity_alone_does_not_claim_business_content_change(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    service._publish()
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_due", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(service.publisher, "current_generation_id", lambda: "generation-new-run-same-content")

    result = service.refresh()

    assert result["generation_id"] == "generation-new-run-same-content"
    assert result["checked"] is False
    assert result["content_changed"] is False
    assert result["catalog_changed"] is False
    assert result["reason_code"] == "no_content_change"


def test_background_units_are_batched_but_current_hero_publishes_before_worker_exit(runtime, monkeypatch):
    from types import SimpleNamespace

    root, _, binding = runtime
    published = []

    def process(command, *, observe, **kwargs):
        output = Path(command[command.index("--result-output") + 1])
        directory = output.parent / "units"
        ranking, hero = _units(binding, directory)
        observe()
        assert published == [set()]
        atomic_write_json(directory / "hero-1.pointer.json", hero)
        observe()
        assert published == [set(), {"1"}]
        # Flow-only fixture: integrity is exercised by the real-unit tests above.
        atomic_write_json(directory / "hero-2.pointer.json", hero)
        observe()
        atomic_write_json(directory / "hero-3.pointer.json", hero)
        observe()
        assert len(published) == 2
        atomic_write_json(output, {"state": "ready", "pointer": ranking})
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    service._projection = SimpleNamespace(
        validate_champion=lambda *args: None,
        ranked_champion_ids=lambda _pointer: frozenset({"1", "2", "3"}),
    )
    monkeypatch.setattr(service, "_publish", lambda: published.append(set(service._heroes)) or "generation")
    service._run("aramkit", root / "work")
    assert published == [set(), {"1"}, {"1", "2", "3"}]


def test_new_ranking_atomically_drops_heroes_outside_projected_cohort(runtime, monkeypatch):
    root, _, binding = runtime
    ranking, hero = _units(binding, root / "old-units")
    published = []

    def process(command, *, observe, **kwargs):
        output = Path(command[command.index("--result-output") + 1])
        directory = output.parent / "units"
        new_ranking, _ = _units(binding, directory)
        observe()
        atomic_write_json(output, {"state": "ready", "pointer": new_ranking})
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    service._ranking = ranking
    service._heroes = {"1": hero, "2": hero}
    service._projection = SimpleNamespace(
        ranked_champion_ids=lambda _pointer: frozenset({"1"}),
    )
    monkeypatch.setattr(
        service,
        "_publish",
        lambda: published.append(set(service._heroes)) or "generation",
    )

    service._run("aramkit", root / "work")

    assert published == [{"1"}]
    assert service._heroes == {"1": hero}


def test_ranking_cohort_reduction_rolls_back_heroes_when_publish_fails(runtime, monkeypatch):
    root, _, binding = runtime
    old_ranking, hero = _units(binding, root / "old-units")

    def process(command, *, observe, **kwargs):
        output = Path(command[command.index("--result-output") + 1])
        _units(binding, output.parent / "units")
        observe()
        atomic_write_json(output, {"state": "ready", "pointer": old_ranking})
        return IsolatedProcessResult(0, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    service._ranking = old_ranking
    service._heroes = {"1": hero, "2": hero}
    service._projection = SimpleNamespace(
        ranked_champion_ids=lambda _pointer: frozenset({"1"}),
    )
    monkeypatch.setattr(
        service,
        "_publish",
        lambda: (_ for _ in ()).throw(ValueError("projection rejected")),
    )

    with pytest.raises(RuntimeError, match="incremental_publish_failed"):
        service._run("aramkit", root / "work")

    assert service._ranking == old_ranking
    assert service._heroes == {"1": hero, "2": hero}


def test_missing_optional_is_due_even_if_old_catalog_schedule_is_ready(runtime):
    from dataclasses import replace
    from hextech.contracts import RefreshSourceState

    root, _, _ = runtime
    service = IncrementalRefreshService(publisher=DataSnapshotPublisher(root / "snapshots"), root=root,
                                       game_state_probe=lambda: False, champion_probe=lambda: "1")
    schedule = service.schedule_store.load()
    states = dict(schedule.sources)
    states["apex"] = RefreshSourceState(state="ready", current_run_id="old-catalog-run",
                                        next_due_at="2999-01-01T00:00:00+00:00")
    service.schedule_store.save(replace(schedule, sources=states))
    assert service._due("apex")
    states["apex"] = replace(states["apex"], state="backoff")
    service.schedule_store.save(replace(schedule, sources=states))
    assert not service._due("apex")


def test_core_checks_catalog_and_marker_despite_seed_future_due(runtime, monkeypatch):
    from dataclasses import replace
    from hextech.contracts import RefreshSourceState

    root, catalog, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    generation = service._publish()
    schedule = service.schedule_store.load()
    service.schedule_store.save(replace(schedule, sources={source: RefreshSourceState(
        state="ready", next_due_at="2999-01-01T00:00:00+00:00") for source in schedule.sources}))
    calls, markers = [], []
    service.marker_probe = lambda: markers.append(1) or version_marker(resolve_version(_version()))
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    def run(source, work, **kwargs):
        calls.append(source)
        if source == "catalog":
            return catalog
        raise RuntimeError("optional_unavailable")
    monkeypatch.setattr(service, "_run", run)
    result = service.refresh(scope="core")
    assert calls == ["catalog", "blitz"] and markers == [1]
    assert result["promotion_disposition"] == "unchanged"
    assert result["checked"] is True
    assert result["content_changed"] is False
    assert result["catalog_changed"] is False
    assert result["source_outcomes"]["aramkit"]["state"] == "unchanged"
    assert result["source_outcomes"]["blitz"]["state"] == "unavailable"
    assert result["source_outcomes"]["blitz"]["failure_kind"] == "source_runtime"
    assert result["source_outcomes"]["blitz"]["next_retry_at"]
    assert result["generation_id"] == generation
    progress = service.progress()
    assert progress["state"] == "unchanged"
    assert progress["checked_at"] > 0
    from datetime import datetime, timezone
    expected = datetime.fromtimestamp(resolve_version(_version())["buildTimeUnixMs"] / 1000, timezone.utc).isoformat()
    assert progress["data_at"] == expected
    assert progress["data_at"] != ranking["completed_at"]
    assert progress["reason_code"] == "core_complete_optional_failed"
    aram_state = service.schedule_store.load().sources["aramkit"]
    assert aram_state.upstream_revision == resolve_version(_version())["dataPath"]
    assert aram_state.applied_revision == resolve_version(_version())["dataPath"]
    assert aram_state.last_checked_at
    assert format_data_refresh_status(progress)[0] == (
        "已检来源与上游一致 · Blitz 暂不可用 · 部分源待重试"
    )


def test_ingame_catalog_publication_keeps_statistics_binding(runtime, monkeypatch):
    root, catalog, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    generation = service._publish()
    import shutil
    from dataclasses import replace
    from hextech.contracts import CatalogManifestV2
    from hextech.modules.data.catalog.versioned import sha256_file
    old_root = root / "catalog/generations" / str(catalog["catalog_generation_id"])
    new_root = root / "catalog/generations/new-recognition"
    shutil.copytree(old_root, new_root)
    manifest = CatalogManifestV2.from_mapping(json.loads((new_root / "manifest.json").read_text(encoding="utf-8")))
    atomic_write_json(new_root / "manifest.json", replace(manifest, catalog_generation_id="new-recognition").to_dict())
    new_catalog = {**catalog, "catalog_generation_id": "new-recognition", "manifest_sha256": sha256_file(new_root / "manifest.json")}
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_due", lambda source, force=False: source == "catalog")
    monkeypatch.setattr(service, "_run", lambda source, work, **kwargs: new_catalog)
    result = service.refresh()
    assert json.loads((root / "catalog/current.v2.json").read_text()) == new_catalog
    assert service._catalog == catalog
    assert service._ranking == ranking and service._heroes == {"1": hero}
    assert service.publisher.current_generation_id() == generation
    assert result["checked"] is True
    assert result["content_changed"] is False
    assert result["catalog_changed"] is True
    assert result["reason_code"] == "catalog_complete"
    assert result["source_outcomes"]["catalog"]["state"] == "updated"


def test_marker_failure_is_core_failure_not_unchanged(runtime, monkeypatch):
    root, catalog, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    service._publish()
    def bad_marker():
        raise OSError("discovery unavailable")
    service.marker_probe = bad_marker
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_run", lambda source, work, **kwargs: catalog if source == "catalog" else (_ for _ in ()).throw(RuntimeError("optional")))
    result = service.refresh(scope="core")
    assert result["failures"]["aramkit"] == "discovery unavailable"
    assert result["checked"] is False
    assert result["source_outcomes"]["aramkit"]["state"] == "last_good"
    assert result["source_outcomes"]["aramkit"]["availability"] == "available"
    assert result["source_outcomes"]["aramkit"]["used_last_good"] is True
    assert result["source_outcomes"]["aramkit"]["failure_kind"] == "transient_network"
    assert result["source_outcomes"]["aramkit"]["next_retry_at"]
    assert service.progress()["state"] == "failed"
    assert format_data_refresh_status(service.progress())[0] == "检测失败，继续使用已验证数据"


def test_probe_retry_after_reaches_persisted_schedule(runtime, monkeypatch):
    from hextech.infrastructure.sources.aramkit.catalog_binding import AramkitRefreshError
    from hextech.infrastructure.sources.aramkit.http_response import _Response

    root, catalog, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    service._publish()
    response = _Response(
        "https://data.aramkit.com/data/versions.json",
        b"",
        429,
        "http_429",
        "http_429",
        1,
        1,
        "2026-09-15T00:00:00+00:00",
        {"Retry-After": "43200"},
    )

    def failed_probe():
        raise AramkitRefreshError("http_429", response=response)

    service.marker_probe = failed_probe
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(
        service,
        "_run",
        lambda source, work, **kwargs: catalog
        if source == "catalog"
        else (_ for _ in ()).throw(RuntimeError("optional")),
    )
    service.refresh(scope="core")

    state = service.schedule_store.load().sources["aramkit"]
    assert state.failure_kind == "transient_network"
    assert (
        datetime.fromisoformat(state.next_due_at)
        - datetime.fromisoformat(state.last_attempt_at)
    ).total_seconds() == 43200


def test_same_hero_count_does_not_complete_new_ranking_version(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    assert service._core_units_complete()
    version = {**resolve_version(_version()), "dataPath": "new-upstream"}
    service._ranking = publish_unit(version, normalize_rankings({"rows": [_ranking("1")]}), binding=binding)
    assert not service._core_units_complete()


def test_upstream_check_compares_full_version_marker_not_only_data_path(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    changed_version = _version(build_time=5678)
    service.marker_probe = lambda: version_marker(resolve_version(changed_version))

    assert service._upstream_changed() is True
    assert service._last_upstream_marker["dataPath"] == resolve_version(_version())["dataPath"]


def test_source_data_age_never_falls_back_to_local_publication(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    version = {**resolve_version(_version()), "buildTimeUnixMs": 0}
    service._ranking = publish_unit(version, normalize_rankings({"rows": [_ranking("1")]}), binding=binding)
    assert service._ranking["completed_at"]
    assert service._ranking_data_at() == ""


def test_catalog_build_deferred_is_not_a_source_failure(runtime, monkeypatch):
    from hextech.infrastructure.sources.refresh_service import CatalogRefreshDeferred
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    generation = service._publish()
    before = service.schedule_store.load().sources.get("catalog")
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_due", lambda source, force=False: source == "catalog")
    def deferred(source, work, **kwargs):
        raise CatalogRefreshDeferred("catalog_build_deferred_in_game")
    monkeypatch.setattr(service, "_run", deferred)
    result = service.refresh()
    assert result["catalog_state"] == "deferred" and not result["failures"]
    assert service.progress()["catalog_state"] == "deferred"
    assert format_data_refresh_status(service.progress())[0] == "刷新未完成"
    assert result["generation_id"] == generation
    assert service.schedule_store.load().sources.get("catalog") == before


def test_failed_worker_check_evidence_reaches_schedule(runtime, monkeypatch):
    root, _, _ = runtime

    def process(command, **_kwargs):
        output = Path(command[command.index("--result-output") + 1])
        atomic_write_json(
            output,
            {
                "state": "failed",
                "reason_code": "validation_unchanged",
                "source_result": {
                    "check_status": "unknown",
                    "upstream_revision": "u" * 64,
                    "applied_revision": "p" * 64,
                    "failure_fingerprint": "f" * 64,
                    "retry_after_seconds": 21600,
                    "failure_stage": "validation",
                },
            },
        )
        return IsolatedProcessResult(2, 0.1, False, "", "")

    service = _service(runtime, monkeypatch, process)
    with pytest.raises(RuntimeError, match="validation_unchanged"):
        service._run("blitz", root / "failed-blitz")
    service._mark_source("blitz", error="validation")

    state = service.schedule_store.load().sources["blitz"]
    assert state.upstream_revision == "u" * 64
    assert state.applied_revision == "p" * 64
    assert state.failure_fingerprint == "f" * 64
    assert state.check_status == "failed"
    outcome = service._source_outcomes(
        attempted={"blitz"},
        failures={"blitz": "validation_unchanged"},
        deferred=set(),
        initial_identities=service._source_identities(),
    )["blitz"]
    assert outcome["upstream_revision"] == "u" * 64
    assert outcome["applied_revision"] == "p" * 64
    assert outcome["check_evidence"]["failure_fingerprint"] == "f" * 64


def test_force_is_schedule_only_for_blitz(runtime, monkeypatch):
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_due", lambda source, force=False: source == "blitz")
    calls = []

    def fail(source, work, *, force=False):
        calls.append((source, force))
        raise RuntimeError("optional_unavailable")

    monkeypatch.setattr(service, "_run", fail)
    service.refresh(force=True)

    assert ("blitz", False) in calls
    assert ("blitz", True) not in calls


def test_mayhem_worker_force_only_bypasses_its_internal_clock(runtime, monkeypatch):
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_due", lambda source, force=False: source == "mayhem")
    calls = []

    def fail(source, work, *, force=False):
        calls.append((source, force))
        raise RuntimeError("optional_unavailable")

    monkeypatch.setattr(service, "_run", fail)
    service._refresh_optional()

    assert calls == [("mayhem", True)]


def test_core_refresh_singleflight_returns_same_completed_result(runtime, monkeypatch):
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    entered = Event()
    release = Event()
    calls = []

    def refresh_once(**_kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {"state": "ready", "request_id": "shared"}

    monkeypatch.setattr(service, "_refresh_once", refresh_once)
    results = []
    first = threading.Thread(target=lambda: results.append(service.refresh(force=True)))
    second = threading.Thread(target=lambda: results.append(service.refresh(force=True)))
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(2)
    second.join(2)

    assert calls == [1]
    assert results == [
        {"state": "ready", "request_id": "shared"},
        {"state": "ready", "request_id": "shared"},
    ]


def test_core_completeness_rechecks_child_bytes(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    assert service._core_units_complete() is True
    artifact = (
        root
        / "sources"
        / "aramkit"
        / "runs"
        / hero["run_id"]
        / hero["artifact"]["relative_path"]
    )
    index = json.loads(artifact.read_text(encoding="utf-8"))
    child = artifact.parent / index["files"][0]["relative_path"]
    child.write_text("damaged", encoding="utf-8")

    assert service._core_units_complete() is False


def test_missing_hero_manifest_is_incomplete_not_an_exception(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    manifest = root / "sources" / "aramkit" / "runs" / hero["run_id"] / "manifest.json"
    manifest.unlink()

    assert service._core_units_complete() is False


def test_local_damage_bypasses_future_check_due_for_repair(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    service.marker_probe = lambda: version_marker(resolve_version(_version()))
    manifest = root / "sources" / "aramkit" / "runs" / hero["run_id"] / "manifest.json"
    manifest.unlink()
    monkeypatch.setattr(service, "_due", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    calls = []

    def repair(source, work, *, force=False):
        calls.append((source, force))
        raise RuntimeError("repair_fixture_stops_after_dispatch")

    monkeypatch.setattr(service, "_run", repair)
    service.refresh()

    assert calls == [("aramkit", True)]


def test_same_upstream_local_repair_is_recorded_up_to_date(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    ranking, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking, {"1": hero}
    service.marker_probe = lambda: version_marker(resolve_version(_version()))
    monkeypatch.setattr(service, "_core_units_complete", lambda: False)
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_due", lambda source, force=False: source == "aramkit")

    def repaired(source, work, *, force=False):
        assert source == "aramkit" and force is True
        service._source_results[source] = {
            "check_status": "changed",
            "upstream_revision": resolve_version(_version())["dataPath"],
            "applied_revision": resolve_version(_version())["dataPath"],
        }
        return ranking

    monkeypatch.setattr(service, "_run", repaired)
    service.refresh()

    state = service.schedule_store.load().sources["aramkit"]
    assert state.check_status == "up_to_date"


def test_damaged_deterministic_ranking_unit_gets_new_repair_identity(runtime):
    root, _, binding = runtime
    version = resolve_version(_version())
    rows = normalize_rankings({"rows": [_ranking("1")]})
    first = publish_unit(version, rows, binding=binding)
    artifact = (
        root
        / "sources"
        / "aramkit"
        / "runs"
        / first["run_id"]
        / first["artifact"]["relative_path"]
    )
    artifact.write_text(artifact.read_text(encoding="utf-8") + " ", encoding="utf-8")
    damaged = artifact.read_bytes()

    repaired = publish_unit(version, rows, binding=binding)
    reused = publish_unit(version, rows, binding=binding)

    assert repaired["run_id"] != first["run_id"]
    assert "-repair-" in repaired["run_id"]
    assert reused["run_id"] == repaired["run_id"]
    assert artifact.read_bytes() == damaged


def test_legacy_unit_processing_revision_gets_new_repair_identity(runtime):
    root, _, binding = runtime
    version = resolve_version(_version())
    rows = normalize_rankings({"rows": [_ranking("1")]})
    first = publish_unit(version, rows, binding=binding)
    manifest_path = root / "sources" / "aramkit" / "runs" / first["run_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["metadata"].pop("parser_revision")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    repaired = publish_unit(version, rows, binding=binding)

    assert repaired["run_id"] != first["run_id"]
    assert "-repair-" in repaired["run_id"]


def test_worker_newer_revision_wins_over_earlier_probe(runtime, monkeypatch):
    root, _, binding = runtime
    service = _service(runtime, monkeypatch, lambda *args, **kwargs: None)
    v1 = resolve_version(_version())
    ranking_v1, hero = _units(binding, root / "units")
    service._ranking, service._heroes = ranking_v1, {"1": hero}
    service.marker_probe = lambda: version_marker(v1)
    monkeypatch.setattr(service, "_core_units_complete", lambda: False)
    monkeypatch.setattr(service, "_start_optional", lambda: None)
    monkeypatch.setattr(service, "_due", lambda source, force=False: source == "aramkit")
    v2 = {**v1, "dataPath": "data/16.16-fixture", "version": "16.16"}
    ranking_v2 = publish_unit(
        v2,
        normalize_rankings({"rows": [_ranking("1")]}),
        binding=binding,
    )

    def updated(source, work, *, force=False):
        assert source == "aramkit" and force is True
        service._ranking = ranking_v2
        service._source_results[source] = {
            "check_status": "changed",
            "upstream_revision": v2["dataPath"],
            "applied_revision": v2["dataPath"],
        }
        return ranking_v2

    monkeypatch.setattr(service, "_run", updated)
    result = service.refresh()

    state = service.schedule_store.load().sources["aramkit"]
    assert state.upstream_revision == v2["dataPath"]
    assert state.applied_revision == v2["dataPath"]
    assert state.check_status == "up_to_date"
    assert result["source_outcomes"]["aramkit"]["check_status"] == "up_to_date"
    assert result["source_outcomes"]["aramkit"]["checked"] is True

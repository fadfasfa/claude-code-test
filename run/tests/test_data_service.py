from __future__ import annotations

import io
import hashlib
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
import pandas as pd

from hextech.bootstrap.data_service_runtime import (
    DATA_SERVICE_NONCE_HEADER,
    DataBuildResult,
    DataServiceApplication,
    DataServiceCore,
    bootstrap_snapshot,
    build_snapshot_from_runtime,
    _sync_startup_snapshot_status,
    _build_augment_identity_payload,
    _query_payloads_from_dataframe,
)
from hextech.infrastructure.persistence.file_lock import InterProcessFileLock
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher, SnapshotValidationError
from hextech.contracts import SourceProvenance


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
        "identities": {
            "schema_version": 2,
            "champions": {"1": "hero"},
            "augments": {"augment-1": "augment"},
            "augment_aliases": {},
            "catalog_augments": {},
        },
    }


def test_augment_identities_map_vision_ids_without_inventing_source_stats() -> None:
    overlay_hints = {
        "hints": {
            "1027": {"name": "大地苏醒"},
            "1421": {"name": "舞会女王"},
            "2006": {"name": "飞身踢"},
            "1018": {"name": "巨像的勇气"},
        }
    }
    catalog = [
        {"name": "大地苏醒", "tier": "棱彩", "augment_name_id": "ARAM_Earthwake", "cdragon_id": 1027},
        {"name": "舞会女王", "tier": "棱彩", "augment_name_id": "PromQueen", "cdragon_id": 1421},
        {"name": "飞身踢", "tier": "棱彩", "augment_name_id": "ARAM_Dropkick", "cdragon_id": 2006},
        {"name": "巨像的勇气", "tier": "棱彩", "augment_name_id": "ARAM_CourageoftheColossus", "cdragon_id": 1018},
        {"name": "歼灭者", "tier": "白银", "augment_name_id": "Weapon_Nuke", "cdragon_id": 33220},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["aram_earthwake"] == "1027"
    assert identities["augment_aliases"]["promqueen"] == "1421"
    assert identities["augment_aliases"]["aram_dropkick"] == "2006"
    assert identities["augment_aliases"]["aram_courageofthecolossus"] == "1018"
    assert identities["catalog_augments"]["weapon_nuke"]["canonical_id"] == ""
    assert identities["catalog_augments"]["weapon_nuke"]["stats_available"] is False


def test_refresh_and_policy_actions_are_serialized(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    observed: list[str] = []

    def refresh_action(_force: bool) -> dict[str, object]:
        observed.append("refresh")
        entered.set()
        release.wait(timeout=2)
        manifest = _publish(publisher, _build_payload(True), "serialized")
        return {"state": "ready", "generation_id": manifest.generation_id}

    service = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=False,
        refresh_action=refresh_action,
    )
    refresh_thread = threading.Thread(target=service.refresh)
    refresh_thread.start()
    assert entered.wait(timeout=1)

    policy_thread = threading.Thread(target=service.set_private_stats, args=(True,))
    policy_thread.start()
    release.set()
    refresh_thread.join(timeout=2)
    policy_thread.join(timeout=2)

    assert observed == ["refresh"]
    client = DataSnapshotClient(tmp_path)
    generation_id = client.load_manifest().generation_id
    assert service.status()["desired_private_stats_enabled"] is True
    assert client.load_manifest().generation_id == generation_id


def test_private_policy_generation_updates_public_startup_status(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path / "runtime" / "snapshots")
    manifest = _publish(publisher, _build_payload(True), "policy-status")
    service = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=True,
        refresh_action=lambda _force: pytest.fail("展示策略不得触发刷新"),
        initial_result={"state": "ready", "generation_id": manifest.generation_id},
    )

    result = service.set_private_stats(False)
    status = json.loads((tmp_path / "runtime" / "state" / "startup_status.json").read_text(encoding="utf-8"))

    assert result["state"] == "ready"
    assert status["data_snapshot"]["generation_id"] == result["generation_id"]
    assert service.status()["desired_private_stats_enabled"] is False
    assert result["generation_id"] == manifest.generation_id


def test_failed_refresh_preserves_last_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    service = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=True,
        refresh_action=_publishing_refresh_action(publisher, lambda: _build_payload(True)),
    )
    first = service.refresh()
    service._refresh_action = lambda _force: (_ for _ in ()).throw(RuntimeError("remote failed"))

    failed = service.refresh()

    assert first["state"] == "ready"
    assert failed["state"] == "degraded"
    assert failed["reason_code"] == "refresh_failed_last_good_preserved"
    assert DataSnapshotClient(tmp_path).status()["generation_id"] == first["generation_id"]


def test_bootstrap_reuses_healthy_runtime_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    current = _publish(publisher, _build_payload(True), "bootstrap-current")

    result = bootstrap_snapshot(
        publisher,
        builder=lambda: pytest.fail("healthy current must not be rebuilt"),
        seed_preparer=lambda: pytest.fail("healthy current must not be seeded"),
    )

    assert result == {
        "state": "ready",
        "generation_id": current.generation_id,
        "source": "runtime_current",
    }


def test_bootstrap_does_not_change_generation_for_display_privacy(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    payload = _build_payload(True)
    payload["overlay_hints"] = {
        "source": {"private_policy_stats_enabled": True},
        "hints": {"augment-1": {"name": "augment", "winrate": 0.51, "stats_by_champion_id": {"1": {}}}},
    }
    current = _publish(publisher, payload, "privacy-stable")

    result = bootstrap_snapshot(
        publisher,
        builder=lambda: pytest.fail("展示隐私不得重建 canonical generation"),
        seed_preparer=lambda: pytest.fail("健康 current 不应重新播种"),
    )

    view = DataSnapshotClient(tmp_path).open_view()
    assert result["generation_id"] == current.generation_id
    assert view.get_overlay_hints()["hints"]["augment-1"]["winrate"] == pytest.approx(0.51)


def test_dataframe_fallback_preserves_web_champion_dto_shape() -> None:
    dataframe = pd.DataFrame(
        [
            {
                "英雄ID": 24, "英雄名称": "武器大师", "英雄评级": "A", "英雄胜率": 0.52,
                "英雄出场率": 0.01, "海克斯ID": 100, "海克斯名称": "强化 A", "海克斯阶级": "金",
                "海克斯胜率": 0.53, "海克斯出场率": 0.1, "胜率差": 0.01,
            },
            {
                "英雄ID": 245, "英雄名称": "时间刺客", "英雄评级": "A", "英雄胜率": 0.51,
                "英雄出场率": 0.02, "海克斯ID": 101, "海克斯名称": "强化 B", "海克斯阶级": "金",
                "海克斯胜率": 0.52, "海克斯出场率": 0.1, "胜率差": 0.01,
            },
        ]
    )

    champions, details = _query_payloads_from_dataframe(dataframe)

    assert {"英雄 ID", "英雄名称", "英文名", "综合分数", "id", "name"}.issubset(champions[0])
    assert {item["英雄 ID"] for item in champions} == {"24", "245"}
    assert set(details) == {"武器大师", "时间刺客"}


def test_publisher_rejects_legacy_identity_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    legacy = _build_payload(False)
    legacy["identities"] = {"champions": {"1": "hero"}, "augments": {"augment-1": "augment"}}

    with pytest.raises(SnapshotValidationError, match="identities schema_version"):
        publisher.publish(legacy, source_files=_complete_provenance("legacy"))

    assert DataSnapshotClient(tmp_path).status()["state"] == "unavailable"


def test_bootstrap_builds_generation_from_startup_seed(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    prepared: list[bool] = []

    result = bootstrap_snapshot(
        publisher,
        builder=lambda: DataBuildResult(_build_payload(True), _complete_provenance("bootstrap-build")),
        seed_preparer=lambda: prepared.append(True) or True,
    )

    assert prepared == [True]
    assert result["state"] == "ready"
    assert result["source"] == "startup_data_built"
    assert DataSnapshotClient(tmp_path).status()["generation_id"] == result["generation_id"]


def test_bootstrap_accepts_complete_seed_without_runtime_rebuild(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    seeded_generation_id = ""

    def prepare_seed() -> bool:
        nonlocal seeded_generation_id
        manifest = publisher.publish(
            _build_payload(True),
            source_files=_complete_provenance("seed"),
            require_complete_provenance=True,
        )
        seeded_generation_id = manifest.generation_id
        return True

    result = bootstrap_snapshot(
        publisher,
        builder=lambda: (_ for _ in ()).throw(AssertionError("完整 seed 不应从运行态重建")),
        seed_preparer=prepare_seed,
    )

    assert result == {
        "state": "ready",
        "generation_id": seeded_generation_id,
        "source": "verified_seed",
    }


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


def _publish(
    publisher: DataSnapshotPublisher,
    payload: dict[str, object],
    marker: str,
    *,
    source: str = "hextech",
    role: str = "stats",
):
    return publisher.publish(payload, source_files=(_provenance(marker, source=source, role=role),))


def _publishing_refresh_action(
    publisher: DataSnapshotPublisher,
    payload_factory,
):
    counter = 0

    def refresh(force: bool) -> dict[str, object]:
        nonlocal counter
        counter += 1
        manifest = _publish(publisher, payload_factory(), f"refresh-{counter}")
        return {
            "state": "ready",
            "generation_id": manifest.generation_id,
            "source": "test-refresh",
            "forced": force,
        }

    return refresh


def test_startup_status_recognizes_current_synergy_artifact(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path / "snapshots")
    _publish(publisher, _build_payload(True), "synergy", source="apex", role="synergy")

    _sync_startup_snapshot_status(publisher, {"state": "ready", "source": "verified_seed"})

    status = json.loads((tmp_path / "state" / "startup_status.json").read_text(encoding="utf-8"))
    assert status["synergy_ready"] is True
    assert status["last_error"] == ""


def test_bootstrap_failure_never_publishes_partial_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)

    result = bootstrap_snapshot(
        publisher,
        builder=lambda: (_ for _ in ()).throw(ValueError("incomplete seed")),
        seed_preparer=lambda: True,
    )

    assert result["state"] == "failed"
    assert result["reason_code"] == "bootstrap_failed_no_snapshot"
    assert DataSnapshotClient(tmp_path).status()["state"] == "unavailable"


def test_service_publishes_source_summary(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    source = _provenance("summary")

    def refresh_action(_force: bool) -> dict[str, object]:
        manifest = publisher.publish(_build_payload(True), source_files=(source,))
        return {"state": "ready", "generation_id": manifest.generation_id}

    service = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=True,
        refresh_action=refresh_action,
    )

    assert service.refresh()["state"] == "ready"
    assert DataSnapshotClient(tmp_path).load_manifest().source_files == (source,)


def test_private_stats_action_does_not_refresh_or_change_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    manifest = _publish(publisher, _build_payload(True), "privacy-action")
    service = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=False,
        refresh_action=lambda _force: pytest.fail("展示策略不得触发来源刷新"),
        initial_result={"state": "ready", "generation_id": manifest.generation_id},
    )

    result = service.set_private_stats(True)

    assert result["state"] == "ready"
    assert result["reason_code"] == "display_policy_updated"
    assert service.status()["desired_private_stats_enabled"] is True
    assert DataSnapshotClient(tmp_path).load_manifest().generation_id == manifest.generation_id


def test_control_plane_queues_actions_and_requires_nonce(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    service = DataServiceCore(
        publisher=publisher,
        private_stats_enabled=False,
        refresh_action=_publishing_refresh_action(publisher, lambda: _build_payload(True)),
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
    from hextech.interfaces.desktop import runtime

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


def test_refresh_action_coalesces_running_triggers_into_one_recheck(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    rechecked = threading.Event()
    calls: list[bool] = []

    def refresh_action(force: bool) -> dict[str, object]:
        calls.append(force)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=2)
        else:
            rechecked.set()
        return {"state": "ready", "generation_id": "test-generation"}

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            private_stats_enabled=False,
            refresh_action=refresh_action,
        ),
        parent_pid=1,
    )
    first = application.submit_action("refresh")
    assert entered.wait(timeout=1)
    second = application.submit_action("refresh")
    third = application.submit_action("refresh")
    release.set()
    assert rechecked.wait(timeout=1)
    application.request_shutdown()

    assert first["accepted"] is True
    assert second["status"] == "coalesced"
    assert third["status"] == "coalesced"
    assert calls == [False, False]


def test_force_refresh_upgrades_pending_recheck(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    followed_up = threading.Event()
    calls: list[bool] = []

    def refresh_action(force: bool) -> dict[str, object]:
        calls.append(force)
        if len(calls) == 1:
            entered.set()
            release.wait(timeout=2)
        else:
            followed_up.set()
        return {"state": "ready", "generation_id": "test-generation"}

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            private_stats_enabled=False,
            refresh_action=refresh_action,
        ),
        parent_pid=1,
    )
    application.submit_action("refresh")
    assert entered.wait(timeout=1)

    normal = application.submit_action("refresh")
    forced = application.submit_action("refresh", {"force": True})
    release.set()
    assert followed_up.wait(timeout=1)
    application.request_shutdown()

    assert normal["force"] is False
    assert forced["force"] is True
    assert calls == [False, True]


def test_shutdown_clears_pending_refresh_without_starting_followup(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def refresh_action(force: bool) -> dict[str, object]:
        calls.append(force)
        entered.set()
        release.wait(timeout=2)
        return {"state": "ready", "generation_id": "test-generation"}

    application = DataServiceApplication(
        core=DataServiceCore(
            publisher=DataSnapshotPublisher(tmp_path),
            private_stats_enabled=False,
            refresh_action=refresh_action,
        ),
        parent_pid=1,
    )
    application.submit_action("refresh")
    assert entered.wait(timeout=1)
    application.submit_action("refresh", {"force": True})

    application.request_shutdown()
    release.set()
    time.sleep(0.05)

    assert calls == [False]
    assert application.submit_action("refresh") == {"accepted": False, "reason_code": "shutdown_requested"}


def test_data_service_instance_lock_is_exclusive(tmp_path: Path) -> None:
    first = InterProcessFileLock(tmp_path / "data-service.lock")
    second = InterProcessFileLock(tmp_path / "data-service.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_data_service_refresh_delegates_force_to_coordinator(tmp_path: Path) -> None:
    calls: list[bool] = []
    service = DataServiceCore(
        publisher=DataSnapshotPublisher(tmp_path),
        private_stats_enabled=False,
        refresh_action=lambda force: calls.append(force) or {"state": "ready", "generation_id": "g"},
    )

    assert service.refresh(force=True)["state"] == "ready"
    assert calls == [True]


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
        json.dumps({"266": {"synergy_items": [{"content": "同代联动"}]}}, ensure_ascii=False),
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

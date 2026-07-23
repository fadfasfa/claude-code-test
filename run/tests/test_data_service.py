from __future__ import annotations

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
    _sync_startup_snapshot_status,
    _build_augment_identity_payload,
    _query_payloads_from_dataframe,
)
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
            "hints": {"augment-1": {"augment_id": "augment-1", "name": "augment"}},
            "name_index": {"augment-1": "augment-1", "augment": "augment-1"},
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


def test_augment_identity_names_are_not_first_wins_when_canonical_ids_are_ambiguous() -> None:
    overlay_hints = {
        "hints": {
            "100": {"name": "同名强化"},
            "200": {"name": "同名强化"},
        }
    }
    catalog = [
        {"name": "同名强化", "tier": "黄金", "augment_name_id": "Same_Gold", "cdragon_id": 1},
        {"name": "同名强化", "tier": "棱彩", "augment_name_id": "Same_Prismatic", "cdragon_id": 2},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["100"] == "100"
    assert identities["augment_aliases"]["200"] == "200"
    assert "同名强化" not in identities["augment_aliases"]
    assert identities["catalog_augments"]["same_gold"]["canonical_id"] == ""
    assert identities["catalog_augments"]["same_prismatic"]["canonical_id"] == ""


def test_glass_cannon_visual_variants_share_unique_name_statistics() -> None:
    overlay_hints = {"hints": {"1325": {"name": "玻璃大炮"}}}
    catalog = [
        {"name": "玻璃大炮", "tier": "黄金", "augment_name_id": "Special_GlassCannon", "cdragon_id": 33307},
        {"name": "玻璃大炮", "tier": "棱彩", "augment_name_id": "GlassCannon", "cdragon_id": 1325},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["special_glasscannon"] == "1325"
    assert identities["augment_aliases"]["glasscannon"] == "1325"
    assert identities["augment_aliases"]["玻璃大炮"] == "1325"


def test_current_hard_names_resolve_to_the_unique_statistical_ids() -> None:
    overlay_hints = {
        "hints": {
            "1020": {"name": "黎明使者的坚决"},
            "2089": {"name": "哎哟，我的硬币！"},
        }
    }
    catalog = [
        {"name": "黎明使者的坚决", "tier": "黄金", "augment_name_id": "ARAM_DawnbringersResolve"},
        {"name": "黎明使者的坚决", "tier": "黄金", "augment_name_id": "DawnbringersResolve"},
        {"name": "哎哟，我的硬币！", "tier": "黄金", "augment_name_id": "ARAM_YowchMyCoins"},
    ]

    identities = _build_augment_identity_payload(overlay_hints, catalog)

    assert identities["augment_aliases"]["黎明使者的坚决"] == "1020"
    assert identities["augment_aliases"]["aram_dawnbringersresolve"] == "1020"
    assert identities["augment_aliases"]["dawnbringersresolve"] == "1020"
    assert identities["augment_aliases"]["哎哟，我的硬币！"] == "2089"
    assert identities["augment_aliases"]["aram_yowchmycoins"] == "2089"


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
        "hints": {
            "augment-1": {
                "augment_id": "augment-1",
                "name": "augment",
                "winrate": 0.51,
                "stats_by_champion_id": {"1": {}},
            }
        },
        "name_index": {"augment-1": "augment-1", "augment": "augment-1"},
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

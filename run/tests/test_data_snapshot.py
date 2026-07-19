from __future__ import annotations

import json
import hashlib
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

import hextech.modules.data.generation as snapshot_module
from hextech.modules.data.generation import DataSnapshotClient, DataSnapshotPublisher, SnapshotValidationError
from hextech.contracts import SourceProvenance
from hextech.modules.data.ports.paths import resource_path


def _payload(*, marker: str, private: bool = True) -> dict[str, object]:
    champion_name = f"hero-{marker}"
    augment_name = f"augment-{marker}"
    return {
        "champions": [{"id": 1, "name": champion_name}],
        "champion_hextech": {
            champion_name: {"hero_id": 1, "augments": [{"id": 9, "win_rate": 0.51}]},
        },
        "overlay_hints": {
            "source": {"private_policy_stats_enabled": private},
            "hints": {"9": {"augment_id": "9", "name": augment_name}},
            "name_index": {"9": "9", augment_name: "9"},
        },
        "identities": {
            "schema_version": 2,
            "champions": {"1": champion_name},
            "augments": {"9": augment_name},
        },
    }


def _provenance(marker: str) -> SourceProvenance:
    artifact_hash = hashlib.sha256(f"artifact:{marker}".encode()).hexdigest()
    manifest_hash = hashlib.sha256(f"manifest:{marker}".encode()).hexdigest()
    return SourceProvenance(
        source="hextech",
        run_id=f"run-{marker}",
        catalog_generation_id="catalog-test",
        artifact_role="stats",
        artifact_sha256=artifact_hash,
        record_count=1,
        manifest_sha256=manifest_hash,
        content_schema_version=2,
    )


def _publish(publisher: DataSnapshotPublisher, payload: dict[str, object], marker: str):
    return publisher.publish(payload, source_files=(_provenance(marker),))


def test_committed_seed_remains_readable_under_current_validation() -> None:
    view = DataSnapshotClient(resource_path("seeds")).open_view()

    assert view.manifest.champion_count == 173
    assert view.manifest.augment_count == 204
    assert view.manifest.stat_record_count == 21_569
    assert len(view.get_champions()) == 173


def test_publish_switches_complete_generation_and_records_manifest(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)

    manifest = _publish(publisher, _payload(marker="one"), "one")

    client = DataSnapshotClient(tmp_path)
    status = client.status()
    assert status["state"] == "ready"
    assert status["generation_id"] == manifest.generation_id
    assert client.get_champion(1)["name"] == "hero-one"
    assert client.get_combo_stats(1, 9)["win_rate"] == 0.51
    assert manifest.champion_count == 1
    assert manifest.augment_count == 1
    assert manifest.stat_record_count == 1
    assert {item.role for item in manifest.files} == {
        "champions",
        "champion_hextech",
        "overlay_hints",
        "identities",
    }


def test_same_payload_with_new_provenance_publishes_new_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    payload = _payload(marker="same")
    first = publisher.publish(
        payload,
        source_files=(_provenance("one"),),
        source_status={"hextech": {"run_id": "run-one", "freshness": "fresh"}},
    )
    second = publisher.publish(
        payload,
        source_files=(_provenance("two"),),
        source_status={"hextech": {"run_id": "run-two", "freshness": "fresh"}},
    )

    assert second.generation_id != first.generation_id
    assert DataSnapshotClient(tmp_path).load_manifest().source_files == (_provenance("two"),)


def test_fully_identical_publish_is_noop(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    payload = _payload(marker="same")
    kwargs = {
        "source_files": (_provenance("one"),),
        "source_status": {"hextech": {"run_id": "run-one", "freshness": "fresh"}},
    }
    first = publisher.publish(payload, **kwargs)
    second = publisher.publish(payload, **kwargs)
    assert second.generation_id == first.generation_id


def test_view_resolves_vision_augment_id_and_preserves_catalog_only_identity(tmp_path: Path) -> None:
    payload = _payload(marker="one")
    payload["overlay_hints"] = {
        "hints": {"9": {"augment_id": "9", "name": "大地苏醒"}},
        "name_index": {"9": "9", "大地苏醒": "9", "aram_earthwake": "9"},
    }
    payload["identities"] = {
        "schema_version": 2,
        "champions": {"1": "hero-one"},
        "augments": {"9": "大地苏醒"},
        "augment_aliases": {"aram_earthwake": "9", "大地苏醒": "9"},
        "catalog_augments": {
            "aram_earthwake": {
                "vision_id": "aram_earthwake",
                "name": "大地苏醒",
                "tier": "棱彩",
                "canonical_id": "9",
                "stats_available": True,
            },
            "weapon_nuke": {
                "vision_id": "weapon_nuke",
                "name": "歼灭者",
                "tier": "白银",
                "canonical_id": "",
                "stats_available": False,
            },
        },
    }
    _publish(DataSnapshotPublisher(tmp_path), payload, "one")
    view = DataSnapshotClient(tmp_path).open_view()

    assert view.resolve_augment("ARAM_Earthwake")["canonical_id"] == "9"
    assert view.get_combo_stats(1, "aram_earthwake")["win_rate"] == pytest.approx(0.51)
    assert view.resolve_augment("weapon_nuke") == {
        "vision_id": "weapon_nuke",
        "name": "歼灭者",
        "tier": "白银",
        "canonical_id": "",
        "stats_available": False,
    }
    assert view.get_combo_stats(1, "weapon_nuke") is None


def test_failed_publish_keeps_current_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    first = _publish(publisher, _payload(marker="one"), "one")

    with pytest.raises(SnapshotValidationError):
        _publish(publisher, {**_payload(marker="broken"), "champions": "not-a-list"}, "broken")

    client = DataSnapshotClient(tmp_path)
    assert client.status()["generation_id"] == first.generation_id
    assert client.get_champion(1)["name"] == "hero-one"


def test_zero_stat_generation_is_rejected_and_keeps_last_good(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    first = _publish(publisher, _payload(marker="one"), "one")
    empty = _payload(marker="empty")
    empty["champion_hextech"] = {"hero-empty": {"hero_id": 1, "augments": []}}

    with pytest.raises(SnapshotValidationError, match="非空统计"):
        _publish(publisher, empty, "empty")

    assert DataSnapshotClient(tmp_path).status()["generation_id"] == first.generation_id


def test_client_falls_back_to_whole_previous_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    first = _publish(publisher, _payload(marker="one"), "one")
    second = _publish(publisher, _payload(marker="two"), "two")
    second_dir = tmp_path / "generations" / second.generation_id
    (second_dir / "overlay_hints.json").write_text("{}", encoding="utf-8")

    client = DataSnapshotClient(tmp_path)
    status = client.status()
    assert status["state"] == "degraded"
    assert status["generation_id"] == first.generation_id
    assert status["failed_generation_id"] == second.generation_id
    assert client.get_champion(1)["name"] == "hero-one"
    assert client.get_overlay_hints()["hints"]["9"]["name"] == "augment-one"


def test_existing_client_observes_new_current_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    _publish(publisher, _payload(marker="one"), "one")
    client = DataSnapshotClient(tmp_path)
    assert client.get_champion(1)["name"] == "hero-one"

    second = _publish(publisher, _payload(marker="two"), "two")

    assert client.status()["generation_id"] == second.generation_id
    assert client.get_champion(1)["name"] == "hero-two"


def test_open_view_stays_on_one_generation_after_pointer_switch(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    first = _publish(publisher, _payload(marker="one"), "one")
    client = DataSnapshotClient(tmp_path)
    view = client.open_view()

    second = _publish(publisher, _payload(marker="two"), "two")

    assert view.status()["generation_id"] == first.generation_id
    assert view.get_champion(1)["name"] == "hero-one"
    assert view.get_overlay_hints()["hints"]["9"]["name"] == "augment-one"
    assert client.status()["generation_id"] == second.generation_id


def test_desktop_generation_watcher_loads_first_published_snapshot(tmp_path: Path) -> None:
    from hextech.interfaces.desktop.app import HextechUI

    _publish(DataSnapshotPublisher(tmp_path), _payload(marker="one"), "one")

    class StopAfterOnePass:
        calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    rendered: list[dict] = []
    ui = SimpleNamespace(
        _snapshot_client=DataSnapshotClient(tmp_path),
        _snapshot_generation_id="",
        stop_event=StopAfterOnePass(),
        _champions_lock=threading.Lock(),
        champions=[],
        current_candidate_groups={"selected_champion_ids": ["1"], "bench_champion_ids": []},
        _set_status=lambda *_args: None,
        update_ui=lambda groups: rendered.append(dict(groups)),
        _run_on_ui_thread=lambda callback: callback() or True,
    )
    ui.load_data = MethodType(HextechUI.load_data, ui)

    HextechUI._snapshot_watch_loop(ui)

    assert [champion["id"] for champion in ui.champions] == [1]
    assert rendered == [{"selected_champion_ids": ["1"], "bench_champion_ids": []}]


def test_consumer_cannot_mutate_cached_generation(tmp_path: Path) -> None:
    _publish(DataSnapshotPublisher(tmp_path), _payload(marker="one"), "one")
    client = DataSnapshotClient(tmp_path)
    hints = client.get_overlay_hints()
    hints["hints"]["9"]["name"] = "mutated"
    champions = client.get_champions()
    champions[0]["name"] = "mutated"

    assert client.get_overlay_hints()["hints"]["9"]["name"] == "augment-one"
    assert client.get_champion(1)["name"] == "hero-one"


def test_champion_detail_is_returned_as_unpolluted_copy(tmp_path: Path) -> None:
    payload = _payload(marker="one")
    details = payload["champion_hextech"]
    assert isinstance(details, dict)
    hero_detail = details["hero-one"]
    assert isinstance(hero_detail, dict)
    hero_detail["synergy"] = {"synergy_items": [{"name": "pair"}]}
    _publish(DataSnapshotPublisher(tmp_path), payload, "one")
    client = DataSnapshotClient(tmp_path)

    detail = client.get_champion_detail(1)
    assert detail is not None
    detail["augments"][0]["win_rate"] = 0.99

    assert client.get_champion_detail(1)["augments"][0]["win_rate"] == 0.51
    assert client.get_synergy_data()["1"]["synergy_items"][0]["name"] == "pair"


def test_manifest_rejects_path_escape(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    manifest = _publish(publisher, _payload(marker="one"), "one")
    manifest_path = tmp_path / "generations" / manifest.generation_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"][0]["relative_path"] = "../outside.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    client = DataSnapshotClient(tmp_path)
    assert client.status()["state"] == "unavailable"
    with pytest.raises(SnapshotValidationError):
        client.get_champions()


@pytest.mark.parametrize(
    ("field", "value"),
    [("champion_count", 99), ("content_fingerprint", "invalid")],
)
def test_manifest_rejects_invalid_counts_and_types(tmp_path: Path, field: str, value: object) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    manifest = _publish(publisher, _payload(marker="one"), "one")
    manifest_path = tmp_path / "generations" / manifest.generation_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert DataSnapshotClient(tmp_path).status()["state"] == "unavailable"


def test_manifest_rejects_duplicate_file_roles(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    manifest = _publish(publisher, _payload(marker="one"), "one")
    manifest_path = tmp_path / "generations" / manifest.generation_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["files"].append(dict(payload["files"][0]))
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert DataSnapshotClient(tmp_path).status()["state"] == "unavailable"


def test_invalid_utf8_current_manifest_falls_back_to_previous_generation(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    previous = _publish(publisher, _payload(marker="one"), "one")
    current = _publish(publisher, _payload(marker="two"), "two")
    manifest_path = tmp_path / "generations" / current.generation_id / "manifest.json"
    manifest_path.write_bytes(b"\xff\xfe\x00")

    status = DataSnapshotClient(tmp_path).status()

    assert status["state"] == "degraded"
    assert status["generation_id"] == previous.generation_id
    assert status["failed_generation_id"] == current.generation_id


def test_pointer_write_failure_keeps_current_and_removes_unpublished_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    first = _publish(publisher, _payload(marker="one"), "one")
    real_write = snapshot_module.atomic_write_json

    def fail_current(path: object, payload: object, **kwargs: object) -> None:
        if Path(path) == publisher.current_path:
            raise OSError("pointer unavailable")
        real_write(path, payload, **kwargs)

    monkeypatch.setattr(snapshot_module, "atomic_write_json", fail_current)
    with pytest.raises(OSError, match="pointer unavailable"):
        _publish(publisher, _payload(marker="two"), "two")

    assert DataSnapshotClient(tmp_path).status()["generation_id"] == first.generation_id
    generation_dirs = [path for path in publisher.generations_dir.iterdir() if path.is_dir()]
    assert [path.name for path in generation_dirs] == [first.generation_id]


def test_cleanup_failure_does_not_rollback_committed_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    _publish(publisher, _payload(marker="one"), "one")
    monkeypatch.setattr(
        publisher,
        "_remove_unreferenced_generations",
        lambda keep: (_ for _ in ()).throw(OSError("cleanup busy")),
    )

    second = _publish(publisher, _payload(marker="two"), "two")

    assert DataSnapshotClient(tmp_path).status()["generation_id"] == second.generation_id


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", True), ("champion_count", 1.0), ("source_size", "abc"), ("relative_path", 7)],
)
def test_malformed_manifest_is_reported_as_unavailable(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    manifest = _publish(publisher, _payload(marker="one"), "one")
    manifest_path = tmp_path / "generations" / manifest.generation_id / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field == "source_size":
        payload["source_files"][0]["record_count"] = value
    elif field == "relative_path":
        payload["files"][0]["relative_path"] = value
    else:
        payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert DataSnapshotClient(tmp_path).status()["state"] == "unavailable"


def test_publisher_leaves_generation_retention_to_data_service(tmp_path: Path) -> None:
    publisher = DataSnapshotPublisher(tmp_path)
    ids = [_publish(publisher, _payload(marker=str(index)), str(index)).generation_id for index in range(3)]

    remaining = {path.name for path in (tmp_path / "generations").iterdir() if path.is_dir()}
    assert remaining == set(ids)


def test_overlay_unavailable_state_does_not_fall_back_to_legacy_cache(tmp_path: Path) -> None:
    from hextech.modules.data.overlay_source import SharedOverlayDataSource

    class UnavailableClient:
        @staticmethod
        def status() -> dict:
            return {"state": "unavailable", "generation_id": ""}

        @staticmethod
        def get_overlay_hints() -> dict:
            raise AssertionError("unavailable snapshot must not be read")

    legacy_cache = tmp_path / "overlay_hint_cache.v1.json"
    legacy_cache.write_text('{"hints":{"legacy":{}}}', encoding="utf-8")
    payload = SharedOverlayDataSource(snapshot_client=UnavailableClient()).read_hint_cache()

    assert payload["hints"] == {}
    assert payload["snapshot"]["state"] == "unavailable"


def test_overlay_data_source_observes_first_generation_without_restart(tmp_path: Path) -> None:
    from hextech.modules.data.overlay_source import SharedOverlayDataSource

    client = DataSnapshotClient(tmp_path)
    source = SharedOverlayDataSource(snapshot_client=client)
    assert source.read_hint_cache()["snapshot"]["state"] == "unavailable"

    manifest = _publish(DataSnapshotPublisher(tmp_path), _payload(marker="hot"), "hot")
    refreshed = source.read_hint_cache()

    assert refreshed["snapshot"]["state"] == "ready"
    assert refreshed["snapshot"]["generation_id"] == manifest.generation_id
    assert refreshed["hints"]["9"]["name"] == "augment-hot"


def test_web_and_overlay_read_the_same_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hextech.interfaces.web.backend import api as web_api
    from hextech.modules.data.overlay_source import SharedOverlayDataSource

    manifest = _publish(DataSnapshotPublisher(tmp_path), _payload(marker="one"), "one")
    snapshot_client = DataSnapshotClient(tmp_path)
    monkeypatch.setattr(web_api, "_snapshot_client", snapshot_client)
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda _name: "hero-one")
    app = FastAPI()
    web_api.register_routes(app)

    web_payload = TestClient(app).get("/api/champion/hero-one/hextechs").json()
    overlay_payload = SharedOverlayDataSource(snapshot_client=snapshot_client).read_hint_cache()

    assert web_payload["generation_id"] == manifest.generation_id
    assert overlay_payload["snapshot"]["generation_id"] == manifest.generation_id


def test_web_and_overlay_response_stays_pinned_during_pointer_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hextech.interfaces.web.backend import api as web_api
    from hextech.modules.data.overlay_source import SharedOverlayDataSource

    publisher = DataSnapshotPublisher(tmp_path)
    first = _publish(publisher, _payload(marker="one"), "one")
    client = DataSnapshotClient(tmp_path)
    pinned = client.open_view()
    _publish(publisher, _payload(marker="two"), "two")
    monkeypatch.setattr(client, "open_view", lambda: pinned)
    monkeypatch.setattr(web_api, "_snapshot_client", client)
    monkeypatch.setattr(web_api.web_runtime, "resolve_canonical_hero_name", lambda _name: "hero-one")
    app = FastAPI()
    web_api.register_routes(app)

    web_payload = TestClient(app).get("/api/champion/hero-one/hextechs").json()
    overlay_payload = SharedOverlayDataSource(snapshot_client=client).read_hint_cache()

    assert web_payload["generation_id"] == first.generation_id
    assert web_payload["augments"][0]["win_rate"] == 0.51
    assert overlay_payload["snapshot"]["generation_id"] == first.generation_id
    assert overlay_payload["hints"]["9"]["name"] == "augment-one"

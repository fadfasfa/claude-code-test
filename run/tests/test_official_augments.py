from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from hextech.infrastructure.sources import catalog_versioned as catalog
from hextech.infrastructure.sources.official_augments import (
    OFFICIAL_AUGMENTS_URL, bind_official_entries, normalize_official_augments,
)
from hextech.modules.acquisition.hextech.production_pool import build_production_augment_pool, validate_production_augment_pool


def row(identity=3078, **changes):
    return {"augmentID": identity, "name_cn": "鼓声暴起 低音轰鸣", "name_en": "TheBoomAndTheBass",
            "level": "kGold", "mode": "KIWI, KIWI_JADE", "isPBE": 0, "isNew": 0,
            "small_Icon": "https://game.gtimg.cn/images/lol/act/img/rune/boom.png", **changes}


def test_official_mode_is_exact_and_new_flag_does_not_control_eligibility():
    result = normalize_official_augments([row(), row(1, mode="KIWI_JADE"), row(2, isPBE=1),
                                          row(3, mode="CHERRY"), row(4, mode="KIWI", isNew=1)])
    assert set(result) == {"3078", "4"}


@pytest.mark.parametrize("payload", [[], {}, [row(), row()], [row(augmentID=True)],
                                    [row(isPBE=False)], [row(isPBE=None)], [row(name_cn="")]])
def test_malformed_official_roster_cannot_replace_last_good(payload):
    with pytest.raises(ValueError, match="official_roster"):
        normalize_official_augments(payload)


def test_official_name_replaces_resource_placeholder_without_weakening_pool_validation():
    metadata = normalize_official_augments([row(3176, name_cn="赛季之星:五杀摇滚", name_en="Pentakill")])
    merged = bind_official_entries({"entries": [{"cdragon_id": 3176, "name": "BlankName",
                                                "augment_name_id": "Pentakill"}]}, metadata)
    candidate = build_production_augment_pool(metadata, merged["entries"], schema_version=2)
    validate_production_augment_pool(candidate)
    assert candidate["identities"][0]["name"] == "赛季之星:五杀摇滚"
    assert not candidate["name_conflicts"]
    assert merged["entries"][0]["source_name"] == "BlankName"


def test_missing_or_conflicting_resource_keeps_official_identity_without_wrong_icon():
    metadata = normalize_official_augments([row(small_Icon="")])
    merged = bind_official_entries({"entries": [{"cdragon_id": 3078, "name": "旧名字",
        "augment_name_id": "wrong", "source_icon_url": "https://raw.communitydragon.org/wrong.png"}]}, metadata)
    candidate = build_production_augment_pool(metadata, merged["entries"], schema_version=2)
    validate_production_augment_pool(candidate)
    assert candidate["canonical_ids"] == ["3078"]
    assert candidate["identities"][0]["name_ready"]
    assert not candidate["identities"][0]["icon_ready"]
    assert not merged["entries"][0]["source_icon_url"]


def test_resource_pool_cannot_admit_extra_non_official_identity():
    metadata = normalize_official_augments([row()])
    merged = bind_official_entries({"entries": [{"cdragon_id": 9999, "name": "非官方候选"}]}, metadata)
    candidate = build_production_augment_pool(metadata, merged["entries"], schema_version=2)
    assert candidate["canonical_ids"] == ["3078"]
    assert {entry["cdragon_id"] for entry in merged["entries"]} == {3078, 9999}


@pytest.mark.parametrize("icon", ["https://game.gtimg.cn:garbage/icon.png", "https://[bad/icon.png"])
def test_malformed_optional_icon_does_not_veto_official_roster(icon):
    metadata = normalize_official_augments([row(small_Icon=icon)])
    assert metadata["3078"]["enabled"] is True
    assert metadata["3078"]["icon_url"] == ""


def test_catalog_conditional_fetch_and_error_preserve_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "catalog_root", lambda: tmp_path / "catalog")
    calls = []
    responses = [SimpleNamespace(status_code=200, body=b'[1]', response_headers={"ETag": '"v1"'}, error="", error_kind=""),
                 SimpleNamespace(status_code=304, body=b'', response_headers={}, error="", error_kind=""),
                 SimpleNamespace(status_code=503, body=b'', response_headers={"Retry-After": "43200"}, error="outage", error_kind="http_5xx")]
    def fetch(_url, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)
    monkeypatch.setattr(catalog, "fetch_text", fetch)
    assert catalog._load_json_result(OFFICIAL_AUGMENTS_URL, timeout_ms=1000) == [1]
    assert catalog._load_json_result(OFFICIAL_AUGMENTS_URL, timeout_ms=1000) == [1]
    assert calls[1]["headers"]["If-None-Match"] == '"v1"'
    with pytest.raises(catalog.CatalogRefreshError) as error:
        catalog._load_json_result(OFFICIAL_AUGMENTS_URL, timeout_ms=1000)
    assert error.value.response.response_headers["Retry-After"] == "43200"
    assert error.value.reason == "http_5xx"


def test_resource_outage_does_not_block_official_identity_probe(tmp_path, monkeypatch):
    previous = tmp_path / "catalog"
    previous.mkdir()
    (previous / "海克斯资源目录.v1.json").write_text(json.dumps({"entries": []}), encoding="utf-8")
    monkeypatch.setattr(catalog, "load_active_catalog", lambda: SimpleNamespace(root=previous))
    def load(url, **_kwargs):
        if url == catalog.DDRAGON_VERSIONS_URL:
            return ["16.21.1"]
        if url == OFFICIAL_AUGMENTS_URL:
            return [row()]
        raise catalog.CatalogRefreshError("network timeout")
    monkeypatch.setattr(catalog, "_load_json_result", load)
    version, _, metadata, marker = catalog._probe_sources()
    assert version == "16.21.1" and list(metadata) == ["3078"] and len(marker) == 64


def test_recorded_29_missing_ids_bind_without_hand_written_aliases():
    fixture = json.loads((Path(__file__).parent / "fixtures/official_kiwi_20261024.json").read_text(encoding="utf-8"))
    metadata = normalize_official_augments(fixture["rows"])
    assert set(map(str, fixture["regression_ids"])) <= metadata.keys()
    assert not {"3060", "3159"} & metadata.keys()
    catalog_data = bind_official_entries({"entries": []}, metadata)
    candidate = build_production_augment_pool(metadata, catalog_data["entries"], schema_version=2)
    validate_production_augment_pool(candidate)
    assert metadata["3071"]["displayName"] == "黄铜爆音"
    assert metadata["3075"]["displayName"] == "反冲"
    assert metadata["3078"]["displayName"] == "鼓声暴起 低音轰鸣"


def test_official_schema_error_stays_validation_across_worker_boundary():
    from hextech.bootstrap.acquisition_worker import _unexpected_failure_payload
    from hextech.infrastructure.sources.refresh_policy import source_failure_kind
    error = catalog.CatalogRefreshError("official_roster_has_no_live_kiwi_identities", reason="schema_changed")
    result = _unexpected_failure_payload(error)
    assert result["failure_stage"] == "validation"
    assert source_failure_kind(result["source_result"], error) == "validation"

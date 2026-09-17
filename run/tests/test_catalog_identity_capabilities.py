"""Enabled identities survive missing channels without expanding to the audit catalog."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hextech.infrastructure.persistence.production_pool_binding import (
    _catalog_production_pool, validate_production_pool_assets,
)
from hextech.infrastructure.sources import catalog_versioned as source
from hextech.infrastructure.vision import template_build, template_runtime
from hextech.infrastructure.vision.ocr_shadow import build_ocr_vocabulary, match_ocr_text
from hextech.modules.acquisition.hextech.production_pool import (
    build_production_augment_pool, validate_production_augment_pool,
)
from hextech.modules.data.catalog.versioned import (
    CatalogValidationError, CatalogView, build_catalog_manifest, validate_catalog_files,
)


def metadata():
    return {
        "7001": {"enabled": True, "displayName": "新增强化", "name": "ARAM_New"},
        "7002": {"enabled": True, "displayName": "", "name": "ARAM_NoName"},
        "7003": {"enabled": False, "displayName": "禁用强化", "name": "ARAM_Disabled"},
    }


def pool():
    result = build_production_augment_pool(metadata(), [], schema_version=2, upstream_marker_sha256="a" * 64)
    result.update(mode="aram-mayhem", source_marker_sha256="b" * 64)
    return result


def write_catalog(root: Path, identities: dict):
    root.mkdir(parents=True, exist_ok=True)
    resources = Path(__file__).resolve().parents[1] / "resources" / "catalog"
    for filename in ("英雄目录.v1.json", "海克斯资源目录.v1.json", "hero_version.txt"):
        shutil.copy2(resources / filename, root / filename)
    (root / "augment_assets.v1.json").write_text(json.dumps({"schema_version": 1, "entries": []}), encoding="utf-8")
    (root / "augment_identities.v2.json").write_text(json.dumps(identities), encoding="utf-8")
    manifest = build_catalog_manifest(root, created_at="fixture")
    validate_catalog_files(root, manifest)
    return CatalogView(root, manifest, "c" * 64)


def test_v2_keeps_enabled_identity_when_name_or_catalog_mapping_is_missing():
    candidate = pool()
    validate_production_augment_pool(candidate)
    assert candidate["canonical_ids"] == ["7001", "7002"]
    assert candidate["disabled_ids"] == ["7003"]
    assert candidate["enabled_count"] == 2
    first, second = candidate["identities"]
    assert first["name_ready"] is True
    assert first["icon_ready"] is False
    assert first["exemplar_ready"] is False
    assert first["capability_reasons"]["icon"] == "catalog_mapping_missing"
    assert second["name_ready"] is False
    assert second["capability_reasons"]["name"] == "name_missing"


@pytest.mark.parametrize("broken", [{}, {"1": "wrong"}, {"1": {"enabled": "yes"}}, {"x": {"enabled": True}}])
def test_v2_rejects_bad_metadata_as_a_whole(broken):
    with pytest.raises(ValueError, match="metadata_invalid"):
        build_production_augment_pool(broken, [], schema_version=2)


def test_v2_rejects_conflicting_identity_metadata_not_just_missing_capability():
    candidate = build_production_augment_pool(metadata(), [{"cdragon_id": 7001, "name": "混版旧名称"}], schema_version=2)
    with pytest.raises(ValueError, match="unavailable"):
        validate_production_augment_pool(candidate)


def test_same_name_multiple_ids_degrades_only_ambiguous_names():
    shared = {"1": {"enabled": True, "displayName": "同名强化", "name": "one"},
              "2": {"enabled": True, "displayName": "同名强化", "name": "two"},
              "3": {"enabled": True, "displayName": "正常强化", "name": "three"}}
    candidate = build_production_augment_pool(shared, [], schema_version=2)
    validate_production_augment_pool(candidate)
    assert candidate["canonical_ids"] == ["1", "2", "3"]
    assert not candidate["duplicate_ids"]
    assert [i["name_ready"] for i in candidate["identities"]] == [False, False, True]
    assert candidate["identities"][0]["capability_reasons"]["name"] == "ambiguous_name"


def test_missing_icon_download_does_not_delete_identity_or_block_other_assets(tmp_path, monkeypatch):
    entries = [
        {"cdragon_id": 7001, "name": "新增强化", "source_icon_url": "missing"},
        {"cdragon_id": 9001, "name": "目录池外", "source_icon_url": "audit-only"},
    ]
    def fail(entry, *_args, **_kwargs):
        assert entry["cdragon_id"] == 7001
        raise source.CatalogRefreshError("HTTP 404")
    monkeypatch.setattr(source, "_fetch_icon", fail)
    catalog = {"entries": entries}
    frozen = source._freeze_enabled_assets(tmp_path, catalog, {"7001", "7002"}, tmp_path)
    assert frozen["entries"] == []
    candidate = build_production_augment_pool(metadata(), entries, schema_version=2)
    validate_production_augment_pool(candidate)
    assert candidate["canonical_ids"] == ["7001", "7002"]
    assert candidate["full_catalog_count"] == 2


def test_v2_catalog_manifest_binds_enabled_identity_artifact_even_with_zero_icons(tmp_path):
    catalog = write_catalog(tmp_path, pool())
    candidate = _catalog_production_pool(catalog)
    assert candidate["canonical_ids"] == ["7001", "7002"]
    assert candidate["catalog_generation_id"] == catalog.generation_id
    assert candidate["catalog_manifest_sha256"] == catalog.manifest_sha256
    validate_production_pool_assets(candidate, tmp_path)
    assert next(d.record_count for d in catalog.manifest.files if d.role == "augment_assets") == 0
    identity_path = tmp_path / "augment_identities.v2.json"
    payload = json.loads(identity_path.read_text(encoding="utf-8"))
    payload["identities"][0]["name"] = "篡改"
    identity_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogValidationError, match="校验失败"):
        validate_catalog_files(tmp_path, catalog.manifest)
    with pytest.raises(ValueError, match="unbound"):
        _catalog_production_pool(catalog)


def test_v2_cannot_bind_asset_from_another_identity_or_hide_ready_icon(tmp_path):
    candidate = pool()
    assets_path = tmp_path / "augment_assets.v1.json"
    assets_path.write_text(json.dumps({"entries": [{"canonical_id": "9999", "relative_path": "x.png", "sha256": "d" * 64}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown=\\['9999'\\]"):
        validate_production_pool_assets(candidate, tmp_path)
    assets_path.write_text(json.dumps({"entries": []}), encoding="utf-8")
    candidate["identities"][0]["icon_ready"] = True
    with pytest.raises(ValueError, match="missing=\\['7001'\\]"):
        validate_production_pool_assets(candidate, tmp_path)


def test_name_only_identity_reaches_ocr_and_missing_name_does_not_affect_neighbor(tmp_path, monkeypatch):
    candidate = pool()
    write_catalog(tmp_path, candidate)
    monkeypatch.setattr(template_build, "_production_catalog_root", lambda _pool: tmp_path)
    monkeypatch.setattr(template_build, "ASSET_DIR", tmp_path)
    hints = {"source": {"production_augment_pool": candidate}, "vision": {"catalog_generation_id": "catalog-new"}, "snapshot": {"generation_id": "stats-old"}}
    entries = template_build.load_default_template_entries(hint_cache=hints, require_production_pool=True)
    assert {entry.augment_id for entry in entries} == {"7001", "7002"}
    assert all(not entry.icon_fingerprints and not entry.observed_name_fingerprints for entry in entries)
    vocabulary = build_ocr_vocabulary(entries)
    assert [entry.canonical_id for entry in vocabulary] == ["7001"]
    assert match_ocr_text("新增强化", vocabulary)["matched_id"] == "7001"
    assert match_ocr_text("目录池外", vocabulary)["matched_id"] == ""
    matrices = template_runtime.rank_template_matrices(entries)
    stats = template_runtime._production_pool_runtime_stats(hints, matrices)
    assert stats["recognition_catalog_id"] == "catalog-new"
    assert stats["observed_data_generation_id"] == "stats-old"
    assert stats["identity_capabilities"]["7001"]["name_ready"] is True
    assert stats["identity_capabilities"]["7002"]["name_ready"] is False


def test_new_identity_without_any_renderable_fingerprint_is_still_in_ocr(tmp_path, monkeypatch):
    write_catalog(tmp_path, pool())
    monkeypatch.setattr(template_build, "_production_catalog_root", lambda _pool: tmp_path)
    monkeypatch.setattr(template_build, "ASSET_DIR", tmp_path)
    monkeypatch.setattr(template_build, "build_template_index", lambda _raw: [])
    entries = template_build.load_default_template_entries(hint_cache={"source": {"production_augment_pool": pool()}})
    assert match_ocr_text("新增强化", build_ocr_vocabulary(entries))["matched_id"] == "7001"


def test_unchanged_marker_even_for_forced_check_does_not_build_or_download(tmp_path, monkeypatch):
    candidate = pool()
    catalog = write_catalog(tmp_path / "catalog", candidate)
    monkeypatch.setattr(source, "load_active_catalog", lambda: catalog)
    monkeypatch.setattr(source, "_probe_sources", lambda: ("new", [], metadata(), candidate["source_marker_sha256"]))
    monkeypatch.setattr(source, "_write_candidate", lambda *_args, **_kwargs: pytest.fail("unchanged must not fetch champions/icons"))
    pointer = tmp_path / "candidate.json"
    result = source.refresh_catalog(force=True, pointer_output=pointer)
    assert result["changed"] is False
    assert result["marker_state"] == "unchanged"
    assert json.loads(pointer.read_text(encoding="utf-8"))["catalog_generation_id"] == catalog.generation_id


def test_remote_candidate_publishes_enabled_metadata_independent_of_icon_success(tmp_path, monkeypatch):
    active = write_catalog(tmp_path / "active", pool())
    monkeypatch.setattr(source, "load_active_catalog", lambda: active)
    monkeypatch.setattr(source, "catalog_root", lambda: tmp_path / "runtime")
    monkeypatch.setattr(source, "_load_json_result", lambda *_args, **_kwargs: {"data": {"Annie": {"key": "1", "name": "安妮", "title": "黑暗之女", "id": "Annie"}}})
    monkeypatch.setattr(source, "_fetch_icon", lambda *_args, **_kwargs: (_ for _ in ()).throw(source.CatalogRefreshError("missing icon")))
    cdragon = [{"id": 7001, "nameTRA": "新增强化", "augmentNameId": "ARAM_New", "augmentSmallIconPath": "new.png"}]
    staging, changed, _ = source._write_candidate(tmp_path, allow_remote=True, remote_probe=("v2", cdragon, metadata(), "d" * 64))
    assert changed
    published = json.loads((staging / "augment_identities.v2.json").read_text(encoding="utf-8"))
    assert published["canonical_ids"] == ["7001", "7002"]
    assert published["identities"][0]["name_ready"] is True
    assert published["identities"][0]["icon_ready"] is False
    manifest = build_catalog_manifest(staging, created_at="test")
    validate_catalog_files(staging, manifest)


def test_changed_catalog_checks_marker_in_game_but_defers_build(tmp_path, monkeypatch):
    from hextech.modules.acquisition.champion_downloads import DownloadContext

    catalog = write_catalog(tmp_path / "catalog", pool())
    monkeypatch.setattr(source, "load_active_catalog", lambda: catalog)
    monkeypatch.setattr(source, "_probe_sources", lambda: ("new", [], metadata(), "f" * 64))
    monkeypatch.setattr(source, "_write_candidate", lambda *_args, **_kwargs: pytest.fail("in-game must not build"))
    pointer = tmp_path / "deferred.json"
    result = source.refresh_catalog(force=True, pointer_output=pointer, context=lambda: DownloadContext(in_game=True))
    assert result["state"] == "deferred"
    assert result["reason_code"] == "catalog_build_deferred_in_game"
    assert not pointer.exists()


def test_catalog_icon_downloads_share_existing_dynamic_download_budget(tmp_path, monkeypatch):
    from hextech.modules.acquisition.champion_downloads import DownloadContext
    contexts = []
    class Scheduler:
        def __init__(self, context):
            self.context = context
        def run(self, ids, fetch, *, stop):
            contexts.append(self.context())
            return {item: fetch(item) for item in ids}
    monkeypatch.setattr(source, "ChampionDownloads", Scheduler)
    monkeypatch.setattr(source, "_fetch_icon", lambda *_args, **_kwargs: (_ for _ in ()).throw(source.CatalogRefreshError("missing")))
    source._freeze_enabled_assets(tmp_path, {"entries": [{"cdragon_id": 7001, "name": "新增强化"}]}, {"7001"}, tmp_path,
        context=lambda: DownloadContext(champion_id="7001", in_game=True, pause_background=False))
    assert contexts == [DownloadContext(in_game=True)]


def test_v2_package_capability_gate_accepts_no_exemplar_and_zero_icons_but_rejects_missing_diagnostics():
    from hextech.modules.acquisition.hextech.production_pool import production_capability_status_valid

    status = {"production_pool_schema_version": 2, "matrix_rows": {"icon": 0, "observed_name": 0},
              "identity_capabilities": {i["canonical_id"]: i for i in pool()["identities"]}}
    assert production_capability_status_valid(status, 2)
    status["identity_capabilities"]["7001"]["icon_ready"] = True
    assert not production_capability_status_valid(status, 2)
    status["identity_capabilities"].pop("7002")
    assert not production_capability_status_valid(status, 2)


def test_name_only_identity_uses_strict_095_exact_and_existing_three_frame_reducer(tmp_path, monkeypatch):
    from test_overlay_ocr_completed_evidence import _raw_slot, _review_event
    from hextech.infrastructure.vision.ocr_completed import OcrEvidenceContext, build_production_evidence
    from hextech.infrastructure.vision.ocr_shadow import OCR_PRODUCTION_MIN_CONFIDENCE
    from hextech.infrastructure.vision.state import SelectionTracker

    write_catalog(tmp_path, pool())
    monkeypatch.setattr(template_build, "_production_catalog_root", lambda _pool: tmp_path)
    monkeypatch.setattr(template_build, "ASSET_DIR", tmp_path)
    entries = template_build.load_default_template_entries(hint_cache={"source": {"production_augment_pool": pool()}})
    vocabulary = build_ocr_vocabulary(entries)
    tracker = SelectionTracker(scene_enter_frames=1)
    assert OCR_PRODUCTION_MIN_CONFIDENCE == 0.95
    for frame in (1, 2, 3):
        slot = _raw_slot(0, frame)
        context = OcrEvidenceContext("session-completed", 1, 0, 1, f"{frame:064x}", f"fp-0-{frame}", frame, 100 + frame * .05)
        match = match_ocr_text("新增强化", vocabulary)
        rejected = build_production_evidence({**match, "confidence": 0.949}, context, minimum_confidence=OCR_PRODUCTION_MIN_CONFIDENCE)
        assert rejected["state"] == "rejected"
        evidence = build_production_evidence({**match, "confidence": 0.99}, context, minimum_confidence=OCR_PRODUCTION_MIN_CONFIDENCE)
        slot["ocr_production"] = evidence
        result = tracker.update(_review_event(frame, slots=[slot]))
        assert result["slots"][0]["state"] == ("ready" if frame == 3 else "detecting")
    assert result["slots"][0]["augment_id"] == "7001"


def test_packaged_smoke_accepts_verified_enabled_pool_with_name_only_identities(tmp_path, monkeypatch):
    from test_package_deployment import _cohort_metadata, _sidecar_pool_status
    from tooling.acceptance import smoke_packaged_startup as smoke

    expected = _cohort_metadata(pool_count=2)
    package = tmp_path / "package"
    runtime = tmp_path / "runtime"
    def run(command, **_kwargs):
        status = _sidecar_pool_status(expected)
        status.update(production_pool_schema_version=2,
            identity_capabilities={i["canonical_id"]: i for i in pool()["identities"]},
            matrix_rows={"icon": 0, "name": 1, "alt_name": 1, "observed_name": 0})
        path = runtime / "state/game_overlay_sidecar_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status), encoding="utf-8")
        return smoke.subprocess.CompletedProcess(command, 0, stdout=b"")
    monkeypatch.setattr(smoke.subprocess, "run", run)
    result = smoke._sidecar_pool_smoke(package / "fixture.exe", package, {}, runtime, {"build_id": "test-build", "cohort_seed": expected})
    assert result["state"] == "ready"


def _repair_fixture(tmp_path, monkeypatch, missing_count=1):
    from test_catalog_v2 import _png_bytes

    baseline = write_catalog(tmp_path / "baseline", pool())
    root = tmp_path / "runtime/catalog"
    monkeypatch.setattr(source, "catalog_root", lambda: root)
    monkeypatch.setattr(source, "load_active_catalog", lambda: baseline)
    monkeypatch.setattr(source, "_load_json_result", lambda *_args, **_kwargs: {"data": {"Annie": {"key": "1", "name": "安妮", "title": "黑暗之女", "id": "Annie"}}})
    raw = {str(8000+i): {"enabled": True, "displayName": f"强化{i}", "name": f"ARAM_{i}"} for i in range(missing_count + 1)}
    cdragon = [{"id": int(key), "nameTRA": value["displayName"], "augmentNameId": value["name"], "augmentSmallIconPath": f"{key}.png"} for key, value in raw.items()]
    def initial(entry, *_args, **_kwargs):
        if entry["cdragon_id"] == 8000:
            return _png_bytes()
        raise source.CatalogRefreshError("temporary failure")
    monkeypatch.setattr(source, "_fetch_icon", initial)
    probe = ("v2", cdragon, raw, "e" * 64)
    staging, _, _ = source._write_candidate(root, allow_remote=True, remote_probe=probe)
    manifest = build_catalog_manifest(staging, created_at="test")
    active = CatalogView(staging, manifest, "f" * 64)
    monkeypatch.setattr(source, "load_active_catalog", lambda: active)
    monkeypatch.setattr(source, "_probe_sources", lambda: probe)
    return active, root


def test_same_marker_repairs_only_missing_icons_and_changes_catalog_fingerprint(tmp_path, monkeypatch):
    from test_catalog_v2 import _png_bytes

    active, root = _repair_fixture(tmp_path, monkeypatch)
    calls = []
    def repair(entry, *_args, **_kwargs):
        calls.append(entry["cdragon_id"])
        return _png_bytes()
    monkeypatch.setattr(source, "_fetch_icon", repair)
    monkeypatch.setattr(source, "_write_candidate", lambda *_a, **_kw: pytest.fail("must not rebuild full source catalog"))
    result = source.refresh_catalog(pointer_output=tmp_path / "repaired.json")
    assert calls == [8001]
    assert result["changed"] is True
    assert result["content_sha256"] != active.content_sha256
    published = root / "generations" / result["catalog_generation_id"]
    identities = json.loads((published / "augment_identities.v2.json").read_text(encoding="utf-8"))
    assert identities["source_marker_sha256"] == "e" * 64
    assert all(item["icon_ready"] for item in identities["identities"])
    prior_assets = json.loads((active.root / "augment_assets.v1.json").read_text(encoding="utf-8"))["entries"]
    updated_assets = json.loads((published / "augment_assets.v1.json").read_text(encoding="utf-8"))["entries"]
    assert updated_assets[0] == prior_assets[0]


def test_in_game_missing_icon_receipt_survives_until_safe_catalog_rebuild_without_redownload(tmp_path, monkeypatch):
    from test_catalog_v2 import _png_bytes
    from hextech.modules.acquisition.champion_downloads import DownloadContext

    active, root = _repair_fixture(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(source, "_fetch_icon", lambda entry, *_a, **_kw: calls.append(entry["cdragon_id"]) or _png_bytes())
    pointer = tmp_path / "repaired.json"
    result = source.refresh_catalog(pointer_output=pointer, context=lambda: DownloadContext(in_game=True))
    assert result["state"] == "deferred"
    assert not pointer.exists()
    assert not (root / "generations").exists()
    result = source.refresh_catalog(pointer_output=pointer)
    assert result["state"] == "ready"
    assert result["catalog_generation_id"] != active.generation_id
    assert calls == [8001]


def test_missing_icon_retry_budget_rotates_past_persistent_failures(tmp_path, monkeypatch):
    _active, _root = _repair_fixture(tmp_path, monkeypatch, missing_count=6)
    calls = []
    def fail(entry, *_args, **_kwargs):
        calls.append(entry["cdragon_id"])
        raise source.CatalogRefreshError("still unavailable")
    monkeypatch.setattr(source, "_fetch_icon", fail)
    assert source.refresh_catalog()["capability_retry_pending_count"] == 6
    assert calls == [8001, 8002, 8003, 8004]
    assert source.refresh_catalog()["changed"] is False
    assert calls[4:6] == [8005, 8006]
    assert len(calls) == 8

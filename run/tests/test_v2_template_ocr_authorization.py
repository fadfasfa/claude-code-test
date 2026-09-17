"""V2 similarity is diagnostic until exact OCR, except trusted real exemplars.

The historical screenshot is used once per holdout for single-frame authorization
only. Synthetic reducer observations below are control-flow tests, not extra
captures or real-game acceptance.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hextech.infrastructure.vision import matcher, template_build, template_cache, template_runtime
from hextech.infrastructure.vision.template_models import TemplateEntry, TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION
from hextech.infrastructure.vision.state import SelectionTracker
from support.vision_events import ready_slot
from test_overlay_ocr_completed_evidence import _raw_slot, _review_event


def synthetic_v2_slot(index=0):
    slot = ready_slot(index, "1349", "终极唤醒")
    for channel in slot["channels"].values():
        for candidate in channel["top_candidates"]:
            candidate["requires_exact_ocr"] = True
    return slot


def test_synthetic_v2_consensus_cannot_vote_or_block_exact_ocr():
    slot = synthetic_v2_slot()
    assert matcher.candidate_from_slot(slot) is None
    assert matcher.strong_evidence_identities(slot) == set()
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame in (1, 2, 3):
        raw = {**_raw_slot(0, frame, ocr=True), **synthetic_v2_slot()}
        # Exact 双刀流 vs high-scoring synthetic 终极唤醒 is not a strong conflict.
        result = tracker.update(_review_event(frame, slots=[raw]))
        assert result["slots"][0]["state"] == ("ready" if frame == 3 else "detecting")
    assert result["slots"][0]["augment_id"] == "1225"


def test_synthetic_consensus_observations_cannot_self_confirm_when_ocr_unknown():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame in range(1, 8):
        raw = {**_raw_slot(0, frame), **synthetic_v2_slot()}
        result = tracker.update(_review_event(frame, slots=[raw]))
        assert result["slots"][0]["state"] == "detecting"
        assert all(item.candidate is None for item in tracker.slots[0].observations)


def test_trusted_observed_name_fast_path_keeps_original_gate_and_slots_independent():
    tracker = SelectionTracker(scene_enter_frames=1)
    for frame in (1, 2):
        observed = synthetic_v2_slot(0)
        observed["channels"]["observed_name"] = {"margin": .09, "top_candidates": [
            {"augment_id": "1349", "name": "终极唤醒", "confidence": .93, "requires_exact_ocr": True}]}
        candidate = matcher.candidate_from_slot(observed)
        assert candidate.rule == "observed_name" and candidate.evidence_grade == "strong"
        bad = {**_raw_slot(1, frame), **synthetic_v2_slot(1)}
        raw = {**_raw_slot(0, frame), **observed}
        result = tracker.update(_review_event(frame, slots=[raw, bad]))
    assert result["slots"][0]["state"] == "ready"
    assert result["slots"][1]["state"] == "detecting"
    observed["channels"]["observed_name"]["margin"] = .079
    assert matcher.candidate_from_slot(observed) is None


def test_exact_ocr_requirement_survives_cache_roundtrip_and_v5_is_invalidated(tmp_path, monkeypatch):
    from hextech.infrastructure.vision.sidecar_fingerprints import _name_fingerprint
    entry = TemplateEntry("7001", "新增强化", "Gold", "fixture", name_fingerprint=_name_fingerprint("新增强化"), requires_exact_ocr=True)
    encoded = template_cache.template_entry_to_cache(entry)
    assert template_cache.template_entry_from_cache(encoded).requires_exact_ocr is True
    monkeypatch.setattr(template_runtime, "load_default_template_index", lambda *_a, **_kw: [entry])
    path = tmp_path / "cache.npz"
    signature = {"fixture": True}
    first = template_runtime.load_or_build_default_template_runtime(cache_file=path, resource_signature=signature)
    second = template_runtime.load_or_build_default_template_runtime(cache_file=path, resource_signature=signature)
    assert first.stats["cache_hit"] is False and second.stats["cache_hit"] is True
    assert second.template_index[0].requires_exact_ocr is True
    assert TEMPLATE_RUNTIME_CACHE_SCHEMA_VERSION == 6
    # An old schema is a cache miss even when its other signatures match.
    assert template_cache.read_template_runtime_cache(path, resource_signature=signature, hint_signature="", schema_version=5) is None


@pytest.fixture(scope="module")
def archived_enabled_templates():
    from hextech.modules.acquisition.hextech.production_pool import LEGACY_SELECTION_POOL_IDS
    from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries
    root = Path(__file__).resolve().parents[1]
    aliases = {str(item.get("augment_name_id") or "").casefold(): str(item.get("cdragon_id") or "")
               for item in load_augment_manifest_entries(root / "resources/catalog")
               if str(item.get("cdragon_id") or "") in LEGACY_SELECTION_POOL_IDS}
    entries = template_build.load_default_template_entries(base_dir=root)
    # The fixture's historical legacy enabled pool, never the full audit catalog.
    return [replace(entry, requires_exact_ocr=True) for entry in entries
            if entry.augment_id.casefold() in aliases or entry.augment_id in LEGACY_SELECTION_POOL_IDS]


@pytest.mark.parametrize("hidden", [None, "心灵净化", "不动如山", "贪欲束缚"])
def test_real_historical_frame_holdout_does_not_authorize_neighbor_similarity(archived_enabled_templates, hidden):
    from hextech.infrastructure.vision.sidecar_detection import detect_overlay_choices
    path = Path(__file__).parent / "fixtures/diagnostics/overlay_vision_fixtures/hextech_20260720/frame.png"
    with Image.open(path) as source:
        frame = source.convert("RGB")
    entries = [entry for entry in archived_enabled_templates if entry.name != hidden]
    assert entries
    event = detect_overlay_choices(frame, entries)
    assert len(event["_raw_slots"]) == 3
    expected = ["心灵净化", "不动如山", "贪欲束缚"]  # Read from the actual pixels.
    for index, slot in enumerate(event["_raw_slots"]):
        candidate = matcher.candidate_from_slot(slot)
        assert candidate is None or (candidate.rule == "observed_name" and candidate.name == expected[index])
        if hidden == expected[index]:
            assert candidate is None


def test_alias_or_same_name_cannot_transfer_real_exemplar_to_a_new_id(tmp_path, monkeypatch):
    from test_catalog_identity_capabilities import pool, write_catalog
    candidate = pool()
    write_catalog(tmp_path, candidate)
    monkeypatch.setattr(template_build, "_production_catalog_root", lambda _: tmp_path)
    monkeypatch.setattr(template_build, "_attach_observed_name_exemplars", lambda entries, *_a, **_kw: [
        replace(entry, observed_name_fingerprints=(np.array([0., 1.]),)) for entry in entries])
    monkeypatch.setattr(template_build, "load_augment_manifest_entries", lambda _: [
        {"cdragon_id": 1234, "name": "新增强化", "augment_name_id": "ARAM_New"}])
    entries = template_build.load_default_template_entries(hint_cache={"source": {"production_augment_pool": candidate}})
    assert not entries[0].observed_name_fingerprints
    assert entries[0].requires_exact_ocr


def test_real_onnx_reads_historical_three_names_above_strict_gate_and_holdout_is_local(archived_enabled_templates):
    from hextech.infrastructure.vision.ocr_shadow import OnnxTextRecognizer, OCR_MODEL_FILENAME, build_ocr_vocabulary, match_ocr_text
    from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset
    from hextech.infrastructure.vision.sidecar_common import detect_selection_scene, apply_transform
    from hextech.modules.data.ports.paths import resource_path

    path = Path(__file__).parent / "fixtures/diagnostics/overlay_vision_fixtures/hextech_20260720/frame.png"
    with Image.open(path) as source:
        frame = source.convert("RGB")
    preset = resolve_roi_preset(*frame.size, preset="auto")
    scene = detect_selection_scene(frame, layout_id=preset.name)
    assert scene.present
    crops = [frame.crop(apply_transform(box, frame.size, scene.transform)) for box in preset.name_slots]
    recognized = OnnxTextRecognizer(resource_path("ocr", OCR_MODEL_FILENAME)).recognize(crops)
    expected = ["心灵净化", "不动如山", "贪欲束缚"]
    assert [text for text, confidence in recognized] == expected
    assert all(confidence >= .95 for text, confidence in recognized)
    for hidden in (None, *expected):
        vocabulary = build_ocr_vocabulary([entry for entry in archived_enabled_templates if entry.name != hidden])
        for text, confidence in recognized:
            result = match_ocr_text(text, vocabulary)
            if text == hidden:
                assert result["matched_id"] == ""
            else:
                assert result["match_rule"] == "exact", result

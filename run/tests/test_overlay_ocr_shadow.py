"""OCR Shadow 的严格词表、线程隔离、降级与诊断契约。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from hextech.infrastructure.vision.ocr_shadow import (
    OCR_MODEL_SHA256,
    OCR_MODEL_SIZE,
    OcrShadowRuntime,
    OcrVocabularyEntry,
    OnnxTextRecognizer,
    match_ocr_text,
    normalize_ocr_text,
    ocr_mode,
    ocr_input_sha256,
    prepare_ocr_crop,
)
from hextech.infrastructure.vision.sidecar_diagnostics import (
    _public_event_payload,
    _selection_timeline_entry,
)
from hextech.infrastructure.vision.sidecar_detection import detect_overlay_choices
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.infrastructure.vision.template_runtime import load_default_template_index


RUN_DIR = Path(__file__).resolve().parents[1]
MODEL_PATH = RUN_DIR / "resources" / "ocr" / "ch_PP-OCRv4_rec_infer.onnx"


def _templates() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(augment_id="1115", name="最万用的瞄准镜"),
        SimpleNamespace(augment_id="1225", name="双刀流"),
        SimpleNamespace(augment_id="1349", name="终极唤醒"),
    ]


def _vocabulary() -> tuple[OcrVocabularyEntry, ...]:
    return tuple(
        OcrVocabularyEntry(item.augment_id, item.name, normalize_ocr_text(item.name))
        for item in _templates()
    )


def _wait_for_state(runtime: OcrShadowRuntime, expected: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    status = runtime.status()
    while status["state"] != expected and time.monotonic() < deadline:
        time.sleep(0.01)
        status = runtime.status()
    return status


def test_strict_vocabulary_accepts_exact_and_unique_decorated_containment() -> None:
    exact = match_ocr_text("最万用的瞄准镜", _vocabulary())
    decorated = match_ocr_text("廣双刀流", _vocabulary())

    assert exact["matched_id"] == "1115"
    assert exact["match_rule"] == "exact"
    assert decorated["matched_id"] == "1225"
    assert decorated["match_rule"] == "unique_containment"


def test_ambiguous_or_edit_distance_only_text_never_becomes_match() -> None:
    vocabulary = (
        OcrVocabularyEntry("1", "万用瞄准镜", "万用瞄准镜"),
        OcrVocabularyEntry("2", "最万用瞄准镜", "最万用瞄准镜"),
    )

    ambiguous = match_ocr_text("x最万用瞄准镜", vocabulary)
    fuzzy_only = match_ocr_text("最万用描准镜", vocabulary)

    assert ambiguous["matched_id"] == ""
    assert ambiguous["match_rule"] == ""
    assert fuzzy_only["matched_id"] == ""
    assert fuzzy_only["top3"][0]["distance"] == 1


def test_ocr_mode_defaults_to_admit_and_legacy_boolean_maps_only_off_or_observe(monkeypatch) -> None:
    monkeypatch.delenv("HEXTECH_OCR_MODE", raising=False)
    monkeypatch.delenv("HEXTECH_OCR_SHADOW_ENABLED", raising=False)

    assert ocr_mode() == "admit"
    assert ocr_mode(legacy_enabled="0") == "off"
    assert ocr_mode(legacy_enabled="1") == "observe"
    assert ocr_mode("observe", legacy_enabled="0") == "observe"


def test_real_recognition_model_reads_three_frozen_hextech_rois() -> None:
    recognizer = OnnxTextRecognizer(MODEL_PATH)
    root = RUN_DIR / "tests" / "fixtures" / "diagnostics" / "ocr_three_names"
    images = [Image.open(root / f"name_{index}.png") for index in range(3)]

    recognized = recognizer.recognize(images)
    matches = [match_ocr_text(text, _vocabulary()) for text, _confidence in recognized]

    assert [item["matched_id"] for item in matches] == ["1115", "1225", "1349"]
    assert all(confidence >= 0.95 for _text, confidence in recognized)


def test_bright_text_crop_removes_dark_margins_and_hashes_prepared_input() -> None:
    tight = Image.new("RGB", (30, 20), "black")
    for x in range(8, 22):
        for y in range(6, 14):
            tight.putpixel((x, y), (220, 220, 220))
    wide = Image.new("RGB", (70, 20), "black")
    wide.paste(tight, (20, 0))

    prepared_tight = prepare_ocr_crop(tight)
    prepared_wide = prepare_ocr_crop(wide)

    assert prepared_tight.size == prepared_wide.size == (24, 18)
    assert prepared_tight.tobytes() == prepared_wide.tobytes()
    assert ocr_input_sha256(tight) == ocr_input_sha256(wide)


def test_real_ocr_regression_covers_eight_name_rois_fifteen_frame_slots_and_body_shards() -> None:
    import hashlib
    import json

    from hextech.infrastructure.vision.sidecar_scene_geometry import resolve_roi_preset
    from hextech.modules.vision.layout import apply_transform, detect_selection_scene

    truth = json.loads(
        (RUN_DIR / "tests" / "fixtures" / "diagnostics" / "overlay_matching_truth.v1.json").read_text(
            encoding="utf-8"
        )
    )
    expected_names = {
        str(name)
        for sample in [*truth["name_roi_samples"], *truth["samples"]]
        for name in [*(sample.get("expected_names") or []), *(sample.get("expected_slots") or [])]
        if name
    }
    vocabulary = tuple(
        OcrVocabularyEntry(str(index), name, normalize_ocr_text(name))
        for index, name in enumerate(sorted(expected_names), start=1)
    )
    recognizer = OnnxTextRecognizer(MODEL_PATH)

    accepted_roi_names: list[str] = []
    roi_confidences: list[float] = []
    roi_match_rules: list[str] = []
    expected_roi_names: list[str] = []
    for sample in truth["name_roi_samples"]:
        if sample["expected_body_shard"]:
            continue
        sample_paths = [RUN_DIR / path for path in sample["name_crops"]]
        if sample.get("source_sha256"):
            assert len(sample_paths) == 1
            assert hashlib.sha256(sample_paths[0].read_bytes()).hexdigest() == sample["source_sha256"]
        images = [Image.open(path).convert("RGB") for path in sample_paths]
        for text, confidence in recognizer.recognize(images):
            matched = match_ocr_text(text, vocabulary)
            accepted_roi_names.append(matched["matched_name"])
            roi_match_rules.append(matched["match_rule"])
            roi_confidences.append(confidence)
        expected_roi_names.extend(sample["expected_names"])

    accepted_frame_names: list[str] = []
    expected_frame_names: list[str] = []
    for sample in truth["samples"]:
        frame = Image.open(RUN_DIR / sample["frame"]).convert("RGB")
        preset = resolve_roi_preset(*frame.size, preset="auto")
        scene = detect_selection_scene(frame, layout_id=preset.name)
        boxes = [apply_transform(box, frame.size, scene.transform) for box in preset.name_slots]
        accepted_frame_names.extend(
            match_ocr_text(text, vocabulary)["matched_name"]
            for text, _confidence in recognizer.recognize([frame.crop(box) for box in boxes])
        )
        expected_frame_names.extend(sample["expected_slots"])

    shard_sample = next(sample for sample in truth["name_roi_samples"] if sample["expected_body_shard"])
    shard_images = [Image.open(RUN_DIR / path).convert("RGB") for path in shard_sample["name_crops"]]
    shard_matches = [
        match_ocr_text(text, vocabulary)["matched_name"]
        for text, _confidence in recognizer.recognize(shard_images)
    ]

    assert len(expected_roi_names) == 12
    assert accepted_roi_names == expected_roi_names
    assert roi_match_rules == ["exact"] * len(expected_roi_names)
    assert min(roi_confidences) >= 0.95
    assert "属性叠属性叠属性！" not in accepted_roi_names
    assert len(expected_frame_names) == 15
    assert accepted_frame_names == expected_frame_names
    assert shard_matches == ["", "", ""]


class _FakeRecognizer:
    model_sha256 = OCR_MODEL_SHA256

    def __init__(self, results: list[tuple[str, float]]) -> None:
        self.results = results
        self.calls = 0

    def recognize(self, images: list[Image.Image] | tuple[Image.Image, ...]) -> list[tuple[str, float]]:
        self.calls += 1
        return self.results[: len(images)]


def _production_context(
    runtime: OcrShadowRuntime,
    *,
    frame_id: int,
    epoch: int = 1,
    slot_generation: int = 1,
) -> None:
    runtime.set_production_context(
        session_id="session-ocr",
        selection_epoch=epoch,
        slot_generations=(slot_generation,),
        captured_frame_id=frame_id,
    )


@pytest.mark.parametrize(
    ("text", "confidence", "expected_state"),
    [
        ("双刀流", 0.950, "admitted"),
        ("双刀流", 0.949, "rejected"),
        ("廣双刀流", 0.999, "rejected"),
    ],
)
def test_admit_mode_allows_only_unique_exact_at_confidence_threshold(
    text: str,
    confidence: float,
    expected_state: str,
) -> None:
    recognizer = _FakeRecognizer([(text, confidence)])
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=5.0,
        recognizer_factory=lambda _path: recognizer,
    )
    image = Image.new("RGB", (160, 32), "black")
    try:
        _production_context(runtime, frame_id=1)
        runtime.observe([image], [{"evidence_fingerprint": "fp-exact"}])
        assert runtime.wait_until_idle()

        _production_context(runtime, frame_id=2)
        second = [{"evidence_fingerprint": "fp-exact"}]
        runtime.observe([image], second)

        assert second[0]["ocr_production"]["state"] == expected_state
        assert second[0]["ocr_production"]["captured_frame_id"] == 2
        assert second[0]["ocr_production"]["rgb_sha256"] == ocr_input_sha256(image)
        assert second[0]["ocr_production"]["canonical_id"] == ("1225" if expected_state == "admitted" else "")
        # production 首批不受五秒 Shadow 限频。
        assert recognizer.calls == 1
    finally:
        runtime.close()


def test_admit_cache_reuses_inference_but_binds_each_independent_epoch_frame() -> None:
    recognizer = _FakeRecognizer([("双刀流", 0.99)])
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: recognizer,
    )
    image = Image.new("RGB", (160, 32), "black")
    try:
        _production_context(runtime, frame_id=1, epoch=1)
        runtime.observe([image], [{"evidence_fingerprint": "fp-epoch"}])
        assert runtime.wait_until_idle()
        _production_context(runtime, frame_id=2, epoch=2)
        cross_epoch = [{"evidence_fingerprint": "fp-epoch"}]
        runtime.observe([image], cross_epoch)

        assert cross_epoch[0]["ocr_production"]["selection_epoch"] == 2
        assert cross_epoch[0]["ocr_production"]["captured_frame_id"] == 2
        _production_context(runtime, frame_id=3, epoch=2)
        rebound = [{"evidence_fingerprint": "fp-epoch"}]
        runtime.observe([image], rebound)
        assert rebound[0]["ocr_production"]["selection_epoch"] == 2
        assert rebound[0]["ocr_production"]["captured_frame_id"] == 3
        assert recognizer.calls == 1
    finally:
        runtime.close()


def test_worker_caches_exact_ocr_input_and_only_attaches_private_diagnostics() -> None:
    recognizer = _FakeRecognizer([("双刀流", 0.99)])
    runtime = OcrShadowRuntime(
        _templates(),
        min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: recognizer,
    )
    image = Image.new("RGB", (160, 32), "black")
    first = [{"evidence_fingerprint": "fp-1", "augment_id": "1225"}]
    try:
        runtime.observe([image], first)
        assert first[0]["ocr_shadow"]["state"] == "pending"
        assert runtime.wait_until_idle()
        second = [{"evidence_fingerprint": "fp-1", "augment_id": "1225"}]
        runtime.observe([image], second)

        assert recognizer.calls == 1
        assert second[0]["ocr_shadow"]["matched_id"] == "1225"
        assert second[0]["ocr_shadow"]["comparison"] == "agree"
        assert second[0]["ocr_shadow"]["input_sha256"] == ocr_input_sha256(image)
        assert runtime.status()["cache_hits"] == 1
    finally:
        runtime.close()


class _BlockingRecognizer(_FakeRecognizer):
    def __init__(self) -> None:
        super().__init__([("双刀流", 0.99)])
        self.started = threading.Event()
        self.release = threading.Event()

    def recognize(self, images: list[Image.Image] | tuple[Image.Image, ...]) -> list[tuple[str, float]]:
        self.calls += 1
        self.started.set()
        assert self.release.wait(5.0)
        return [("双刀流", 0.99)] * len(images)


def test_latest_only_queue_drops_displaced_batch() -> None:
    recognizer = _BlockingRecognizer()
    runtime = OcrShadowRuntime(
        _templates(),
        min_batch_interval_seconds=0.05,
        recognizer_factory=lambda _path: recognizer,
    )
    images = [
        Image.new("RGB", (160, 32), color)
        for color in ("black", "gray", "white")
    ]
    try:
        runtime.observe([images[0]], [{"evidence_fingerprint": "fp-1"}])
        assert recognizer.started.wait(5.0)
        runtime.observe([images[1]], [{"evidence_fingerprint": "fp-2"}])
        runtime.observe([images[2]], [{"evidence_fingerprint": "fp-3"}])
        recognizer.release.set()
        assert runtime.wait_until_idle()

        status = runtime.status()
        assert status["queue_drops"] == 1
        assert status["batch_calls"] == 2
        assert status["slot_calls"] == 2
        assert status["min_batch_interval_seconds"] == 0.05
    finally:
        recognizer.release.set()
        runtime.close()


def test_production_mailbox_batches_three_slots_without_cross_slot_starvation() -> None:
    recognizer = _BlockingRecognizer()
    runtime = OcrShadowRuntime(
        _templates(),
        mode="admit",
        min_batch_interval_seconds=5.0,
        recognizer_factory=lambda _path: recognizer,
    )
    images = [Image.new("RGB", (160, 32), color) for color in ("black", "gray", "white")]
    try:
        runtime.set_production_context(
            session_id="session-three-slots",
            selection_epoch=7,
            slot_generations=(1, 1, 1),
            captured_frame_id=1,
        )
        runtime.observe(
            images,
            [{"evidence_fingerprint": f"fp-{index}"} for index in range(3)],
        )
        assert recognizer.started.wait(5.0)
        recognizer.release.set()
        assert runtime.wait_until_idle()

        status = runtime.status()
        assert status["production_submitted"] == 3
        assert status["production_completed"] == 3
        assert status["production_coalesced"] == 0
        assert status["slot_starvation_count"] == 0
        assert recognizer.calls == 1
    finally:
        recognizer.release.set()
        runtime.close()


class _PixelRecognizer(_FakeRecognizer):
    def __init__(self) -> None:
        super().__init__([])

    def recognize(self, images: list[Image.Image] | tuple[Image.Image, ...]) -> list[tuple[str, float]]:
        self.calls += 1
        return [
            ("双刀流", 0.99)
            if image.convert("RGB").tobytes()[0] < 128
            else ("终极唤醒", 0.99)
            for image in images
        ]


def test_perceptual_fingerprint_collision_never_reuses_other_roi_text() -> None:
    recognizer = _PixelRecognizer()
    runtime = OcrShadowRuntime(
        _templates(),
        min_batch_interval_seconds=0.0,
        recognizer_factory=lambda _path: recognizer,
    )
    black = Image.new("RGB", (160, 32), "black")
    white = Image.new("RGB", (160, 32), "white")
    try:
        first = [{"evidence_fingerprint": "same-lossy-fingerprint", "augment_id": "1225"}]
        runtime.observe([black], first)
        assert runtime.wait_until_idle()
        first_cached = [{"evidence_fingerprint": "same-lossy-fingerprint", "augment_id": "1225"}]
        runtime.observe([black], first_cached)
        assert first_cached[0]["ocr_shadow"]["matched_id"] == "1225"

        second = [{"evidence_fingerprint": "same-lossy-fingerprint", "augment_id": "1349"}]
        runtime.observe([white], second)
        assert second[0]["ocr_shadow"]["state"] == "pending"
        assert second[0]["ocr_shadow"]["input_sha256"] != first_cached[0]["ocr_shadow"]["input_sha256"]
        assert runtime.wait_until_idle()
        second_cached = [{"evidence_fingerprint": "same-lossy-fingerprint", "augment_id": "1349"}]
        runtime.observe([white], second_cached)

        assert recognizer.calls == 2
        assert second_cached[0]["ocr_shadow"]["matched_id"] == "1349"
        assert second_cached[0]["ocr_shadow"]["comparison"] == "agree"
    finally:
        runtime.close()


@pytest.mark.parametrize("payload", [b"", b"corrupt-model"])
def test_missing_or_corrupt_model_degrades_without_raising(tmp_path: Path, payload: bytes) -> None:
    model_path = tmp_path / "missing.onnx"
    if payload:
        model_path.write_bytes(payload)
    runtime = OcrShadowRuntime(_templates(), model_path=model_path)
    try:
        status = _wait_for_state(runtime, "unavailable")
        slot = {"evidence_fingerprint": "fp"}
        runtime.observe([Image.new("RGB", (10, 10))], [slot])

        assert status["diagnostic"] == "ocr_shadow_unavailable"
        assert slot["ocr_shadow"]["state"] == "unavailable"
    finally:
        runtime.close()


def _ocr_selection_event(frame_id: int, *, template_conflict: bool = False) -> dict:
    raw_slot: dict = {
        "slot": 0,
        "evidence_fingerprint": "fp-tracker",
        "ocr_production": {
            "schema_version": 1,
            "state": "admitted",
            "session_id": "session-tracker",
            "selection_epoch": 1,
            "slot_index": 0,
            "slot_generation": 1,
            "rgb_sha256": "a" * 64,
            "perceptual_fingerprint": "fp-tracker",
            "captured_frame_id": frame_id,
            "canonical_id": "1225",
            "name": "双刀流",
            "confidence": 0.99,
            "match_rule": "exact",
            "acceptance_rule": "ocr_exact_fallback",
        },
    }
    if template_conflict:
        raw_slot["channels"] = {
            "observed_name": {
                "margin": 0.20,
                "top_candidates": [
                    {"augment_id": "1349", "name": "终极唤醒", "confidence": 0.99}
                ],
            }
        }
    return {
        "active": True,
        "selection_type": "hextech",
        "source": {
            "session_id": "session-tracker",
            "scene_present": True,
            "selection_window_active": True,
            "frame_id": frame_id,
        },
        "_raw_slots": [raw_slot],
        "timing": {"captured_at": float(frame_id), "recognition_completed_at": float(frame_id)},
    }


def test_ocr_exact_fallback_requires_three_of_five_independent_frames() -> None:
    tracker = SelectionTracker(scene_enter_frames=1)

    first = tracker.update(_ocr_selection_event(1))
    second = tracker.update(_ocr_selection_event(2))
    third = tracker.update(_ocr_selection_event(3))

    assert first["slots"][0]["state"] == "detecting"
    assert second["slots"][0]["state"] == "detecting"
    assert third["slots"][0]["state"] == "ready"
    assert third["slots"][0]["augment_id"] == "1225"
    assert third["slots"][0]["acceptance_rule"] == "ocr_exact_fallback"
    assert third["slots"][0]["observed_frames"] == 3


def test_strong_template_conflict_with_ocr_exact_fails_closed() -> None:
    tracker = SelectionTracker(scene_enter_frames=1)

    observed = [tracker.update(_ocr_selection_event(frame_id, template_conflict=True)) for frame_id in range(1, 4)]

    assert all(event["slots"][0]["state"] == "detecting" for event in observed)
    assert observed[-1]["slots"][0]["rejection_reason"] == "ocr_template_conflict"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("session_id", "other-session"),
        ("selection_epoch", 2),
        ("slot_index", 1),
        ("slot_generation", 2),
        ("rgb_sha256", "short"),
        ("perceptual_fingerprint", "other-fingerprint"),
        ("captured_frame_id", 999),
    ],
)
def test_stale_ocr_binding_never_enters_production_candidate(field: str, bad_value: object) -> None:
    tracker = SelectionTracker(scene_enter_frames=1)
    observed = []
    for frame_id in range(1, 4):
        event = _ocr_selection_event(frame_id)
        event["_raw_slots"][0]["ocr_production"][field] = bad_value
        observed.append(tracker.update(event))

    assert all(item["slots"][0]["state"] == "detecting" for item in observed)
    assert observed[-1]["slots"][0]["rejection_reason"] == "ocr_result_stale"


def test_timeline_keeps_ocr_shadow_but_public_event_does_not() -> None:
    event = {
        "selection_type": "hextech",
        "active": True,
        "source": {
            "session_id": "session-1",
            "selection_epoch": 1,
            "selection_revision": 2,
            "scene_state": "active",
        },
        "slots": [
            {
                "slot": 0,
                "state": "ready",
                "augment_id": "1225",
                "name": "双刀流",
                "slot_generation": 3,
                "acceptance_rule": "ocr_exact_fallback",
                "replacement_reason": "ocr_exact_transition",
                "rejection_reason": "",
            }
        ],
        "_raw_slots": [
            {
                "slot": 0,
                "transition_observation": "content_absent",
                "ocr_shadow": {
                    "state": "ready",
                    "raw_text": "廣双刀流",
                    "matched_id": "1225",
                    "comparison": "agree",
                    "input_sha256": "a" * 64,
                },
                "ocr_production": {
                    "state": "admitted",
                    "session_id": "session-1",
                    "selection_epoch": 1,
                    "slot_index": 0,
                    "slot_generation": 3,
                    "rgb_sha256": "b" * 64,
                    "perceptual_fingerprint": "fp-1",
                    "captured_frame_id": 9,
                    "canonical_id": "1225",
                    "confidence": 0.99,
                    "match_rule": "exact",
                    "acceptance_rule": "ocr_exact_fallback",
                },
            }
        ],
    }

    timeline = _selection_timeline_entry(event, 1)
    public = _public_event_payload(event)

    assert timeline["slots"][0]["ocr_shadow"]["raw_text"] == "廣双刀流"
    assert timeline["slots"][0]["ocr_shadow"]["input_sha256"] == "a" * 64
    assert timeline["slots"][0]["ocr_production"]["canonical_id"] == "1225"
    assert timeline["slots"][0]["ocr_production"]["captured_frame_id"] == 9
    assert timeline["slots"][0]["slot_generation"] == 3
    assert timeline["slots"][0]["replacement_reason"] == "ocr_exact_transition"
    assert timeline["slots"][0]["transition_observation"] == "content_absent"
    assert "_raw_slots" not in public
    assert "ocr_shadow" not in str(public)
    assert "ocr_production" not in str(public)


def test_shadow_on_off_keeps_production_candidate_revision_scene_and_event_identical() -> None:
    class DiagnosticOnlyShadow:
        def observe(self, _images, slots) -> None:
            for slot in slots:
                slot["ocr_shadow"] = {
                    "state": "ready",
                    "raw_text": "诊断文本",
                    "matched_id": "",
                    "comparison": "unmatched",
                }

    frame_path = RUN_DIR / "tests/fixtures/diagnostics/overlay_vision_fixtures/hextech_20260720/frame.png"
    template_index = load_default_template_index(RUN_DIR)
    with Image.open(frame_path) as source:
        frame = source.convert("RGB")
    raw_off = detect_overlay_choices(frame, template_index)
    raw_on = detect_overlay_choices(frame, template_index, ocr_shadow=DiagnosticOnlyShadow())
    trackers = (SelectionTracker(scene_enter_frames=1), SelectionTracker(scene_enter_frames=1))
    final_events = []
    for tracker, raw in zip(trackers, (raw_off, raw_on), strict=True):
        event = {}
        for sequence in range(1, 4):
            observed = dict(raw)
            observed["source"] = dict(raw["source"])
            observed["timing"] = {
                "captured_at": float(sequence),
                "recognition_completed_at": float(sequence),
            }
            event = tracker.update(observed)
        final_events.append(event)

    def production_semantics(event: dict) -> dict:
        source = event["source"]
        return {
            "active": event["active"],
            "selection_type": event["selection_type"],
            "slots": event["slots"],
            "source": {
                key: source.get(key)
                for key in (
                    "selection_epoch",
                    "selection_revision",
                    "scene_state",
                    "scene_kind",
                    "gate_state",
                    "ready_slots",
                    "content_ready",
                    "slot_states",
                    "reason",
                )
            },
        }

    assert production_semantics(final_events[0]) == production_semantics(final_events[1])
    assert "ocr_shadow" not in str(_public_event_payload(final_events[1]))


def test_model_resource_is_exact_and_manifested() -> None:
    import hashlib
    import json

    assert MODEL_PATH.stat().st_size == OCR_MODEL_SIZE
    assert hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() == OCR_MODEL_SHA256
    manifest = json.loads((RUN_DIR / "resources" / "manifest.v2.json").read_text(encoding="utf-8"))
    descriptor = next(
        item
        for item in manifest["files"]
        if item["path"] == "resources/ocr/ch_PP-OCRv4_rec_infer.onnx"
    )
    assert descriptor["package_role"] == "package"
    assert descriptor["size"] == OCR_MODEL_SIZE
    assert descriptor["sha256"] == OCR_MODEL_SHA256

    from tooling.build.manifest import build_bundle_manifest
    from tooling.build.package import PYINSTALLER_HIDDEN_IMPORTS

    bundle = build_bundle_manifest(RUN_DIR)
    bundled_model = next(item for item in bundle["ocr_resources"] if item["path"] == descriptor["path"])
    assert bundled_model["size"] == OCR_MODEL_SIZE
    assert bundled_model["sha256"] == OCR_MODEL_SHA256
    assert {"onnxruntime", "onnxruntime.capi._pybind_state"} <= set(PYINSTALLER_HIDDEN_IMPORTS)

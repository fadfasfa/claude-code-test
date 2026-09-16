"""将人工真值绑定至选择区证据并离线回放；不修改样本或晋升模板。"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from hextech.modules.session.selection_diagnostics import _reject_reparse_path


def export_labelled_candidate(group: Path, truth: dict, output: Path) -> dict:
    """显式导出人工绑定的版本化候选；从不写入识别器资产路径。"""
    from hextech.modules.data.ports.paths import get_var_dir

    allowed = (get_var_dir() / "recognition/corpus/samples").resolve()
    _reject_reparse_path(output)
    if allowed not in output.resolve().parents or output.exists():
        raise ValueError("candidate_output_must_be_new_corpus_directory")
    # Same complete evidence checks as replay; no model output manufactures truth.
    replay_group(group, truth, lambda _: {"slots": []})
    manifest = json.loads((group / "manifest.json").read_text(encoding="utf-8"))
    assets, records = {"capture-manifest.json": (group / "manifest.json").read_bytes()}, []
    for frame, annotation in zip(manifest["frames"], truth["frames"], strict=True):
        assets[frame["file"]] = (group / frame["file"]).read_bytes()
        rois = frame.get("slot_rois")
        if not isinstance(rois, list) or len(rois) != 3:
            raise ValueError("saved_slot_geometry_missing")
        for slot in rois:
            for kind in ("name", "icon"):
                descriptor = slot[kind]
                if descriptor.get("valid") is not True:
                    continue
                filename = descriptor["file"]
                if Path(filename).name != filename or filename != str(descriptor["sha256"])+".png":
                    raise ValueError("invalid_labelled_asset_path")
                image_path = group / filename
                _reject_reparse_path(image_path)
                content = image_path.read_bytes()
                if len(content) != descriptor["byte_size"] or hashlib.sha256(content).hexdigest() != descriptor["sha256"]:
                    raise ValueError("labelled_asset_hash_mismatch")
                assets[filename] = content
        for index, canonical_id in enumerate(annotation["expected_slots"]):
            if canonical_id is None:
                continue
            roi = rois[index]["name"]
            if roi.get("valid") is not True:
                raise ValueError("labelled_name_crop_invalid")
            filename = roi["file"]
            if Path(filename).name != filename or filename != str(roi["sha256"])+".png":
                raise ValueError("invalid_labelled_asset_path")
            path = group / filename
            _reject_reparse_path(path)
            content = path.read_bytes()
            if len(content) != roi["byte_size"] or hashlib.sha256(content).hexdigest() != roi["sha256"]:
                raise ValueError("labelled_asset_hash_mismatch")
            assets[filename] = content
            records.append({"canonical_id": canonical_id, "file": filename,
                            "capture_id": frame["frame_id"], "slot": index})
    if not records:
        raise ValueError("no_labelled_names")
    payload = {"schema_version": 1, "state": "candidate_requires_holdout_review",
               "automatic_exemplar_eligible": False, "truth": truth, "samples": records}
    payload["candidate_version"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    output.mkdir(parents=True, exist_ok=False)
    for filename, content in assets.items():
        with (output / filename).open("xb") as stream:
            stream.write(content)
    with (output / "candidate.json").open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return payload


def replay_group(group: Path, truth: dict, recognize) -> dict:
    """recognize(frame) 使用现有检测器；稀疏采样不得声称端到端时序验收。"""
    _reject_reparse_path(group)
    manifest_path = group / "manifest.json"
    if manifest_path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("manifest_oversize")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("owner") != "overlay-selection-cache-v2" or manifest.get("schema_version") != 2:
        raise ValueError("unsupported_selection_capture")
    digest = hashlib.sha256(raw).hexdigest()
    if truth.get("manifest_sha256") != digest or truth.get("diagnostic_id") != manifest.get("diagnostic_id"):
        raise ValueError("truth_capture_binding_mismatch")
    annotations = truth.get("frames")
    if not isinstance(annotations, list) or len(annotations) != len(manifest["frames"]):
        raise ValueError("all_saved_frames_require_human_truth")
    results, wrong, unknown, evaluated = [], 0, 0, 0
    seen = set()
    for index, (record, annotation) in enumerate(zip(manifest["frames"], annotations, strict=True)):
        expected = annotation.get("expected_slots")
        if annotation.get("frame_index") != index or not isinstance(expected, list) or len(expected) != 3:
            raise ValueError("invalid_human_frame_truth")
        if any(value is not None and (not isinstance(value, str) or not value.isdecimal()) for value in expected):
            raise ValueError("truth_must_be_canonical_id_or_null")
        identity = (record.get("game_instance_id"), record.get("frame_id"), record.get("captured_at"))
        if identity in seen or not identity[0] or not identity[1] or not identity[2]:
            raise ValueError("missing_or_duplicate_capture_identity")
        seen.add(identity)
        filename = record["file"]
        if Path(filename).name != filename or len(filename) != 68 or not filename.endswith(".png"):
            raise ValueError("invalid_image_path")
        path = group / filename
        _reject_reparse_path(path)
        if path.stat().st_size > 32 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != filename[:-4]:
            raise ValueError("image_hash_or_size_mismatch")
        width, height = record["frame_size"]
        if not (0 < width <= 7680 and 0 < height <= 4320):
            raise ValueError("invalid_frame_size")
        x1, y1, x2, y2 = record["selection_box"]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("invalid_capture_geometry")
        with Image.open(path) as image:
            if image.size != (x2-x1, y2-y1):
                raise ValueError("capture_geometry_mismatch")
            frame = Image.new("RGB", (width, height))
            frame.paste(image.convert("RGB"), (x1, y1))
        frame.info.update(hextech_roi_origin=(x1, y1), hextech_roi_size=(x2-x1, y2-y1))
        frame.info["saved_slot_rois"] = record.get("slot_rois", [])
        event = recognize(frame)
        slots = event.get("slots", [])
        actual = [str(slots[i].get("augment_id") or "") if i < len(slots) and slots[i].get("state") == "ready" else ""
                  for i in range(3)]
        for target, observed in zip(expected, actual, strict=True):
            evaluated += 1
            wrong += bool(observed and observed != target)
            unknown += bool(target is not None and not observed)
        results.append({"frame_index": index, "expected_slots": expected, "actual_slots": actual})
    return {"schema_version": 1, "diagnostic_id": manifest["diagnostic_id"], "manifest_sha256": digest,
            "truth_sha256": hashlib.sha256(json.dumps(truth, sort_keys=True).encode()).hexdigest(),
            "frames": results, "evaluated_slots": evaluated, "wrong_ready": wrong, "unknown_slots": unknown,
            "unknown_rate": unknown/evaluated if evaluated else None,
            "temporal_acceptance": False, "automatic_exemplar_eligible": False,
            "qualified": bool(evaluated and not wrong and not unknown)}


def replay_scene_group(group: Path) -> dict:
    """Read-only scene diagnosis; padding is never accepted as observed pixels."""
    from hextech.modules.vision.layout import (
        BUTTON_SEARCH_REGION, apply_transform, detect_selection_scene, pick_card_panels, LayoutTransform,
    )

    _reject_reparse_path(group / "manifest.json")
    content = (group / "manifest.json").read_bytes()
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("scene_manifest_size_exceeded")
    manifest = json.loads(content)
    records = manifest.get("frames")
    if (manifest.get("schema_version") != 2 or manifest.get("owner") != "overlay-selection-cache-v2"
        or not isinstance(records, list) or not 0 < len(records) <= 512):
        raise ValueError("invalid_scene_capture_manifest")
    frames = []
    for index, record in enumerate(records):
        filename = record["file"]
        digest = record["sha256"]
        if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest) or filename != digest + ".png"):
            raise ValueError("invalid_image_path")
        path = group / filename
        _reject_reparse_path(path)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("image_hash_or_size_mismatch")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("image_hash_or_size_mismatch")
        width, height = record["frame_size"]
        x1, y1, x2, y2 = record["selection_box"]
        if (any(type(value) is not int for value in (width, height, x1, y1, x2, y2))
            or not (0 < width <= 7680 and 0 < height <= 4320)
            or not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height)):
            raise ValueError("invalid_capture_geometry")
        def covered(box):
            return x1 <= box[0] < box[2] <= x2 and y1 <= box[1] < box[3] <= y2
        if not covered(apply_transform(BUTTON_SEARCH_REGION, (width, height), LayoutTransform())):
            raise ValueError("scene_button_search_not_captured")
        with Image.open(path) as image:
            if image.size != (x2-x1, y2-y1):
                raise ValueError("capture_geometry_mismatch")
            frame = Image.new("RGB", (width, height))
            frame.paste(image.convert("RGB"), (x1, y1))
        frame.info.update(hextech_roi_origin=(x1, y1), hextech_roi_size=(x2-x1, y2-y1))
        scene = detect_selection_scene(frame, layout_id=str(record.get("layout_id") or "saved"))
        if not all(covered(apply_transform(box, frame.size, scene.transform)) for box in pick_card_panels(frame.size)):
            raise ValueError("scene_panels_not_captured")
        frames.append({"frame_index": index, "frame_id": record.get("frame_id"),
                       "captured_at": record.get("captured_at"), "image_sha256": digest,
                       "saved_scene_state": record.get("scene_state"), "replayed_scene": asdict(scene)})
    return {"diagnostic_id": manifest["diagnostic_id"], "manifest_sha256": hashlib.sha256(content).hexdigest(),
            "evaluation_mode": "scene_only_no_identity_or_temporal_acceptance", "frames": frames,
            "qualified": False, "temporal_acceptance": False, "automatic_exemplar_eligible": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=Path, required=True)
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--catalog-id")
    parser.add_argument("--scene-only", action="store_true", help="只读回放场景结构，不运行 OCR 或产生验收结论")
    parser.add_argument("--export-candidate", type=Path)
    args = parser.parse_args()
    if args.scene_only:
        if args.export_candidate or args.truth or args.catalog_id:
            parser.error("--scene-only cannot label/export candidates or load a Catalog")
        print(json.dumps(replay_scene_group(args.group), ensure_ascii=False, indent=2))
        return
    if not args.truth or not args.catalog_id:
        parser.error("identity replay requires --truth and --catalog-id")
    from hextech.infrastructure.vision.data_source import CatalogVisionDataSource
    from hextech.infrastructure.vision.template_build import load_default_template_entries
    from hextech.infrastructure.vision.sidecar_detection import detect_overlay_choices
    from hextech.infrastructure.vision.matcher import candidate_from_slot
    from hextech.infrastructure.vision.ocr_shadow import OnnxTextRecognizer, OCR_MODEL_FILENAME, build_ocr_vocabulary, match_ocr_text
    from hextech.modules.data.ports.paths import resource_path

    hints = CatalogVisionDataSource(catalog_id=args.catalog_id).read_hint_cache()
    templates = load_default_template_entries(hint_cache=hints, require_production_pool=True)
    recognizer = OnnxTextRecognizer(resource_path("ocr", OCR_MODEL_FILENAME))
    vocabulary = build_ocr_vocabulary(templates)
    def recognize(frame):
        event = detect_overlay_choices(frame, templates)
        slots = []
        for index, raw in enumerate(event.get("_raw_slots", [])):
            candidate = candidate_from_slot(raw)
            slots.append({"state": "ready" if candidate else "detecting",
                          "augment_id": candidate.augment_id if candidate else "", "slot": index})
        if event.get("source", {}).get("scene_kind") == "hextech":
            for item in frame.info.get("saved_slot_rois", []):
                descriptor = item.get("name", {})
                index = item.get("slot")
                if descriptor.get("valid") is not True or not isinstance(index, int) or not 0 <= index < len(slots):
                    continue
                box = descriptor.get("box", [])
                if len(box) != 4:
                    raise ValueError("saved_slot_geometry_invalid")
                x, y = frame.info["hextech_roi_origin"]
                w, h = frame.info["hextech_roi_size"]
                if not (x <= box[0] < box[2] <= x+w and y <= box[1] < box[3] <= y+h):
                    raise ValueError("saved_slot_outside_capture")
                text, confidence = recognizer.recognize([frame.crop(box)])[0]
                match = match_ocr_text(text, vocabulary)
                if confidence >= .95 and match["match_rule"] == "exact":
                    template_id = slots[index]["augment_id"]
                    matched = match["matched_id"]
                    slots[index] = {"state": "ready" if not template_id or template_id == matched else "detecting",
                                    "augment_id": matched, "slot": index}
        return {"slots": slots}
    report = replay_group(args.group, json.loads(args.truth.read_text(encoding="utf-8")), recognize)
    report.update(catalog_id=args.catalog_id, evaluation_mode="single_frame_template_and_exact_ocr_not_production_ready")
    report["wrong_candidates"] = report.pop("wrong_ready")
    report["qualified"] = False
    if args.export_candidate:
        candidate = export_labelled_candidate(args.group, json.loads(args.truth.read_text(encoding="utf-8")), args.export_candidate)
        report["candidate_version"] = candidate["candidate_version"]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

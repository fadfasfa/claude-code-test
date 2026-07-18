"""一键刷新并校验 overlay 视觉识别资源。

本工具把 CommunityDragon 海克斯目录、图标、视觉模板覆盖审计、全量合成识别
和长期真机 fixture 回归收口到一个入口。默认模式只在临时 snapshot 全部通过后
才发布到稳定 `resources` 与 `tests/fixtures`；`--check-only` 只读当前资源，不联网、不改文件。

调用方: 见 import 此模块的代码; 关键依赖: catalog.version_catalog、overlay.vision、overlay.vision.state。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.modules.data.catalog.version_catalog import load_augment_manifest_entries
from hextech.infrastructure.vision import sidecar as overlay_vision_sidecar
from hextech.infrastructure.vision.state import SelectionTracker
from hextech.modules.acquisition.common.icons import normalize_augment_name
from hextech.modules.data.ports.atomic import atomic_write_json
from tooling.diagnostics import vision_eval as eval_overlay_matching
from tooling.setup import catalog as sync_cdragon_augments


RESOURCE_DIR = RUN_DIR / "resources"
VERSION_DATA_DIR = RESOURCE_DIR / "catalog"
IMAGE_DIR = RESOURCE_DIR / "assets" / "augments"
DIAGNOSTIC_DIR = RUN_DIR / "tests" / "fixtures" / "diagnostics"
TRUTH_PATH = DIAGNOSTIC_DIR / "overlay_matching_truth.v1.json"
FIXTURE_ROOT = DIAGNOSTIC_DIR / "overlay_vision_fixtures"
CATALOG_RELATIVE_PATH = Path("resources") / "catalog" / "海克斯资源目录.v1.json"
IMAGE_RELATIVE_DIR = Path("resources") / "assets" / "augments"
SYNTHETIC_SIZE = (2560, 1600)
MIN_FULL_FRAME_SAMPLE_COUNT = 5


class RefreshValidationError(RuntimeError):
    """刷新校验失败；调用方应保留当前稳定资源并返回非零退出码。"""


@dataclass(frozen=True)
class VariantCase:
    name: str
    identity: str
    filename: str
    image_path: Path


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _resource_path_allowed(path_text: str) -> bool:
    normalized = str(path_text or "").replace("\\", "/").lstrip("/")
    return normalized.startswith("tests/fixtures/diagnostics/overlay_vision_fixtures/")


def _truth_sample_paths(sample: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    frame = _clean_text(sample.get("frame"))
    if frame:
        paths.append(frame)
    name_crops = sample.get("name_crops")
    if isinstance(name_crops, Sequence) and not isinstance(name_crops, (str, bytes)):
        paths.extend(_clean_text(item) for item in name_crops if _clean_text(item))
    return paths


def validate_truth_manifest(path: Path = TRUTH_PATH, *, run_dir: Path = RUN_DIR) -> dict[str, Any]:
    """验证长期真机 fixture 只引用稳定样本目录，缺文件直接计为失败。"""

    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise RefreshValidationError(f"truth manifest 不是对象：{path}")
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    name_roi_samples = payload.get("name_roi_samples") if isinstance(payload.get("name_roi_samples"), list) else []
    retired_samples = payload.get("retired_samples") if isinstance(payload.get("retired_samples"), list) else []
    active_samples = [
        item
        for item in [*samples, *name_roi_samples]
        if isinstance(item, Mapping)
    ]
    invalid_paths: list[dict[str, str]] = []
    missing_paths: list[dict[str, str]] = []
    for sample in active_samples:
        sample_id = _clean_text(sample.get("id")) or "unnamed"
        for relative_path in _truth_sample_paths(sample):
            if not _resource_path_allowed(relative_path):
                invalid_paths.append({"id": sample_id, "path": relative_path})
                continue
            absolute_path = run_dir / relative_path
            if not absolute_path.is_file():
                missing_paths.append({"id": sample_id, "path": str(absolute_path)})

    return {
        "truth_path": str(path),
        "active_sample_count": len(active_samples),
        "full_frame_sample_count": len([item for item in samples if isinstance(item, Mapping)]),
        "name_roi_sample_count": len([item for item in name_roi_samples if isinstance(item, Mapping)]),
        "retired_sample_count": len([item for item in retired_samples if isinstance(item, Mapping)]),
        "invalid_path_count": len(invalid_paths),
        "missing_count": len(missing_paths),
        "invalid_path_sample": invalid_paths[:10],
        "missing_sample": missing_paths[:10],
    }


def _entry_icon_filename(entry: Mapping[str, Any]) -> str:
    filename = _clean_text(entry.get("filename"))
    if filename:
        return Path(filename.replace("\\", "/")).name
    local_path = _clean_text(entry.get("local_path"))
    return Path(local_path.replace("\\", "/")).name if local_path else ""


def _icon_path_for_entry(root: Path, entry: Mapping[str, Any]) -> Path:
    local_path = Path(_clean_text(entry.get("local_path")).replace("\\", "/"))
    if local_path.parts and not local_path.is_absolute() and ".." not in local_path.parts:
        candidate = root / "resources" / local_path
        if candidate.is_file():
            return candidate
    filename = _entry_icon_filename(entry)
    return root / IMAGE_RELATIVE_DIR / filename


def validate_official_catalog(root: Path) -> dict[str, Any]:
    """校验官方目录字段、稳定 ID、图标路径和可读取图标。"""

    entries = load_augment_manifest_entries(root / "resources" / "catalog")
    missing_fields: list[dict[str, str]] = []
    missing_icons: list[dict[str, str]] = []
    invalid_icons: list[dict[str, str]] = []
    ids_seen: set[str] = set()
    duplicate_ids: list[str] = []
    by_identity: dict[str, set[str]] = {}
    by_icon: dict[str, set[str]] = {}

    for entry in entries:
        name = _clean_text(entry.get("name"))
        cdragon_id = _clean_text(entry.get("cdragon_id"))
        name_id = _clean_text(entry.get("augment_name_id"))
        filename = _entry_icon_filename(entry)
        stable_id = name_id or cdragon_id
        if not name or not stable_id or not filename:
            missing_fields.append(
                {
                    "name": name,
                    "cdragon_id": cdragon_id,
                    "augment_name_id": name_id,
                    "filename": filename,
                }
            )
            continue
        if stable_id in ids_seen:
            duplicate_ids.append(stable_id)
        ids_seen.add(stable_id)
        identity = normalize_augment_name(name)
        by_identity.setdefault(identity, set()).add(filename.lower())
        by_icon.setdefault(filename.lower(), set()).add(identity)
        icon_path = _icon_path_for_entry(root, entry)
        if not icon_path.is_file():
            missing_icons.append({"name": name, "filename": filename})
            continue
        try:
            with Image.open(icon_path) as image:
                image.verify()
        except Exception as exc:  # noqa: BLE001 - 需要把 Pillow 具体异常转成摘要
            invalid_icons.append({"name": name, "filename": filename, "error": str(exc)})

    return {
        "official_record_count": len(entries),
        "identity_count": len(by_identity),
        "icon_variant_count": sum(len(value) for value in by_identity.values()),
        "shared_icon_count": sum(1 for identities in by_icon.values() if len(identities) > 1),
        "same_name_multi_icon_count": sum(1 for filenames in by_identity.values() if len(filenames) > 1),
        "missing_field_count": len(missing_fields),
        "duplicate_stable_id_count": len(duplicate_ids),
        "missing_icon_count": len(missing_icons),
        "invalid_icon_count": len(invalid_icons),
        "missing_field_sample": missing_fields[:10],
        "duplicate_stable_id_sample": duplicate_ids[:10],
        "missing_icon_sample": missing_icons[:10],
        "invalid_icon_sample": invalid_icons[:10],
    }


def _paint_selection_button(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    box = (
        int(image.size[0] * 0.445),
        int(image.size[1] * 0.775),
        int(image.size[0] * 0.555),
        int(image.size[1] * 0.813),
    )
    draw.rounded_rectangle(box, radius=14, fill="#168fcf", outline="#54d5ff", width=4)


def _paint_card_borders(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    for left, top, right, bottom in overlay_vision_sidecar.pick_card_panels(image.size):
        draw.rectangle(
            (
                int(left * image.width),
                int(top * image.height),
                int(right * image.width),
                int(bottom * image.height),
            ),
            outline="#d8b36f",
            width=8,
        )


def _paint_slot_name(image: Image.Image, box: tuple[int, int, int, int], name: str) -> None:
    left, top, right, bottom = box
    draw = ImageDraw.Draw(image)
    draw.rectangle(box, fill="#001010")
    name_mask = overlay_vision_sidecar.render_name_mask(name)
    if name_mask is None:
        return
    text_image = Image.merge("RGB", (name_mask, name_mask, name_mask))
    x = left + max(0, (right - left - text_image.width) // 2)
    y = top + max(0, (bottom - top - text_image.height) // 2)
    image.paste(text_image, (x, y))


def _build_variant_cases(root: Path) -> list[VariantCase]:
    cases: list[VariantCase] = []
    seen: set[tuple[str, str]] = set()
    for entry in load_augment_manifest_entries(root / "resources" / "catalog"):
        name = _clean_text(entry.get("name"))
        identity = normalize_augment_name(name)
        filename = _entry_icon_filename(entry)
        if not name or not identity or not filename:
            continue
        key = (identity, filename.lower())
        if key in seen:
            continue
        image_path = _icon_path_for_entry(root, entry)
        if not image_path.is_file():
            continue
        cases.append(VariantCase(name=name, identity=identity, filename=filename, image_path=image_path))
        seen.add(key)
    return sorted(cases, key=lambda item: (item.identity, item.filename))


def _variant_batches(cases: Sequence[VariantCase]) -> list[list[VariantCase]]:
    """把待测变体组三槽批次；每批尽量避免重复规范化身份。"""

    remaining = list(cases)
    fillers = list(cases)
    batches: list[list[VariantCase]] = []
    while remaining:
        batch: list[VariantCase] = []
        used_identities: set[str] = set()
        next_remaining: list[VariantCase] = []
        for case in remaining:
            if len(batch) < 3 and case.identity not in used_identities:
                batch.append(case)
                used_identities.add(case.identity)
            else:
                next_remaining.append(case)
        if len(batch) < 3:
            for filler in fillers:
                if len(batch) >= 3:
                    break
                if filler.identity in used_identities:
                    continue
                batch.append(filler)
                used_identities.add(filler.identity)
        if len(batch) < 3:
            raise RefreshValidationError("合成校验至少需要 3 个不同身份的海克斯模板。")
        batches.append(batch)
        remaining = next_remaining
    return batches


def _render_synthetic_frame(batch: Sequence[VariantCase]) -> Image.Image:
    frame = Image.new("RGB", SYNTHETIC_SIZE, "#070b12")
    _paint_selection_button(frame)
    _paint_card_borders(frame)
    preset = overlay_vision_sidecar.resolve_roi_preset(*SYNTHETIC_SIZE, preset="auto")
    scene = overlay_vision_sidecar.detect_selection_scene(frame, layout_id=preset.name)
    icon_boxes = [
        overlay_vision_sidecar.apply_transform(box, frame.size, scene.transform)
        for box in preset.slots
    ]
    name_boxes = [
        overlay_vision_sidecar.apply_transform(box, frame.size, scene.transform)
        for box in preset.name_slots
    ]
    for case, icon_box, name_box in zip(batch, icon_boxes, name_boxes):
        left, top, right, bottom = icon_box
        with Image.open(case.image_path) as icon:
            frame.paste(icon.convert("RGB").resize((right - left, bottom - top)), (left, top))
        _paint_slot_name(frame, name_box, case.name)
    return frame


def run_synthetic_recognition(root: Path) -> dict[str, Any]:
    """为每个规范化名称 + 图标变体生成三槽画面并走正式识别路径。"""

    template_index = overlay_vision_sidecar.load_default_template_index(root, hint_cache={})
    overlay_vision_sidecar.rank_template_matrices(template_index)
    cases = _build_variant_cases(root)
    batches = _variant_batches(cases)
    failures: list[dict[str, Any]] = []
    checked = 0
    with TemporaryDirectory() as temp_dir:
        calibration_path = Path(temp_dir) / "overlay_anchor_calibration.v1.json"
        for batch_index, batch in enumerate(batches):
            frame = _render_synthetic_frame(batch)
            raw_event = overlay_vision_sidecar.detect_overlay_choices(
                frame,
                template_index,
                preset_name="auto",
                min_confidence=overlay_vision_sidecar.DEFAULT_MIN_CONFIDENCE,
                calibration_path=calibration_path,
            )
            tracker = SelectionTracker()
            tracker.update(raw_event)
            event = tracker.update(raw_event)
            slots = event.get("slots") if isinstance(event.get("slots"), list) else []
            observed_identities: list[str] = []
            for slot_index, expected in enumerate(batch):
                checked += 1
                slot = slots[slot_index] if slot_index < len(slots) and isinstance(slots[slot_index], Mapping) else {}
                observed_name = _clean_text(slot.get("name"))
                observed_identity = normalize_augment_name(observed_name)
                observed_identities.append(observed_identity)
                if (
                    not event.get("active")
                    or slot.get("state") != "ready"
                    or observed_identity != expected.identity
                ):
                    failures.append(
                        {
                            "batch": batch_index,
                            "slot": slot_index,
                            "expected": expected.name,
                            "expected_filename": expected.filename,
                            "observed": observed_name,
                            "state": _clean_text(slot.get("state")),
                            "diagnostic": _clean_text(slot.get("diagnostic")),
                            "active": bool(event.get("active")),
                        }
                    )
            if len(set(observed_identities)) != len(observed_identities):
                failures.append(
                    {
                        "batch": batch_index,
                        "slot": -1,
                        "expected": "unique identities",
                        "observed": ",".join(observed_identities),
                        "state": "duplicate_identity",
                        "diagnostic": "synthetic_batch_duplicate_identity",
                    }
                )
    return {
        "synthetic_variant_count": len(cases),
        "synthetic_batch_count": len(batches),
        "synthetic_checked_count": checked,
        "synthetic_passed_count": checked - sum(1 for item in failures if int(item.get("slot", -1)) >= 0),
        "synthetic_failure_count": len(failures),
        "synthetic_failure_sample": failures[:20],
    }


def run_fixture_regression(root: Path) -> dict[str, Any]:
    truth = validate_truth_manifest(TRUTH_PATH if root == RUN_DIR else root / TRUTH_PATH.relative_to(RUN_DIR), run_dir=root)
    truth_path = Path(truth["truth_path"])
    summary = eval_overlay_matching.evaluate_truth(
        truth_path,
        min_confidence=overlay_vision_sidecar.DEFAULT_MIN_CONFIDENCE,
        base_dir=root,
        run_dir=root,
    )
    return {
        **truth,
        "fixture_sample_count": summary["sample_count"],
        "fixture_evaluated_count": summary["evaluated_count"],
        "fixture_missing_count": summary["missing_count"],
        "fixture_failure_count": len(summary["failures"]),
        "fixture_failures": summary["failures"][:20],
        "fixture_missing": summary["missing"][:20],
        "fixture_accuracy": summary["accuracy"],
    }


def validate_snapshot(root: Path) -> dict[str, Any]:
    catalog_summary = validate_official_catalog(root)
    template_audit = overlay_vision_sidecar.audit_default_template_index(root, hint_cache={})
    synthetic_summary = run_synthetic_recognition(root)
    fixture_summary = run_fixture_regression(root)
    summary = {
        **catalog_summary,
        **template_audit,
        **synthetic_summary,
        **fixture_summary,
    }
    blockers = {
        "missing_field_count": catalog_summary["missing_field_count"],
        "duplicate_stable_id_count": catalog_summary["duplicate_stable_id_count"],
        "missing_icon_count": catalog_summary["missing_icon_count"],
        "invalid_icon_count": catalog_summary["invalid_icon_count"],
        "missing_identity_count": template_audit["missing_identity_count"],
        "missing_variant_count": template_audit["missing_variant_count"],
        "synthetic_failure_count": synthetic_summary["synthetic_failure_count"],
        "full_frame_sample_deficit": max(0, MIN_FULL_FRAME_SAMPLE_COUNT - int(fixture_summary["full_frame_sample_count"] or 0)),
        "truth_missing_count": fixture_summary["missing_count"],
        "invalid_path_count": fixture_summary["invalid_path_count"],
        "fixture_missing_count": fixture_summary["fixture_missing_count"],
        "fixture_failure_count": fixture_summary["fixture_failure_count"],
    }
    summary["passed"] = all(int(value or 0) == 0 for value in blockers.values())
    summary["blockers"] = blockers
    return summary


def _copy_existing_resources_to_snapshot(snapshot_root: Path) -> None:
    version_dir = snapshot_root / "resources" / "catalog"
    image_dir = snapshot_root / "resources" / "assets"
    diagnostic_dir = snapshot_root / "tests" / "fixtures" / "diagnostics"
    version_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    if (VERSION_DATA_DIR / "海克斯资源目录.v1.json").is_file():
        shutil.copy2(VERSION_DATA_DIR / "海克斯资源目录.v1.json", version_dir / "海克斯资源目录.v1.json")
    if (RESOURCE_DIR / "assets").exists():
        shutil.copytree(RESOURCE_DIR / "assets", image_dir, dirs_exist_ok=True)
    if DIAGNOSTIC_DIR.exists():
        shutil.copytree(DIAGNOSTIC_DIR, diagnostic_dir, dirs_exist_ok=True)


def build_refresh_snapshot(
    snapshot_root: Path,
    *,
    timeout: float,
    max_workers: int,
    force_icons: bool,
) -> dict[str, Any]:
    _copy_existing_resources_to_snapshot(snapshot_root)
    return sync_cdragon_augments.sync_cdragon_augments(
        download=True,
        force_icons=force_icons,
        max_workers=max_workers,
        timeout=timeout,
        asset_dir=snapshot_root / IMAGE_RELATIVE_DIR,
        catalog_path=snapshot_root / CATALOG_RELATIVE_PATH,
        static_dir=snapshot_root / "resources" / "catalog",
    )


def _atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temp)
    temp.replace(target)


def publish_snapshot(snapshot_root: Path, *, target_run_dir: Path = RUN_DIR) -> dict[str, Any]:
    """发布 snapshot：先逐图标原子替换，最后更新目录 JSON；不删除旧图标。"""

    source_image_dir = snapshot_root / IMAGE_RELATIVE_DIR
    target_image_dir = target_run_dir / IMAGE_RELATIVE_DIR
    icon_count = 0
    for icon_path in sorted(source_image_dir.glob("*")):
        if not icon_path.is_file():
            continue
        _atomic_copy_file(icon_path, target_image_dir / icon_path.name)
        icon_count += 1

    source_catalog = snapshot_root / CATALOG_RELATIVE_PATH
    target_catalog = target_run_dir / CATALOG_RELATIVE_PATH
    catalog_payload = _read_json(source_catalog)
    atomic_write_json(target_catalog, catalog_payload, ensure_ascii=False, indent=2)
    return {
        "icon_count": icon_count,
        "catalog_path": str(target_catalog),
    }


def _compact_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "official_record_count",
        "identity_count",
        "icon_variant_count",
        "template_count",
        "text_only_template_count",
        "missing_identity_count",
        "missing_variant_count",
        "synthetic_passed_count",
        "synthetic_checked_count",
        "full_frame_sample_count",
        "name_roi_sample_count",
        "retired_sample_count",
        "fixture_evaluated_count",
        "fixture_sample_count",
        "fixture_missing_count",
        "fixture_failure_count",
        "synthetic_failure_count",
        "snapshot_retained",
        "snapshot_cleanup_error",
        "passed",
    )
    return {key: summary.get(key) for key in keys if key in summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="刷新并校验 Hextech overlay 视觉识别资源。")
    parser.add_argument("--check-only", action="store_true", help="只校验当前稳定资源，不联网、不发布。")
    parser.add_argument("--force-icons", action="store_true", help="默认刷新模式下强制重下已存在图标。")
    parser.add_argument("--timeout", type=float, default=30.0, help="CommunityDragon 请求超时秒数。")
    parser.add_argument("--max-workers", type=int, default=8, help="图标下载并发数。")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 摘要。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check_only:
            summary = validate_snapshot(RUN_DIR)
            print(json.dumps(summary if args.json else _compact_summary(summary), ensure_ascii=False, indent=2))
            return 0 if summary["passed"] else 1

        snapshot_root = RUN_DIR / "var" / "cache" / "vision" / f"overlay_recognition_refresh_{int(time.time())}"
        snapshot_root.mkdir(parents=True, exist_ok=False)
        sync_summary = build_refresh_snapshot(
            snapshot_root,
            timeout=args.timeout,
            max_workers=args.max_workers,
            force_icons=args.force_icons,
        )
        validation_summary = validate_snapshot(snapshot_root)
        summary = {
            "snapshot_root": str(snapshot_root),
            "sync": sync_summary,
            **validation_summary,
        }
        if not validation_summary["passed"]:
            print(json.dumps(summary if args.json else _compact_summary(summary), ensure_ascii=False, indent=2))
            return 1
        publish_summary = publish_snapshot(snapshot_root)
        summary["published"] = publish_summary
        cleanup_error = ""
        try:
            shutil.rmtree(snapshot_root)
        except OSError as exc:
            cleanup_error = str(exc)
        summary["snapshot_retained"] = snapshot_root.exists()
        if summary["snapshot_retained"]:
            summary["snapshot_cleanup_error"] = cleanup_error or "snapshot_cleanup_incomplete"
        print(json.dumps(summary if args.json else _compact_summary(summary), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转成诊断输出
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

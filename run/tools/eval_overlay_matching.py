from __future__ import annotations

"""用本机真机转储帧评测 overlay 图标/文字双通道匹配。

本工具只读取 `data/runtime/debug/**/frame.png` 与 `resources/诊断样例` 真值 JSON，不截图、不联网、不写事件。
runtime 样本默认不入 Git；缺样本时会报告 skipped，方便在有真机转储的机器上复跑。
"""

import argparse
import json
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.overlay.vision import sidecar as overlay_vision_sidecar
from hextech.overlay.vision.state import SelectionTracker


DEFAULT_TRUTH_PATH = RUN_DIR / "resources" / "诊断样例" / "overlay_matching_truth.v1.json"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _load_truth(path: Path) -> list[dict[str, Any]]:
    """加载真值 JSON，校验格式并标准化字段。

    真值结构：
    {
      "samples": [
        {
          "frame": "data/runtime/debug/xxx/frame.png",
          "expected_slots": ["强化名称1", "强化名称2", null],
          "expected_active": true/false,
          ...
        }
      ]
    }
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"truth payload must be an object: {path}")
    samples = payload.get("samples")
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise ValueError(f"truth payload samples must be a list: {path}")
    result: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        frame = _clean_text(sample.get("frame"))
        expected_slots = sample.get("expected_slots")
        if not frame or not isinstance(expected_slots, Sequence) or isinstance(expected_slots, (str, bytes)):
            continue
        result.append(
            {
                "id": _clean_text(sample.get("id"),) or Path(frame).parent.name,
                "frame": frame,
                "expected_slots": [None if item is None else _clean_text(item) for item in list(expected_slots)[:3]],
                "expected_active": sample.get("expected_active") if isinstance(sample.get("expected_active"), bool) else None,
                "expected_source_reason": _clean_text(sample.get("expected_source_reason")),
                "expected_ready_slots": sample.get("expected_ready_slots")
                if isinstance(sample.get("expected_ready_slots"), int)
                else None,
                "expected_ready_slots_max": sample.get("expected_ready_slots_max")
                if isinstance(sample.get("expected_ready_slots_max"), int)
                else None,
                "notes": _clean_text(sample.get("notes")),
            }
        )
    return result


def _load_name_roi_truth(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload.get("name_roi_samples") if isinstance(payload, Mapping) else None
    if samples is None:
        return []
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise ValueError(f"truth payload name_roi_samples must be a list: {path}")
    result: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        name_crops = sample.get("name_crops")
        expected_names = sample.get("expected_names")
        if (
            not isinstance(name_crops, Sequence)
            or isinstance(name_crops, (str, bytes))
            or not isinstance(expected_names, Sequence)
            or isinstance(expected_names, (str, bytes))
        ):
            continue
        result.append(
            {
                "id": _clean_text(sample.get("id")) or "name-roi",
                "name_crops": [_clean_text(item) for item in list(name_crops)[:3]],
                "expected_names": [
                    None if item is None else _clean_text(item) for item in list(expected_names)[:3]
                ],
                "expected_body_shard": bool(sample.get("expected_body_shard")),
                "notes": _clean_text(sample.get("notes")),
            }
        )
    return result


def _top_name(slot: Mapping[str, Any], channel: str) -> str:
    channels = slot.get("channels") if isinstance(slot.get("channels"), Mapping) else {}
    payload = channels.get(channel) if isinstance(channels.get(channel), Mapping) else {}
    candidates = payload.get("top_candidates") if isinstance(payload.get("top_candidates"), list) else []
    if not candidates or not isinstance(candidates[0], Mapping):
        return ""
    return _clean_text(candidates[0].get("name"))


def _ready_slot_count(event: Mapping[str, Any]) -> int:
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    try:
        return int(source.get("ready_slots"))
    except (TypeError, ValueError):
        slots = event.get("slots") if isinstance(event.get("slots"), list) else []
        return sum(
            1
            for slot in slots[:3]
            if isinstance(slot, Mapping) and slot.get("state") == "ready" and (slot.get("augment_id") or slot.get("name"))
        )


def _evaluate_sample(
    sample: Mapping[str, Any],
    *,
    template_index: Sequence[overlay_vision_sidecar.TemplateEntry],
    min_confidence: float,
) -> dict[str, Any]:
    frame_path = RUN_DIR / str(sample["frame"])
    if not frame_path.is_file():
        return {
            "id": sample["id"],
            "frame": str(frame_path),
            "status": "missing_frame",
            "expected_slots": sample["expected_slots"],
            "checks": [],
        }

    image = Image.open(frame_path)
    with TemporaryDirectory() as tmp_dir:
        raw_event = overlay_vision_sidecar.detect_overlay_choices(
            image,
            template_index,
            preset_name="auto",
            min_confidence=min_confidence,
            calibration_path=Path(tmp_dir) / "overlay_anchor_calibration.v1.json",
        )
    tracker = SelectionTracker()
    tracker.update(raw_event)
    event = tracker.update(raw_event)
    source_reason = _clean_text((event.get("source") or {}).get("reason") if isinstance(event.get("source"), Mapping) else "")
    # 阻塞弹窗的公开事件会清空槽位；匹配准确率仍应检查单帧视觉诊断槽。
    slots = raw_event.get("slots") if isinstance(raw_event.get("slots"), list) else []
    checks: list[dict[str, Any]] = []
    for index, expected in enumerate(sample["expected_slots"]):
        if not expected:
            continue
        slot = slots[index] if index < len(slots) and isinstance(slots[index], Mapping) else {}
        observed = _clean_text(slot.get("name"))
        text_top = _top_name(slot, "text")
        text_alt_top = _top_name(slot, "text_alt")
        icon_top = _top_name(slot, "icon")
        checks.append(
            {
                "slot": index,
                "expected": expected,
                "observed": observed,
                "matched": observed == expected,
                "state": _clean_text(slot.get("state")),
                "diagnostic": _clean_text(slot.get("diagnostic")),
                "confidence": slot.get("confidence"),
                "text_top": text_top,
                "text_alt_top": text_alt_top,
                "icon_top": icon_top,
            }
        )

    active_check = None
    if isinstance(sample.get("expected_active"), bool):
        expected_reason = _clean_text(sample.get("expected_source_reason"))
        active_matched = bool(event.get("active")) is bool(sample["expected_active"])
        reason_matched = not expected_reason or source_reason == expected_reason
        active_check = {
            "expected_active": bool(sample["expected_active"]),
            "observed_active": bool(event.get("active")),
            "expected_source_reason": expected_reason,
            "observed_source_reason": source_reason,
            "matched": active_matched and reason_matched,
        }

    ready_slots = _ready_slot_count(event)
    source = event.get("source") if isinstance(event.get("source"), Mapping) else {}
    scene_present = bool(source.get("scene_present") or source.get("selection_window_active"))
    scene_check = None
    if sample.get("expected_active") is True or sample.get("expected_source_reason") == "selection_scene_not_detected":
        expected_scene_present = sample.get("expected_active") is True
        scene_check = {
            "expected_scene_present": expected_scene_present,
            "observed_scene_present": scene_present,
            "matched": scene_present is expected_scene_present,
        }
    ready_slots_check = None
    expected_ready_slots = sample.get("expected_ready_slots")
    expected_ready_slots_max = sample.get("expected_ready_slots_max")
    if isinstance(expected_ready_slots, int) or isinstance(expected_ready_slots_max, int):
        exact_matched = not isinstance(expected_ready_slots, int) or ready_slots == expected_ready_slots
        max_matched = not isinstance(expected_ready_slots_max, int) or ready_slots <= expected_ready_slots_max
        ready_slots_check = {
            "expected_ready_slots": expected_ready_slots,
            "expected_ready_slots_max": expected_ready_slots_max,
            "observed_ready_slots": ready_slots,
            "matched": exact_matched and max_matched,
        }

    return {
        "id": sample["id"],
        "frame": str(frame_path),
        "status": "evaluated",
        "active": bool(event.get("active")),
        "source_reason": source_reason,
        "ready_slots": ready_slots,
        "scene_present": scene_present,
        "scene_check": scene_check,
        "active_check": active_check,
        "ready_slots_check": ready_slots_check,
        "slot_signature": list(overlay_vision_sidecar._slot_signature(event)),
        "checks": checks,
        "_event": raw_event,
    }


def _build_stability_checks(evaluated: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    active_results = [
        result
        for result in evaluated
        if bool(result.get("active")) and isinstance(result.get("_event"), Mapping)
    ]
    for result in active_results:
        tracker = SelectionTracker()
        tracker.update(result["_event"])
        stable = tracker.update(result["_event"])
        expected_ready_slots = int(result.get("ready_slots") or 0)
        checks.append(
            {
                "sample_id": result["id"],
                "kind": "partial_timeline",
                "expected_active": True,
                "observed_active": bool(stable.get("active")),
                "matched": bool(stable.get("active"))
                and _ready_slot_count(stable) == expected_ready_slots
                and len(stable.get("slots") or []) == 3,
            }
        )

    changed_pair = None
    for left_index, left in enumerate(active_results):
        for right in active_results[left_index + 1 :]:
            if left.get("slot_signature") != right.get("slot_signature"):
                changed_pair = (left, right)
                break
        if changed_pair is not None:
            break
    if changed_pair is not None:
        left, right = changed_pair
        tracker = SelectionTracker()
        tracker.update(left["_event"])
        tracker.update(left["_event"])
        stable = tracker.update(right["_event"])
        left_signature = list(left.get("slot_signature") or [])
        right_signature = list(right.get("slot_signature") or [])
        unchanged_ready = sum(
            1
            for index in range(min(3, len(left_signature), len(right_signature)))
            if left_signature[index] == right_signature[index] and left_signature[index].startswith("ready:")
        )
        expected_active = unchanged_ready >= 1
        checks.append(
            {
                "sample_id": f"{left['id']} -> {right['id']}",
                "kind": "single_slot_reroll",
                "expected_active": expected_active,
                "observed_active": bool(stable.get("active")),
                "matched": bool(stable.get("active")) is expected_active
                and _ready_slot_count(stable) == unchanged_ready,
            }
        )
    return checks


def _evaluate_name_roi_sample(
    sample: Mapping[str, Any],
    *,
    template_index: Sequence[overlay_vision_sidecar.TemplateEntry],
) -> dict[str, Any]:
    paths = [RUN_DIR / str(item) for item in sample["name_crops"]]
    missing_paths = [str(path) for path in paths if not path.is_file()]
    if missing_paths:
        return {
            "id": sample["id"],
            "status": "missing_name_roi",
            "missing_paths": missing_paths,
            "checks": [],
        }

    crops: list[Image.Image] = []
    for path in paths:
        with Image.open(path) as image:
            crops.append(image.copy())
    body_shard_scores = overlay_vision_sidecar._body_shard_name_scores(crops)
    observed_body_shard = overlay_vision_sidecar._body_shard_scene_present(body_shard_scores)
    checks: list[dict[str, Any]] = [
        {
            "kind": "body_shard",
            "expected": bool(sample["expected_body_shard"]),
            "observed": observed_body_shard,
            "matched": observed_body_shard is bool(sample["expected_body_shard"]),
        }
    ]
    observed_names: list[str] = []
    for index, crop in enumerate(crops):
        _std, ranked = overlay_vision_sidecar._rank_name_templates(crop, template_index)
        observed = ranked[0][0].name if ranked else ""
        observed_names.append(observed)
        expected = sample["expected_names"][index] if index < len(sample["expected_names"]) else None
        if expected:
            checks.append(
                {
                    "kind": "name_top1",
                    "slot": index,
                    "expected": expected,
                    "observed": observed,
                    "matched": observed == expected,
                }
            )
    return {
        "id": sample["id"],
        "status": "evaluated",
        "body_shard_scores": list(body_shard_scores),
        "observed_body_shard": observed_body_shard,
        "observed_names": observed_names,
        "checks": checks,
    }


def evaluate_truth(path: Path, *, min_confidence: float) -> dict[str, Any]:
    """加载真值 → 逐帧运行 sidecar 匹配 → 对比结果 → 生成评测报告。

    评测维度：
    - 槽位 Top1 名称匹配（slot 0/1/2 各通道 top1 是否与真值一致）
    - active 状态与 source_reason 匹配
    - ready_slots 数量匹配
    - 场景存在性匹配
    - 锻体碎片检测（name_roi 样本）
    """
    samples = _load_truth(path)
    name_roi_samples = _load_name_roi_truth(path)
    template_index = overlay_vision_sidecar.load_default_template_index(RUN_DIR)
    overlay_vision_sidecar._rank_matrices(template_index)
    results = [
        _evaluate_sample(sample, template_index=template_index, min_confidence=min_confidence)
        for sample in samples
    ]
    name_roi_results = [
        _evaluate_name_roi_sample(sample, template_index=template_index)
        for sample in name_roi_samples
    ]
    evaluated = [result for result in results if result["status"] == "evaluated"]
    missing = [result for result in results if result["status"] != "evaluated"]
    checks = [check for result in evaluated for check in result["checks"]]
    per_slot_top1 = {
        str(slot_index): {
            "expected": sum(1 for check in checks if int(check["slot"]) == slot_index),
            "matched": sum(1 for check in checks if int(check["slot"]) == slot_index and bool(check["matched"])),
        }
        for slot_index in range(3)
    }
    slot_failures = [
        check | {"sample_id": result["id"], "kind": "slot"}
        for result in evaluated
        for check in result["checks"]
        if not check["matched"]
    ]
    active_checks = [
        result["active_check"]
        for result in evaluated
        if isinstance(result.get("active_check"), Mapping)
    ]
    active_failures = [
        {
            "sample_id": result["id"],
            "kind": "active",
            "expected": f"active={result['active_check']['expected_active']} reason={result['active_check']['expected_source_reason'] or '<any>'}",
            "observed": f"active={result['active_check']['observed_active']} reason={result['active_check']['observed_source_reason'] or '<empty>'}",
        }
        for result in evaluated
        if isinstance(result.get("active_check"), Mapping) and not result["active_check"]["matched"]
    ]
    ready_slots_checks = [
        result["ready_slots_check"]
        for result in evaluated
        if isinstance(result.get("ready_slots_check"), Mapping)
    ]
    ready_slots_failures = [
        {
            "sample_id": result["id"],
            "kind": "ready_slots",
            "expected": (
                f"ready_slots={result['ready_slots_check']['expected_ready_slots']}"
                if isinstance(result["ready_slots_check"].get("expected_ready_slots"), int)
                else f"ready_slots<={result['ready_slots_check']['expected_ready_slots_max']}"
            ),
            "observed": f"ready_slots={result['ready_slots_check']['observed_ready_slots']}",
        }
        for result in evaluated
        if isinstance(result.get("ready_slots_check"), Mapping) and not result["ready_slots_check"]["matched"]
    ]
    scene_checks = [
        result["scene_check"]
        for result in evaluated
        if isinstance(result.get("scene_check"), Mapping)
    ]
    scene_failures = [
        {
            "sample_id": result["id"],
            "kind": "scene",
            "expected": f"scene_present={result['scene_check']['expected_scene_present']}",
            "observed": f"scene_present={result['scene_check']['observed_scene_present']}",
        }
        for result in evaluated
        if isinstance(result.get("scene_check"), Mapping) and not result["scene_check"]["matched"]
    ]
    stability_checks = _build_stability_checks(evaluated)
    stability_failures = [
        {
            "sample_id": check["sample_id"],
            "kind": "stability",
            "expected": f"active={check['expected_active']} ({check['kind']})",
            "observed": f"active={check['observed_active']}",
        }
        for check in stability_checks
        if not check["matched"]
    ]
    name_roi_evaluated = [result for result in name_roi_results if result["status"] == "evaluated"]
    name_roi_missing = [result for result in name_roi_results if result["status"] != "evaluated"]
    name_roi_checks = [check for result in name_roi_evaluated for check in result["checks"]]
    name_roi_failures = [
        {
            "sample_id": result["id"],
            "kind": check["kind"],
            "expected": check["expected"],
            "observed": check["observed"],
        }
        for result in name_roi_evaluated
        for check in result["checks"]
        if not check["matched"]
    ]
    failures = (
        slot_failures
        + active_failures
        + ready_slots_failures
        + scene_failures
        + stability_failures
        + name_roi_failures
    )
    public_results = [
        {key: value for key, value in result.items() if key != "_event"}
        for result in results
    ]
    return {
        "truth_path": str(path),
        "template_count": len(template_index),
        "sample_count": len(samples) + len(name_roi_samples),
        "evaluated_count": len(evaluated) + len(name_roi_evaluated),
        "missing_count": len(missing) + len(name_roi_missing),
        "frame_sample_count": len(samples),
        "name_roi_sample_count": len(name_roi_samples),
        "matched_name_roi_count": len(name_roi_checks) - len(name_roi_failures),
        "expected_name_roi_count": len(name_roi_checks),
        "expected_slot_count": len(checks),
        "matched_slot_count": len(checks) - len(slot_failures),
        "expected_active_count": len(active_checks),
        "matched_active_count": len(active_checks) - len(active_failures),
        "expected_ready_slots_count": len(ready_slots_checks),
        "matched_ready_slots_count": len(ready_slots_checks) - len(ready_slots_failures),
        "expected_scene_count": len(scene_checks),
        "matched_scene_count": len(scene_checks) - len(scene_failures),
        "expected_stability_count": len(stability_checks),
        "matched_stability_count": len(stability_checks) - len(stability_failures),
        "accuracy": (len(checks) - len(slot_failures)) / len(checks) if checks else 0.0,
        "per_slot_top1": per_slot_top1,
        "failures": failures,
        "missing": missing + name_roi_missing,
        "results": public_results,
        "name_roi_results": name_roi_results,
    }


def _print_text_summary(summary: Mapping[str, Any]) -> None:
    print(
        "overlay matching eval: "
        f"{summary['matched_slot_count']}/{summary['expected_slot_count']} slots matched, "
        f"{summary['matched_active_count']}/{summary['expected_active_count']} active checks matched, "
        f"{summary['matched_ready_slots_count']}/{summary['expected_ready_slots_count']} ready-slot checks matched, "
        f"{summary['matched_scene_count']}/{summary['expected_scene_count']} scene checks matched, "
        f"{summary['matched_stability_count']}/{summary['expected_stability_count']} stability checks matched, "
        f"{summary['matched_name_roi_count']}/{summary['expected_name_roi_count']} name-ROI checks matched, "
        f"{summary['evaluated_count']}/{summary['sample_count']} samples evaluated, "
        f"template_count={summary['template_count']}"
    )
    print(
        "per-slot top1: "
        + ", ".join(
            f"slot {slot}={counts['matched']}/{counts['expected']}"
            for slot, counts in summary["per_slot_top1"].items()
        )
    )
    if summary["missing_count"]:
        print(f"missing samples: {summary['missing_count']}")
        for item in summary["missing"][:10]:
            location = item.get("frame") or ", ".join(item.get("missing_paths") or [])
            print(f"  - {item['id']}: {location}")
    if summary["failures"]:
        print("failures:")
        for failure in summary["failures"]:
            if failure.get("kind") in {
                "active",
                "ready_slots",
                "scene",
                "stability",
                "body_shard",
                "name_top1",
            }:
                print(f"  - {failure['sample_id']} {failure['kind']}: expected={failure['expected']} observed={failure['observed']}")
            else:
                print(
                    "  - "
                    f"{failure['sample_id']} slot {failure['slot']}: "
                    f"expected={failure['expected']} observed={failure['observed'] or '<empty>'} "
                    f"text_top={failure['text_top'] or '<empty>'} icon_top={failure['icon_top'] or '<empty>'} "
                    f"state={failure['state']} diagnostic={failure['diagnostic']}"
                )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评测 overlay 视觉匹配在真机转储帧上的 top1 命中率。")
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH_PATH, help="真值 JSON 路径。")
    parser.add_argument("--min-confidence", type=float, default=overlay_vision_sidecar.DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--require-existing", action="store_true", help="缺少 runtime frame 时返回失败。")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON。")
    args = parser.parse_args(argv)

    summary = evaluate_truth(args.truth, min_confidence=args.min_confidence)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_text_summary(summary)

    if summary["failures"]:
        return 1
    if args.require_existing and summary["missing_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

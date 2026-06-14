from __future__ import annotations

"""用本机真机转储帧评测 overlay 图标/文字双通道匹配。

本工具只读取 `data/runtime/debug/**/frame.png` 与静态真值 JSON，不截图、不联网、不写事件。
runtime 样本默认不入 Git；缺样本时会报告 skipped，方便在有真机转储的机器上复跑。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

RUN_DIR = Path(__file__).resolve().parents[1]
if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from processing import overlay_vision_sidecar


DEFAULT_TRUTH_PATH = RUN_DIR / "data" / "static" / "overlay_matching_truth.v1.json"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _load_truth(path: Path) -> list[dict[str, Any]]:
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
    event = overlay_vision_sidecar.detect_overlay_choices(
        image,
        template_index,
        preset_name="auto",
        min_confidence=min_confidence,
        calibration_path=None,
    )
    slots = event.get("slots") if isinstance(event.get("slots"), list) else []
    checks: list[dict[str, Any]] = []
    for index, expected in enumerate(sample["expected_slots"]):
        if not expected:
            continue
        slot = slots[index] if index < len(slots) and isinstance(slots[index], Mapping) else {}
        observed = _clean_text(slot.get("name"))
        text_top = _top_name(slot, "text")
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
                "icon_top": icon_top,
            }
        )

    return {
        "id": sample["id"],
        "frame": str(frame_path),
        "status": "evaluated",
        "active": bool(event.get("active")),
        "source_reason": _clean_text((event.get("source") or {}).get("reason") if isinstance(event.get("source"), Mapping) else ""),
        "checks": checks,
    }


def evaluate_truth(path: Path, *, min_confidence: float) -> dict[str, Any]:
    samples = _load_truth(path)
    template_index = overlay_vision_sidecar.load_default_template_index(RUN_DIR)
    results = [
        _evaluate_sample(sample, template_index=template_index, min_confidence=min_confidence)
        for sample in samples
    ]
    evaluated = [result for result in results if result["status"] == "evaluated"]
    missing = [result for result in results if result["status"] != "evaluated"]
    checks = [check for result in evaluated for check in result["checks"]]
    failures = [check | {"sample_id": result["id"]} for result in evaluated for check in result["checks"] if not check["matched"]]
    return {
        "truth_path": str(path),
        "template_count": len(template_index),
        "sample_count": len(samples),
        "evaluated_count": len(evaluated),
        "missing_count": len(missing),
        "expected_slot_count": len(checks),
        "matched_slot_count": len(checks) - len(failures),
        "accuracy": (len(checks) - len(failures)) / len(checks) if checks else 0.0,
        "failures": failures,
        "missing": missing,
        "results": results,
    }


def _print_text_summary(summary: Mapping[str, Any]) -> None:
    print(
        "overlay matching eval: "
        f"{summary['matched_slot_count']}/{summary['expected_slot_count']} slots matched, "
        f"{summary['evaluated_count']}/{summary['sample_count']} samples evaluated, "
        f"template_count={summary['template_count']}"
    )
    if summary["missing_count"]:
        print(f"missing samples: {summary['missing_count']}")
        for item in summary["missing"][:10]:
            print(f"  - {item['id']}: {item['frame']}")
    if summary["failures"]:
        print("failures:")
        for failure in summary["failures"]:
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

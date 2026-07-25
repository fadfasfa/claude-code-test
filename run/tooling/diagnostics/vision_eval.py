"""用本机真机转储帧评测 overlay 图标/文字双通道匹配。

本工具只读取 `var/reports/**/frame.png` 与 `tests/fixtures/diagnostics` 真值 JSON，不截图、不联网、不写事件。
runtime 样本默认不入 Git；缺样本时会报告 skipped，方便在有真机转储的机器上复跑。

调用方: 见 import 此模块的代码; 关键依赖: overlay.vision、overlay.vision.state。
"""

from __future__ import annotations

import argparse
import json
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image

RUN_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = RUN_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hextech.infrastructure.vision import sidecar as overlay_vision_sidecar
from hextech.infrastructure.vision.matcher import (
    OBSERVED_NAME_CONFIDENCE,
    OBSERVED_NAME_MARGIN,
    candidate_from_slot,
)
from hextech.infrastructure.vision.state import SelectionTracker


DEFAULT_TRUTH_PATH = RUN_DIR / "tests" / "fixtures" / "diagnostics" / "overlay_matching_truth.v1.json"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _load_truth(path: Path) -> list[dict[str, Any]]:
    """加载真值 JSON，校验格式并标准化字段。

    真值结构：
    {
      "samples": [
        {
          "frame": "tests/fixtures/diagnostics/overlay_vision_fixtures/example/frame.png",
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


def _load_timeline_truth(path: Path) -> list[dict[str, Any]]:
    """加载结构化 observation 序列；静态截图不能声明为时序样本。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload.get("timeline_samples") if isinstance(payload, Mapping) else None
    if samples is None:
        return []
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise ValueError(f"truth payload timeline_samples must be a list: {path}")
    result: list[dict[str, Any]] = []
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        fixture = _clean_text(sample.get("fixture"))
        expected_slots = sample.get("expected_slots")
        expected_grades = sample.get("expected_grades")
        if (
            not fixture
            or not isinstance(expected_slots, Sequence)
            or isinstance(expected_slots, (str, bytes))
            or not isinstance(expected_grades, Sequence)
            or isinstance(expected_grades, (str, bytes))
        ):
            continue
        result.append(
            {
                "id": _clean_text(sample.get("id")) or Path(fixture).stem,
                "fixture": fixture,
                "expected_slots": [_clean_text(item) for item in list(expected_slots)[:3]],
                "expected_grades": [_clean_text(item) for item in list(expected_grades)[:3]],
                "expected_final_states": [
                    _clean_text(item) for item in list(sample.get("expected_final_states") or ["ready"] * 3)[:3]
                ],
                "expected_temporal_states": [
                    _clean_text(item) for item in list(sample.get("expected_temporal_states") or [""] * 3)[:3]
                ],
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
    run_dir: Path = RUN_DIR,
) -> dict[str, Any]:
    frame_path = run_dir / str(sample["frame"])
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
    source = raw_event.get("source") if isinstance(raw_event.get("source"), Mapping) else {}
    source_reason = _clean_text(source.get("reason"))
    # 完整帧只校验单帧候选，不再重复提交同一张图片伪造连续观察。
    observation_slots = raw_event.get("slots") if isinstance(raw_event.get("slots"), list) else []
    raw_slots = (
        raw_event.get("_raw_slots")
        if isinstance(raw_event.get("_raw_slots"), list)
        else observation_slots
    )
    checks: list[dict[str, Any]] = []
    false_ready: list[dict[str, Any]] = []
    for index, expected in enumerate(sample["expected_slots"]):
        observation_slot = (
            observation_slots[index]
            if index < len(observation_slots) and isinstance(observation_slots[index], Mapping)
            else {}
        )
        raw_slot = raw_slots[index] if index < len(raw_slots) and isinstance(raw_slots[index], Mapping) else {}
        observed = _clean_text(observation_slot.get("name"))
        observation_ready = observation_slot.get("state") == "ready"
        if observation_ready and observed != (expected or ""):
            false_ready.append({"slot": index, "expected": expected or "", "observed": observed})
        if not expected:
            continue
        text_top = _top_name(raw_slot, "text")
        text_alt_top = _top_name(raw_slot, "text_alt")
        icon_top = _top_name(raw_slot, "icon")
        channel_candidates = tuple(name for name in (text_top, text_alt_top, icon_top) if name)
        candidate_matched = expected in channel_candidates or observed == expected
        reported_candidate = observed or (expected if candidate_matched else (channel_candidates[0] if channel_candidates else ""))
        checks.append(
            {
                "slot": index,
                "expected": expected,
                "observed": reported_candidate,
                "matched": candidate_matched,
                "state": "candidate" if candidate_matched else "detecting",
                "diagnostic": _clean_text(raw_slot.get("diagnostic")),
                "confidence": observation_slot.get("confidence"),
                "text_top": text_top,
                "text_alt_top": text_alt_top,
                "icon_top": icon_top,
            }
        )

    active_check = None
    if isinstance(sample.get("expected_active"), bool):
        expected_reason = _clean_text(sample.get("expected_source_reason"))
        observed_active = bool(source.get("scene_present") or source.get("selection_window_active"))
        active_matched = observed_active is bool(sample["expected_active"])
        reason_matched = not expected_reason or source_reason == expected_reason
        active_check = {
            "expected_active": bool(sample["expected_active"]),
            "observed_active": observed_active,
            "expected_source_reason": expected_reason,
            "observed_source_reason": source_reason,
            "matched": active_matched and reason_matched,
        }

    # 历史 manifest 沿用 expected_ready_slots 字段；静态帧门禁现在统计单帧
    # 三通道 Top-1 中的正确候选数，不要求同一张图直接通过时序 ready。
    ready_slots = sum(bool(check.get("matched")) for check in checks)
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
        "active": bool(source.get("scene_present") or source.get("selection_window_active")),
        "source_reason": source_reason,
        "ready_slots": ready_slots,
        "scene_present": scene_present,
        "scene_check": scene_check,
        "active_check": active_check,
        "ready_slots_check": ready_slots_check,
        "slot_signature": [
            str(check.get("observed") or "")
            for check in checks
        ],
        "checks": checks,
        "false_ready": false_ready,
    }


def _evaluate_timeline_sample(sample: Mapping[str, Any], *, run_dir: Path) -> dict[str, Any]:
    fixture_path = run_dir / str(sample["fixture"])
    if not fixture_path.is_file():
        return {"id": sample["id"], "status": "missing_timeline", "failures": [], "fixture": str(fixture_path)}
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    observations = payload.get("observations") if isinstance(payload, Mapping) else None
    if not isinstance(observations, list) or not observations:
        return {
            "id": sample["id"],
            "status": "invalid_timeline",
            "failures": [{"kind": "timeline", "expected": "non-empty observations", "observed": "invalid"}],
        }
    tracker = SelectionTracker(scene_enter_frames=1)
    expected_slots = list(sample["expected_slots"])
    expected_grades = list(sample["expected_grades"])
    failures: list[dict[str, Any]] = []
    ready_at: list[int | None] = [None, None, None]
    valid_observation_counts = [0, 0, 0]
    previous_time = 0.0
    seen_ids: set[str] = set()

    def fixture_slot(raw: Mapping[str, Any], index: int) -> dict[str, Any]:
        if isinstance(raw.get("channels"), Mapping):
            return dict(raw)
        candidate = raw.get("candidate") if isinstance(raw.get("candidate"), Mapping) else {}
        name = _clean_text(candidate.get("name"))
        augment_id = _clean_text(candidate.get("augment_id")) or name
        grade = _clean_text(candidate.get("grade"))
        if not name or grade not in {"strong", "medium"}:
            return {"slot": index, "diagnostic": _clean_text(raw.get("diagnostic")) or "no_candidate", "channels": {}}
        top = {"augment_id": augment_id, "name": name, "confidence": 0.96 if grade == "strong" else 0.86}
        channels: dict[str, Any] = {
            "text": {"margin": 0.08, "top_candidates": [top]},
            "text_alt": {"margin": 0.07, "top_candidates": [dict(top)]},
        }
        if grade == "strong":
            channels["icon"] = {"margin": 0.04, "top_candidates": [{**top, "confidence": 0.91}]}
        return {"slot": index, "diagnostic": _clean_text(raw.get("diagnostic")), "channels": channels}

    latency_values: dict[str, list[float]] = {"capture": [], "recognition": [], "total": []}
    for sequence, observation in enumerate(observations, start=1):
        if not isinstance(observation, Mapping):
            failures.append({"kind": "timeline", "expected": "object", "observed": f"item {sequence}"})
            continue
        observation_id = _clean_text(observation.get("observation_id"))
        try:
            observed_at = float(observation.get("recognition_completed_at") or 0.0)
        except (TypeError, ValueError):
            observed_at = 0.0
        if not observation_id or observation_id in seen_ids or observed_at <= previous_time:
            failures.append(
                {
                    "kind": "timeline_order",
                    "expected": "unique observation_id and increasing timestamp",
                    "observed": f"{observation_id or '<empty>'}@{observed_at}",
                }
            )
        seen_ids.add(observation_id)
        previous_time = max(previous_time, observed_at)
        raw_slots = observation.get("slots") if isinstance(observation.get("slots"), list) else []
        observation_source = observation.get("source") if isinstance(observation.get("source"), Mapping) else {}
        try:
            capture_started_at = float(observation.get("capture_started_at") or 0.0)
            captured_at = float(observation.get("captured_at") or 0.0)
        except (TypeError, ValueError):
            capture_started_at = 0.0
            captured_at = 0.0
        for key, start, end in (
            ("capture", capture_started_at, captured_at),
            ("recognition", captured_at, observed_at),
            ("total", capture_started_at, observed_at),
        ):
            if end >= start > 0.0:
                latency_values[key].append(round((end - start) * 1000.0, 3))
        fixture_slots = [
            fixture_slot(
                raw_slots[index]
                if index < len(raw_slots) and isinstance(raw_slots[index], Mapping)
                else {},
                index,
            )
            for index in range(3)
        ]
        for index, slot in enumerate(fixture_slots):
            if candidate_from_slot(slot) is not None:
                valid_observation_counts[index] += 1
        raw_event = {
            "active": True,
            "selection_type": "hextech",
            "source": {
                "scene_present": bool(observation_source.get("scene_present", True)),
                "selection_window_active": bool(observation_source.get("selection_window_active", True)),
                "selection_button_present": bool(observation_source.get("selection_button_present", True)),
                "card_residue": bool(observation_source.get("card_residue")),
                "name_residue": list(observation_source.get("name_residue"))
                if isinstance(observation_source.get("name_residue"), list)
                else [],
                "cursor_over_slots": list(observation_source.get("cursor_over_slots"))
                if isinstance(observation_source.get("cursor_over_slots"), list)
                else [],
                "cursor_over_cards": bool(observation_source.get("cursor_over_cards")),
                "selection_click": bool(observation_source.get("selection_click")),
                "selection_confirmed": bool(observation_source.get("selection_confirmed")),
            },
            "timing": {
                "capture_started_at": capture_started_at,
                "captured_at": captured_at,
                "recognition_completed_at": observed_at,
            },
            "_raw_slots": fixture_slots,
        }
        event = tracker.update(raw_event)
        slots = event.get("slots") if isinstance(event.get("slots"), list) else []
        for index, expected in enumerate(expected_slots):
            slot = slots[index] if index < len(slots) and isinstance(slots[index], Mapping) else {}
            state = str(slot.get("state") or "detecting")
            observed_name = _clean_text(slot.get("name"))
            if state == "failed":
                failures.append({"kind": "false_failed", "slot": index, "expected": "detecting/ready", "observed": state})
            if state == "ready" and observed_name != expected:
                failures.append({"kind": "false_ready", "slot": index, "expected": expected, "observed": observed_name})
            if state == "ready" and ready_at[index] is None:
                ready_at[index] = valid_observation_counts[index]
    final_slots = event.get("slots") if isinstance(event.get("slots"), list) else []
    expected_final_states = list(sample.get("expected_final_states") or ["ready"] * 3)
    expected_temporal_states = list(sample.get("expected_temporal_states") or [""] * 3)
    for index, expected in enumerate(expected_slots):
        slot = final_slots[index] if index < len(final_slots) and isinstance(final_slots[index], Mapping) else {}
        grade = expected_grades[index] if index < len(expected_grades) else "medium"
        limit = 3 if grade == "strong" else 5
        expected_state = expected_final_states[index] if index < len(expected_final_states) else "ready"
        expected_temporal = expected_temporal_states[index] if index < len(expected_temporal_states) else ""
        if str(slot.get("state") or "") != expected_state:
            failures.append({"kind": "final_state", "slot": index, "expected": expected_state, "observed": slot.get("state")})
        elif expected and _clean_text(slot.get("name")) != expected:
            failures.append({"kind": "final_ready", "slot": index, "expected": expected, "observed": _clean_text(slot.get("name"))})
        elif expected_temporal and str(slot.get("temporal_state") or "") != expected_temporal:
            failures.append({"kind": "final_temporal", "slot": index, "expected": expected_temporal, "observed": slot.get("temporal_state")})
        elif expected_state == "ready" and (ready_at[index] is None or int(ready_at[index]) > limit):
            failures.append({"kind": "confirmation_budget", "slot": index, "expected": f"<={limit}", "observed": ready_at[index]})

    def latency_summary(values: list[float]) -> dict[str, float | int | None]:
        ordered = sorted(values)
        if not ordered:
            return {"count": 0, "p50": None, "p95": None}
        midpoint = len(ordered) // 2
        p50 = (
            ordered[midpoint]
            if len(ordered) % 2
            else round((ordered[midpoint - 1] + ordered[midpoint]) / 2.0, 3)
        )
        p95_index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95) + 0.999999) - 1))
        return {"count": len(ordered), "p50": p50, "p95": ordered[p95_index]}
    return {
        "id": sample["id"],
        "status": "evaluated",
        "fixture": str(fixture_path),
        "observation_count": len(observations),
        "ready_at": ready_at,
        "latency_ms": {key: latency_summary(values) for key, values in latency_values.items()},
        "failures": failures,
    }


def _evaluate_name_roi_sample(
    sample: Mapping[str, Any],
    *,
    template_index: Sequence[overlay_vision_sidecar.TemplateEntry],
    run_dir: Path = RUN_DIR,
) -> dict[str, Any]:
    paths = [run_dir / str(item) for item in sample["name_crops"]]
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
        fingerprint = overlay_vision_sidecar._normalized_fingerprint(
            overlay_vision_sidecar._text_levels(crop)
        )
        observed_ranked = (
            overlay_vision_sidecar._rank_observed_name_fingerprint(fingerprint, template_index)
            if fingerprint is not None
            else []
        )
        observed_margin = overlay_vision_sidecar._candidate_margin(observed_ranked)
        if (
            observed_ranked
            and observed_ranked[0][1] >= OBSERVED_NAME_CONFIDENCE
            and observed_margin >= OBSERVED_NAME_MARGIN
        ):
            observed = observed_ranked[0][0].name
        else:
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


def evaluate_truth(
    path: Path,
    *,
    min_confidence: float,
    base_dir: Path = RUN_DIR,
    run_dir: Path = RUN_DIR,
) -> dict[str, Any]:
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
    timeline_samples = _load_timeline_truth(path)
    template_index = overlay_vision_sidecar.load_default_template_index(base_dir)
    overlay_vision_sidecar._rank_matrices(template_index)
    results = [
        _evaluate_sample(sample, template_index=template_index, min_confidence=min_confidence, run_dir=run_dir)
        for sample in samples
    ]
    name_roi_results = [
        _evaluate_name_roi_sample(sample, template_index=template_index, run_dir=run_dir)
        for sample in name_roi_samples
    ]
    timeline_results = [
        _evaluate_timeline_sample(sample, run_dir=run_dir)
        for sample in timeline_samples
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
    timeline_evaluated = [result for result in timeline_results if result["status"] == "evaluated"]
    timeline_missing = [result for result in timeline_results if result["status"] != "evaluated"]
    timeline_matched = [result for result in timeline_evaluated if not result.get("failures")]
    timeline_failures = [
        dict(failure) | {"sample_id": result["id"]}
        for result in timeline_results
        for failure in result.get("failures", [])
        if isinstance(failure, Mapping)
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
        + timeline_failures
        + name_roi_failures
    )
    public_results = [
        {key: value for key, value in result.items() if key != "_event"}
        for result in results
    ]
    false_ready = [
        item | {"sample_id": result["id"]}
        for result in evaluated
        for item in (result.get("false_ready") or [])
        if isinstance(item, Mapping)
    ]
    frame_slot_accuracy = (len(checks) - len(slot_failures)) / len(checks) if checks else None
    name_roi_accuracy = (
        (len(name_roi_checks) - len(name_roi_failures)) / len(name_roi_checks)
        if name_roi_checks
        else None
    )
    return {
        "truth_path": str(path),
        "template_count": len(template_index),
        "sample_count": len(samples) + len(name_roi_samples) + len(timeline_samples),
        "evaluated_count": len(evaluated) + len(name_roi_evaluated) + len(timeline_evaluated),
        "missing_count": len(missing) + len(name_roi_missing) + len(timeline_missing),
        "frame_sample_count": len(samples),
        "name_roi_sample_count": len(name_roi_samples),
        "matched_name_roi_count": len(name_roi_checks) - len(name_roi_failures),
        "expected_name_roi_count": len(name_roi_checks),
        "expected_slot_count": len(checks),
        "matched_slot_count": len(checks) - len(slot_failures),
        "false_ready_count": len(false_ready),
        "false_ready": false_ready,
        "expected_active_count": len(active_checks),
        "matched_active_count": len(active_checks) - len(active_failures),
        "expected_ready_slots_count": len(ready_slots_checks),
        "matched_ready_slots_count": len(ready_slots_checks) - len(ready_slots_failures),
        "expected_scene_count": len(scene_checks),
        "matched_scene_count": len(scene_checks) - len(scene_failures),
        "timeline_sample_count": len(timeline_samples),
        "matched_timeline_count": len(timeline_matched),
        "frame_slot_accuracy": frame_slot_accuracy,
        "name_roi_accuracy": name_roi_accuracy,
        # 兼容既有诊断消费者；不再用 0.0 冒充“没有完整帧”。
        "accuracy": frame_slot_accuracy,
        "per_slot_top1": per_slot_top1,
        "failures": failures,
        "missing": missing + name_roi_missing + timeline_missing,
        "results": public_results,
        "name_roi_results": name_roi_results,
        "timeline_results": timeline_results,
    }


def _print_text_summary(summary: Mapping[str, Any]) -> None:
    print(
        "overlay matching eval: "
        f"{summary['matched_slot_count']}/{summary['expected_slot_count']} slots matched, "
        f"{summary['matched_active_count']}/{summary['expected_active_count']} active checks matched, "
        f"{summary['matched_ready_slots_count']}/{summary['expected_ready_slots_count']} ready-slot checks matched, "
        f"{summary['matched_scene_count']}/{summary['expected_scene_count']} scene checks matched, "
        f"{summary['matched_timeline_count']}/{summary['timeline_sample_count']} timeline samples matched, "
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
    print(
        "accuracy: "
        f"frame_slot={summary.get('frame_slot_accuracy')}, "
        f"name_roi={summary.get('name_roi_accuracy')}, "
        f"false_ready={summary.get('false_ready_count')}"
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
                "timeline",
                "timeline_order",
                "false_failed",
                "false_ready",
                "final_ready",
                "confirmation_budget",
                "final_state",
                "final_temporal",
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
    parser.add_argument("--base-dir", type=Path, default=RUN_DIR, help="模板和资源根目录，默认当前 run。")
    parser.add_argument("--run-dir", type=Path, default=RUN_DIR, help="真机样本相对路径根目录，默认当前 run。")
    parser.add_argument("--min-confidence", type=float, default=overlay_vision_sidecar.DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--require-existing", action="store_true", help="缺少 runtime frame 时返回失败。")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON。")
    args = parser.parse_args(argv)

    summary = evaluate_truth(args.truth, min_confidence=args.min_confidence, base_dir=args.base_dir, run_dir=args.run_dir)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_text_summary(summary)

    if summary["failures"]:
        return 1
    if summary["false_ready_count"]:
        return 1
    if args.require_existing and summary["missing_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

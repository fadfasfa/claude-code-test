"""游戏内显示性能验收摘要工具。

本工具只生成结构化性能报告，供阶段 5 人工记录四种服务状态和延迟样本。默认不写
运行态文件、不启动服务、不访问网络。

调用方: dev_checks; 关键依赖: 见 imports。
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


SERVICE_STATE_KEYS = ("all_off", "web_only", "game_overlay_only", "web_and_overlay")
MIN_CAPTURED_OBSERVATIONS = 50
MIN_PRESENTATION_SAMPLES = 20
MIN_REROLL_TRANSITIONS = 15
CAPTURE_RECOGNITION_P95_TARGET_MS = 180.0
FIRST_FEEDBACK_TARGET_MS = 300.0
FIRST_FULL_CANVAS_TARGET_MS = 900.0
PRESENTATION_P95_TARGET_MS = 100.0
REROLL_READY_TARGET_MS = 900.0


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(len(ordered) * max(0.0, min(100.0, percentile)) / 100.0))
    return round(ordered[min(len(ordered) - 1, rank - 1)], 3)


def _normalize_service_sample(sample: Mapping[str, Any] | None) -> dict[str, float]:
    source = sample if isinstance(sample, Mapping) else {}
    return {
        "rss_mb": round(_coerce_float(source.get("rss_mb")), 3),
        "cpu_percent": round(_coerce_float(source.get("cpu_percent")), 3),
    }


def build_overlay_performance_report(
    *,
    service_samples: Mapping[str, Mapping[str, Any]] | None = None,
    latency_samples_ms: Sequence[float] | None = None,
    recognition_samples_ms: Sequence[float] | None = None,
    render_samples_ms: Sequence[float] | None = None,
    source_tag: str = "manual",
) -> dict[str, Any]:
    """生成阶段 5 性能记录结构；真实采样由人工或后续自动探针填入。"""

    services = service_samples if isinstance(service_samples, Mapping) else {}
    latency_samples = [float(value) for value in (latency_samples_ms or [])]
    recognition_samples = [float(value) for value in (recognition_samples_ms or [])]
    render_samples = [float(value) for value in (render_samples_ms or [])]
    overlay_p95_ms = _percentile(latency_samples, 95)
    return {
        "generated_at": time.time(),
        "source": {"tag": str(source_tag or "manual")},
        "service_states": {
            key: _normalize_service_sample(services.get(key))
            for key in SERVICE_STATE_KEYS
        },
        "latency": {
            "samples_ms": [round(value, 3) for value in latency_samples],
            "count": len(latency_samples),
            "avg_ms": round(statistics.fmean(latency_samples), 3) if latency_samples else 0.0,
            "p50_ms": _percentile(latency_samples, 50),
            "p95_ms": overlay_p95_ms,
            "pass_p95": bool(latency_samples and overlay_p95_ms <= FIRST_FULL_CANVAS_TARGET_MS),
            "segments": {
                "recognition": _latency_summary(
                    recognition_samples,
                    target_p95_ms=CAPTURE_RECOGNITION_P95_TARGET_MS,
                ),
                "render": _latency_summary(render_samples, target_p95_ms=PRESENTATION_P95_TARGET_MS),
            },
        },
        "targets": {
            "recognition_p95_ms": CAPTURE_RECOGNITION_P95_TARGET_MS,
            "overlay_p95_ms": FIRST_FULL_CANVAS_TARGET_MS,
            "render_p95_ms": PRESENTATION_P95_TARGET_MS,
        },
        "warm_path_only": True,
        "manual_acceptance_required": True,
    }


def _latency_summary(samples: Sequence[float], *, target_p95_ms: float) -> dict[str, Any]:
    p95_ms = _percentile(samples, 95)
    return {
        "samples_ms": [round(float(value), 3) for value in samples],
        "count": len(samples),
        "p50_ms": _percentile(samples, 50),
        "p95_ms": p95_ms,
        "target_p95_ms": float(target_p95_ms),
        "pass_p95": bool(samples and p95_ms <= float(target_p95_ms)),
    }


def _read_timeline(path: Path) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError(f"时间线第 {line_number} 行不是 JSON object：{path}")
        result.append(payload)
    return result


def _read_reports(path: Path) -> list[Mapping[str, Any]]:
    files = [path] if path.is_file() else sorted(path.glob("overlay-session-*.json"))
    result: list[Mapping[str, Any]] = []
    for file in files:
        payload = json.loads(file.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            result.append(payload)
    return result


def _full_ready_report(report: Mapping[str, Any], *, epoch: int) -> bool:
    event = report.get("event") if isinstance(report.get("event"), Mapping) else {}
    source = report.get("source") if isinstance(report.get("source"), Mapping) else {}
    render = report.get("render") if isinstance(report.get("render"), Mapping) else {}
    rows = render.get("rows") if isinstance(render.get("rows"), list) else []
    return bool(
        event.get("visible") is True
        and int(_coerce_float(source.get("selection_epoch"))) == epoch
        and len(rows) >= 3
        and all(
            isinstance(row, Mapping) and str(row.get("status_code") or "") == "READY"
            for row in rows[:3]
        )
    )


def _report_timing(report: Mapping[str, Any]) -> Mapping[str, Any]:
    value = report.get("timing")
    return value if isinstance(value, Mapping) else {}


def _bound_presentation_records(
    reports: Sequence[Mapping[str, Any]],
    *,
    session_id: str,
) -> list[dict[str, Any]]:
    """把 composed 时间绑定到真正触发该次 Canvas 绘制的事件。

    新报告直接携带 ``presented_event_written_at``。旧报告的 composition 回调
    会再次读取事件文件，因此同一份报告里的 event 时间可能比 presented_at
    更新；旧格式只能用绘制报告和 composed 报告共享的 draw_completed_at 回配。
    """

    selected = [report for report in reports if str(report.get("session_id") or "") == session_id]
    by_draw_completed: dict[float, list[Mapping[str, Any]]] = {}
    for report in selected:
        completed = _coerce_float(_report_timing(report).get("draw_completed_at"))
        if completed > 0.0:
            by_draw_completed.setdefault(completed, []).append(report)

    records: list[dict[str, Any]] = []
    seen: set[tuple[float, float]] = set()
    for report in selected:
        timing = _report_timing(report)
        presented = _coerce_float(timing.get("presented_at"))
        if presented <= 0.0:
            continue
        presentation = report.get("presentation")
        presentation_state = presentation if isinstance(presentation, Mapping) else {}
        written = _coerce_float(timing.get("presented_event_written_at"))
        bound_report = report
        if written <= 0.0:
            completed = _coerce_float(timing.get("draw_completed_at"))
            candidates = []
            for candidate in by_draw_completed.get(completed, []):
                candidate_timing = _report_timing(candidate)
                host_read = _coerce_float(candidate_timing.get("host_read_at"))
                draw_started = _coerce_float(candidate_timing.get("draw_started_at"))
                candidate_written = _coerce_float(candidate_timing.get("event_written_at"))
                if candidate_written > 0.0 and draw_started >= host_read > 0.0:
                    candidates.append(candidate)
            if candidates:
                bound_report = min(
                    candidates,
                    key=lambda candidate: _coerce_float(_report_timing(candidate).get("host_read_at")),
                )
                written = _coerce_float(_report_timing(bound_report).get("event_written_at"))
            else:
                # 简单 fixture/早期报告没有绘制时间；保留同报告回退，但拒绝
                # presented 早于写入的无效配对。
                written = _coerce_float(timing.get("event_written_at"))
        if written <= 0.0 or presented < written:
            continue
        pair = (written, presented)
        if pair in seen:
            continue
        seen.add(pair)
        bound_timing = _report_timing(bound_report)
        bound_source = bound_report.get("source")
        source = bound_source if isinstance(bound_source, Mapping) else {}
        epoch = int(
            _coerce_float(
                presentation_state.get("event_selection_epoch")
                or source.get("selection_epoch")
            )
        )
        explicit_ready = "ready_frame" in presentation_state
        ready_frame = bool(presentation_state.get("ready_frame")) if explicit_ready else _full_ready_report(
            bound_report,
            epoch=epoch,
        )
        records.append(
            {
                "event_written_at": written,
                "host_read_at": _coerce_float(bound_timing.get("host_read_at")),
                "draw_started_at": _coerce_float(bound_timing.get("draw_started_at")),
                "draw_completed_at": _coerce_float(bound_timing.get("draw_completed_at")),
                "presented_at": presented,
                "selection_epoch": epoch,
                "selection_revision": int(_coerce_float(source.get("selection_revision"))),
                "ready_frame": ready_frame,
                "slots": [dict(item) for item in bound_report.get("slots", []) if isinstance(item, Mapping)]
                if isinstance(bound_report.get("slots"), list)
                else [],
            }
        )
    return records


def _first_event_presentations(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """每个原事件只计首次呈现；同事件后续内容升级仍留在原记录供三槽/重随验收。"""
    first: dict[tuple[int, float], Mapping[str, Any]] = {}
    for record in records:
        key = (int(record["selection_epoch"]), float(record["event_written_at"]))
        if key not in first or float(record["presented_at"]) < float(first[key]["presented_at"]):
            first[key] = record
    return list(first.values())


def _recognition_observation_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """同一真实帧只计一次；优先使用 sidecar 单调 observation_seq。"""

    epoch = int(_coerce_float(item.get("selection_epoch")))
    sequence = int(_coerce_float(item.get("observation_seq")))
    if sequence > 0:
        return (epoch, "sequence", sequence)
    return (
        epoch,
        "timing",
        _coerce_float(item.get("capture_started_at")),
        _coerce_float(item.get("captured_at")),
        _coerce_float(item.get("recognition_completed_at")),
    )


def _qualify_hextech_epochs(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[set[int], dict[str, int]]:
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for item in observations:
        epoch = int(_coerce_float(item.get("selection_epoch")))
        if epoch > 0:
            grouped.setdefault(epoch, []).append(item)
    eligible: set[int] = set()
    excluded: dict[str, int] = {}
    for items in grouped.values():
        active_hextech = any(
            str(item.get("selection_type") or "") == "hextech"
            and str(item.get("scene_state") or "") == "active"
            and item.get("selection_window_active") is True
            for item in items
        )
        if active_hextech:
            eligible.add(int(_coerce_float(items[0].get("selection_epoch"))))
            continue
        selection_types = {str(item.get("selection_type") or "") for item in items}
        scene_states = {str(item.get("scene_state") or "") for item in items}
        observation_kinds = {str(item.get("observation_kind") or "") for item in items}
        if "body_shard" in selection_types:
            reason = "body_shard"
        elif "blocked" in scene_states:
            reason = "blocked"
        elif "candidate" in scene_states:
            reason = "candidate_only"
        elif observation_kinds <= {"visibility_probe", "capture_failure"}:
            reason = "probe_or_pause_only"
        elif "hextech" not in selection_types:
            reason = "non_hextech"
        else:
            reason = "inactive_hextech"
        excluded[reason] = excluded.get(reason, 0) + 1
    return eligible, excluded


def _runtime_root_from_reports(path: Path) -> Path | None:
    report_dir = path.parent if path.is_file() else path
    if report_dir.name == "overlay_sessions" and report_dir.parent.name == "reports":
        return report_dir.parent.parent
    return None


def _pixel_evidence_summary(
    reports_path: Path,
    *,
    session_id: str,
    eligible_epochs: set[int],
) -> dict[str, Any]:
    runtime_root = _runtime_root_from_reports(reports_path)
    if runtime_root is None:
        return {"count": 0, "epoch_count": 0, "eligible_epoch_count": len(eligible_epochs)}
    evidence_root = runtime_root / "state" / "session_evidence"
    observed_epochs: set[int] = set()
    screenshot_count = 0
    for path in evidence_root.glob("overlay-*.v2.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping) or str(payload.get("session_id") or "") != session_id:
            continue
        vision = payload.get("vision") if isinstance(payload.get("vision"), Mapping) else {}
        epoch = int(_coerce_float(vision.get("epoch")))
        screenshot_name = str(payload.get("screenshot") or "")
        if epoch in eligible_epochs and screenshot_name and (evidence_root / Path(screenshot_name).name).is_file():
            observed_epochs.add(epoch)
            screenshot_count += 1
    return {
        "count": screenshot_count,
        "epoch_count": len(observed_epochs),
        "eligible_epoch_count": len(eligible_epochs),
    }


def _slot_starvation_summary(reports_path: Path) -> dict[str, Any]:
    runtime_root = _runtime_root_from_reports(reports_path)
    if runtime_root is None:
        return {"available": False, "count": None, "passed": True}
    try:
        payload = json.loads(
            (runtime_root / "state" / "game_overlay_sidecar_status.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"available": False, "count": None, "passed": True}
    ocr = payload.get("ocr_shadow") if isinstance(payload, Mapping) else {}
    count = int(_coerce_float(ocr.get("slot_starvation_count"))) if isinstance(ocr, Mapping) else 0
    return {"available": True, "count": count, "passed": count == 0}


def _slot_at(items: object, index: int) -> Mapping[str, Any]:
    if not isinstance(items, list):
        return {}
    for item in items:
        if isinstance(item, Mapping) and int(_coerce_float(item.get("slot"), -1.0)) == index:
            return item
    return items[index] if 0 <= index < len(items) and isinstance(items[index], Mapping) else {}


def _reroll_refresh_summary(
    observations: Sequence[Mapping[str, Any]],
    presentation_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    transitions: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in observations:
        epoch = int(_coerce_float(item.get("selection_epoch")))
        sequence = int(_coerce_float(item.get("mouse_event_sequence")))
        observed_at = _coerce_float(item.get("mouse_event_observed_at"))
        try:
            slot_index = int(item.get("transition_slot"))
        except (TypeError, ValueError):
            slot_index = -1
        if (
            epoch <= 0
            or sequence <= 0
            or observed_at <= 0.0
            or slot_index not in range(3)
            or str(item.get("transition_source") or "") != "async_mouse_down"
            or (epoch, sequence) in seen
        ):
            continue
        target = _slot_at(item.get("slots"), slot_index)
        generation = int(_coerce_float(target.get("slot_generation")))
        if generation <= 0:
            continue
        seen.add((epoch, sequence))
        unchanged = {
            index: (
                int(_coerce_float(_slot_at(item.get("slots"), index).get("slot_generation"))),
                str(_slot_at(item.get("slots"), index).get("augment_id") or ""),
            )
            for index in range(3)
            if index != slot_index
        }
        transitions.append(
            {
                "selection_epoch": epoch,
                "mouse_event_sequence": sequence,
                "mouse_event_observed_at": observed_at,
                "event_written_at": _coerce_float(item.get("event_written_at")),
                "transition_slot": slot_index,
                "slot_generation": generation,
                "unchanged": unchanged,
            }
        )

    transition_event_samples: list[float] = []
    detecting_samples: list[float] = []
    ready_samples: list[float] = []
    feedback_samples: list[float] = []
    missed = 0
    unchanged_mutations = 0
    old_generation_reappearances = 0
    details: list[dict[str, Any]] = []
    ordered_presentations = sorted(
        presentation_records,
        key=lambda item: _coerce_float(item.get("presented_at")),
    )
    for transition in transitions:
        observed_at = float(transition["mouse_event_observed_at"])
        written_at = float(transition["event_written_at"])
        epoch = int(transition["selection_epoch"])
        slot_index = int(transition["transition_slot"])
        generation = int(transition["slot_generation"])
        if written_at >= observed_at:
            transition_event_samples.append((written_at - observed_at) * 1000.0)
        candidates = [
            item
            for item in ordered_presentations
            if int(_coerce_float(item.get("selection_epoch"))) == epoch
            and _coerce_float(item.get("presented_at")) >= observed_at
        ]
        detecting_at = 0.0
        ready_at = 0.0
        mutation = False
        reappearance = False
        for item in candidates:
            target = _slot_at(item.get("slots"), slot_index)
            target_generation = int(_coerce_float(target.get("slot_generation")))
            target_state = str(target.get("state") or "")
            presented_at = _coerce_float(item.get("presented_at"))
            if target_generation < generation:
                reappearance = True
            if target_generation >= generation and target_state != "ready" and detecting_at <= 0.0:
                detecting_at = presented_at
            if (
                target_generation == generation
                and target_state == "ready"
                and str(target.get("augment_id") or "")
            ):
                ready_at = presented_at
                break
            for other_index, baseline in transition["unchanged"].items():
                other = _slot_at(item.get("slots"), int(other_index))
                identity = (
                    int(_coerce_float(other.get("slot_generation"))),
                    str(other.get("augment_id") or ""),
                )
                if identity != baseline:
                    mutation = True
        feedback_at = detecting_at or ready_at
        if detecting_at > 0.0:
            detecting_samples.append((detecting_at - observed_at) * 1000.0)
        if feedback_at > 0.0:
            feedback_samples.append((feedback_at - observed_at) * 1000.0)
        if ready_at > 0.0:
            ready_samples.append((ready_at - observed_at) * 1000.0)
        else:
            missed += 1
        unchanged_mutations += int(mutation)
        old_generation_reappearances += int(reappearance)
        details.append(
            {
                "selection_epoch": epoch,
                "mouse_event_sequence": int(transition["mouse_event_sequence"]),
                "transition_slot": slot_index,
                "slot_generation": generation,
                "transition_event_ms": round((written_at - observed_at) * 1000.0, 3)
                if written_at >= observed_at
                else None,
                "detecting_presented_ms": round((detecting_at - observed_at) * 1000.0, 3)
                if detecting_at > 0.0
                else None,
                "ready_presented_ms": round((ready_at - observed_at) * 1000.0, 3)
                if ready_at > 0.0
                else None,
            }
        )
    count = len(transitions)
    transition_event = _latency_summary(
        transition_event_samples,
        target_p95_ms=PRESENTATION_P95_TARGET_MS,
    )
    detecting = _latency_summary(
        detecting_samples,
        target_p95_ms=FIRST_FEEDBACK_TARGET_MS,
    )
    feedback = _latency_summary(
        feedback_samples,
        target_p95_ms=FIRST_FEEDBACK_TARGET_MS,
    )
    ready = _latency_summary(ready_samples, target_p95_ms=REROLL_READY_TARGET_MS)
    passed = bool(
        count >= MIN_REROLL_TRANSITIONS
        and len(transition_event_samples) == count
        and len(feedback_samples) == count
        and len(ready_samples) == count
        and transition_event["p95_ms"] <= PRESENTATION_P95_TARGET_MS
        and feedback["p95_ms"] <= FIRST_FEEDBACK_TARGET_MS
        and ready["p95_ms"] <= REROLL_READY_TARGET_MS
        and missed == 0
        and unchanged_mutations == 0
        and old_generation_reappearances == 0
    )
    return {
        "count": count,
        "minimum_count": MIN_REROLL_TRANSITIONS,
        "mouse_down_to_transition_event": transition_event,
        "mouse_down_to_detecting_presented": detecting,
        "mouse_down_to_feedback_presented": feedback,
        "mouse_down_to_new_ready_presented": ready,
        "unchanged_slot_mutation_count": unchanged_mutations,
        "old_generation_reappearance_count": old_generation_reappearances,
        "missed_transition_count": missed,
        "transitions": details,
        "passed": passed,
    }


def build_real_session_performance_report(
    timeline_path: str | Path,
    host_reports_path: str | Path,
    *,
    session_id: str = "",
    expected_build_id: str = "",
) -> dict[str, Any]:
    """从真实 timeline/report 计算锁定的三道 Overlay 性能门。"""

    timeline = Path(timeline_path)
    reports_path = Path(host_reports_path)
    observations = _read_timeline(timeline)
    expected_build = str(expected_build_id or "").strip()
    if expected_build:
        observations = [item for item in observations if str(item.get("build_id") or "") == expected_build]
        if not observations:
            raise ValueError(f"时间线没有目标 Build 记录：{expected_build}")
        sidecar_instances = {
            str(item.get("sidecar_instance_id") or "")
            for item in observations
            if str(item.get("sidecar_instance_id") or "")
        }
        if len(sidecar_instances) != 1:
            raise ValueError("目标 Build 时间线必须只来自一个 Sidecar instance")
    sessions = {
        str(item.get("session_id") or item.get("game_instance_id") or "")
        for item in observations
        if str(item.get("session_id") or item.get("game_instance_id") or "")
    }
    selected = str(session_id or "").strip()
    if not selected:
        if len(sessions) != 1:
            raise ValueError("时间线必须只包含一个 session，或显式提供 --session-id")
        selected = next(iter(sessions))
    session_observations = [
        item
        for item in observations
        if str(item.get("session_id") or item.get("game_instance_id") or "") == selected
    ]
    eligible_epochs, excluded_epochs = _qualify_hextech_epochs(session_observations)
    captured_candidates = [
        item
        for item in session_observations
        if int(_coerce_float(item.get("selection_epoch"))) in eligible_epochs
        and str(item.get("observation_kind") or "") == "recognition"
        and str(item.get("capture_status") or "") == "captured"
        and _coerce_float(item.get("capture_started_at")) > 0.0
        and _coerce_float(item.get("recognition_completed_at")) >= _coerce_float(item.get("captured_at"))
        >= _coerce_float(item.get("capture_started_at"))
    ]
    captured_by_frame: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    for item in captured_candidates:
        captured_by_frame.setdefault(_recognition_observation_key(item), item)
    captured = list(captured_by_frame.values())
    capture_samples = [
        (_coerce_float(item.get("captured_at")) - _coerce_float(item.get("capture_started_at")))
        * 1000.0
        for item in captured
    ]
    recognition_samples = [
        (_coerce_float(item.get("recognition_completed_at")) - _coerce_float(item.get("captured_at")))
        * 1000.0
        for item in captured
    ]
    total_samples = [
        (_coerce_float(item.get("recognition_completed_at")) - _coerce_float(item.get("capture_started_at")))
        * 1000.0
        for item in captured
    ]
    reports = _read_reports(reports_path)
    if expected_build:
        reports = [report for report in reports if str(report.get("build_id") or "") == expected_build]
        if not reports:
            raise ValueError(f"Host reports 没有目标 Build 记录：{expected_build}")
    epoch_started_at: dict[int, float] = {}
    for item in captured:
        epoch = int(_coerce_float(item.get("selection_epoch")))
        started_at = _coerce_float(item.get("capture_started_at"))
        if epoch > 0 and started_at > 0.0:
            epoch_started_at[epoch] = min(epoch_started_at.get(epoch, started_at), started_at)
    presentation_records = [
        record
        for record in _bound_presentation_records(reports, session_id=selected)
        if int(record["selection_epoch"]) in eligible_epochs
    ]
    first_feedback_by_epoch: dict[int, float] = {}
    first_presented_by_epoch: dict[int, float] = {}
    for record in presentation_records:
        epoch = int(record["selection_epoch"])
        presented = float(record["presented_at"])
        started = epoch_started_at.get(epoch, 0.0)
        if started > 0.0 and presented >= started:
            first_feedback_by_epoch[epoch] = min(first_feedback_by_epoch.get(epoch, presented), presented)
            if bool(record["ready_frame"]):
                first_presented_by_epoch[epoch] = min(first_presented_by_epoch.get(epoch, presented), presented)
    first_feedback_samples = [
        (first_feedback_by_epoch[epoch] - started) * 1000.0
        for epoch, started in sorted(epoch_started_at.items())
        if epoch in first_feedback_by_epoch
    ]
    first_feedback = _latency_summary(
        first_feedback_samples,
        target_p95_ms=FIRST_FEEDBACK_TARGET_MS,
    )
    first_feedback["expected_epoch_count"] = len(epoch_started_at)
    first_feedback["complete_epoch_count"] = len(first_feedback_samples)
    first_feedback["pass_coverage"] = bool(
        epoch_started_at and len(first_feedback_samples) == len(epoch_started_at)
    )
    first_full_canvas_samples = [
        (first_presented_by_epoch[epoch] - started) * 1000.0
        for epoch, started in sorted(epoch_started_at.items())
        if epoch in first_presented_by_epoch
    ]
    first_full_canvas = _latency_summary(
        first_full_canvas_samples,
        target_p95_ms=FIRST_FULL_CANVAS_TARGET_MS,
    )
    first_full_canvas["expected_epoch_count"] = len(epoch_started_at)
    first_full_canvas["complete_epoch_count"] = len(first_full_canvas_samples)
    first_full_canvas["pass_coverage"] = bool(
        epoch_started_at and len(first_full_canvas_samples) == len(epoch_started_at)
    )
    independent_presentations = _first_event_presentations(presentation_records)
    presentation_samples = [
        (float(record["presented_at"]) - float(record["event_written_at"])) * 1000.0
        for record in independent_presentations
    ]
    event_to_read_samples = [
        (float(record["host_read_at"]) - float(record["event_written_at"])) * 1000.0
        for record in independent_presentations
        if float(record["host_read_at"]) >= float(record["event_written_at"]) > 0.0
    ]
    read_to_draw_samples = [
        (float(record["draw_started_at"]) - float(record["host_read_at"])) * 1000.0
        for record in independent_presentations
        if float(record["draw_started_at"]) >= float(record["host_read_at"]) > 0.0
    ]
    draw_to_present_samples = [
        (float(record["presented_at"]) - float(record["draw_completed_at"])) * 1000.0
        for record in independent_presentations
        if float(record["presented_at"]) >= float(record["draw_completed_at"]) > 0.0
    ]
    total = _latency_summary(total_samples, target_p95_ms=CAPTURE_RECOGNITION_P95_TARGET_MS)
    capture_latency = _latency_summary(
        capture_samples,
        target_p95_ms=CAPTURE_RECOGNITION_P95_TARGET_MS,
    )
    recognition_latency = _latency_summary(
        recognition_samples,
        target_p95_ms=CAPTURE_RECOGNITION_P95_TARGET_MS,
    )
    presentation = _latency_summary(presentation_samples, target_p95_ms=PRESENTATION_P95_TARGET_MS)
    presentation["minimum_count"] = MIN_PRESENTATION_SAMPLES
    presentation["sample_contract"] = "first_presentation_per_event"
    presentation["repeated_presentations"] = len(presentation_records) - len(independent_presentations)
    presentation["pass_count"] = len(presentation_samples) >= MIN_PRESENTATION_SAMPLES
    slot_starvation = _slot_starvation_summary(reports_path)
    reroll_refresh = _reroll_refresh_summary(session_observations, presentation_records)
    gates = {
        "captured_observations": len(captured) >= MIN_CAPTURED_OBSERVATIONS,
        "capture_recognition_p95": bool(total_samples and total["p95_ms"] <= CAPTURE_RECOGNITION_P95_TARGET_MS),
        "first_feedback": bool(
            first_feedback["pass_coverage"]
            and first_feedback_samples
            and first_feedback["p95_ms"] <= FIRST_FEEDBACK_TARGET_MS
        ),
        "first_full_canvas": bool(
            first_full_canvas["pass_coverage"]
            and first_full_canvas_samples
            and first_full_canvas["p95_ms"] <= FIRST_FULL_CANVAS_TARGET_MS
        ),
        "event_written_to_presented": bool(
            len(presentation_samples) >= MIN_PRESENTATION_SAMPLES
            and presentation["p95_ms"] <= PRESENTATION_P95_TARGET_MS
        ),
        "slot_starvation": bool(slot_starvation["passed"]),
        "reroll_refresh": bool(reroll_refresh["passed"]),
    }
    slowest_observations = sorted(
        (
            {
                "selection_epoch": int(_coerce_float(item.get("selection_epoch"))),
                "observation_seq": int(_coerce_float(item.get("observation_seq"))),
                "total_ms": round(
                    (
                        _coerce_float(item.get("recognition_completed_at"))
                        - _coerce_float(item.get("capture_started_at"))
                    )
                    * 1000.0,
                    3,
                ),
                "matching_timing": dict(item.get("matching_timing") or {})
                if isinstance(item.get("matching_timing"), Mapping)
                else {},
            }
            for item in captured
        ),
        key=lambda item: float(item["total_ms"]),
        reverse=True,
    )[:10]
    return {
        "schema_version": 1,
        "source": {"tag": "real-session", "timeline": str(timeline), "host_reports": str(reports_path)},
        "expected_build_id": expected_build,
        "session_id": selected,
        "eligible_epoch_count": len(eligible_epochs),
        "eligible_epochs": sorted(eligible_epochs),
        "excluded_epochs_by_reason": excluded_epochs,
        "captured_observations": {
            "count": len(captured),
            "minimum_count": MIN_CAPTURED_OBSERVATIONS,
            "passed": gates["captured_observations"],
        },
        "latency": {
            "capture_recognition": total,
            "capture": capture_latency,
            "recognition": recognition_latency,
            "event_written_to_presented": presentation,
            "presentation_segments": {
                "event_written_to_host_read": _latency_summary(
                    event_to_read_samples,
                    target_p95_ms=PRESENTATION_P95_TARGET_MS,
                ),
                "host_read_to_draw_started": _latency_summary(
                    read_to_draw_samples,
                    target_p95_ms=PRESENTATION_P95_TARGET_MS,
                ),
                "draw_completed_to_presented": _latency_summary(
                    draw_to_present_samples,
                    target_p95_ms=PRESENTATION_P95_TARGET_MS,
                ),
            },
        },
        "first_full_canvas": {**first_full_canvas, "passed": gates["first_full_canvas"]},
        "first_feedback": {**first_feedback, "passed": gates["first_feedback"]},
        "reroll_refresh": reroll_refresh,
        "slowest_observations": slowest_observations,
        "pixel_evidence": _pixel_evidence_summary(
            reports_path,
            session_id=selected,
            eligible_epochs=eligible_epochs,
        ),
        "slot_starvation": slot_starvation,
        "targets": {
            "capture_recognition_p95_ms": CAPTURE_RECOGNITION_P95_TARGET_MS,
            "first_feedback_ms": FIRST_FEEDBACK_TARGET_MS,
            "first_full_canvas_ms": FIRST_FULL_CANVAS_TARGET_MS,
            "presentation_p95_ms": PRESENTATION_P95_TARGET_MS,
            "reroll_ready_p95_ms": REROLL_READY_TARGET_MS,
        },
        "gates": gates,
        "passed": all(gates.values()),
        "manual_interaction_acceptance_required": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Hextech 游戏内显示性能验收摘要。")
    parser.add_argument("--latency-ms", nargs="*", type=float, default=[], help="手工录入的端到端延迟样本。")
    parser.add_argument("--recognition-ms", nargs="*", type=float, default=[], help="识别事件写入前的延迟样本。")
    parser.add_argument("--render-ms", nargs="*", type=float, default=[], help="事件写入到 overlay 渲染的延迟样本。")
    parser.add_argument("--source-tag", default="manual")
    parser.add_argument("--timeline", type=Path, help="真实 selection timeline JSONL。")
    parser.add_argument("--host-reports", type=Path, help="Host session report 文件或目录。")
    parser.add_argument("--session-id", default="", help="多 session 时显式选择会话。")
    parser.add_argument("--expected-build-id", default="", help="只验收目标第三候选 Build 的 timeline v2。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.timeline) != bool(args.host_reports):
        raise SystemExit("--timeline 与 --host-reports 必须同时提供")
    report = (
        build_real_session_performance_report(
            args.timeline,
            args.host_reports,
            session_id=args.session_id,
            expected_build_id=args.expected_build_id,
        )
        if args.timeline
        else build_overlay_performance_report(
            latency_samples_ms=args.latency_ms,
            recognition_samples_ms=args.recognition_ms,
            render_samples_ms=args.render_ms,
            source_tag=args.source_tag,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not args.timeline or report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

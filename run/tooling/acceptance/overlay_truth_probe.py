"""用独立截图人工真值核验 Overlay READY 的只读验收工具。

该工具刻意与性能探针分离。内部 ``READY``、OCR 输出或最终稳定结果都不能
生成真值；缺少可回读截图、人工标注或完整运行身份时，报告只能是
``qualified=false``。工具不修改 timeline、截图或运行态文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


TRUTH_SCHEMA_VERSION = 1
ALL_THREE_CORRECT_TARGET_MS = 900.0
_TIME_TOLERANCE_SECONDS = 0.000_001


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _finite_positive(value: object) -> float | None:
    number = _number(value)
    return number if math.isfinite(number) and number > 0.0 else None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(len(ordered) * percentile / 100.0))
    return round(ordered[min(rank - 1, len(ordered) - 1)], 3)


def _empty_report(
    timeline: Path,
    truth_file: Path,
    *,
    expected_build_id: str,
    session_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "scope": "overlay-first-correct-truth-only",
        "source": {"timeline": str(timeline), "truth_file": str(truth_file)},
        "identity": {
            "expected_build_id": expected_build_id,
            "session_id": session_id,
            "sidecar_instance_id": "",
            "sidecar_pid": 0,
        },
        "qualified": False,
        "qualification_errors": [],
        "eligible_timeline_epochs": [],
        "eligible_timeline_frame_count": 0,
        "qualified_frame_count": 0,
        "duplicate_capture_count": 0,
        "timeline_duplicate_count": 0,
        "invalid_result_count": 0,
        "excluded_frames_by_reason": {},
        "false_ready": {"count": 0, "records": []},
        "first_correct_per_slot": [],
        "all_three_correct": {
            "count": 0,
            "expected_state_count": 0,
            "samples_ms": [],
            "states": [],
            "p95_ms": None,
            "target_p95_ms": ALL_THREE_CORRECT_TARGET_MS,
            "passed": False,
        },
        "unconfirmed_timeouts": {"count": 0, "records": []},
        "gates": {
            "identity_and_provenance": False,
            "unique_captures": False,
            "valid_result_bindings": False,
            "zero_false_ready": False,
            "all_truth_spans_confirmed": False,
            "all_three_correct_latency": False,
        },
        "passed": False,
        "whole_real_game_go": False,
        "manual_real_game_acceptance_required": True,
    }


def _add_reason(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1


def _read_json_object(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("truth_file_not_object")
    return payload


def _read_jsonl(path: Path) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, Mapping):
            raise ValueError(f"timeline_line_not_object:{line_number}")
        records.append(payload)
    return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_decodable_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
            return image.width > 0 and image.height > 0
    except (OSError, ValueError):
        return False


def _slot_at(record: Mapping[str, Any], slot_index: int) -> Mapping[str, Any]:
    slots = record.get("slots")
    if not isinstance(slots, list):
        return {}
    for item in slots:
        if isinstance(item, Mapping) and _integer(item.get("slot"), -1) == slot_index:
            return item
    return {}


def _has_exact_slot_set(record: Mapping[str, Any]) -> bool:
    slots = record.get("slots")
    if not isinstance(slots, list):
        return False
    indexes = [_integer(item.get("slot"), -1) for item in slots if isinstance(item, Mapping)]
    return len(indexes) == 3 and sorted(indexes) == [0, 1, 2]


def _timeline_captured_frame_id(record: Mapping[str, Any]) -> int:
    top_level = _integer(record.get("captured_frame_id"))
    nested: set[int] = set()
    for slot_index in range(3):
        ocr = _slot_at(record, slot_index).get("ocr_production")
        if isinstance(ocr, Mapping) and ocr:
            frame_id = _integer(ocr.get("captured_frame_id"))
            if frame_id > 0:
                nested.add(frame_id)
    if top_level > 0:
        return top_level if not nested or nested == {top_level} else -1
    return next(iter(nested)) if len(nested) == 1 else 0


def _covered_by_span(epoch: int, sequence: int, spans: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        _integer(span.get("selection_epoch")) == epoch
        and _integer(span.get("start_observation_seq")) <= sequence <= _integer(span.get("end_observation_seq"))
        for span in spans
    )


def _label_matches(slot: Mapping[str, Any], span: Mapping[str, Any]) -> bool:
    expected_id = str(span.get("augment_id") or "")
    expected_name = str(span.get("name") or "")
    return bool(
        str(slot.get("state") or "") == "ready"
        and _integer(slot.get("slot_generation")) == _integer(span.get("slot_generation"))
        and (not expected_id or str(slot.get("augment_id") or "") == expected_id)
        and (not expected_name or str(slot.get("name") or "") == expected_name)
    )


def _ocr_binding_valid(
    slot: Mapping[str, Any],
    *,
    expected_session: str,
    epoch: int,
    slot_index: int,
    slot_generation: int,
    captured_frame_id: int,
) -> bool:
    ocr = slot.get("ocr_production")
    if not isinstance(ocr, Mapping) or not ocr:
        return True
    if str(ocr.get("state") or "") != "admitted":
        return True
    actual_frame_id = _integer(ocr.get("captured_frame_id"))
    canonical_id = str(ocr.get("canonical_id") or "")
    return bool(
        str(ocr.get("session_id") or "") == expected_session
        and _integer(ocr.get("selection_epoch")) == epoch
        and _integer(ocr.get("slot_index"), -1) == slot_index
        and _integer(ocr.get("slot_generation")) == slot_generation
        and captured_frame_id > 0
        and actual_frame_id > 0
        and actual_frame_id == captured_frame_id
        and bool(canonical_id)
        and canonical_id == str(slot.get("augment_id") or "")
    )


def build_overlay_truth_report(
    timeline_path: str | Path,
    truth_file_path: str | Path,
    *,
    expected_build_id: str = "",
    session_id: str = "",
    expected_sidecar_instance_id: str = "",
    expected_sidecar_pid: int = 0,
) -> dict[str, Any]:
    """核验一份 timeline 的首次正确 READY；任何输入文件都不会被修改。"""

    timeline = Path(timeline_path)
    truth_file = Path(truth_file_path)
    requested_build = str(expected_build_id or "").strip()
    requested_session = str(session_id or "").strip()
    requested_sidecar = str(expected_sidecar_instance_id or "").strip()
    requested_sidecar_pid = _integer(expected_sidecar_pid)
    report = _empty_report(
        timeline,
        truth_file,
        expected_build_id=requested_build,
        session_id=requested_session,
    )
    report["identity"].update(
        {
            "sidecar_instance_id": requested_sidecar,
            "sidecar_pid": requested_sidecar_pid,
        }
    )
    errors: list[str] = report["qualification_errors"]

    if not requested_build:
        errors.append("expected_build_id_required")
    if not requested_session:
        errors.append("expected_session_id_required")
    if not requested_sidecar:
        errors.append("expected_sidecar_instance_id_required")
    if requested_sidecar_pid <= 0:
        errors.append("expected_sidecar_pid_required")

    if not truth_file.is_file():
        errors.append("truth_file_missing")
        return report
    if not timeline.is_file():
        errors.append("timeline_file_missing")
        return report
    try:
        truth = _read_json_object(truth_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"truth_file_invalid:{exc}")
        return report
    try:
        timeline_records = _read_jsonl(timeline)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"timeline_file_invalid:{exc}")
        return report
    if not timeline_records:
        errors.append("timeline_records_missing")

    if _integer(truth.get("schema_version")) != TRUTH_SCHEMA_VERSION:
        errors.append("truth_schema_unsupported")
    truth_build = str(truth.get("build_id") or "").strip()
    truth_session = str(truth.get("session_id") or "").strip()
    truth_sidecar = str(truth.get("sidecar_instance_id") or "").strip()
    truth_sidecar_pid = _integer(truth.get("sidecar_pid"))
    if not truth_build:
        errors.append("truth_build_id_missing")
    if not truth_session:
        errors.append("truth_session_id_missing")
    if not truth_sidecar:
        errors.append("truth_sidecar_instance_id_missing")
    if truth_sidecar_pid <= 0:
        errors.append("truth_sidecar_pid_missing")
    if requested_build and requested_build != truth_build:
        errors.append("expected_build_truth_mismatch")
    if requested_session and requested_session != truth_session:
        errors.append("requested_session_truth_mismatch")
    if requested_sidecar and requested_sidecar != truth_sidecar:
        errors.append("expected_sidecar_truth_mismatch")
    if requested_sidecar_pid > 0 and requested_sidecar_pid != truth_sidecar_pid:
        errors.append("expected_sidecar_pid_truth_mismatch")
    selected_build = requested_build or truth_build
    selected_session = requested_session or truth_session
    selected_sidecar = requested_sidecar or truth_sidecar
    selected_sidecar_pid = requested_sidecar_pid or truth_sidecar_pid
    report["identity"] = {
        "expected_build_id": selected_build,
        "session_id": selected_session,
        "sidecar_instance_id": selected_sidecar,
        "sidecar_pid": selected_sidecar_pid,
    }

    raw_frames = truth.get("frames")
    raw_spans = truth.get("truth_spans")
    frames = [item for item in raw_frames if isinstance(item, Mapping)] if isinstance(raw_frames, list) else []
    spans = [item for item in raw_spans if isinstance(item, Mapping)] if isinstance(raw_spans, list) else []
    if not frames:
        errors.append("captured_frames_missing")
    if not spans:
        errors.append("human_truth_spans_missing")

    valid_spans: list[Mapping[str, Any]] = []
    for span in spans:
        valid = True
        if str(span.get("label_source") or "") != "human":
            valid = False
        if _integer(span.get("selection_epoch")) <= 0:
            valid = False
        if _integer(span.get("slot"), -1) not in range(3):
            valid = False
        if _integer(span.get("slot_generation")) <= 0:
            valid = False
        start = _integer(span.get("start_observation_seq"))
        end = _integer(span.get("end_observation_seq"))
        if start <= 0 or end < start:
            valid = False
        if not str(span.get("augment_id") or "") and not str(span.get("name") or ""):
            valid = False
        if valid:
            valid_spans.append(span)
        else:
            errors.append("human_truth_span_invalid")

    excluded: dict[str, int] = report["excluded_frames_by_reason"]
    unique_frames: list[Mapping[str, Any]] = []
    seen_capture_ids: set[str] = set()
    seen_captured_frame_ids: set[int] = set()
    seen_provenance: set[tuple[Any, ...]] = set()
    previous_seq_by_epoch: dict[int, int] = {}
    previous_time_by_epoch: dict[int, float] = {}
    for frame in frames:
        capture_id = str(frame.get("capture_id") or "").strip()
        epoch = _integer(frame.get("selection_epoch"))
        sequence = _integer(frame.get("observation_seq"))
        captured_at = _finite_positive(frame.get("captured_at"))
        source_text = str(frame.get("image_source") or "").strip()
        expected_sha = str(frame.get("image_sha256") or "").strip().lower()
        image_kind = str(frame.get("image_kind") or "").strip()
        captured_frame_id = _integer(frame.get("captured_frame_id"))
        if not capture_id or epoch <= 0 or sequence <= 0 or captured_at is None or captured_frame_id <= 0:
            errors.append("capture_provenance_missing")
            _add_reason(excluded, "capture_provenance_missing")
            continue
        if not source_text or not expected_sha:
            errors.append("image_source_or_hash_missing")
            _add_reason(excluded, "image_source_or_hash_missing")
            continue
        if image_kind != "full_frame":
            errors.append("full_frame_source_required")
            _add_reason(excluded, "full_frame_source_required")
            continue
        source = Path(source_text)
        source = source if source.is_absolute() else truth_file.parent / source
        try:
            source = source.resolve(strict=True)
        except OSError:
            errors.append("image_source_missing")
            _add_reason(excluded, "image_source_missing")
            continue
        try:
            digest = _sha256(source)
        except OSError:
            errors.append("image_source_unreadable")
            _add_reason(excluded, "image_source_unreadable")
            continue
        if not _is_decodable_image(source):
            errors.append("image_source_not_decodable")
            _add_reason(excluded, "image_source_not_decodable")
            continue
        if digest != expected_sha:
            errors.append("image_sha256_mismatch")
            _add_reason(excluded, "image_sha256_mismatch")
            continue
        provenance = (epoch, sequence, captured_at, str(source), captured_frame_id)
        if (
            capture_id in seen_capture_ids
            or captured_frame_id in seen_captured_frame_ids
            or provenance in seen_provenance
        ):
            report["duplicate_capture_count"] += 1
            errors.append("capture_identity_duplicate")
            _add_reason(excluded, "duplicate_capture")
            continue
        seen_capture_ids.add(capture_id)
        seen_captured_frame_ids.add(captured_frame_id)
        seen_provenance.add(provenance)
        if sequence <= previous_seq_by_epoch.get(
            epoch, 0
        ) or captured_at + _TIME_TOLERANCE_SECONDS < previous_time_by_epoch.get(epoch, 0.0):
            report["invalid_result_count"] += 1
            _add_reason(excluded, "out_of_order_capture")
            continue
        previous_seq_by_epoch[epoch] = sequence
        previous_time_by_epoch[epoch] = captured_at
        unique_frames.append(frame)

    truth_keys = {
        (_integer(frame.get("selection_epoch")), _integer(frame.get("observation_seq"))) for frame in unique_frames
    }
    truth_sequences = {_integer(frame.get("observation_seq")) for frame in unique_frames}
    eligible_timeline_keys: set[tuple[int, int]] = set()
    for record in timeline_records:
        epoch = _integer(record.get("selection_epoch"))
        sequence = _integer(record.get("observation_seq"))
        eligible = bool(
            _integer(record.get("schema_version")) == 2
            and str(record.get("observation_kind") or "recognition") == "recognition"
            and str(record.get("capture_status") or "") == "captured"
            and str(record.get("selection_type") or "") == "hextech"
            and str(record.get("scene_state") or "") == "active"
            and record.get("selection_window_active") is True
            and str(record.get("build_id") or "") == selected_build
            and str(record.get("session_id") or record.get("game_instance_id") or "") == selected_session
            and str(record.get("sidecar_instance_id") or "") == selected_sidecar
            and _integer(record.get("sidecar_pid")) == selected_sidecar_pid
        )
        if eligible:
            eligible_timeline_keys.add((epoch, sequence))
            if (epoch, sequence) not in truth_keys:
                errors.append("truth_frame_coverage_incomplete")
                _add_reason(excluded, "truth_frame_coverage_incomplete")
    report["eligible_timeline_epochs"] = sorted({epoch for epoch, _ in eligible_timeline_keys})
    report["eligible_timeline_frame_count"] = len(eligible_timeline_keys)
    timeline_by_key: dict[tuple[int, int], Mapping[str, Any]] = {}
    seen_timeline_identity: set[tuple[Any, ...]] = set()
    previous_timeline_seq: dict[int, int] = {}
    previous_timeline_time: dict[int, float] = {}
    for record in timeline_records:
        if str(record.get("observation_kind") or "recognition") != "recognition":
            continue
        if _integer(record.get("schema_version")) != 2:
            report["invalid_result_count"] += 1
            _add_reason(excluded, "timeline_schema_unsupported")
            errors.append("timeline_schema_unsupported")
            continue
        epoch = _integer(record.get("selection_epoch"))
        sequence = _integer(record.get("observation_seq"))
        record_identity = (
            str(record.get("build_id") or ""),
            str(record.get("session_id") or record.get("game_instance_id") or ""),
            str(record.get("sidecar_instance_id") or ""),
            epoch,
            sequence,
            _number(record.get("captured_at")),
        )
        if record_identity in seen_timeline_identity:
            report["timeline_duplicate_count"] += 1
            continue
        seen_timeline_identity.add(record_identity)
        exact_identity = bool(
            str(record.get("build_id") or "") == selected_build
            and str(record.get("session_id") or record.get("game_instance_id") or "") == selected_session
            and str(record.get("sidecar_instance_id") or "") == selected_sidecar
            and _integer(record.get("sidecar_pid")) == selected_sidecar_pid
        )
        if not exact_identity or ((epoch, sequence) not in truth_keys and sequence in truth_sequences):
            report["invalid_result_count"] += 1
            _add_reason(excluded, "timeline_identity_mismatch")
            errors.append("timeline_identity_mismatch")
            continue
        started_at = _finite_positive(record.get("capture_started_at"))
        captured_at = _finite_positive(record.get("captured_at"))
        completed_at = _finite_positive(record.get("recognition_completed_at"))
        if (
            started_at is None
            or captured_at is None
            or completed_at is None
            or not (started_at <= captured_at <= completed_at)
        ):
            report["invalid_result_count"] += 1
            _add_reason(excluded, "timeline_timing_invalid")
            continue
        if sequence <= previous_timeline_seq.get(
            epoch, 0
        ) or captured_at + _TIME_TOLERANCE_SECONDS < previous_timeline_time.get(epoch, 0.0):
            report["invalid_result_count"] += 1
            _add_reason(excluded, "out_of_order_timeline")
            continue
        previous_timeline_seq[epoch] = sequence
        previous_timeline_time[epoch] = captured_at
        key = (epoch, sequence)
        if key in timeline_by_key:
            report["timeline_duplicate_count"] += 1
            continue
        timeline_by_key[key] = record

    qualified: list[tuple[Mapping[str, Any], Mapping[str, Any], list[Mapping[str, Any]]]] = []
    for frame in unique_frames:
        epoch = _integer(frame.get("selection_epoch"))
        sequence = _integer(frame.get("observation_seq"))
        record = timeline_by_key.get((epoch, sequence))
        if record is None:
            report["invalid_result_count"] += 1
            _add_reason(excluded, "timeline_frame_missing")
            continue
        if str(record.get("capture_status") or "") != "captured":
            report["invalid_result_count"] += 1
            _add_reason(excluded, "timeline_frame_not_captured")
            continue
        if not (
            str(record.get("selection_type") or "") == "hextech"
            and str(record.get("scene_state") or "") == "active"
            and record.get("selection_window_active") is True
        ):
            report["invalid_result_count"] += 1
            _add_reason(excluded, "inactive_or_non_hextech_frame")
            continue
        if abs(_number(record.get("captured_at")) - _number(frame.get("captured_at"))) > _TIME_TOLERANCE_SECONDS:
            report["invalid_result_count"] += 1
            _add_reason(excluded, "capture_timestamp_mismatch")
            continue
        truth_frame_id = _integer(frame.get("captured_frame_id"))
        timeline_frame_id = _timeline_captured_frame_id(record)
        if timeline_frame_id <= 0:
            report["invalid_result_count"] += 1
            errors.append("timeline_capture_identity_missing_or_ambiguous")
            _add_reason(excluded, "timeline_capture_identity_missing_or_ambiguous")
            continue
        if timeline_frame_id != truth_frame_id:
            report["invalid_result_count"] += 1
            errors.append("captured_frame_id_mismatch")
            _add_reason(excluded, "captured_frame_id_mismatch")
            continue
        if not _has_exact_slot_set(record):
            report["invalid_result_count"] += 1
            _add_reason(excluded, "timeline_slots_ambiguous")
            continue
        frame_spans: list[Mapping[str, Any]] = []
        labels_valid = True
        for slot_index in range(3):
            matching = [
                span
                for span in valid_spans
                if _integer(span.get("selection_epoch")) == epoch
                and _integer(span.get("slot"), -1) == slot_index
                and _integer(span.get("start_observation_seq")) <= sequence <= _integer(span.get("end_observation_seq"))
            ]
            if len(matching) != 1:
                labels_valid = False
                break
            frame_spans.append(matching[0])
        if not labels_valid:
            errors.append("human_truth_missing_or_ambiguous")
            _add_reason(excluded, "human_truth_missing_or_ambiguous")
            continue
        captured_frame_id = truth_frame_id
        stale = any(
            not _ocr_binding_valid(
                _slot_at(record, slot_index),
                expected_session=selected_session,
                epoch=epoch,
                slot_index=slot_index,
                slot_generation=_integer(frame_spans[slot_index].get("slot_generation")),
                captured_frame_id=captured_frame_id,
            )
            for slot_index in range(3)
        )
        if stale:
            report["invalid_result_count"] += 1
            _add_reason(excluded, "stale_result_binding")
            continue
        qualified.append((frame, record, frame_spans))

    errors[:] = list(dict.fromkeys(errors))
    report["qualified_frame_count"] = len(qualified)
    report["qualified"] = not errors and bool(qualified)

    span_started_at: dict[tuple[int, int, int, int, int], float] = {}
    span_last_at: dict[tuple[int, int, int, int, int], float] = {}
    state_started_at: dict[tuple[int, tuple[tuple[int, int, int, int, int], ...]], float] = {}
    for frame, record, frame_spans in qualified:
        epoch = _integer(frame.get("selection_epoch"))
        start = _number(record.get("capture_started_at"), _number(frame.get("captured_at")))
        ready_at = _number(record.get("recognition_completed_at"), _number(frame.get("captured_at")))
        state_span_keys = tuple(
            (
                epoch,
                slot_index,
                _integer(span.get("slot_generation")),
                _integer(span.get("start_observation_seq")),
                _integer(span.get("end_observation_seq")),
            )
            for slot_index, span in enumerate(frame_spans)
        )
        state_key = (epoch, state_span_keys)
        state_started_at[state_key] = min(state_started_at.get(state_key, start), start)
        for span_key in state_span_keys:
            span_started_at[span_key] = min(span_started_at.get(span_key, start), start)
            span_last_at[span_key] = max(span_last_at.get(span_key, ready_at), ready_at)

    first_correct: dict[tuple[int, int, int, int, int], dict[str, Any]] = {}
    all_three_by_state: dict[tuple[int, tuple[tuple[int, int, int, int, int], ...]], float] = {}
    false_ready_records: list[dict[str, Any]] = []
    for frame, record, frame_spans in qualified:
        epoch = _integer(frame.get("selection_epoch"))
        sequence = _integer(frame.get("observation_seq"))
        ready_at = _number(record.get("recognition_completed_at"), _number(frame.get("captured_at")))
        all_correct = True
        for slot_index, span in enumerate(frame_spans):
            slot = _slot_at(record, slot_index)
            span_key = (
                epoch,
                slot_index,
                _integer(span.get("slot_generation")),
                _integer(span.get("start_observation_seq")),
                _integer(span.get("end_observation_seq")),
            )
            correct = _label_matches(slot, span)
            all_correct = all_correct and correct
            if correct and span_key not in first_correct:
                first_correct[span_key] = {
                    "selection_epoch": epoch,
                    "slot": slot_index,
                    "slot_generation": _integer(span.get("slot_generation")),
                    "expected_augment_id": str(span.get("augment_id") or ""),
                    "expected_name": str(span.get("name") or ""),
                    "observation_seq": sequence,
                    "ready_at": ready_at,
                    "latency_ms": round((ready_at - span_started_at[span_key]) * 1000.0, 3),
                }
            elif str(slot.get("state") or "") == "ready" and not correct:
                false_ready_records.append(
                    {
                        "selection_epoch": epoch,
                        "observation_seq": sequence,
                        "slot": slot_index,
                        "expected_slot_generation": _integer(span.get("slot_generation")),
                        "observed_slot_generation": _integer(slot.get("slot_generation")),
                        "expected_augment_id": str(span.get("augment_id") or ""),
                        "observed_augment_id": str(slot.get("augment_id") or ""),
                        "expected_name": str(span.get("name") or ""),
                        "observed_name": str(slot.get("name") or ""),
                    }
                )
        if all_correct:
            state_span_keys = tuple(
                (
                    epoch,
                    slot_index,
                    _integer(span.get("slot_generation")),
                    _integer(span.get("start_observation_seq")),
                    _integer(span.get("end_observation_seq")),
                )
                for slot_index, span in enumerate(frame_spans)
            )
            state_key = (epoch, state_span_keys)
            all_three_by_state[state_key] = min(all_three_by_state.get(state_key, ready_at), ready_at)

    report["false_ready"] = {"count": len(false_ready_records), "records": false_ready_records}
    report["first_correct_per_slot"] = sorted(
        first_correct.values(),
        key=lambda item: (
            item["selection_epoch"],
            item["slot"],
            item["slot_generation"],
        ),
    )
    all_three_details = [
        {
            "selection_epoch": state_key[0],
            "slot_generations": [span_key[2] for span_key in state_key[1]],
            "latency_ms": round((ready_at - state_started_at[state_key]) * 1000.0, 3),
        }
        for state_key, ready_at in sorted(all_three_by_state.items())
    ]
    all_three_samples = [float(item["latency_ms"]) for item in all_three_details]
    all_three_p95 = _percentile(all_three_samples, 95)
    expected_states = set(state_started_at)
    all_three_passed = bool(
        expected_states
        and expected_states == set(all_three_by_state)
        and all_three_p95 is not None
        and all_three_p95 <= ALL_THREE_CORRECT_TARGET_MS
    )
    report["all_three_correct"] = {
        "count": len(all_three_samples),
        "expected_state_count": len(expected_states),
        "samples_ms": all_three_samples,
        "states": all_three_details,
        "p95_ms": all_three_p95,
        "target_p95_ms": ALL_THREE_CORRECT_TARGET_MS,
        "passed": all_three_passed,
    }

    unconfirmed: list[dict[str, Any]] = []
    span_by_key: dict[tuple[int, int, int, int, int], Mapping[str, Any]] = {}
    for span in valid_spans:
        key = (
            _integer(span.get("selection_epoch")),
            _integer(span.get("slot"), -1),
            _integer(span.get("slot_generation")),
            _integer(span.get("start_observation_seq")),
            _integer(span.get("end_observation_seq")),
        )
        span_by_key[key] = span
    truth_frame_positions = [
        (_integer(frame.get("selection_epoch")), _integer(frame.get("observation_seq"))) for frame in unique_frames
    ]
    expected_span_keys = {
        key
        for key in span_by_key
        if any(epoch == key[0] and key[3] <= sequence <= key[4] for epoch, sequence in truth_frame_positions)
    }
    for key in sorted(expected_span_keys - set(first_correct)):
        epoch, slot_index, generation, start_seq, end_seq = key
        span = span_by_key[key]
        unconfirmed.append(
            {
                "selection_epoch": epoch,
                "slot": slot_index,
                "slot_generation": generation,
                "expected_augment_id": str(span.get("augment_id") or ""),
                "expected_name": str(span.get("name") or ""),
                "start_observation_seq": start_seq,
                "end_observation_seq": end_seq,
                "retained_until": span_last_at.get(key),
                "elapsed_ms": round(
                    (span_last_at.get(key, 0.0) - span_started_at.get(key, 0.0)) * 1000.0,
                    3,
                ),
            }
        )
    report["unconfirmed_timeouts"] = {"count": len(unconfirmed), "records": unconfirmed}

    gates = {
        "identity_and_provenance": report["qualified"],
        "unique_captures": bool(
            qualified and report["duplicate_capture_count"] == 0 and report["timeline_duplicate_count"] == 0
        ),
        "valid_result_bindings": bool(qualified and report["invalid_result_count"] == 0),
        "zero_false_ready": bool(qualified and not false_ready_records),
        "all_truth_spans_confirmed": bool(qualified and not unconfirmed),
        "all_three_correct_latency": all_three_passed,
    }
    report["gates"] = gates
    report["passed"] = all(gates.values())
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="核验 Overlay 首次 READY 的独立截图人工真值。")
    parser.add_argument("--timeline", type=Path, required=True, help="真实 selection timeline JSONL。")
    parser.add_argument("--truth-file", type=Path, required=True, help="独立截图与人工真值 span JSON。")
    parser.add_argument("--expected-build-id", default="", help="必须与真值及 timeline 相同的 Build ID。")
    parser.add_argument("--session-id", default="", help="必须与真值及 timeline 相同的 session ID。")
    parser.add_argument(
        "--expected-sidecar-instance-id",
        default="",
        help="必须与真值及 timeline 相同的 Sidecar instance ID。",
    )
    parser.add_argument(
        "--expected-sidecar-pid",
        type=int,
        default=0,
        help="必须与真值及 timeline 相同的 Sidecar PID。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_overlay_truth_report(
        args.timeline,
        args.truth_file,
        expected_build_id=args.expected_build_id,
        session_id=args.session_id,
        expected_sidecar_instance_id=args.expected_sidecar_instance_id,
        expected_sidecar_pid=args.expected_sidecar_pid,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

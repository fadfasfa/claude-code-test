"""运行态日志采集器。

用途：
- 真实对局结束后读取已有 runtime state/log/debug 文件，生成可复盘诊断包。
- 真实对局期间可开启轻量 watch，周期性只读 runtime 文件并写诊断快照。
- 不截图，不联网，不读取 token/auth/cookie 等敏感文件。

调用方: tests.test_runtime_diagnostics_collector; 关键依赖: catalog.runtime_store、overlay.renderer、overlay.host。
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

RUN_DIR = Path(__file__).resolve().parents[1]
import sys

if str(RUN_DIR) not in sys.path:
    sys.path.insert(0, str(RUN_DIR))

from hextech.catalog.runtime_store import get_runtime_root_dir  # noqa: E402
from hextech.support.user_diagnostics import (  # noqa: E402
    redact_diagnostic_tail_line,
    redact_diagnostic_text,
    redact_diagnostic_value,
)


DEFAULT_TAIL_LINES = 500
DEFAULT_RECENT_MINUTES = 180
DEFAULT_WATCH_INTERVAL_SECONDS = 60.0
DEFAULT_WATCH_DURATION_MINUTES = 0.0
SENSITIVE_NAME_PARTS = (
    "api_key",
    "auth",
    "authorization",
    "token",
    "cookie",
    "credential",
    "lcu",
    "nonce",
    "riot",
    "session",
    "secret",
    "password",
    "local.yaml",
    "proxies.json",
    "accounts.json",
)
SAFE_STATE_EXTENSIONS = {".json"}
TAIL_EXTENSIONS = {".log", ".jsonl", ".txt"}
SAFE_DEBUG_EXTENSIONS = {".json", ".jsonl", ".log", ".txt"}
SENSITIVE_FILENAME_RE = re.compile(
    r"(?i)\b(?:"
    r"[A-Za-z0-9_.-]*(?:auth|authorization|token|cookie|secret|password|credential|nonce|lcu|riot|session)[A-Za-z0-9_.-]*"
    r"\.(?:txt|json|yaml|yml|env|ini|cfg|conf|log)"
    r"|local\.yaml|proxies\.json|accounts\.json"
    r")\b"
)
STATE_TAIL_FILES = {
    "supervisor_events.v1.jsonl",
    "runtime_events.v1.jsonl",
    "overlay_vision_trace_history.v1.json",
}
TRACE_HISTORY_FILE = "overlay_vision_trace_history.v1.json"
TRACE_CURRENT_FILE = "overlay_vision_trace.v1.json"
OVERLAY_EVENT_FILE = "game_overlay_slots.v1.json"
SIDECAR_STATUS_FILE = "game_overlay_sidecar_status.json"
FEATURE_FLAGS_FILE = "ui_feature_flags.json"
WEB_PORT_FILE = "web_server_port.txt"
FOCUS_BLOCK_REASONS = {"game_not_foreground", "game_window_missing"}
BLOCKING_REASONS = {"blocking_modal_present", "scoreboard_key_down"}
CONTENT_WAIT_REASONS = {
    "partial_ready",
    "selection_scene_not_detected",
    "unstable",
    "warming_up",
    "capture_unavailable",
}
TRACE_SEGMENT_GAP_SECONDS = 15.0
WATCH_EVENTS_FILE = "watch_events.jsonl"
LATEST_SUMMARY_FILE = "latest_summary.json"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_sensitive_path(path: Path) -> bool:
    text = str(path).replace("\\", "/").lower()
    return any(part in text for part in SENSITIVE_NAME_PARTS)


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return path.name


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        if not isinstance(value, str):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _entry_timestamp(entry: Mapping[str, Any]) -> float | None:
    for key in ("generated_at", "updated_at", "timestamp"):
        ts = _float_or_none(entry.get(key))
        if ts is not None:
            return ts
    return None


def _tail_text(path: Path, *, max_lines: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-max_lines:]) + ("\n" if lines else "")


def _tail_jsonl_events(path: Path, *, max_lines: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in _tail_text(path, max_lines=max_lines).splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _copy_json_file_redacted(source: Path, target: Path, *, copied: list[dict[str, Any]]) -> None:
    payload = _read_json(source)
    if payload is None:
        return
    redacted = redact_diagnostic_value(source.name, payload)
    text = json.dumps(redacted, ensure_ascii=False, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    copied.append({"source": str(source), "target": str(target), "bytes": len(text.encode("utf-8"))})


def _redact_bundle_text(value: object) -> str:
    return SENSITIVE_FILENAME_RE.sub("<sensitive-file>", redact_diagnostic_text(value))


def _redact_bundle_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_redact_bundle_text(key): _redact_bundle_value(child_value) for key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_bundle_value(item) for item in value]
    if isinstance(value, str):
        return _redact_bundle_text(value)
    return value


def _copy_debug_file_redacted(source: Path, target: Path, *, copied: list[dict[str, Any]]) -> None:
    """复制可分享 debug 文本；debug_recent 不允许原样带出本机路径或 token。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".json":
        payload = _read_json(source)
        if payload is not None:
            redacted = _redact_bundle_value(redact_diagnostic_value(source.name, payload))
            text = json.dumps(redacted, ensure_ascii=False, indent=2) + "\n"
            target.write_text(text, encoding="utf-8")
            copied.append({"source": str(source), "target": str(target), "bytes": len(text.encode("utf-8"))})
            return

    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    text = "\n".join(_redact_bundle_text(redact_diagnostic_tail_line(line)) for line in lines) + ("\n" if lines else "")
    target.write_text(text, encoding="utf-8")
    copied.append({"source": str(source), "target": str(target), "bytes": len(text.encode("utf-8"))})


def _write_tail(source: Path, target: Path, *, max_lines: int, copied: list[dict[str, Any]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = _tail_text(source, max_lines=max_lines).splitlines()
    text = "\n".join(_redact_bundle_text(redact_diagnostic_tail_line(line)) for line in lines) + (
        "\n" if lines else ""
    )
    target.write_text(text, encoding="utf-8")
    copied.append({"source": str(source), "target": str(target), "bytes": len(text.encode("utf-8")), "tail_lines": max_lines})


def _iter_recent_files(root: Path, *, since_timestamp: float, extensions: set[str]) -> Iterable[Path]:
    if not root.exists():
        return []
    result: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or _is_sensitive_path(path):
            continue
        if path.suffix.lower() not in extensions:
            continue
        try:
            if path.stat().st_mtime >= since_timestamp:
                result.append(path)
        except OSError:
            continue
    return sorted(result, key=lambda item: str(item).lower())


def _state_summary(state_dir: Path) -> dict[str, Any]:
    startup = _read_json(state_dir / "startup_status.json")
    scraper = _read_json(state_dir / "scraper_status.json")
    overlay_trace = _read_json(state_dir / TRACE_CURRENT_FILE)
    context = _read_json(state_dir / "game_overlay_context.v1.json")
    overlay_event = _read_json(state_dir / OVERLAY_EVENT_FILE)
    sidecar = _read_json(state_dir / SIDECAR_STATUS_FILE)
    feature_flags = _read_json(state_dir / FEATURE_FLAGS_FILE)
    flags = feature_flags if isinstance(feature_flags, dict) else {}
    port_path = state_dir / WEB_PORT_FILE
    web_port = ""
    try:
        web_port = port_path.read_text(encoding="utf-8").strip()
    except OSError:
        web_port = ""
    web_status = "running" if web_port else "web_disabled_until_user_action"
    if flags and bool(flags.get("web_frontend_enabled", False)) and not web_port:
        web_status = "startup_requested_no_port"
    return {
        "startup_status": startup if isinstance(startup, dict) else {},
        "scraper_status": scraper if isinstance(scraper, dict) else {},
        "overlay_trace": overlay_trace if isinstance(overlay_trace, dict) else {},
        "context": context if isinstance(context, dict) else {},
        "overlay_event": overlay_event if isinstance(overlay_event, dict) else {},
        "sidecar_status": sidecar if isinstance(sidecar, dict) else {},
        "feature_flags": flags,
        "web_frontend": {
            "status": web_status,
            "enabled_intent": bool(flags.get("web_frontend_enabled", False)),
            "port_file_present": bool(web_port),
            "port": web_port,
        },
    }


def _hint_cache_diagnostics(cache_dir: Path) -> dict[str, Any]:
    payload = _read_json(cache_dir / "overlay_hint_cache.v1.json")
    if not isinstance(payload, Mapping):
        return {"ok": False, "error": "hint_cache_missing_or_damaged"}
    hints = payload.get("hints") if isinstance(payload.get("hints"), Mapping) else {}
    name_index = payload.get("name_index") if isinstance(payload.get("name_index"), Mapping) else {}
    stats_counts: list[int] = []
    for hint in hints.values():
        if not isinstance(hint, Mapping):
            continue
        by_id = hint.get("stats_by_champion_id")
        stats_counts.append(len(by_id) if isinstance(by_id, Mapping) else 0)
    stats_counts.sort()

    def percentile(index: int) -> int | None:
        if not stats_counts:
            return None
        return stats_counts[max(0, min(len(stats_counts) - 1, index))]

    return {
        "ok": True,
        "source": payload.get("source") if isinstance(payload.get("source"), Mapping) else {},
        "hint_count": len(hints),
        "name_index_count": len(name_index),
        "zero_stats_hint_count": sum(1 for count in stats_counts if count == 0),
        "min_stats_heroes": percentile(0),
        "median_stats_heroes": percentile(len(stats_counts) // 2),
        "p75_stats_heroes": percentile((len(stats_counts) * 3) // 4),
        "max_stats_heroes": percentile(len(stats_counts) - 1),
        "less_than_20_heroes": sum(1 for count in stats_counts if count < 20),
        "less_than_60_heroes": sum(1 for count in stats_counts if count < 60),
    }


def _render_status_diagnostics(state_dir: Path, cache_dir: Path) -> dict[str, Any]:
    event = _read_json(state_dir / OVERLAY_EVENT_FILE)
    context = _read_json(state_dir / "game_overlay_context.v1.json")
    hint_cache = _read_json(cache_dir / "overlay_hint_cache.v1.json")
    if not isinstance(event, Mapping):
        return {"ok": False, "error": "overlay_event_missing_or_damaged"}
    if not isinstance(context, Mapping):
        context = {}
    if not isinstance(hint_cache, Mapping):
        hint_cache = {}
    try:
        from hextech.overlay.renderer import build_render_model

        model = build_render_model(event, hint_cache=hint_cache, context=context)
    except Exception as exc:
        return {
            "ok": False,
            "error": "render_model_failed",
            "error_type": exc.__class__.__name__,
            "error_message": str(exc)[:240],
        }
    stats = model.get("stats") if isinstance(model, Mapping) else []
    rows = [dict(item) for item in stats[:3] if isinstance(item, Mapping)] if isinstance(stats, list) else []
    return {
        "ok": True,
        "status_counts": dict(Counter(str(row.get("status_code") or "") for row in rows if row.get("status_code"))),
        "rows": [
            {
                "slot": row.get("slot"),
                "name": row.get("name"),
                "state": row.get("state"),
                "status_code": row.get("status_code"),
                "status_text": row.get("status_text"),
                "stats_text": row.get("stats_text"),
            }
            for row in rows
        ],
        "context": {
            "ok": bool(context.get("ok")),
            "error": str(context.get("error") or ""),
            "source": str(context.get("source") or ""),
            "champion_id": str(context.get("champion_id") or ""),
            "champion_name": str(context.get("champion_name") or ""),
        },
    }


def _event_summary(paths: Sequence[Path], *, max_lines: int) -> dict[str, Any]:
    event_counter: Counter[str] = Counter()
    level_counter: Counter[str] = Counter()
    recent_problems: list[dict[str, Any]] = []
    for path in paths:
        for event in _tail_jsonl_events(path, max_lines=max_lines):
            event_name = str(event.get("event") or event.get("name") or "").strip()
            level = str(event.get("level") or "").strip().upper()
            if event_name:
                event_counter[event_name] += 1
            if level:
                level_counter[level] += 1
            if level in {"ERROR", "WARNING"}:
                recent_problems.append(
                    {
                        "file": str(path),
                        "timestamp": event.get("timestamp"),
                        "level": level,
                        "event": event_name,
                        "reason_code": event.get("reason_code"),
                        "error_type": event.get("error_type"),
                        "message": event.get("error_message_sanitized") or event.get("message") or "",
                    }
                )
    return {
        "events": dict(event_counter.most_common(20)),
        "levels": dict(level_counter),
        "recent_problems": recent_problems[-30:],
    }


def _log_summary(log_paths: Sequence[Path], *, max_lines: int) -> dict[str, Any]:
    problem_lines: list[dict[str, str]] = []
    for path in log_paths:
        for line in _tail_text(path, max_lines=max_lines).splitlines():
            upper = line.upper()
            if "ERROR" in upper or "WARNING" in upper or "失败" in line or "异常" in line:
                problem_lines.append({"file": str(path), "line": redact_diagnostic_text(line[:1000])})
    return {"recent_problem_lines": problem_lines[-80:]}


def _trace_entries(state_dir: Path, *, since_timestamp: float | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    history = _read_json(state_dir / TRACE_HISTORY_FILE)
    raw_entries = history.get("entries") if isinstance(history, Mapping) else None
    if isinstance(raw_entries, list):
        entries.extend(dict(item) for item in raw_entries if isinstance(item, Mapping))

    current = _read_json(state_dir / TRACE_CURRENT_FILE)
    if isinstance(current, Mapping):
        entries.append(dict(current))

    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in entries:
        key = (
            entry.get("generated_at"),
            entry.get("selection_epoch"),
            entry.get("active"),
            entry.get("selection_type"),
            entry.get("reason"),
            entry.get("gate_state"),
            tuple(entry.get("slot_signature") or []),
        )
        deduped[key] = entry
    ordered = sorted(deduped.values(), key=lambda item: _entry_timestamp(item) or 0.0)
    if since_timestamp is None:
        return ordered
    return [entry for entry in ordered if (_entry_timestamp(entry) or 0.0) >= since_timestamp]


def _truthy(entry: Mapping[str, Any], key: str) -> bool:
    return bool(entry.get(key))


def _int_value(entry: Mapping[str, Any], key: str) -> int:
    try:
        return int(entry.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _is_body_shard(entry: Mapping[str, Any]) -> bool:
    return (
        str(entry.get("selection_type") or "") == "body_shard"
        or str(entry.get("scene_kind") or "") == "body_shard"
        or str(entry.get("reason") or "") == "body_shard_only"
        or bool(entry.get("body_shard_latched"))
    )


def _is_hextech_selection_observation(entry: Mapping[str, Any]) -> bool:
    if str(entry.get("selection_type") or "") != "hextech":
        return False
    reason = str(entry.get("reason") or "")
    if reason in FOCUS_BLOCK_REASONS:
        return False
    scene_state = str(entry.get("scene_state") or "")
    return bool(
        entry.get("active")
        or entry.get("visible")
        or entry.get("selection_window_active")
        or entry.get("selection_button_present")
        or _int_value(entry, "ready_slots") > 0
        or scene_state in {"candidate", "active"}
    )


def _is_waiting_for_content(entry: Mapping[str, Any]) -> bool:
    reason = str(entry.get("reason") or "")
    return (
        str(entry.get("selection_type") or "") == "hextech"
        and not bool(entry.get("active"))
        and reason in CONTENT_WAIT_REASONS
        and (
            entry.get("selection_window_active")
            or entry.get("selection_button_present")
            or _int_value(entry, "ready_slots") > 0
            or str(entry.get("scene_state") or "") in {"candidate", "active"}
            or bool(entry.get("card_residue"))
        )
    )


def _longest_segment_seconds(entries: Sequence[Mapping[str, Any]], predicate, *, include_transition: bool = False) -> float:
    current_start: float | None = None
    previous_ts: float | None = None
    longest = 0.0
    for entry in entries:
        ts = _entry_timestamp(entry)
        if ts is None:
            continue
        matched = bool(predicate(entry))
        if previous_ts is not None and (ts - previous_ts) > TRACE_SEGMENT_GAP_SECONDS:
            if current_start is not None:
                longest = max(longest, previous_ts - current_start)
            current_start = None
        if matched:
            if current_start is None:
                current_start = ts
            longest = max(longest, ts - current_start)
        else:
            if include_transition and current_start is not None:
                longest = max(longest, ts - current_start)
            current_start = None
        previous_ts = ts
    if current_start is not None and previous_ts is not None:
        longest = max(longest, previous_ts - current_start)
    return round(longest, 3)


def _activation_count(entries: Sequence[Mapping[str, Any]], predicate) -> int:
    count = 0
    was_active = False
    for entry in entries:
        active = bool(predicate(entry))
        if active and not was_active:
            count += 1
        was_active = active
    return count


def _brief_trace(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        return {}
    return {
        "generated_at": entry.get("generated_at"),
        "active": bool(entry.get("active")),
        "visible": bool(entry.get("visible")),
        "selection_type": str(entry.get("selection_type") or ""),
        "reason": str(entry.get("reason") or ""),
        "gate_state": str(entry.get("gate_state") or ""),
        "scene_state": str(entry.get("scene_state") or ""),
        "scene_kind": str(entry.get("scene_kind") or ""),
        "layout_id": str(entry.get("layout_id") or ""),
        "selection_epoch": entry.get("selection_epoch"),
        "ready_slots": _int_value(entry, "ready_slots"),
        "stable_frames": _int_value(entry, "stable_frames"),
        "selection_button_present": bool(entry.get("selection_button_present")),
        "selection_window_active": bool(entry.get("selection_window_active")),
        "scoreboard_key_down": bool(entry.get("scoreboard_key_down")),
        "cursor_over_cards": bool(entry.get("cursor_over_cards")),
        "card_residue": bool(entry.get("card_residue")),
        "hover_occluded": bool(entry.get("hover_occluded")),
        "slot_signature": list(entry.get("slot_signature") or []),
    }


def _brief_overlay_event(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    return {
        "generated_at": payload.get("generated_at"),
        "active": bool(payload.get("active")),
        "selection_type": str(payload.get("selection_type") or ""),
        "reason": str(source.get("reason") or payload.get("reason") or ""),
        "gate_state": str(source.get("gate_state") or ""),
        "ready_slots": _int_value(source, "ready_slots"),
        "selection_window_active": bool(source.get("selection_window_active")),
        "scoreboard_key_down": bool(source.get("scoreboard_key_down")),
    }


def _selection_epoch_timeline(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for entry in entries:
        try:
            epoch = int(entry.get("selection_epoch") or 0)
        except (TypeError, ValueError):
            epoch = 0
        if epoch <= 0:
            continue
        grouped.setdefault(epoch, []).append(entry)

    timeline: list[dict[str, Any]] = []
    for epoch, epoch_entries in sorted(grouped.items()):
        ordered = sorted(epoch_entries, key=lambda item: _entry_timestamp(item) or 0.0)
        first_ts = _entry_timestamp(ordered[0]) if ordered else None
        last_ts = _entry_timestamp(ordered[-1]) if ordered else None
        selection_types = sorted({str(item.get("selection_type") or "") for item in ordered if item.get("selection_type")})
        reasons = Counter(str(item.get("reason") or "") for item in ordered if item.get("reason"))
        gate_states = Counter(str(item.get("gate_state") or "") for item in ordered if item.get("gate_state"))
        became_visible = any(
            bool(item.get("active") or item.get("visible")) and str(item.get("selection_type") or "") == "hextech"
            for item in ordered
        )
        waiting_samples = sum(1 for item in ordered if _is_waiting_for_content(item))
        final = ordered[-1] if ordered else {}
        timeline.append(
            {
                "selection_epoch": epoch,
                "start_at": first_ts,
                "end_at": last_ts,
                "duration_seconds": round(max(0.0, (last_ts or 0.0) - (first_ts or 0.0)), 3)
                if first_ts is not None and last_ts is not None
                else 0.0,
                "sample_count": len(ordered),
                "selection_types": selection_types,
                "max_ready_slots": max((_int_value(item, "ready_slots") for item in ordered), default=0),
                "became_visible": became_visible,
                "waiting_samples": waiting_samples,
                "waiting_resolved_to_visible": bool(waiting_samples and became_visible),
                "body_shard_samples": sum(1 for item in ordered if _is_body_shard(item)),
                "focus_or_window_block_samples": sum(1 for item in ordered if str(item.get("reason") or "") in FOCUS_BLOCK_REASONS),
                "blocking_or_hidden_samples": sum(
                    1
                    for item in ordered
                    if str(item.get("reason") or "") in BLOCKING_REASONS or bool(item.get("scoreboard_key_down"))
                ),
                "selection_button_present_samples": sum(1 for item in ordered if _truthy(item, "selection_button_present")),
                "selection_window_active_samples": sum(1 for item in ordered if _truthy(item, "selection_window_active")),
                "hover_occluded_samples": sum(
                    1 for item in ordered if _truthy(item, "hover_occluded") or str(item.get("reason") or "") == "hover_occluded"
                ),
                "hover_expired_samples": sum(1 for item in ordered if str(item.get("reason") or "") == "hover_occluded_expired"),
                "top_reasons": dict(reasons.most_common(8)),
                "top_gate_states": dict(gate_states.most_common(8)),
                "final": _brief_trace(final),
            }
        )
    return timeline[-20:]


def _run_host_self_check() -> dict[str, Any]:
    try:
        from hextech.overlay.host import run_self_check

        payload = run_self_check()
    except Exception as exc:
        return {
            "ok": False,
            "error_type": exc.__class__.__name__,
            "error_message": str(exc)[:240],
        }
    if not isinstance(payload, Mapping):
        return {"ok": False, "error_type": "invalid_self_check_payload"}
    keys = (
        "ok",
        "event_ok",
        "event_visible",
        "event_error",
        "event_reason",
        "ready_slots",
        "selection_window_active",
        "cache_ok",
        "hint_cache_error",
        "hint_count",
        "private_stats_enabled",
        "synergy_hint_count",
        "context_status",
        "context_ok",
        "context_error",
        "context_source",
        "render_stats_count",
        "render_synergy_count",
        "render_status_counts",
        "state_age_ms",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def _official_provider_summary(debug_paths: Sequence[Path]) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for path in debug_paths:
        if "official_overlay_provider" not in str(path).replace("\\", "/"):
            continue
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            continue
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), Mapping) else {}
        live = diagnostics.get("live_client") if isinstance(diagnostics.get("live_client"), Mapping) else {}
        lcu = diagnostics.get("lcu") if isinstance(diagnostics.get("lcu"), Mapping) else {}
        samples.append(
            {
                "path": str(path),
                "status": str(payload.get("status") or ""),
                "generated_at": payload.get("generated_at"),
                "reason": str(diagnostics.get("reason") or ""),
                "live_client_reason": str(live.get("reason") or ""),
                "lcu_reason": str(lcu.get("reason") or ""),
                "ready_choice_count": sum(
                    1
                    for choice in (payload.get("choices") if isinstance(payload.get("choices"), list) else [])
                    if isinstance(choice, Mapping) and str(choice.get("state") or "") == "ready"
                ),
            }
        )
    samples.sort(key=lambda item: _float_or_none(item.get("generated_at")) or 0.0)
    return {
        "sample_count": len(samples),
        "status_counts": dict(Counter(item["status"] for item in samples if item["status"]).most_common(12)),
        "reason_counts": dict(Counter(item["reason"] for item in samples if item["reason"]).most_common(12)),
        "live_client_reason_counts": dict(
            Counter(item["live_client_reason"] for item in samples if item["live_client_reason"]).most_common(12)
        ),
        "lcu_reason_counts": dict(Counter(item["lcu_reason"] for item in samples if item["lcu_reason"]).most_common(12)),
        "ready_sample_count": sum(1 for item in samples if item["status"] == "candidates_ready" or item["ready_choice_count"] >= 3),
        "last_sample": samples[-1] if samples else {},
    }


def _refresh_action_summary(refresh_events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    ungrouped = 0
    for event in refresh_events:
        correlation_id = str(event.get("correlation_id") or "").strip()
        if not correlation_id:
            ungrouped += 1
            continue
        grouped.setdefault(correlation_id, []).append(event)

    actions: list[dict[str, Any]] = []
    for correlation_id, events in grouped.items():
        ordered = sorted(events, key=lambda item: _entry_timestamp(item) or 0.0)
        names = [str(item.get("event") or "") for item in ordered if item.get("event")]
        first_ts = _entry_timestamp(ordered[0]) if ordered else None
        last_ts = _entry_timestamp(ordered[-1]) if ordered else None
        status = "running"
        if any(name == "refresh.failed" for name in names):
            status = "failed"
        elif any(name in {"refresh.completed", "fallback.activated", "fallback.reused", "fallback.recovered"} for name in names):
            status = "completed"
        result_state = next(
            (
                str(item.get("result_state") or item.get("new_state") or item.get("state") or "")
                for item in reversed(ordered)
                if item.get("result_state") or item.get("new_state") or item.get("state")
            ),
            "",
        )
        actions.append(
            {
                "correlation_id": correlation_id,
                "status": status,
                "events": names,
                "started_at": first_ts,
                "last_at": last_ts,
                "duration_seconds": round(max(0.0, (last_ts or 0.0) - (first_ts or 0.0)), 3)
                if first_ts is not None and last_ts is not None
                else None,
                "result_state": result_state,
                "reason_code": next((str(item.get("reason_code") or "") for item in reversed(ordered) if item.get("reason_code")), ""),
                "warning_or_error_count": sum(1 for item in ordered if str(item.get("level") or "").upper() in {"WARNING", "ERROR"}),
            }
        )
    actions.sort(key=lambda item: item.get("last_at") or 0.0)
    running = [item for item in actions if item["status"] == "running"]
    return {
        "action_count": len(actions),
        "running_count": len(running),
        "ungrouped_refresh_event_count": ungrouped,
        "recent_actions": actions[-12:],
        "running_actions": running[-12:],
    }


def _summarize_refresh_events(event_paths: Sequence[Path], *, max_lines: int) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for path in event_paths:
        if path.suffix.lower() != ".jsonl":
            continue
        events.extend(_tail_jsonl_events(path, max_lines=max_lines))
    refresh_events = [
        event
        for event in events
        if str(event.get("event") or "").startswith(("refresh.", "fallback.", "state."))
    ]
    event_counts = Counter(str(event.get("event") or "") for event in refresh_events if event.get("event"))
    failure_events = [
        {
            "timestamp": event.get("timestamp"),
            "event": event.get("event"),
            "reason_code": event.get("reason_code"),
            "state": event.get("state") or event.get("new_state"),
            "message": event.get("error_message_sanitized") or event.get("message") or "",
        }
        for event in refresh_events
        if str(event.get("level") or "").upper() in {"ERROR", "WARNING"}
        or str(event.get("event") or "") in {"refresh.failed", "fallback.activated", "fallback.reused"}
    ]
    return {
        "event_counts": dict(event_counts.most_common(20)),
        "last_event": refresh_events[-1] if refresh_events else {},
        "recent_failures": failure_events[-10:],
        "actions": _refresh_action_summary(refresh_events),
    }


def _scraper_attempt_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    """从 startup/scraper 状态派生刷新诊断摘要，避免排查时只读原始 JSON。"""

    startup = state.get("startup_status") if isinstance(state.get("startup_status"), Mapping) else {}
    scraper = state.get("scraper_status") if isinstance(state.get("scraper_status"), Mapping) else {}
    attempt = scraper.get("last_attempt") if isinstance(scraper.get("last_attempt"), Mapping) else {}
    refresh_summary = startup.get("hextech_refresh") if isinstance(startup.get("hextech_refresh"), Mapping) else {}
    in_progress = list(startup.get("in_progress_tasks") or []) if isinstance(startup.get("in_progress_tasks"), list) else []
    updated_ts = _float_or_none(startup.get("updated_at"))
    stale_seconds = None
    if in_progress and updated_ts is not None:
        stale_seconds = round(max(0.0, time.time() - updated_ts), 3)

    active_csv = (
        refresh_summary.get("active_csv")
        or attempt.get("active_csv")
        or scraper.get("active_csv")
        or startup.get("active_hextech_csv")
        or ""
    )
    return {
        "attempt_id": scraper.get("last_attempt_id") or refresh_summary.get("attempt_id") or attempt.get("attempt_id", ""),
        "result": attempt.get("result") or scraper.get("last_result", ""),
        "reason": attempt.get("reason") or scraper.get("reason", ""),
        "failure_stage": attempt.get("failure_stage") or scraper.get("failure_stage") or refresh_summary.get("failure_stage", ""),
        "started_at": attempt.get("started_at") or refresh_summary.get("started_at", ""),
        "ended_at": attempt.get("ended_at") or refresh_summary.get("ended_at", ""),
        "duration_seconds": attempt.get("duration_seconds"),
        "total_heroes": attempt.get("total_heroes", 0),
        "completed_heroes": attempt.get("completed_heroes", 0),
        "cdn_hit_count": attempt.get("cdn_hit_count", scraper.get("cdn_hit_count", 0)),
        "slow_path_count": attempt.get("slow_path_count", scraper.get("slow_path_count", 0)),
        "success_rows": attempt.get("success_rows", scraper.get("success_rows", 0)),
        "failure_count": attempt.get("failure_count", 0),
        "failure_samples": attempt.get("failure_samples") or scraper.get("failure_samples", []),
        "active_csv": active_csv,
        "active_csv_mtime": scraper.get("active_csv_mtime", 0.0),
        "fallback_used": bool(
            attempt.get("fallback_used")
            or scraper.get("fallback_used")
            or refresh_summary.get("fallback_used")
            or startup.get("hextech_degraded")
        ),
        "blocked_until": scraper.get("blocked_until", ""),
        "next_retry_at": scraper.get("next_retry_at", ""),
        "remote_failure_escalated": bool(scraper.get("remote_failure_escalated", False)),
        "startup_in_progress_tasks": in_progress,
        "startup_in_progress_stale_seconds": stale_seconds,
        "startup_updated_at": startup.get("updated_at", ""),
    }


def _attention_items(
    *,
    trace_entries: Sequence[Mapping[str, Any]],
    trace_summary: Mapping[str, Any],
    state: Mapping[str, Any],
    refresh_summary: Mapping[str, Any],
    host_self_check: Mapping[str, Any],
    official_summary: Mapping[str, Any],
) -> list[str]:
    items: list[str] = []
    flags = state.get("feature_flags") if isinstance(state.get("feature_flags"), Mapping) else {}
    web_frontend = state.get("web_frontend") if isinstance(state.get("web_frontend"), Mapping) else {}
    sidecar = state.get("sidecar_status") if isinstance(state.get("sidecar_status"), Mapping) else {}
    overlay_event = state.get("overlay_event") if isinstance(state.get("overlay_event"), Mapping) else {}

    if not trace_entries:
        items.append("未发现 overlay_vision_trace_history.v1.json 记录；需要先启动游戏内显示/sidecar 再打一局。")
    if flags and not bool(flags.get("game_overlay_enabled", True)):
        items.append("ui_feature_flags 显示 game_overlay_enabled=false；本局不会产生完整游戏内识别 trace。")
    if web_frontend.get("status") == "web_disabled_until_user_action":
        items.append("Web 前端当前为按需启用：web_disabled_until_user_action；点击 Web/详情后才会启动服务。")
    elif web_frontend.get("status") == "startup_requested_no_port":
        items.append("ui_feature_flags 显示 web_frontend_enabled=true，但未发现 web_server_port.txt；检查 Web 启动错误。")
    if sidecar and sidecar.get("status") not in {"running", "ready"}:
        items.append(f"Vision sidecar 当前状态不是 running/ready：{sidecar.get('status') or 'unknown'}。")
    if trace_summary.get("game_window_missing_samples") and not trace_summary.get("active_hextech_samples"):
        items.append("记录到 game_window_missing 且未看到可见海克斯；优先确认游戏窗口名、无边框/窗口模式和分辨率。")
    if trace_summary.get("not_foreground_samples"):
        items.append("记录到 game_not_foreground；窗口切后台/切前台场景已覆盖，若选择期没有恢复需看对应时间段 trace。")
    if trace_summary.get("body_shard_samples"):
        items.append("记录到 body_shard 场景；锻体碎片不会显示三选一，需确认后续普通海克斯是否能恢复。")
    if trace_summary.get("hover_expired_samples"):
        items.append("记录到 hover_occluded_expired；鼠标悬停卡片可能仍会造成内容消失。")
    elif trace_summary.get("hover_occluded_samples"):
        items.append("记录到 hover_occluded；本局覆盖了鼠标悬停卡片场景，可检查是否仍保持稳定三槽。")
    if trace_summary.get("hextech_selection_activations", 0) < 2:
        items.append("本包未覆盖多次海克斯选择；快速自定义验收至少要看到 2 次以上 selection activation。")
    if (
        trace_summary.get("selection_button_present_samples")
        and not trace_summary.get("active_hextech_samples")
    ):
        items.append("检测到选择按钮但未稳定 active；重点查 ready_slots、partial_ready 和 template/文字识别置信度。")
    if trace_summary.get("longest_waiting_for_content_seconds", 0.0) >= 8.0:
        items.append("存在长时间内容获取未完成；优先查看 trace 的 reason/gate_state 与 debug_recent ROI dump。")
    refresh_counts = refresh_summary.get("event_counts") if isinstance(refresh_summary.get("event_counts"), Mapping) else {}
    refresh_actions = refresh_summary.get("actions") if isinstance(refresh_summary.get("actions"), Mapping) else {}
    if refresh_counts.get("refresh.failed") or refresh_counts.get("fallback.activated"):
        items.append("refresh 出现 failed/fallback；海克斯内容缺失可能来自数据刷新链路，而不是 overlay 识别。")
    if int(refresh_actions.get("running_count") or 0) > 0:
        items.append("存在未完成 refresh action；检查 supervisor_events 里的 correlation_id 是否卡住。")
    scraper_attempt = refresh_summary.get("scraper_attempt") if isinstance(refresh_summary.get("scraper_attempt"), Mapping) else {}
    if scraper_attempt.get("startup_in_progress_tasks"):
        items.append(
            "startup_status 仍有 in_progress_tasks："
            f"{scraper_attempt.get('startup_in_progress_tasks')}；检查刷新是否卡在 {scraper_attempt.get('failure_stage') or 'unknown'}。"
        )
    if scraper_attempt.get("failure_stage") and scraper_attempt.get("result") in {"fallback", "failed"}:
        items.append(
            f"最近一次 scraper attempt 在 {scraper_attempt.get('failure_stage')} 失败，"
            f"fallback_used={bool(scraper_attempt.get('fallback_used'))}。"
        )
    if host_self_check and not bool(host_self_check.get("event_ok", True)):
        items.append(f"host 自检读取当前事件异常：{host_self_check.get('event_error') or host_self_check.get('error_type') or 'unknown'}。")
    if int(official_summary.get("sample_count") or 0) > 0 and int(official_summary.get("ready_sample_count") or 0) <= 0:
        items.append("official provider debug 没有 candidates_ready；本局三槽内容主要依赖 vision sidecar。")
    if overlay_event and str(overlay_event.get("selection_type") or "") == "body_shard":
        items.append("当前 overlay_event 停在 body_shard；若已回到普通三选一，需要确认 sidecar 是否继续写新事件。")
    return items[:12]


def _trace_validation_summary(state_dir: Path, *, since_timestamp: float | None = None) -> dict[str, Any]:
    entries = _trace_entries(state_dir, since_timestamp=since_timestamp)
    timeline = _selection_epoch_timeline(entries)
    reason_counter = Counter(str(entry.get("reason") or "") for entry in entries if entry.get("reason"))
    gate_counter = Counter(str(entry.get("gate_state") or "") for entry in entries if entry.get("gate_state"))
    scene_counter = Counter(str(entry.get("scene_kind") or "") for entry in entries if entry.get("scene_kind"))
    layout_counter = Counter(str(entry.get("layout_id") or "") for entry in entries if entry.get("layout_id"))
    ready_counter = Counter(str(_int_value(entry, "ready_slots")) for entry in entries)
    hextech_epochs = {
        int(entry.get("selection_epoch") or 0)
        for entry in entries
        if _is_hextech_selection_observation(entry) and _int_value(entry, "selection_epoch") > 0
    }
    focus_block_samples = sum(1 for entry in entries if str(entry.get("reason") or "") in FOCUS_BLOCK_REASONS)
    blocking_modal_samples = sum(1 for entry in entries if str(entry.get("reason") or "") == "blocking_modal_present")
    scoreboard_samples = sum(1 for entry in entries if _truthy(entry, "scoreboard_key_down") or str(entry.get("reason") or "") == "scoreboard_key_down")
    hover_occluded_samples = sum(
        1 for entry in entries if _truthy(entry, "hover_occluded") or str(entry.get("reason") or "") == "hover_occluded"
    )
    hover_expired_samples = sum(1 for entry in entries if str(entry.get("reason") or "") == "hover_occluded_expired")
    visible_predicate = lambda entry: bool(entry.get("active") or entry.get("visible")) and str(entry.get("selection_type") or "") == "hextech"
    trace_summary = {
        "trace_entry_count": len(entries),
        "first_trace": _brief_trace(entries[0]) if entries else {},
        "last_trace": _brief_trace(entries[-1]) if entries else {},
        "reason_counts": dict(reason_counter.most_common(20)),
        "gate_state_counts": dict(gate_counter.most_common(20)),
        "scene_kind_counts": dict(scene_counter.most_common(12)),
        "layout_counts": dict(layout_counter.most_common(8)),
        "ready_slot_counts": dict(ready_counter),
        "selection_epoch_count": len(hextech_epochs),
        "selection_epochs": sorted(hextech_epochs)[-12:],
        "selection_epoch_timeline": timeline,
        "hextech_selection_activations": _activation_count(entries, _is_hextech_selection_observation),
        "active_hextech_samples": sum(1 for entry in entries if visible_predicate(entry)),
        "selection_button_present_samples": sum(1 for entry in entries if _truthy(entry, "selection_button_present")),
        "selection_window_active_samples": sum(1 for entry in entries if _truthy(entry, "selection_window_active")),
        "body_shard_samples": sum(1 for entry in entries if _is_body_shard(entry)),
        "blocking_modal_samples": blocking_modal_samples,
        "scoreboard_samples": scoreboard_samples,
        "hover_occluded_samples": hover_occluded_samples,
        "hover_expired_samples": hover_expired_samples,
        "not_foreground_samples": sum(1 for entry in entries if str(entry.get("reason") or "") == "game_not_foreground"),
        "game_window_missing_samples": sum(1 for entry in entries if str(entry.get("reason") or "") == "game_window_missing"),
        "focus_or_window_block_samples": focus_block_samples,
        "longest_visible_hextech_seconds": _longest_segment_seconds(entries, visible_predicate),
        "longest_waiting_for_content_seconds": _longest_segment_seconds(
            entries,
            _is_waiting_for_content,
            include_transition=True,
        ),
        "longest_focus_or_window_block_seconds": _longest_segment_seconds(
            entries,
            lambda entry: str(entry.get("reason") or "") in FOCUS_BLOCK_REASONS,
            include_transition=True,
        ),
        "longest_hover_occluded_seconds": _longest_segment_seconds(
            entries,
            lambda entry: _truthy(entry, "hover_occluded") or str(entry.get("reason") or "") == "hover_occluded",
            include_transition=True,
        ),
        "acceptance_observations": {
            "saw_multiple_hextech_selections": len(hextech_epochs) >= 2 or _activation_count(entries, _is_hextech_selection_observation) >= 2,
            "saw_visible_hextech": any(visible_predicate(entry) for entry in entries),
            "saw_body_shard": any(_is_body_shard(entry) for entry in entries),
            "saw_blocking_or_hidden": bool(blocking_modal_samples or scoreboard_samples),
            "saw_window_switch_or_background": bool(focus_block_samples),
            "saw_hover_occlusion": bool(hover_occluded_samples),
        },
    }
    return trace_summary


def _game_validation_summary(
    state_dir: Path,
    event_paths: Sequence[Path],
    debug_paths: Sequence[Path],
    *,
    max_lines: int,
    trace_since_timestamp: float | None = None,
) -> dict[str, Any]:
    state = _state_summary(state_dir)
    cache_dir = state_dir.parent / "cache"
    trace_entries = _trace_entries(state_dir, since_timestamp=trace_since_timestamp)
    trace_summary = _trace_validation_summary(state_dir, since_timestamp=trace_since_timestamp)
    refresh_summary = _summarize_refresh_events(event_paths, max_lines=max_lines)
    refresh_summary["scraper_attempt"] = _scraper_attempt_summary(state)
    try:
        default_state_dir = (get_runtime_root_dir() / "state").resolve()
        current_state_dir = state_dir.resolve()
    except OSError:
        default_state_dir = None
        current_state_dir = None
    host_self_check = (
        _run_host_self_check()
        if default_state_dir is not None and current_state_dir == default_state_dir
        else {"ok": None, "skipped": "custom_runtime_root"}
    )
    official_summary = _official_provider_summary(debug_paths)
    return {
        "purpose": "打一局快速自定义后定位：海克斯选择刷新、长时间信息获取、锻体碎片、阻塞/隐藏、窗口前后台和 refresh。",
        "state": {
            "feature_flags": state.get("feature_flags", {}),
            "sidecar_status": state.get("sidecar_status", {}),
            "context": state.get("context", {}),
            "overlay_trace": _brief_trace(state.get("overlay_trace") if isinstance(state.get("overlay_trace"), Mapping) else {}),
            "overlay_event": _brief_overlay_event(state.get("overlay_event") if isinstance(state.get("overlay_event"), Mapping) else {}),
            "trace_since_timestamp": trace_since_timestamp,
        },
        "selection_trace": trace_summary,
        "refresh": refresh_summary,
        "host_self_check": host_self_check,
        "official_provider": official_summary,
        "hint_cache": _hint_cache_diagnostics(cache_dir),
        "render_status": _render_status_diagnostics(state_dir, cache_dir),
        "attention_items": _attention_items(
            trace_entries=trace_entries,
            trace_summary=trace_summary,
            state=state,
            refresh_summary=refresh_summary,
            host_self_check=host_self_check,
            official_summary=official_summary,
        ),
        "custom_game_checklist": [
            "普通三选一：至少 2 次海克斯选择，观察 active_hextech_samples 和 selection_epoch_count。",
            "长时间信息获取：等待 8 秒以上不选，观察 longest_waiting_for_content_seconds 和 ready_slot_counts。",
            "鼠标悬停：把鼠标放到卡片上 2 秒以上，观察 hover_occluded_samples、hover_expired_samples 和三槽是否仍 visible。",
            "锻体碎片：遇到锻体碎片时观察 body_shard_samples，随后回普通三选一确认可恢复。",
            "阻塞/隐藏：打开计分板或阻塞弹窗，观察 scoreboard_samples/blocking_modal_samples。",
            "窗口切换：切后台再切前台，观察 not_foreground_samples 和恢复后的 active_hextech_samples。",
        ],
        "quick_customization_scope": {
            "can_observe": [
                "overlay 是否在普通三选一出现、是否有空窗、是否 hover 后消失。",
                "vision trace 的 ready_slots、gate_state、selection_epoch、hover_occluded、blocking/modal 和前后台切换。",
                "当前 renderer 显示 READY/NO_STATS/CONTEXT_MISSING/DETECTING 的原因分布。",
                "refresh 是否失败、fallback、卡住，official provider 是否给出 candidates_ready。",
            ],
            "cannot_solve_in_one_game": [
                "不能证明所有英雄和所有海克斯都有统计覆盖；只能证明本局遇到的组合。",
                "不能补齐源 CSV 缺失的英雄-海克斯统计，只能定位缺口属于源数据还是 context/alias。",
                "不能覆盖所有分辨率和窗口模式，只覆盖本局实际运行环境。",
            ],
        },
    }


def _compact_watch_record(summary: Mapping[str, Any], *, snapshot_index: int) -> dict[str, Any]:
    validation = summary.get("game_validation") if isinstance(summary.get("game_validation"), Mapping) else {}
    trace = validation.get("selection_trace") if isinstance(validation.get("selection_trace"), Mapping) else {}
    render = validation.get("render_status") if isinstance(validation.get("render_status"), Mapping) else {}
    official = validation.get("official_provider") if isinstance(validation.get("official_provider"), Mapping) else {}
    refresh = validation.get("refresh") if isinstance(validation.get("refresh"), Mapping) else {}
    refresh_actions = refresh.get("actions") if isinstance(refresh.get("actions"), Mapping) else {}
    return {
        "snapshot_index": snapshot_index,
        "created_at": summary.get("created_at"),
        "bundle_dir": summary.get("bundle_dir"),
        "trace_entry_count": trace.get("trace_entry_count"),
        "selection_epoch_count": trace.get("selection_epoch_count"),
        "active_hextech_samples": trace.get("active_hextech_samples"),
        "hover_occluded_samples": trace.get("hover_occluded_samples"),
        "hover_expired_samples": trace.get("hover_expired_samples"),
        "body_shard_samples": trace.get("body_shard_samples"),
        "context_error": (render.get("context") or {}).get("error") if isinstance(render.get("context"), Mapping) else "",
        "render_status_counts": render.get("status_counts"),
        "official_ready_sample_count": official.get("ready_sample_count"),
        "refresh_running_count": refresh_actions.get("running_count") if isinstance(refresh_actions, Mapping) else None,
        "attention_items": validation.get("attention_items") if isinstance(validation.get("attention_items"), list) else [],
    }


def watch_runtime_diagnostics(
    *,
    output_dir: Path | None = None,
    recent_minutes: int = DEFAULT_RECENT_MINUTES,
    tail_lines: int = DEFAULT_TAIL_LINES,
    interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
    duration_minutes: float = DEFAULT_WATCH_DURATION_MINUTES,
    max_snapshots: int | None = None,
    runtime_root: Path | None = None,
    sleep_func=time.sleep,
) -> dict[str, Any]:
    """周期性生成只读诊断快照；Ctrl+C 停止时保留已有快照。"""

    runtime_root = Path(runtime_root) if runtime_root is not None else get_runtime_root_dir()
    session_root = output_dir or runtime_root / "reports" / "runtime_diagnostics_watch" / _utc_stamp()
    # watch 通常由启动脚本先创建目录用于 stdout/stderr；允许复用空目录，
    # 每个 snapshot 子目录仍带序号和时间戳，避免覆盖已有采样。
    session_root.mkdir(parents=True, exist_ok=True)
    snapshots_root = session_root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    events_path = session_root / WATCH_EVENTS_FILE
    started_at = time.time()
    interval = max(1.0, float(interval_seconds))
    duration_seconds = max(0.0, float(duration_minutes)) * 60.0
    records: list[dict[str, Any]] = []
    index = 0
    try:
        while True:
            index += 1
            snapshot_dir = snapshots_root / f"{index:04d}-{_utc_stamp()}"
            summary = collect_runtime_diagnostics(
                output_dir=snapshot_dir,
                recent_minutes=recent_minutes,
                tail_lines=tail_lines,
                runtime_root=runtime_root,
                trace_since_timestamp=started_at,
            )
            record = _compact_watch_record(summary, snapshot_index=index)
            records.append(record)
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            (session_root / LATEST_SUMMARY_FILE).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if max_snapshots is not None and index >= max(1, int(max_snapshots)):
                break
            if duration_seconds and (time.time() - started_at) >= duration_seconds:
                break
            sleep_func(interval)
    except KeyboardInterrupt:
        pass
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_root": str(runtime_root),
        "watch_dir": str(session_root),
        "snapshot_count": len(records),
        "interval_seconds": interval,
        "duration_minutes": duration_minutes,
        "latest_record": records[-1] if records else {},
        "events_file": str(events_path),
        "latest_summary_file": str(session_root / LATEST_SUMMARY_FILE),
    }
    manifest = redact_diagnostic_value("watch_summary", manifest)
    (session_root / "watch_summary.json").write_text(
        json.dumps(_redact_bundle_value(manifest), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def collect_runtime_diagnostics(
    *,
    output_dir: Path | None = None,
    recent_minutes: int = DEFAULT_RECENT_MINUTES,
    tail_lines: int = DEFAULT_TAIL_LINES,
    runtime_root: Path | None = None,
    trace_since_timestamp: float | None = None,
) -> dict[str, Any]:
    runtime_root = Path(runtime_root) if runtime_root is not None else get_runtime_root_dir()
    reports_root = output_dir or runtime_root / "reports" / "runtime_diagnostics" / _utc_stamp()
    reports_root.mkdir(parents=True, exist_ok=False)
    state_dir = runtime_root / "state"
    logs_dir = runtime_root / "logs"
    debug_dir = runtime_root / "debug"
    since_timestamp = time.time() - max(1, int(recent_minutes)) * 60
    copied: list[dict[str, Any]] = []
    skipped_sensitive: list[str] = []

    if state_dir.exists():
        for path in sorted(state_dir.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            if _is_sensitive_path(path):
                skipped_sensitive.append(_safe_relative(path, runtime_root))
                continue
            if path.name in STATE_TAIL_FILES or path.suffix.lower() in {".jsonl"}:
                _write_tail(path, reports_root / "state_tail" / f"{path.name}.tail", max_lines=tail_lines, copied=copied)
            elif path.suffix.lower() in SAFE_STATE_EXTENSIONS:
                _copy_json_file_redacted(path, reports_root / "state" / path.name, copied=copied)

    log_paths: list[Path] = []
    for path in _iter_recent_files(logs_dir, since_timestamp=since_timestamp, extensions=TAIL_EXTENSIONS):
        log_paths.append(path)
        _write_tail(path, reports_root / "logs_tail" / f"{path.name}.tail", max_lines=tail_lines, copied=copied)

    debug_paths: list[Path] = []
    for path in _iter_recent_files(debug_dir, since_timestamp=since_timestamp, extensions=SAFE_DEBUG_EXTENSIONS):
        debug_paths.append(path)
        relative = _safe_relative(path, debug_dir)
        _copy_debug_file_redacted(path, reports_root / "debug_recent" / relative, copied=copied)

    event_paths = [
        path
        for path in (
            state_dir / "supervisor_events.v1.jsonl",
            state_dir / "runtime_events.v1.jsonl",
            *log_paths,
        )
        if path.exists() and not _is_sensitive_path(path)
    ]
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime_root": str(runtime_root),
        "bundle_dir": str(reports_root),
        "recent_minutes": int(recent_minutes),
        "tail_lines": int(tail_lines),
        "state": _state_summary(state_dir),
        "events": _event_summary(event_paths, max_lines=tail_lines),
        "logs": _log_summary(log_paths, max_lines=tail_lines),
        "game_validation": _game_validation_summary(
            state_dir,
            event_paths,
            debug_paths,
            max_lines=tail_lines,
            trace_since_timestamp=trace_since_timestamp,
        ),
        "copied_files": copied,
        "debug_recent_file_count": len(debug_paths),
        "skipped_sensitive": skipped_sensitive,
    }
    summary = redact_diagnostic_value("summary", summary)
    (reports_root / "summary.json").write_text(
        json.dumps(_redact_bundle_value(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (reports_root / "README.txt").write_text(
        "\n".join(
            [
                "Hextech 运行态诊断包",
                "",
                "使用方式：真实对局结束后运行本工具，把本目录交给 Codex/人工复盘。",
                "也可在真实对局前使用 --watch 周期性写 snapshots/ 与 watch_events.jsonl。",
                "本工具只读 runtime 文件，不截图、不联网。",
                "已跳过 auth/token/cookie/secret 等敏感文件名。",
                "",
                "优先查看：summary.json、state/、state_tail/、logs_tail/。",
                "快速自定义验收重点：summary.json -> game_validation。",
                "game_validation 会汇总多次海克斯选择、长时间信息获取、锻体碎片、阻塞/隐藏、窗口切换和 refresh 状态。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采集 Hextech 运行态日志和状态，用于真实对局期间/赛后复盘。")
    parser.add_argument("--output-dir", default="", help="输出目录；默认写入 data/runtime/reports/runtime_diagnostics/<timestamp>。")
    parser.add_argument("--recent-minutes", type=int, default=DEFAULT_RECENT_MINUTES, help="只采集最近 N 分钟的 logs/debug 文件。")
    parser.add_argument("--tail-lines", type=int, default=DEFAULT_TAIL_LINES, help="每个 log/jsonl 最多保留末尾行数。")
    parser.add_argument("--watch", action="store_true", help="开启轻量 watch，周期性写诊断快照，Ctrl+C 停止。")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_WATCH_INTERVAL_SECONDS, help="watch 快照间隔秒数。")
    parser.add_argument("--duration-minutes", type=float, default=DEFAULT_WATCH_DURATION_MINUTES, help="watch 总时长；0 表示直到 Ctrl+C。")
    parser.add_argument("--max-snapshots", type=int, default=0, help="watch 最多快照数；0 表示不按数量限制。")
    parser.add_argument("--json", action="store_true", help="输出完整 summary JSON；默认只输出目录和摘要。")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir) if str(args.output_dir or "").strip() else None
    if args.watch:
        manifest = watch_runtime_diagnostics(
            output_dir=output_dir,
            recent_minutes=args.recent_minutes,
            tail_lines=args.tail_lines,
            interval_seconds=args.interval_seconds,
            duration_minutes=args.duration_minutes,
            max_snapshots=(int(args.max_snapshots) if int(args.max_snapshots or 0) > 0 else None),
        )
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
        else:
            print(f"watch_dir={manifest['watch_dir']}")
            print(f"snapshot_count={manifest['snapshot_count']}")
            print(f"events_file={manifest['events_file']}")
            print(f"latest_summary_file={manifest['latest_summary_file']}")
        return 0
    summary = collect_runtime_diagnostics(
        output_dir=output_dir,
        recent_minutes=args.recent_minutes,
        tail_lines=args.tail_lines,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"diagnostic_bundle={summary['bundle_dir']}")
        print(f"copied_files={len(summary['copied_files'])}")
        print(f"recent_problem_lines={len(summary['logs']['recent_problem_lines'])}")
        print(f"recent_event_problems={len(summary['events']['recent_problems'])}")
        validation = summary["game_validation"]
        trace = validation["selection_trace"]
        print(f"trace_entries={trace['trace_entry_count']}")
        print(f"hextech_selection_activations={trace['hextech_selection_activations']}")
        print(f"attention_items={len(validation['attention_items'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

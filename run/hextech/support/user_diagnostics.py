from __future__ import annotations

"""用户侧轻量诊断导出。

该模块允许进入打包产物，只做只读 tail、白名单 state 复制、脱敏和 zip 封装。
开发态全量采集继续由 `tools/collect_runtime_diagnostics.py` 负责，避免把 debug/raw/cache/profile 带给用户导出面。
"""

import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from hextech.catalog.runtime_store import ensure_private_runtime_dir, get_runtime_root_dir
from hextech.support.log_utils import redact_log_value

SENSITIVE_PATH_KEYWORDS = (
    "auth",
    "authorization",
    "token",
    "cookie",
    "secret",
    "password",
    "credential",
    "nonce",
    "lcu",
    "riot",
    "overlay_anchor_calibration",
)
STATE_JSON_ALLOWLIST = {
    "startup_status.json",
    "scraper_status.json",
    "ui_feature_flags.json",
    "game_overlay_context.v1.json",
    "game_overlay_sidecar_status.json",
    "game_overlay_slots.v1.json",
    "overlay_vision_trace.v1.json",
    "overlay_vision_trace_history.v1.json",
}
STATE_TAIL_ALLOWLIST = {
    "runtime_events.v1.jsonl",
    "supervisor_events.v1.jsonl",
}
LOG_TAIL_ALLOWLIST = {
    "hextech_runtime_summary.log",
    "hextech_error.log",
}
SENSITIVE_REPORT_DIRS = {"state", "logs"}
TIMESTAMP_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
)


@dataclass(frozen=True)
class DiagnosticsExportResult:
    bundle_dir: Path
    zip_path: Path
    copied_files: int
    skipped_sensitive: list[str]
    warnings: list[str]


def _timestamp_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _is_sensitive_path(path: Path) -> bool:
    lowered = path.as_posix().lower()
    return any(keyword in lowered for keyword in SENSITIVE_PATH_KEYWORDS)


def _redact_json_value(key: str, value):
    key_lower = str(key or "").lower()
    if any(keyword in key_lower for keyword in SENSITIVE_PATH_KEYWORDS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _redact_json_value(str(child_key), child_value) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(key, item) for item in value]
    if isinstance(value, str):
        return redact_log_value(value)
    return value


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): _redact_json_value(str(key), value) for key, value in payload.items()}


def _timestamp_to_epoch(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    if "," in normalized:
        normalized = normalized.replace(",", ".", 1)
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _line_timestamp(line: str) -> float | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in ("generated_at", "updated_at", "timestamp"):
            parsed = _timestamp_to_epoch(payload.get(key))
            if parsed is not None:
                return parsed

    match = TIMESTAMP_PREFIX_RE.match(line.strip())
    if not match:
        return None
    return _timestamp_to_epoch(match.group(1))


def _recent_tail_lines(path: Path, *, line_count: int, since_timestamp: float | None) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if line_count <= 0 or not lines:
        return []
    if since_timestamp is None:
        return lines[-line_count:]

    parsed = [(line, _line_timestamp(line)) for line in lines]
    if any(timestamp is not None for _, timestamp in parsed):
        filtered: list[str] = []
        keep_continuation = False
        for line, timestamp in parsed:
            if timestamp is not None:
                keep_continuation = timestamp >= since_timestamp
            if keep_continuation:
                filtered.append(line)
        return filtered[-line_count:]

    try:
        if path.stat().st_mtime < since_timestamp:
            return []
    except OSError:
        return []
    return lines[-line_count:]


def _write_text_tail(source: Path, target: Path, *, tail_lines: int, since_timestamp: float | None) -> bool:
    lines = _recent_tail_lines(source, line_count=tail_lines, since_timestamp=since_timestamp)
    if not lines:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    redacted = [redact_log_value(line) for line in lines]
    target.write_text("\n".join(redacted) + "\n", encoding="utf-8")
    return True


def _write_json_copy(source: Path, target: Path) -> bool:
    payload = _read_json(source)
    if not payload:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True


def _iter_skipped_sensitive(runtime_root: Path) -> list[str]:
    skipped: list[str] = []
    for dirname in SENSITIVE_REPORT_DIRS:
        directory = runtime_root / dirname
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and _is_sensitive_path(path):
                try:
                    skipped.append(path.relative_to(runtime_root).as_posix())
                except ValueError:
                    skipped.append(path.name)
    return sorted(set(skipped))


def _zip_directory(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file() and path != zip_path:
                archive.write(path, path.relative_to(source_dir).as_posix())


def export_user_diagnostics(
    output_dir: Path | None = None,
    recent_minutes: int = 180,
    tail_lines: int = 500,
) -> DiagnosticsExportResult:
    runtime_root = Path(get_runtime_root_dir())
    export_root = Path(output_dir) if output_dir is not None else runtime_root / "reports" / "user_diagnostics"
    ensure_private_runtime_dir(export_root)
    bundle_dir = export_root / _timestamp_label()
    suffix = 1
    while bundle_dir.exists():
        suffix += 1
        bundle_dir = export_root / f"{_timestamp_label()}_{suffix:02d}"
    ensure_private_runtime_dir(bundle_dir)

    copied_files = 0
    warnings: list[str] = []
    skipped_sensitive = _iter_skipped_sensitive(runtime_root)
    since_timestamp = time.time() - max(1, int(recent_minutes)) * 60

    state_dir = runtime_root / "state"
    for filename in sorted(STATE_JSON_ALLOWLIST):
        source = state_dir / filename
        if not source.is_file() or _is_sensitive_path(source):
            continue
        if _write_json_copy(source, bundle_dir / "state" / filename):
            copied_files += 1

    for filename in sorted(STATE_TAIL_ALLOWLIST):
        source = state_dir / filename
        if not source.is_file() or _is_sensitive_path(source):
            continue
        if _write_text_tail(
            source,
            bundle_dir / "state_tail" / f"{filename}.tail",
            tail_lines=tail_lines,
            since_timestamp=since_timestamp,
        ):
            copied_files += 1

    logs_dir = runtime_root / "logs"
    for filename in sorted(LOG_TAIL_ALLOWLIST):
        source = logs_dir / filename
        if not source.is_file() or _is_sensitive_path(source):
            continue
        if _write_text_tail(
            source,
            bundle_dir / "logs_tail" / f"{filename}.tail",
            tail_lines=tail_lines,
            since_timestamp=since_timestamp,
        ):
            copied_files += 1

    readme = (
        "Hextech 用户诊断导出\n"
        "本包只包含限量日志尾部、白名单 state 和摘要，不包含 debug/cache/profile/raw/reports。\n"
        f"recent_minutes={int(recent_minutes)} tail_lines={int(tail_lines)}\n"
    )
    (bundle_dir / "README.txt").write_text(readme, encoding="utf-8")
    copied_files += 1

    result_without_summary = DiagnosticsExportResult(
        bundle_dir=bundle_dir,
        zip_path=bundle_dir.with_suffix(".zip"),
        copied_files=copied_files + 1,
        skipped_sensitive=skipped_sensitive,
        warnings=warnings,
    )
    summary = {
        **asdict(result_without_summary),
        "bundle_dir": str(result_without_summary.bundle_dir),
        "zip_path": str(result_without_summary.zip_path),
        "runtime_root": str(runtime_root),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recent_minutes": int(recent_minutes),
        "tail_lines": int(tail_lines),
    }
    (bundle_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _zip_directory(bundle_dir, result_without_summary.zip_path)
    return result_without_summary

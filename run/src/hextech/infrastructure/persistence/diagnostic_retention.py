"""持续诊断的统一留存预算与后台清理。

这里只登记可重建的诊断文件；generation、source run、Catalog、模型、模板、
设置和用户数据从不进入 registry。调用方只发出一次合并后的清理请求，目录遍历、
分组和删除都在后台线程完成，生产识别与 Tk 主线程不等待磁盘。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir
from hextech.infrastructure.persistence.file_lock import InterProcessFileLock


MIB = 1024 * 1024
DIAGNOSTIC_GLOBAL_LIMIT_BYTES = 128 * MIB
DIAGNOSTIC_RETENTION_SCHEMA_VERSION = 1
VISION_TIMELINE_MAX_BYTES = 1 * MIB
VISION_TIMELINE_MAX_ENTRIES = 512
DIAGNOSTIC_RETENTION_MIN_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class DiagnosticPolicy:
    name: str
    limit_bytes: int
    max_age: timedelta
    max_items: int | None
    eviction_priority: int


POLICIES: tuple[DiagnosticPolicy, ...] = (
    DiagnosticPolicy("session_evidence", 48 * MIB, timedelta(days=30), 100, 5),
    DiagnosticPolicy("overlay_vision_debug", 28 * MIB, timedelta(days=7), 32, 2),
    DiagnosticPolicy("failure_inbox", 12 * MIB, timedelta(days=30), 200, 6),
    DiagnosticPolicy("vision_timelines", 12 * MIB, timedelta(days=14), 20, 3),
    DiagnosticPolicy("overlay_session_reports", 8 * MIB, timedelta(days=14), 200, 3),
    DiagnosticPolicy("runtime_logs", 12 * MIB, timedelta(days=14), None, 1),
    DiagnosticPolicy("supervisor_events", 4 * MIB, timedelta(days=14), 4, 0),
    DiagnosticPolicy("trace_status", 4 * MIB, timedelta(days=14), None, 1),
)
POLICY_BY_NAME = {policy.name: policy for policy in POLICIES}


@dataclass(frozen=True)
class DiagnosticItem:
    category: str
    key: str
    paths: tuple[Path, ...]
    modified_at: float
    byte_size: int
    pinned: bool = False


def retention_state_path(runtime_root: str | Path | None = None) -> Path:
    root = Path(runtime_root) if runtime_root is not None else get_var_dir()
    return root / "state" / "diagnostic_retention.v1.json"


def _retention_lock_path(root: Path) -> Path:
    return root / "locks" / "diagnostic-retention.lock"


def _parse_iso_timestamp(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _selection_active(root: Path, *, now_timestamp: float) -> bool:
    payload = _read_object(root / "state" / "game_overlay_slots.v1.json")
    if not payload or not bool(payload.get("active")):
        return False
    try:
        generated_at = float(payload.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return generated_at > 0.0 and now_timestamp - generated_at <= 10.0


def _skipped_result(
    existing: Mapping[str, Any],
    *,
    disposition: str,
    skip_reason: str,
    next_eligible_at: float,
    lock_acquired: bool,
) -> dict[str, Any]:
    result = dict(existing)
    previous_last_run = (
        existing.get("last_run") if isinstance(existing.get("last_run"), Mapping) else {}
    )
    result["last_run"] = {
        **{str(key): value for key, value in previous_last_run.items()},
        "disposition": disposition,
        "skip_reason": skip_reason,
        "lock_acquired": lock_acquired,
    }
    result["next_eligible_at"] = datetime.fromtimestamp(
        next_eligible_at,
        tz=timezone.utc,
    ).isoformat()
    return result


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _is_reparse(path: Path) -> bool:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _inside_registered_root(path: Path, registered_root: Path) -> bool:
    """删除前逐层验证边界；任一 reparse point 都按不可安全清理处理。"""

    try:
        root = registered_root.resolve(strict=False)
        target = path.resolve(strict=False)
        if os.path.commonpath((str(root), str(target))) != str(root):
            return False
        relative = path.absolute().relative_to(registered_root.absolute())
    except (OSError, ValueError):
        return False
    current = registered_root
    if current.exists() and _is_reparse(current):
        return False
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            return False
    return True


def _path_size(path: Path) -> int:
    if path.is_file():
        try:
            return max(0, int(path.stat().st_size))
        except OSError:
            return 0
    total = 0
    if path.is_dir():
        try:
            for child in path.rglob("*"):
                if child.is_file() and not _is_reparse(child):
                    total += max(0, int(child.stat().st_size))
        except OSError:
            return total
    return total


def _mtime(paths: Iterable[Path]) -> float:
    values: list[float] = []
    for path in paths:
        try:
            values.append(float(path.stat().st_mtime))
        except OSError:
            continue
    return max(values, default=0.0)


def _item(category: str, key: str, paths: Iterable[Path], *, pinned: bool = False) -> DiagnosticItem:
    existing = tuple(dict.fromkeys(path for path in paths if path.exists()))
    return DiagnosticItem(
        category=category,
        key=key,
        paths=existing,
        modified_at=_mtime(existing),
        byte_size=sum(_path_size(path) for path in existing),
        pinned=pinned,
    )


def _session_evidence_items(root: Path) -> list[DiagnosticItem]:
    evidence_root = root / "state" / "session_evidence"
    if not evidence_root.is_dir() or _is_reparse(evidence_root):
        return []
    latest = evidence_root / "latest_real_session.v2.json"
    items: list[DiagnosticItem] = []
    referenced_screenshots: set[Path] = set()
    for report in evidence_root.glob("overlay-*.v2.json"):
        payload = _read_object(report)
        screenshot_name = str(payload.get("screenshot") or "").strip()
        paths = [report]
        if screenshot_name:
            screenshot = evidence_root / Path(screenshot_name).name
            if screenshot.exists():
                paths.append(screenshot)
                referenced_screenshots.add(screenshot)
        items.append(_item("session_evidence", report.stem, paths))
    for screenshot in evidence_root.glob("overlay-*.png"):
        if screenshot not in referenced_screenshots:
            items.append(_item("session_evidence", screenshot.stem, (screenshot,)))
    if latest.exists():
        items.append(_item("session_evidence", "latest", (latest,), pinned=True))
    return items


def _debug_items(root: Path) -> list[DiagnosticItem]:
    debug_root = root / "debug" / "overlay_vision"
    if not debug_root.is_dir() or _is_reparse(debug_root):
        return []
    items: list[DiagnosticItem] = []
    containers = (debug_root / "overlay_roi_v2", debug_root / "vnext-sessions")
    claimed: set[Path] = set()
    for container in containers:
        if not container.is_dir() or _is_reparse(container):
            continue
        for child in container.iterdir():
            if _is_reparse(child):
                continue
            claimed.add(child)
            prefix = "vnext" if container.name == "vnext-sessions" else "roi"
            items.append(_item("overlay_vision_debug", f"{prefix}:{child.name}", (child,)))
    for child in debug_root.iterdir():
        if child in containers or child in claimed or _is_reparse(child):
            continue
        items.append(_item("overlay_vision_debug", f"legacy:{child.name}", (child,)))
    return items


def _failure_items(root: Path) -> list[DiagnosticItem]:
    inbox = root / "recognition" / "failure-inbox"
    if not inbox.is_dir() or _is_reparse(inbox):
        return []
    index = _read_object(inbox / "index.v1.json")
    records = index.get("records") if isinstance(index.get("records"), Mapping) else {}
    items: list[DiagnosticItem] = []
    for record_id, raw_entry in records.items():
        if not isinstance(raw_entry, Mapping):
            continue
        record = inbox / str(raw_entry.get("record_relative_path") or "")
        payload = _read_object(record)
        paths = [record]
        for field in ("title_roi", "icon_roi", "button_roi"):
            blob = payload.get(field) if isinstance(payload.get(field), Mapping) else {}
            relative = str(blob.get("relative_path") or "")
            if relative:
                paths.append(inbox / relative)
        items.append(_item("failure_inbox", str(record_id), paths))
    index_path = inbox / "index.v1.json"
    if index_path.exists():
        items.append(_item("failure_inbox", "index", (index_path,), pinned=True))
    return items


def _file_items(
    category: str,
    directory: Path,
    patterns: tuple[str, ...],
    *,
    pinned_names: frozenset[str] = frozenset(),
) -> list[DiagnosticItem]:
    if not directory.is_dir() or _is_reparse(directory):
        return []
    paths: dict[Path, None] = {}
    for pattern in patterns:
        for path in directory.glob(pattern):
            if path.is_file() and not _is_reparse(path):
                paths[path] = None
    return [
        _item(category, path.name, (path,), pinned=path.name in pinned_names)
        for path in paths
    ]


def _vision_timeline_items(root: Path) -> list[DiagnosticItem]:
    """只允许现行 v2 timeline 进入淘汰；旧证据和不确定文件永久只读。"""

    timeline_root = root / "state" / "overlay_vision_timelines"
    if not timeline_root.is_dir() or _is_reparse(timeline_root):
        return []
    items: list[DiagnosticItem] = []
    for path in timeline_root.glob("selection-*.jsonl"):
        if not path.is_file() or _is_reparse(path):
            continue
        schema_version: object = None
        try:
            with path.open("r", encoding="utf-8") as stream:
                for raw_line in stream:
                    if not raw_line.strip():
                        continue
                    payload = json.loads(raw_line)
                    if isinstance(payload, Mapping):
                        schema_version = payload.get("schema_version")
                    break
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            schema_version = None
        items.append(
            _item(
                "vision_timelines",
                path.name,
                (path,),
                pinned=schema_version != 2,
            )
        )
    return items


def collect_diagnostic_items(runtime_root: str | Path | None = None) -> dict[str, list[DiagnosticItem]]:
    root = Path(runtime_root) if runtime_root is not None else get_var_dir()
    state = root / "state"
    return {
        "session_evidence": _session_evidence_items(root),
        "overlay_vision_debug": _debug_items(root),
        "failure_inbox": _failure_items(root),
        "vision_timelines": _vision_timeline_items(root),
        "overlay_session_reports": _file_items(
            "overlay_session_reports",
            root / "reports" / "overlay_sessions",
            ("overlay-session-*.json", "latest.json"),
            pinned_names=frozenset({"latest.json"}),
        ),
        "runtime_logs": _file_items("runtime_logs", root / "logs", ("**/*",)),
        "supervisor_events": _file_items(
            "supervisor_events", state, ("supervisor_events.v1.jsonl*",)
        ),
        "trace_status": _file_items(
            "trace_status",
            state,
            (
                "overlay_vision_trace*.json",
                "runtime_events.v1.jsonl*",
                "overlay_sidecar.*.bootstrap.json",
                "overlay_sidecar.*.ready.json",
                "overlay_sidecar.*.exit.json",
                "game_overlay_host.*.exit.json",
                "diagnostic_retention.v1.json",
                "game_overlay_sidecar_status.json",
                "game_overlay_visibility.v1.json",
                "game_overlay_host_status.json",
                "startup_timing.v1.json",
                "background_runtime_transitions.v1.json",
            ),
            pinned_names=frozenset(
                {
                    "overlay_vision_trace.v1.json",
                    "overlay_vision_trace_history.v1.json",
                    "diagnostic_retention.v1.json",
                    "game_overlay_sidecar_status.json",
                    "game_overlay_visibility.v1.json",
                    "game_overlay_host_status.json",
                    "startup_timing.v1.json",
                    "background_runtime_transitions.v1.json",
                }
            ),
        ),
    }


def _registered_root(root: Path, category: str, path: Path) -> Path | None:
    candidates: dict[str, tuple[Path, ...]] = {
        "session_evidence": (root / "state" / "session_evidence",),
        "overlay_vision_debug": (root / "debug" / "overlay_vision",),
        "failure_inbox": (root / "recognition" / "failure-inbox",),
        "vision_timelines": (root / "state" / "overlay_vision_timelines",),
        "overlay_session_reports": (root / "reports" / "overlay_sessions",),
        "runtime_logs": (root / "logs",),
        "supervisor_events": (root / "state",),
        "trace_status": (root / "state",),
    }
    for candidate in candidates.get(category, ()):
        if _inside_registered_root(path, candidate):
            return candidate
    return None


def _delete_item(root: Path, item: DiagnosticItem) -> tuple[int, str]:
    deleted = 0
    for path in item.paths:
        registered = _registered_root(root, item.category, path)
        if registered is None:
            return deleted, f"unsafe_path:{item.category}:{path.name}"
        # failure blob 可能被另一条 record 复用；这里只删 record，随后统一按剩余
        # index 引用回收孤儿 blob，不能在分组淘汰时先删共享图片。
        if item.category == "failure_inbox" and path.suffix.casefold() == ".png":
            continue
        try:
            size = _path_size(path)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            deleted += size
        except OSError as exc:
            return deleted, f"{exc.__class__.__name__}:{item.category}:{path.name}"
    return deleted, ""


def _repair_failure_index(root: Path) -> None:
    inbox = root / "recognition" / "failure-inbox"
    index_path = inbox / "index.v1.json"
    index = _read_object(index_path)
    if not index:
        return
    raw_records = index.get("records") if isinstance(index.get("records"), Mapping) else {}
    records: dict[str, Any] = {}
    for record_id, entry in raw_records.items():
        if not isinstance(entry, Mapping):
            continue
        record_path = inbox / str(entry.get("record_relative_path") or "")
        if record_path.is_file():
            records[str(record_id)] = dict(entry)
    slot_keys = {
        str(key): str(record_id)
        for key, record_id in (index.get("slot_keys") or {}).items()
        if str(record_id) in records
    }
    referenced_blobs: set[str] = set()
    for entry in records.values():
        payload = _read_object(inbox / str(entry.get("record_relative_path") or ""))
        for field in ("title_roi", "icon_roi", "button_roi"):
            blob = payload.get(field) if isinstance(payload.get(field), Mapping) else {}
            relative = str(blob.get("relative_path") or "")
            if relative:
                referenced_blobs.add(relative)
    blobs_root = inbox / "blobs"
    if blobs_root.is_dir() and not _is_reparse(blobs_root):
        for blob in blobs_root.glob("*.png"):
            if f"blobs/{blob.name}" not in referenced_blobs and _inside_registered_root(blob, inbox):
                blob.unlink(missing_ok=True)
    atomic_write_json(
        index_path,
        {"schema_version": 1, "records": records, "slot_keys": slot_keys},
        ensure_ascii=False,
        indent=2,
    )


def _summarize(items: Mapping[str, list[DiagnosticItem]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for policy in POLICIES:
        category_items = items.get(policy.name, [])
        mtimes = [item.modified_at for item in category_items if item.modified_at > 0]
        result[policy.name] = {
            "file_group_count": len(category_items),
            "byte_size": sum(item.byte_size for item in category_items),
            "oldest_at": datetime.fromtimestamp(min(mtimes), tz=timezone.utc).isoformat() if mtimes else "",
            "latest_at": datetime.fromtimestamp(max(mtimes), tz=timezone.utc).isoformat() if mtimes else "",
            "limit_bytes": policy.limit_bytes,
            "max_items": policy.max_items,
            "max_age_seconds": int(policy.max_age.total_seconds()),
        }
    return result


def _timeline_exceeds_file_limit(item: DiagnosticItem) -> bool:
    if item.byte_size > VISION_TIMELINE_MAX_BYTES:
        return True
    for path in item.paths:
        try:
            with path.open("rb") as stream:
                if sum(1 for _line in stream) > VISION_TIMELINE_MAX_ENTRIES:
                    return True
        except OSError:
            return False
    return False


def _category_eviction_key(item: DiagnosticItem) -> tuple[int, float, str]:
    # 旧 full-frame/vnext 诊断先于现行 ROI；其他分类保持最旧优先。
    legacy_priority = (
        0
        if item.category == "overlay_vision_debug"
        and (item.key.startswith("legacy:") or item.key.startswith("vnext:"))
        else 1
    )
    return (legacy_priority, item.modified_at, item.key)


def _apply_diagnostic_retention_unlocked(
    runtime_root: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按年龄、数量、分类字节与全局字节四重门窄清理登记诊断。"""

    root = Path(runtime_root) if runtime_root is not None else get_var_dir()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    items = collect_diagnostic_items(root)
    removed_keys: set[tuple[str, str]] = set()
    deleted_bytes = 0
    deleted_items = 0
    errors: list[str] = []

    def delete(item: DiagnosticItem) -> None:
        nonlocal deleted_bytes, deleted_items
        identity = (item.category, item.key)
        if item.pinned or identity in removed_keys:
            return
        removed, error = _delete_item(root, item)
        if removed > 0 or not any(path.exists() for path in item.paths):
            removed_keys.add(identity)
            deleted_bytes += removed
            deleted_items += 1
        if error:
            errors.append(error)

    for policy in POLICIES:
        category_items = sorted(items.get(policy.name, []), key=_category_eviction_key)
        cutoff = current.timestamp() - policy.max_age.total_seconds()
        for item in category_items:
            if not item.pinned and (
                item.modified_at > 0
                and item.modified_at < cutoff
                or policy.name == "vision_timelines"
                and _timeline_exceeds_file_limit(item)
            ):
                delete(item)
        remaining = [item for item in category_items if (item.category, item.key) not in removed_keys]
        if policy.max_items is not None:
            deletable = [item for item in remaining if not item.pinned]
            # legacy/身份不明 timeline 是永久 pinned 历史，不得占用 v2 的数量配额。
            counted_items = deletable if policy.name == "vision_timelines" else remaining
            overflow = max(0, len(counted_items) - policy.max_items)
            for item in deletable[:overflow]:
                delete(item)
        remaining = [item for item in category_items if (item.category, item.key) not in removed_keys]
        category_bytes = sum(
            item.byte_size
            for item in remaining
            if policy.name != "vision_timelines" or not item.pinned
        )
        for item in remaining:
            if category_bytes <= policy.limit_bytes:
                break
            if item.pinned:
                continue
            delete(item)
            category_bytes -= item.byte_size

    remaining = [
        item
        for category_items in items.values()
        for item in category_items
        if (item.category, item.key) not in removed_keys
    ]
    total_bytes = sum(item.byte_size for item in remaining)
    global_candidates = sorted(
        (item for item in remaining if not item.pinned),
        key=lambda item: (POLICY_BY_NAME[item.category].eviction_priority, item.modified_at, item.key),
    )
    for item in global_candidates:
        if total_bytes <= DIAGNOSTIC_GLOBAL_LIMIT_BYTES:
            break
        delete(item)
        total_bytes -= item.byte_size

    if any(category == "failure_inbox" for category, _ in removed_keys):
        try:
            _repair_failure_index(root)
        except OSError as exc:
            errors.append(f"{exc.__class__.__name__}:failure_inbox:index")

    final_items = collect_diagnostic_items(root)
    categories = _summarize(final_items)
    final_bytes = sum(int(value["byte_size"]) for value in categories.values())
    payload = {
        "schema_version": DIAGNOSTIC_RETENTION_SCHEMA_VERSION,
        "updated_at": current.isoformat(),
        "global_limit_bytes": DIAGNOSTIC_GLOBAL_LIMIT_BYTES,
        "total_bytes": final_bytes,
        "within_budget": final_bytes <= DIAGNOSTIC_GLOBAL_LIMIT_BYTES,
        "categories": categories,
        "last_run": {
            "disposition": "completed",
            "skip_reason": "",
            "lock_acquired": True,
            "deleted_items": deleted_items,
            "deleted_bytes": deleted_bytes,
            "budget_dropped": max(0, total_bytes - DIAGNOSTIC_GLOBAL_LIMIT_BYTES),
            "error_count": len(errors),
            "last_error": errors[-1] if errors else "",
        },
        "next_eligible_at": datetime.fromtimestamp(
            current.timestamp() + DIAGNOSTIC_RETENTION_MIN_INTERVAL_SECONDS,
            tz=timezone.utc,
        ).isoformat(),
    }
    state_path = retention_state_path(root)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_path, payload, ensure_ascii=False, indent=2)
    return payload


def apply_diagnostic_retention(
    runtime_root: str | Path | None = None,
    *,
    now: datetime | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """以跨进程单所有者运行清理；生产请求受 60 秒共享节流和 active 门保护。"""

    root = Path(runtime_root) if runtime_root is not None else get_var_dir()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_timestamp = current.timestamp()
    existing = _read_object(retention_state_path(root))
    lock = InterProcessFileLock(_retention_lock_path(root))
    if not lock.acquire():
        return _skipped_result(
            existing,
            disposition="skipped_lock_busy",
            skip_reason="cross_process_owner_active",
            next_eligible_at=current_timestamp + 1.0,
            lock_acquired=False,
        )
    try:
        # 获取锁后重读，防止另一个进程刚完成清理却被本进程的旧快照覆盖判断。
        existing = _read_object(retention_state_path(root))
        if not force and _selection_active(root, now_timestamp=current_timestamp):
            return _skipped_result(
                existing,
                disposition="skipped_interval",
                skip_reason="selection_active",
                next_eligible_at=current_timestamp + 1.0,
                lock_acquired=True,
            )
        last_completed_at = _parse_iso_timestamp(existing.get("updated_at"))
        next_eligible_at = last_completed_at + DIAGNOSTIC_RETENTION_MIN_INTERVAL_SECONDS
        if not force and last_completed_at > 0.0 and current_timestamp < next_eligible_at:
            return _skipped_result(
                existing,
                disposition="skipped_interval",
                skip_reason="minimum_interval",
                next_eligible_at=next_eligible_at,
                lock_acquired=True,
            )
        return _apply_diagnostic_retention_unlocked(root, now=current)
    finally:
        lock.release()


class DiagnosticRetentionWorker:
    """容量为一的合并 worker；重复请求不让清理任务堆积。"""

    def __init__(self, runtime_root: str | Path | None = None) -> None:
        self.runtime_root = Path(runtime_root) if runtime_root is not None else get_var_dir()
        self._condition = threading.Condition()
        self._requested = False
        self._active = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._completed = 0
        self._coalesced = 0
        self._skipped = 0
        self._failed = 0
        self._last_error = ""
        self._last_disposition = ""
        self._next_request_at = 0.0
        self._last_result: dict[str, Any] = {}

    def request(self) -> bool:
        with self._condition:
            if self._stopping:
                return False
            now = time.monotonic()
            if self._requested or self._active or now < self._next_request_at:
                self._coalesced += 1
                return True
            self._requested = True
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="hextech-diagnostic-retention",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._requested and not self._stopping:
                    self._condition.wait()
                if not self._requested and self._stopping:
                    return
                self._requested = False
                self._active = True
            try:
                result = apply_diagnostic_retention(self.runtime_root, force=False)
                last_run = (
                    result.get("last_run")
                    if isinstance(result.get("last_run"), Mapping)
                    else {}
                )
                disposition = str(last_run.get("disposition") or "completed")
                skip_reason = str(last_run.get("skip_reason") or "")
                with self._condition:
                    if disposition == "completed":
                        self._completed += 1
                    else:
                        self._skipped += 1
                    self._last_disposition = disposition
                    self._next_request_at = time.monotonic() + (
                        1.0
                        if skip_reason in {"selection_active", "cross_process_owner_active"}
                        else DIAGNOSTIC_RETENTION_MIN_INTERVAL_SECONDS
                    )
                    self._last_result = result
            except Exception as exc:
                with self._condition:
                    self._failed += 1
                    self._last_error = exc.__class__.__name__
            finally:
                with self._condition:
                    self._active = False
                    self._condition.notify_all()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while (self._requested or self._active) and time.monotonic() < deadline:
                self._condition.wait(min(0.05, max(0.0, deadline - time.monotonic())))
            return not self._requested and not self._active

    def close(self, timeout: float = 5.0) -> None:
        self.wait_idle(timeout)
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))

    def status(self) -> dict[str, Any]:
        with self._condition:
            last_run = self._last_result.get("last_run") if isinstance(self._last_result.get("last_run"), Mapping) else {}
            return {
                "queue_depth": int(self._requested),
                "active": self._active,
                "completed": self._completed,
                "coalesced": self._coalesced,
                "skipped": self._skipped,
                "last_disposition": self._last_disposition,
                "failed": self._failed,
                "last_error": self._last_error or str(last_run.get("last_error") or ""),
                "total_bytes": int(self._last_result.get("total_bytes") or 0),
                "global_limit_bytes": DIAGNOSTIC_GLOBAL_LIMIT_BYTES,
                "within_budget": bool(self._last_result.get("within_budget", True)),
                "next_eligible_at": str(self._last_result.get("next_eligible_at") or ""),
            }


_WORKERS: dict[str, DiagnosticRetentionWorker] = {}
_WORKERS_LOCK = threading.Lock()


def get_diagnostic_retention_worker(runtime_root: str | Path | None = None) -> DiagnosticRetentionWorker:
    root = Path(runtime_root) if runtime_root is not None else get_var_dir()
    key = str(root.resolve(strict=False))
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is None:
            worker = DiagnosticRetentionWorker(root)
            _WORKERS[key] = worker
        return worker


__all__ = [
    "DIAGNOSTIC_GLOBAL_LIMIT_BYTES",
    "DIAGNOSTIC_RETENTION_MIN_INTERVAL_SECONDS",
    "DiagnosticPolicy",
    "DiagnosticRetentionWorker",
    "POLICIES",
    "apply_diagnostic_retention",
    "collect_diagnostic_items",
    "get_diagnostic_retention_worker",
    "retention_state_path",
]

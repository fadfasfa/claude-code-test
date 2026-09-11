"""持续诊断 128 MiB registry、分组淘汰与路径安全合同。"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _sparse(path: Path, size: int, *, modified_at: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.truncate(size)
    if modified_at is not None:
        os.utime(path, (modified_at, modified_at))
    return path


def test_retention_removes_json_png_bundle_together_and_keeps_latest(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    evidence = tmp_path / "state" / "session_evidence"
    old_at = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
    screenshot = _sparse(evidence / "overlay-old.png", 1024, modified_at=old_at)
    report = evidence / "overlay-old.v2.json"
    report.write_text(json.dumps({"screenshot": screenshot.name}), encoding="utf-8")
    os.utime(report, (old_at, old_at))
    latest = evidence / "latest_real_session.v2.json"
    latest.write_text("{}", encoding="utf-8")

    result = apply_diagnostic_retention(tmp_path)

    assert not report.exists()
    assert not screenshot.exists()
    assert latest.exists()
    assert result["last_run"]["deleted_items"] == 1
    assert result["within_budget"] is True


def test_retention_enforces_debug_observation_count_and_category_bytes(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    roi_root = tmp_path / "debug" / "overlay_vision" / "overlay_roi_v2"
    start = datetime.now(timezone.utc).timestamp() - 3600
    for index in range(35):
        _sparse(roi_root / f"roi-{index:03d}" / "name_0.png", 1024 * 1024, modified_at=start + index)

    result = apply_diagnostic_retention(tmp_path)

    remaining = sorted(path.name for path in roi_root.iterdir())
    assert len(remaining) <= 28
    assert remaining[-1] == "roi-034"
    category = result["categories"]["overlay_vision_debug"]
    assert category["file_group_count"] <= 32
    assert category["byte_size"] <= 28 * 1024 * 1024


def test_retention_only_removes_v2_timeline_over_per_file_limit(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    timelines = tmp_path / "state" / "overlay_vision_timelines"
    oversized = timelines / "selection-a-e0001.jsonl"
    oversized.parent.mkdir(parents=True, exist_ok=True)
    oversized.write_text('{"schema_version":2}\n', encoding="utf-8")
    with oversized.open("r+b") as stream:
        stream.truncate(1024 * 1024 + 1)
    too_many_entries = timelines / "selection-a-e0002.jsonl"
    too_many_entries.write_text('{"schema_version":2}\n' * 513, encoding="utf-8")
    healthy = timelines / "selection-a-e0003.jsonl"
    healthy.write_text('{"schema_version":2}\n' * 512, encoding="utf-8")
    legacy = timelines / "selection-legacy-e0004.jsonl"
    legacy.write_text('{"schema_version":1}\n' * 513, encoding="utf-8")
    unreadable = timelines / "selection-unknown-e0005.jsonl"
    unreadable.write_text("not-json\n" * 513, encoding="utf-8")

    apply_diagnostic_retention(tmp_path)

    assert not oversized.exists()
    assert not too_many_entries.exists()
    assert healthy.exists()
    assert legacy.exists()
    assert unreadable.exists()


def test_retention_keeps_old_v1_timelines_when_age_and_count_exceed_policy(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    timelines = tmp_path / "state" / "overlay_vision_timelines"
    old_at = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    legacy_paths: list[Path] = []
    for index in range(25):
        path = timelines / f"selection-legacy-e{index:04d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version":1}\n', encoding="utf-8")
        os.utime(path, (old_at, old_at))
        legacy_paths.append(path)

    result = apply_diagnostic_retention(tmp_path)

    assert all(path.exists() for path in legacy_paths)
    assert result["categories"]["vision_timelines"]["file_group_count"] == 25
    assert result["last_run"]["deleted_items"] == 0


def test_legacy_timelines_do_not_consume_v2_count_budget(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    timelines = tmp_path / "state" / "overlay_vision_timelines"
    timelines.mkdir(parents=True)
    legacy = []
    current = []
    base = datetime.now(timezone.utc).timestamp() - 1000
    for index in range(25):
        path = timelines / f"selection-legacy-e{index:04d}.jsonl"
        path.write_text('{"schema_version":1}\n', encoding="utf-8")
        os.utime(path, (base + index, base + index))
        legacy.append(path)
    for index in range(20):
        path = timelines / f"selection-current-e{index:04d}.jsonl"
        path.write_text('{"schema_version":2}\n', encoding="utf-8")
        os.utime(path, (base + 100 + index, base + 100 + index))
        current.append(path)

    apply_diagnostic_retention(tmp_path, force=True)

    assert all(path.exists() for path in legacy)
    assert all(path.exists() for path in current)

    newest = timelines / "selection-current-e0020.jsonl"
    newest.write_text('{"schema_version":2}\n', encoding="utf-8")
    os.utime(newest, (base + 200, base + 200))
    apply_diagnostic_retention(tmp_path, force=True)

    assert all(path.exists() for path in legacy)
    assert not current[0].exists()
    assert all(path.exists() for path in current[1:])
    assert newest.exists()


def test_legacy_timeline_bytes_do_not_evict_v2(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    timelines = tmp_path / "state" / "overlay_vision_timelines"
    for index in range(13):
        path = timelines / f"selection-legacy-large-{index:02d}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version":1}\n', encoding="utf-8")
        with path.open("r+b") as stream:
            stream.truncate(1024 * 1024)
    current = timelines / "selection-current-e0001.jsonl"
    current.write_text('{"schema_version":2}\n', encoding="utf-8")

    apply_diagnostic_retention(tmp_path, force=True)

    assert current.exists()


def test_production_retention_skips_recent_run_and_active_selection(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention

    first_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    completed = apply_diagnostic_retention(tmp_path, now=first_at, force=True)
    recent = apply_diagnostic_retention(
        tmp_path,
        now=first_at + timedelta(seconds=10),
        force=False,
    )
    slots = tmp_path / "state" / "game_overlay_slots.v1.json"
    slots.write_text(
        json.dumps({"active": True, "generated_at": (first_at + timedelta(seconds=70)).timestamp()}),
        encoding="utf-8",
    )
    active = apply_diagnostic_retention(
        tmp_path,
        now=first_at + timedelta(seconds=70),
        force=False,
    )

    assert completed["last_run"]["disposition"] == "completed"
    assert recent["last_run"]["disposition"] == "skipped_interval"
    assert recent["last_run"]["skip_reason"] == "minimum_interval"
    assert active["last_run"]["disposition"] == "skipped_interval"
    assert active["last_run"]["skip_reason"] == "selection_active"


def test_retention_refuses_second_cross_process_owner(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import apply_diagnostic_retention
    from hextech.infrastructure.persistence.file_lock import InterProcessFileLock

    lock = InterProcessFileLock(tmp_path / "locks" / "diagnostic-retention.lock")
    assert lock.acquire()
    try:
        result = apply_diagnostic_retention(tmp_path, force=False)
    finally:
        lock.release()

    assert result["last_run"]["disposition"] == "skipped_lock_busy"
    assert result["last_run"]["lock_acquired"] is False


def test_retention_global_budget_evicts_registered_history_not_pinned_state(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import (
        DIAGNOSTIC_GLOBAL_LIMIT_BYTES,
        apply_diagnostic_retention,
    )

    # 各历史分类都在自己的上限内；额外的 pinned trace 迫使全局门按优先级淘汰旧日志。
    _sparse(tmp_path / "logs" / "hextech_runtime_summary.log", 11 * 1024 * 1024)
    for index in range(27):
        _sparse(tmp_path / "debug" / "overlay_vision" / "overlay_roi_v2" / f"roi-{index}" / "x.png", 1024 * 1024)
    for index in range(47):
        _sparse(tmp_path / "state" / "session_evidence" / f"overlay-{index}.v2.json", 1024 * 1024)
    for index in range(11):
        _sparse(tmp_path / "state" / "overlay_vision_timelines" / f"selection-a-e{index:04d}.jsonl", 1024 * 1024)
    for index in range(7):
        _sparse(tmp_path / "reports" / "overlay_sessions" / f"overlay-session-{index}.json", 1024 * 1024)
    _sparse(tmp_path / "state" / "supervisor_events.v1.jsonl", 3 * 1024 * 1024)
    pinned = _sparse(tmp_path / "state" / "overlay_vision_trace.v1.json", 25 * 1024 * 1024)

    result = apply_diagnostic_retention(tmp_path)

    assert pinned.exists()
    assert result["total_bytes"] <= DIAGNOSTIC_GLOBAL_LIMIT_BYTES
    assert result["within_budget"] is True
    assert not (tmp_path / "Temp").exists()
    assert not (tmp_path / "release").exists()


def test_reparse_or_outside_path_is_refused(monkeypatch, tmp_path: Path) -> None:
    from hextech.infrastructure.persistence import diagnostic_retention as retention

    registered = tmp_path / "debug" / "overlay_vision"
    target = _sparse(registered / "overlay_roi_v2" / "roi-1" / "x.png", 10)
    item = retention.DiagnosticItem(
        "overlay_vision_debug",
        "roi-1",
        (target,),
        target.stat().st_mtime,
        target.stat().st_size,
    )
    monkeypatch.setattr(retention, "_is_reparse", lambda path: path.name == "roi-1")

    deleted, error = retention._delete_item(tmp_path, item)

    assert deleted == 0
    assert error.startswith("unsafe_path:")
    assert target.exists()


def test_retention_worker_coalesces_requests_and_exposes_covering_status(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.diagnostic_retention import DiagnosticRetentionWorker

    worker = DiagnosticRetentionWorker(tmp_path)
    assert worker.request()
    assert worker.request()
    assert worker.wait_idle(timeout=3.0)
    status = worker.status()
    worker.close()

    assert status["completed"] >= 1
    assert status["global_limit_bytes"] == 128 * 1024 * 1024
    payload = json.loads((tmp_path / "state" / "diagnostic_retention.v1.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["categories"]


def test_retention_worker_does_not_rescan_within_shared_interval(monkeypatch, tmp_path: Path) -> None:
    from hextech.infrastructure.persistence import diagnostic_retention as retention

    calls = 0
    original = retention.apply_diagnostic_retention

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(retention, "apply_diagnostic_retention", counted)
    worker = retention.DiagnosticRetentionWorker(tmp_path)
    assert worker.request()
    assert worker.wait_idle(timeout=3.0)
    for _ in range(100):
        assert worker.request()
    assert worker.wait_idle(timeout=0.2)
    status = worker.status()
    worker.close()

    assert calls == 1
    assert status["completed"] == 1
    assert status["coalesced"] >= 100


def test_all_production_append_writers_are_registered_and_bounded() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "hextech"
    append_pattern = re.compile(r"(?:with\s+)?(?:[\w.]+\.)?open\([^\n]*(?:\"a\"|'a')")
    observed = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if append_pattern.search(path.read_text(encoding="utf-8"))
    }

    assert observed == {
        "bootstrap/supervisor.py",
        "infrastructure/observability/logging.py",
        "infrastructure/sources/catalog.py",
        "infrastructure/vision/sidecar_diagnostics.py",
    }

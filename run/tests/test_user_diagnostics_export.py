"""测试 用户诊断导出。

调用方: pytest; 关键依赖: hextech.support.user_diagnostics。
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_export_user_diagnostics_creates_limited_redacted_zip(tmp_path, monkeypatch):
    from hextech.support import user_diagnostics

    runtime_root = tmp_path / "runtime"
    state_dir = runtime_root / "state"
    logs_dir = runtime_root / "logs"
    debug_dir = runtime_root / "debug"
    cache_dir = runtime_root / "cache"
    profile_dir = runtime_root / "profile"
    raw_dir = runtime_root / "raw"
    reports_dir = runtime_root / "reports" / "old"
    for directory in (state_dir, logs_dir, debug_dir, cache_dir, profile_dir, raw_dir, reports_dir):
        directory.mkdir(parents=True)

    (state_dir / "startup_status.json").write_text(
        json.dumps({"status": "ready", "token": "state-secret"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "ui_feature_flags.json").write_text(
        json.dumps({"game_overlay_enabled": True}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "runtime_events.v1.jsonl").write_text(
        "\n".join(json.dumps({"event": f"e{index}", "token": f"s{index}"}) for index in range(5)) + "\n",
        encoding="utf-8",
    )
    (state_dir / "supervisor_events.v1.jsonl").write_text(
        "\n".join(json.dumps({"event": f"s{index}"}) for index in range(4)) + "\n",
        encoding="utf-8",
    )
    (state_dir / "auth_token.txt").write_text("secret-token", encoding="utf-8")
    (state_dir / "lcu_session.json").write_text('{"token":"secret"}', encoding="utf-8")
    (state_dir / "riot_client_state.json").write_text('{"cookie":"secret"}', encoding="utf-8")
    (state_dir / "overlay_anchor_calibration.v1.json").write_text('{"nonce":"secret"}', encoding="utf-8")

    (logs_dir / "hextech_runtime_summary.log").write_text(
        "\n".join(f"summary {index} token=secret local.yaml proxies.json accounts.json" for index in range(6)) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "hextech_error.log").write_text("cookie=secret\n", encoding="utf-8")
    (logs_dir / "dev").mkdir()
    (logs_dir / "dev" / "hextech_full.jsonl").write_text('{"debug":true}\n', encoding="utf-8")

    (debug_dir / "frame.png").write_bytes(b"debug")
    (debug_dir / "riot_token_frame.json").write_text("debug secret", encoding="utf-8")
    (cache_dir / "cache.json").write_text("cache", encoding="utf-8")
    (profile_dir / "profile.json").write_text("profile", encoding="utf-8")
    (profile_dir / "Cookies").write_text("profile cookie", encoding="utf-8")
    (raw_dir / "raw.json").write_text("raw", encoding="utf-8")
    (raw_dir / "lcu_session.json").write_text("raw secret", encoding="utf-8")
    (reports_dir / "old.zip").write_bytes(b"old")
    (reports_dir / "auth.zip").write_bytes(b"old-secret")

    monkeypatch.setattr(user_diagnostics, "get_runtime_root_dir", lambda: runtime_root)

    result = user_diagnostics.export_user_diagnostics(
        output_dir=tmp_path / "exports",
        recent_minutes=180,
        tail_lines=2,
    )

    assert result.bundle_dir.is_dir()
    assert result.zip_path.is_file()
    assert result.copied_files >= 5
    assert any("auth_token.txt" in item for item in result.skipped_sensitive)
    assert any("lcu_session.json" in item for item in result.skipped_sensitive)
    assert any("riot_client_state.json" in item for item in result.skipped_sensitive)
    assert any("overlay_anchor_calibration.v1.json" in item for item in result.skipped_sensitive)

    summary = json.loads((result.bundle_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["copied_files"] == result.copied_files
    assert summary["skipped_sensitive"]
    summary_blob = json.dumps(summary, ensure_ascii=False)
    assert str(runtime_root) not in summary_blob
    assert str(result.bundle_dir) not in summary_blob
    assert str(result.zip_path) not in summary_blob
    assert summary["bundle_dir"] == result.bundle_dir.name
    assert summary["zip_path"] == result.zip_path.name
    skipped_blob = json.dumps(summary["skipped_sensitive"], ensure_ascii=False)
    assert "debug/" not in skipped_blob
    assert "profile/" not in skipped_blob
    assert "raw/" not in skipped_blob
    assert "reports/" not in skipped_blob

    copied_startup = json.loads((result.bundle_dir / "state" / "startup_status.json").read_text(encoding="utf-8"))
    assert copied_startup["token"] == "<redacted>"

    event_tail = (result.bundle_dir / "state_tail" / "runtime_events.v1.jsonl.tail").read_text(encoding="utf-8")
    assert "e3" in event_tail and "e4" in event_tail
    assert "e0" not in event_tail
    assert "s4" not in event_tail
    assert '"token": "<redacted>"' in event_tail
    assert "token=<redacted>" in (
        result.bundle_dir / "logs_tail" / "hextech_runtime_summary.log.tail"
    ).read_text(encoding="utf-8")

    with zipfile.ZipFile(result.zip_path) as archive:
        names = set(archive.namelist())
        archived_summary = archive.read("summary.json").decode("utf-8")
        archived_log_tail = archive.read("logs_tail/hextech_runtime_summary.log.tail").decode("utf-8")

    assert "summary.json" in names
    assert "README.txt" in names
    assert "state/startup_status.json" in names
    assert "state/ui_feature_flags.json" in names
    assert "state_tail/runtime_events.v1.jsonl.tail" in names
    assert "logs_tail/hextech_runtime_summary.log.tail" in names
    assert not any(name.startswith(("debug/", "cache/", "profile/", "raw/", "reports/")) for name in names)
    assert not any("hextech_full.jsonl" in name for name in names)
    assert not any("auth" in name.lower() or "token" in name.lower() for name in names)
    assert "auth_token.txt" not in archived_summary
    assert "lcu_session.json" not in archived_summary
    assert "riot_client_state.json" not in archived_summary
    assert "overlay_anchor_calibration.v1.json" not in archived_summary
    assert "local.yaml" not in archived_log_tail
    assert "proxies.json" not in archived_log_tail
    assert "accounts.json" not in archived_log_tail


def test_export_user_diagnostics_filters_tail_by_recent_minutes(tmp_path, monkeypatch):
    from hextech.support import user_diagnostics

    runtime_root = tmp_path / "runtime"
    state_dir = runtime_root / "state"
    logs_dir = runtime_root / "logs"
    state_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    old_timestamp = (now - timedelta(minutes=240)).isoformat(timespec="seconds")
    new_timestamp = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    (state_dir / "runtime_events.v1.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"timestamp": old_timestamp, "event": "old.event"}, ensure_ascii=False),
                json.dumps({"timestamp": new_timestamp, "event": "new.event"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    local_now = datetime.now()
    old_log_timestamp = (local_now - timedelta(minutes=240)).strftime("%Y-%m-%d %H:%M:%S,000")
    new_log_timestamp = (local_now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S,000")
    (logs_dir / "hextech_runtime_summary.log").write_text(
        f"{old_log_timestamp} [source] old log\n{new_log_timestamp} [source] new log\ncontinuation line\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(user_diagnostics, "get_runtime_root_dir", lambda: runtime_root)

    result = user_diagnostics.export_user_diagnostics(
        output_dir=tmp_path / "exports",
        recent_minutes=60,
        tail_lines=10,
    )

    event_tail = (result.bundle_dir / "state_tail" / "runtime_events.v1.jsonl.tail").read_text(encoding="utf-8")
    log_tail = (result.bundle_dir / "logs_tail" / "hextech_runtime_summary.log.tail").read_text(encoding="utf-8")
    summary = json.loads((result.bundle_dir / "summary.json").read_text(encoding="utf-8"))

    assert "new.event" in event_tail
    assert "old.event" not in event_tail
    assert "new log" in log_tail
    assert "continuation line" in log_tail
    assert "old log" not in log_tail
    assert summary["recent_minutes"] == 60


def test_export_user_diagnostics_redacts_local_absolute_paths(tmp_path, monkeypatch):
    from hextech.support import user_diagnostics

    runtime_root = tmp_path / "runtime"
    state_dir = runtime_root / "state"
    state_dir.mkdir(parents=True)
    local_csv = runtime_root / "raw" / "hextech" / "Hextech_Data_2026-07-05.csv"
    repo_path = Path("C:/Users/apple/claudecode/run/data/runtime/raw/hextech/Hextech_Data_2026-07-05.csv")

    (state_dir / "startup_status.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "active_hextech_csv": str(local_csv),
                "repo_data_path": str(repo_path),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (state_dir / "runtime_events.v1.jsonl").write_text(
        json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": "hextech.refresh.published",
                "published_data_path": str(local_csv),
                "fallback_path": str(repo_path),
                "message": f"published to {local_csv}",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(user_diagnostics, "get_runtime_root_dir", lambda: runtime_root)

    result = user_diagnostics.export_user_diagnostics(
        output_dir=tmp_path / "exports",
        recent_minutes=180,
        tail_lines=10,
    )

    startup_text = (result.bundle_dir / "state" / "startup_status.json").read_text(encoding="utf-8")
    event_tail = (result.bundle_dir / "state_tail" / "runtime_events.v1.jsonl.tail").read_text(encoding="utf-8")
    exported_blob = startup_text + event_tail

    assert str(runtime_root) not in exported_blob
    assert "C:\\Users\\apple" not in exported_blob
    assert "C:/Users/apple" not in exported_blob
    assert "Hextech_Data_2026-07-05.csv" not in exported_blob
    assert "<local-path>" in exported_blob

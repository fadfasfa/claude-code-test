from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hextech.infrastructure.persistence.retention import apply_retention, protected_references


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=40)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _directory(path: Path, *, modified_at: datetime = OLD) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    timestamp = modified_at.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def _source_run(root: Path, source: str, run_id: str, *, success: bool) -> Path:
    path = _directory(root / "sources" / source / "runs" / run_id)
    _write_json(
        path / "manifest.json",
        {
            "health": "healthy" if success else "failed",
            "publishable": success,
        },
    )
    os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    return path


def _generation_manifest(root: Path, generation_id: str, source_run: str, catalog_id: str) -> None:
    directory = _directory(root / "snapshots" / "generations" / generation_id)
    _write_json(
        directory / "manifest.json",
        {
            "source_files": [
                {
                    "source": "hextech",
                    "run_id": source_run,
                    "catalog_generation_id": catalog_id,
                }
            ]
        },
    )
    os.utime(directory, (OLD.timestamp(), OLD.timestamp()))


def test_retention_protects_current_previous_journal_and_provenance(tmp_path: Path) -> None:
    _write_json(tmp_path / "catalog" / "current.v2.json", {"catalog_generation_id": "catalog-current"})
    _write_json(tmp_path / "sources" / "hextech" / "current.v2.json", {"run_id": "run-current"})
    _write_json(tmp_path / "snapshots" / "current.v2.json", {"current_generation_id": "gen-current"})
    _write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "gen-previous"})
    _generation_manifest(tmp_path, "gen-current", "run-from-current", "catalog-from-current")
    _generation_manifest(tmp_path, "gen-previous", "run-from-previous", "catalog-from-previous")
    _generation_manifest(tmp_path, "gen-target", "run-from-target", "catalog-from-target")

    generation_pointer = {
        "current": {"current_generation_id": "gen-target"},
        "previous": {"generation_id": "gen-current"},
    }
    _write_json(
        tmp_path / "state" / "data-service" / "promotion_journal.v1.json",
        {
            "old_pointers": {
                "catalog": {"catalog_generation_id": "catalog-old"},
                "hextech": {"run_id": "run-old"},
                "apex": {},
                "mayhem": {},
                "generation": {
                    "current": {"current_generation_id": "gen-current"},
                    "previous": {"generation_id": "gen-previous"},
                },
            },
            "target_pointers": {
                "catalog": {"catalog_generation_id": "catalog-target"},
                "hextech": {"run_id": "run-target"},
                "apex": {},
                "mayhem": {},
                "generation": generation_pointer,
            },
        },
    )

    expected_runs = {
        "run-current",
        "run-old",
        "run-target",
        "run-from-current",
        "run-from-previous",
        "run-from-target",
    }
    for run_id in expected_runs:
        _source_run(tmp_path, "hextech", run_id, success=True)
    expected_catalogs = {
        "catalog-current",
        "catalog-old",
        "catalog-target",
        "catalog-from-current",
        "catalog-from-previous",
        "catalog-from-target",
    }
    for catalog_id in expected_catalogs:
        _directory(tmp_path / "catalog" / "generations" / catalog_id)

    references = protected_references(tmp_path)
    result = apply_retention(tmp_path, now=NOW)

    assert {item.removeprefix("hextech:") for item in references["source_runs"]} == expected_runs
    assert references["catalog_generations"] == expected_catalogs
    assert references["generations"] == {"gen-current", "gen-previous", "gen-target"}
    assert result["source_runs"] == 0
    assert result["catalog_generations"] == 0
    assert result["generations"] == 0


def test_retention_keeps_recent_and_bounded_source_history(tmp_path: Path) -> None:
    for index in range(5):
        path = _source_run(tmp_path, "apex", f"success-{index}", success=True)
        timestamp = (OLD + timedelta(minutes=index)).timestamp()
        os.utime(path, (timestamp, timestamp))
    for index in range(12):
        path = _source_run(tmp_path, "apex", f"failure-{index}", success=False)
        timestamp = (OLD + timedelta(minutes=index)).timestamp()
        os.utime(path, (timestamp, timestamp))
    recent = _source_run(tmp_path, "apex", "failure-recent", success=False)
    timestamp = (NOW - timedelta(days=2)).timestamp()
    os.utime(recent, (timestamp, timestamp))
    malformed = _directory(tmp_path / "sources" / "apex" / "runs" / "malformed")
    (malformed / "manifest.json").write_text("{", encoding="utf-8")
    os.utime(malformed, (OLD.timestamp(), OLD.timestamp()))

    result = apply_retention(tmp_path, now=NOW)
    remaining = {path.name for path in (tmp_path / "sources" / "apex" / "runs").iterdir()}

    assert {"success-2", "success-3", "success-4"} <= remaining
    assert "success-0" not in remaining
    assert "success-1" not in remaining
    assert len({name for name in remaining if name.startswith("failure-")}) == 10
    assert "failure-recent" in remaining
    assert "malformed" in remaining
    assert result["source_runs"] == 5


def test_retention_removes_only_expired_unprotected_catalog_generation_and_staging(tmp_path: Path) -> None:
    _write_json(tmp_path / "catalog" / "current.v2.json", {"catalog_generation_id": "catalog-current"})
    _directory(tmp_path / "catalog" / "generations" / "catalog-current")
    _directory(tmp_path / "catalog" / "generations" / "catalog-old")
    recent_catalog = _directory(tmp_path / "catalog" / "generations" / "catalog-recent")
    recent_timestamp = (NOW - timedelta(days=2)).timestamp()
    os.utime(recent_catalog, (recent_timestamp, recent_timestamp))

    old_staging = _directory(tmp_path / "snapshots" / "staging" / "old")
    recent_staging = _directory(tmp_path / "sources" / "mayhem" / "staging" / "recent")
    recent_staging_timestamp = (NOW - timedelta(hours=2)).timestamp()
    os.utime(recent_staging, (recent_staging_timestamp, recent_staging_timestamp))

    result = apply_retention(tmp_path, now=NOW)

    assert (tmp_path / "catalog" / "generations" / "catalog-current").is_dir()
    assert (tmp_path / "catalog" / "generations" / "catalog-recent").is_dir()
    assert not (tmp_path / "catalog" / "generations" / "catalog-old").exists()
    assert not old_staging.exists()
    assert recent_staging.is_dir()
    assert result["catalog_generations"] == 1
    assert result["staging"] == 1

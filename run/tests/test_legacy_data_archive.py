from __future__ import annotations

import os
from pathlib import Path

import pytest

from tooling.data.archive_legacy_data import ArchivePaths, archive_legacy_data, build_manifest


def _make_legacy_data(root: Path) -> Path:
    data = root / "run" / "data"
    (data / "runtime" / "profile").mkdir(parents=True)
    (data / "runtime" / "logs").mkdir(parents=True)
    (data / "runtime" / "cache").mkdir(parents=True)
    (data / "raw").mkdir()
    (data / "processed").mkdir()
    (data / "static").mkdir()
    (data / "runtime" / "profile" / "Cookies").write_bytes(b"do-not-read")
    (data / "runtime" / "logs" / "private.log").write_bytes(b"log-content")
    (data / "runtime" / "cache" / "cache.bin").write_bytes(b"cache")
    (data / "raw" / "source.json").write_bytes(b"raw")
    (data / "processed" / "result.json").write_bytes(b"processed")
    (data / "static" / "Champion_Core_Data.json").write_bytes(b"static")
    return data


def test_manifest_never_reads_or_lists_profile_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _make_legacy_data(tmp_path)
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        normalized = os.fspath(path).replace("\\", "/")
        if "/runtime/profile/" in normalized or "/runtime/logs/" in normalized:
            raise AssertionError("sensitive content must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    manifest = build_manifest(data)
    serialized = str(manifest)
    assert manifest["profile"] == {"opaque": True, "present": True}
    assert manifest["logs"] == {"content_read": False, "file_count": 1, "total_bytes": 11}
    assert "Cookies" not in serialized
    assert "private.log" not in serialized


def test_archive_moves_data_and_verifies_non_sensitive_files(tmp_path: Path) -> None:
    data = _make_legacy_data(tmp_path)
    destination = tmp_path / ".archive" / "hextech-data-v1-test"
    result = archive_legacy_data(ArchivePaths(data, destination))
    assert result == destination
    assert not data.exists()
    assert (destination / "data" / "runtime" / "profile" / "Cookies").is_file()
    assert (destination / "archive_manifest.v1.json").is_file()


def test_archive_refuses_suspicious_raw_names(tmp_path: Path) -> None:
    data = _make_legacy_data(tmp_path)
    (data / "raw" / "session-token.json").write_bytes(b"secret")
    with pytest.raises(RuntimeError, match="疑似敏感命名"):
        build_manifest(data)

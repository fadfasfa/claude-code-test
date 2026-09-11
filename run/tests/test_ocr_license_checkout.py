"""OCR legal resources retain their manifest bytes under Windows checkout filters."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("filename", ["LICENSE.apache-2.0.txt", "NOTICE.txt"])
@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_ocr_legal_checkout_matches_manifest(filename: str, autocrlf: str) -> None:
    run_root = Path(__file__).resolve().parents[1]
    relative = f"resources/ocr/{filename}"
    manifest = json.loads((run_root / "resources/manifest.v2.json").read_text(encoding="utf-8"))
    entry = next(row for row in manifest["files"] if row["path"] == relative)
    checked_out = subprocess.run(
        ["git", "-c", f"core.autocrlf={autocrlf}", "cat-file", "--filters", f"HEAD:run/{relative}"],
        cwd=run_root.parent,
        check=True,
        capture_output=True,
    ).stdout
    assert b"\r\n" not in checked_out
    assert len(checked_out) == entry["size"]
    assert hashlib.sha256(checked_out).hexdigest() == entry["sha256"]

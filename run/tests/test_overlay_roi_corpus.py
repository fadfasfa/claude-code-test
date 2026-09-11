"""私有五类 ROI corpus 的边界、哈希与 fail-closed 测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image

from tooling.diagnostics.overlay_roi_corpus import REQUIRED_CLASSES, validate_private_corpus


def _file(path: Path) -> dict:
    return {"name": path.name, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_private_corpus_requires_exact_five_classes_and_runtime_reports(tmp_path: Path, monkeypatch) -> None:
    roots = []
    for label in sorted(REQUIRED_CLASSES):
        root = tmp_path / label
        root.mkdir()
        for index in range(3):
            Image.new("RGB", (80, 24), "black").save(root / f"name_{index}.png")
        (root / "report.json").write_text(
            json.dumps({"source": {"ready_slots": 0}}),
            encoding="utf-8",
        )
        roots.append(
            {
                "id": label,
                "class": label,
                "evidence_source": "private_frozen",
                "root": str(root),
                "files": [_file(path) for path in sorted(root.glob("name_*.png"))],
            }
        )
    manifest = tmp_path / "corpus.json"
    manifest.write_text(json.dumps({"schema_version": 1, "samples": roots}), encoding="utf-8")
    monkeypatch.setattr("tooling.diagnostics.overlay_roi_corpus._allowed_roots", lambda: (tmp_path.resolve(),))
    monkeypatch.setattr("tooling.diagnostics.overlay_roi_corpus._body_shard_scene_present", lambda _scores: True)
    result = validate_private_corpus(manifest)
    false_positives = {
        item["id"]
        for item in result["failures"]
        if item.get("kind") == "sample_failed" and "body_shard_false_positive" in item["details"]
    }
    assert false_positives == REQUIRED_CLASSES - {"body_shard"}
    assert result["missing_runtime_report_classes"] == []


def test_private_corpus_fails_when_tracked_fixture_has_no_runtime_report(tmp_path: Path, monkeypatch) -> None:
    samples = []
    for label in sorted(REQUIRED_CLASSES):
        root = tmp_path / label
        root.mkdir()
        for index in range(3):
            Image.new("RGB", (80, 24), "black").save(root / f"name_{index}.png")
        if label != "body_shard":
            (root / "report.json").write_text(json.dumps({"source": {"ready_slots": 0}}), encoding="utf-8")
        samples.append(
            {
                "id": label,
                "class": label,
                "evidence_source": "tracked_fixture" if label == "body_shard" else "private_frozen",
                "root": str(root),
                "files": [_file(item) for item in sorted(root.glob("name_*.png"))],
            }
        )
    manifest = tmp_path / "corpus.json"
    manifest.write_text(json.dumps({"schema_version": 1, "samples": samples}), encoding="utf-8")
    monkeypatch.setattr("tooling.diagnostics.overlay_roi_corpus._allowed_roots", lambda: (tmp_path.resolve(),))
    monkeypatch.setattr("tooling.diagnostics.overlay_roi_corpus._body_shard_scene_present", lambda _scores: True)

    result = validate_private_corpus(manifest)

    assert result["passed"] is False
    assert result["missing_runtime_report_classes"] == ["body_shard"]
    assert any(
        failure.get("id") == "body_shard" and "report_missing" in failure.get("details", [])
        for failure in result["failures"]
    )


def test_private_corpus_rejects_path_escape(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "corpus.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "samples": [
                    {
                        "id": label,
                        "class": label,
                        "evidence_source": "private_frozen",
                        "root": str(tmp_path / label),
                        "files": [],
                    }
                    for label in sorted(REQUIRED_CLASSES)
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("tooling.diagnostics.overlay_roi_corpus._allowed_roots", lambda: ((tmp_path / "allowed").resolve(),))

    result = validate_private_corpus(manifest)

    assert result["passed"] is False
    assert all(
        "path_outside_allowed_roots" in failure["details"]
        for failure in result["failures"]
        if failure.get("kind") == "sample_failed"
    )

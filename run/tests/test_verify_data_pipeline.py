"""Generation 与来源 current 验收测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hextech.modules.data.generation import DataSnapshotPublisher, SnapshotValidationError
from tooling.acceptance import verify_data_pipeline


def _payloads() -> dict[str, object]:
    return {
        "champions": [{"id": "1", "name": "英雄一"}],
        "champion_hextech": {
            "英雄一": {"hero_id": "1", "augments": [{"id": "a1", "name": "强化一"}]}
        },
        "overlay_hints": {"augments": {"a1": {"name": "强化一"}}},
        "identities": {"augments": {"a1": "强化一"}},
    }


def test_verify_generation_cross_checks_manifest_counts(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    manifest = DataSnapshotPublisher(root).publish(_payloads(), private_stats_enabled=True)

    result = verify_data_pipeline.verify_generation(root)

    assert result["generation_id"] == manifest.generation_id
    assert result["champion_count"] == 1
    assert result["stat_record_count"] == 1


def test_verify_generation_rejects_corrupted_payload(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    manifest = DataSnapshotPublisher(root).publish(_payloads(), private_stats_enabled=True)
    (root / "generations" / manifest.generation_id / "champions.json").write_text("[]", encoding="utf-8")

    with pytest.raises(SnapshotValidationError, match="校验失败"):
        verify_data_pipeline.verify_generation(root)


def test_verify_sources_requires_all_three_valid_currents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify_data_pipeline,
        "load_source_current",
        lambda source, verify_hash=True: {
            "run_id": f"{source}-run",
            "record_count": 1,
            "sha256": "a" * 64,
        },
    )

    result = verify_data_pipeline.verify_sources()

    assert set(result) == {"hextech", "apex", "mayhem"}
    assert all(item["state"] == "ready" for item in result.values())


def test_verify_sources_rejects_unknown_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify_data_pipeline,
        "load_source_current",
        lambda source, verify_hash=True: {} if source == "apex" else {
            "run_id": f"{source}-run",
            "record_count": 1,
            "sha256": "a" * 64,
        },
    )

    with pytest.raises(RuntimeError, match="apex"):
        verify_data_pipeline.verify_sources()


def test_direct_acceptance_script_adds_src_before_late_imports(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tooling" / "acceptance" / "verify_data_pipeline.py"
    completed = subprocess.run(
        [sys.executable, "-c", f"import runpy; runpy.run_path({str(script)!r}); import hextech; print(hextech.__file__)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert str(script.parents[2] / "src" / "hextech") in completed.stdout

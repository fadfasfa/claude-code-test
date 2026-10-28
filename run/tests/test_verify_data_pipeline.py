"""V4 acceptance rejects broken immutable closure and preserves explicit runtime scope."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
import pytest
from tests.support.v4_release import make_release
from tooling.acceptance import verify_data_pipeline
from hextech.modules.data.generation import DataSnapshotClient, SnapshotValidationError


def test_verify_generation_cross_checks_release_components(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    manifest = make_release(runtime, monkeypatch)
    actual = verify_data_pipeline.verify_generation(runtime / "snapshots")
    assert actual["generation_id"] == manifest.generation_id
    assert actual["champion_count"] == 1 and actual["complete_champion_count"] == 1
    assert actual["stat_record_count"] == manifest.stat_record_count


def test_verify_generation_rejects_corrupted_bound_unit(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    manifest = make_release(runtime, monkeypatch)
    ref = next(ref for ref in manifest.source_files if ref.artifact_role == "hero_rankings")
    path = runtime / "sources/aramkit/runs" / ref.run_id / "manifest.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError, SnapshotValidationError)):
        verify_data_pipeline.verify_generation(runtime / "snapshots")


def test_sources_are_validated_from_release_refs_and_ignore_unrelated_currents(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    make_release(runtime, monkeypatch)
    # Optional sources may be absent; fabricated current heads grant no authority.
    (runtime / "sources/aramkit/current.v2.json").write_text("{corrupt unrelated head", encoding="utf-8")
    actual = verify_data_pipeline.verify_sources(runtime / "snapshots")
    assert actual["ranking_run_id"]
    assert actual["optional_sources"] == []
    assert actual["complete_champion_count"] == 1
    assert all(row["source"] in {"aramkit", "apex", "mayhem"} for row in actual["units"])


def test_pipeline_reports_pending_champion_and_optional_sources_without_faking_completion(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    make_release(runtime, monkeypatch, complete=False)
    summary = verify_data_pipeline.verify_strict_full_chain(runtime / "snapshots")
    assert summary["generation"]["complete_champion_count"] == 0
    assert summary["sources"]["optional_sources"] == []
    assert summary["real_session_acceptance"] == "not_run"
    assert DataSnapshotClient(runtime / "snapshots").open_view().is_champion_complete("1") is False


def test_acceptance_report_dir_records_success(monkeypatch, tmp_path):
    runtime = tmp_path / "resources/seeds/runtime.v4"
    make_release(runtime, monkeypatch)
    report_dir = tmp_path / "reports/success"
    monkeypatch.setattr(verify_data_pipeline, "RUN_DIR", tmp_path)
    assert verify_data_pipeline.main(["--require-sources", "--report-dir", str(report_dir)]) == 0
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is True and summary["generation"]["champion_count"] == 1


def test_runtime_mode_uses_configured_snapshot_root(monkeypatch, tmp_path):
    runtime = tmp_path / "configured-runtime"
    make_release(runtime, monkeypatch)
    seen = []
    def verify(root):
        seen.append(root)
        return {"snapshot_root": str(root)}
    monkeypatch.setattr(verify_data_pipeline, "default_snapshot_root", lambda: runtime / "snapshots")
    monkeypatch.setattr(verify_data_pipeline, "verify_strict_full_chain", verify)
    assert verify_data_pipeline.main(["--runtime", "--strict-full-chain"]) == 0
    assert seen == [runtime / "snapshots"]


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


def test_acceptance_report_dir_records_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report_dir = tmp_path / "reports" / "failure"
    monkeypatch.setattr(verify_data_pipeline, "RUN_DIR", tmp_path)

    exit_code = verify_data_pipeline.main(["--strict-full-chain", "--report-dir", str(report_dir)])
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))

    assert exit_code == 1
    assert summary["passed"] is False
    assert summary["error_type"] == "AcceptanceFailure"

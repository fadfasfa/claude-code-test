"""隔离 acquisition worker 的结果语义回归。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hextech.bootstrap import acquisition_worker


def test_hextech_fallback_is_not_reported_as_refresh_success(tmp_path: Path) -> None:
    pointer = tmp_path / "hextech.pointer.v2.json"
    status = {
        "last_result": "fallback",
        "reason": "schema_changed",
        "failure_stage": "champion_catalog",
        "fallback_used": True,
        "active_csv": str(tmp_path / "last-good.csv"),
        "failure_diagnostics": {
            "missing_count": 60,
            "missing_ids": ["60001", "60002"],
        },
    }

    with patch(
        "hextech.infrastructure.sources.hextech.refresh_support.load_scraper_status",
        return_value=status,
    ):
        result = acquisition_worker._hextech_worker_result(True, pointer)

    assert result == {
        "state": "fallback",
        "success": False,
        "reason_code": "schema_changed",
        "failure_stage": "champion_catalog",
        "fallback_used": True,
        "last_good_available": True,
        "diagnostics": {
            "missing_count": 60,
            "missing_ids": ["60001", "60002"],
        },
    }


def test_worker_missing_pointer_raises_structured_source_failure(tmp_path: Path) -> None:
    pointer = tmp_path / "aramkit.pointer.v2.json"
    cancel = tmp_path / "aramkit.cancel"
    result = {
        "success": False,
        "reason": "schema_changed",
        "run_id": "run-failed",
    }

    with (
        patch("hextech.infrastructure.sources.aramkit.service.refresh_aramkit", return_value=result),
        pytest.raises(acquisition_worker.SourceRefreshFailed) as captured,
    ):
        acquisition_worker.run_worker(
            "aramkit",
            force=True,
            pointer_output=pointer,
            cancel_file=cancel,
        )

    assert captured.value.payload["reason_code"] == "schema_changed"
    assert captured.value.payload["fallback_used"] is False
    assert captured.value.payload["last_good_available"] is False


def test_worker_cli_writes_structured_failure_result(tmp_path: Path) -> None:
    result_path = tmp_path / "aramkit.result.json"
    failure = acquisition_worker.SourceRefreshFailed(
        "aramkit",
        {
            "reason_code": "schema_changed",
            "failure_stage": "champion_catalog",
            "fallback_used": True,
            "last_good_available": True,
            "diagnostics": {"missing_ids": ["60001"]},
        },
    )

    with patch.object(acquisition_worker, "run_worker", side_effect=failure):
        exit_code = acquisition_worker.main(
            [
                "--source", "aramkit",
                "--pointer-output", str(tmp_path / "pointer.json"),
                "--result-output", str(result_path),
                "--cancel-file", str(tmp_path / "cancel"),
            ]
        )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert payload["state"] == "failed"
    assert payload["error_type"] == "SourceRefreshFailed"
    assert payload["reason_code"] == "schema_changed"
    assert payload["fallback_used"] is True
    assert payload["last_good_available"] is True


def test_worker_cli_forwards_blitz_coverage_policy(tmp_path: Path) -> None:
    result_path = tmp_path / "blitz.result.json"
    expected = {"state": "ready", "source": "blitz", "pointer": {"schema_version": 2}}

    with patch.object(acquisition_worker, "run_worker", return_value=expected) as worker:
        exit_code = acquisition_worker.main(
            [
                "--source", "blitz",
                "--pointer-output", str(tmp_path / "pointer.json"),
                "--result-output", str(result_path),
                "--cancel-file", str(tmp_path / "cancel"),
                "--coverage-policy", "active_partial",
            ]
        )

    assert exit_code == 0
    assert worker.call_args.kwargs["coverage_policy"] == "active_partial"
    assert json.loads(result_path.read_text(encoding="utf-8")) == expected


def test_worker_cli_unexpected_error_keeps_fixed_failure_contract(tmp_path: Path) -> None:
    result_path = tmp_path / "catalog.result.json"

    with patch.object(acquisition_worker, "run_worker", side_effect=OSError("network down")):
        exit_code = acquisition_worker.main(
            [
                "--source", "catalog",
                "--pointer-output", str(tmp_path / "pointer.json"),
                "--result-output", str(result_path),
                "--cancel-file", str(tmp_path / "cancel"),
            ]
        )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert payload["reason_code"] == "worker_exception"
    assert payload["failure_stage"] == "worker_execution"
    assert payload["fallback_used"] is False
    assert payload["last_good_available"] is False
    assert payload["diagnostics"] == {"error_type": "OSError"}


def test_worker_cli_self_check_needs_no_source_or_pointer(tmp_path: Path) -> None:
    result_path = tmp_path / "self-check.result.json"
    expected = {
        "schema_version": 1,
        "state": "ready",
        "build_id": "build-test",
        "checks": {"curl_cffi._wrapper": "ready"},
    }

    with patch.object(acquisition_worker, "run_import_self_check", return_value=expected):
        exit_code = acquisition_worker.main(
            ["--self-check", "--result-output", str(result_path)]
        )

    assert exit_code == 0
    assert json.loads(result_path.read_text(encoding="utf-8")) == expected


def test_worker_cli_classifies_native_import_path_failure(tmp_path: Path) -> None:
    result_path = tmp_path / "self-check.result.json"

    with patch.object(
        acquisition_worker,
        "run_import_self_check",
        side_effect=ImportError("DLL load failed while importing _wrapper: 文件名或扩展名太长。"),
    ):
        exit_code = acquisition_worker.main(
            ["--self-check", "--result-output", str(result_path)]
        )

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert payload["reason_code"] == "native_runtime_path_too_long"
    assert payload["failure_stage"] == "worker_import"
    assert payload["diagnostics"] == {
        "error_type": "ImportError",
        "native_path_limit": 259,
    }

"""Generation 与来源 current 验收测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hextech.modules.data.generation import DataSnapshotPublisher, SnapshotValidationError
from tooling.acceptance import verify_data_pipeline


def _payloads() -> dict[str, object]:
    return {
        "champions": [{"id": "1", "name": "英雄一"}],
        "champion_hextech": {
            "英雄一": {"hero_id": "1", "augments": [{"id": "a1", "name": "强化一"}]}
        },
        "overlay_hints": {
            "hints": {"a1": {"augment_id": "a1", "name": "强化一"}},
            "name_index": {"a1": "a1", "强化一": "a1"},
        },
        "identities": {
            "schema_version": 2,
            "champions": {"1": "英雄一"},
            "augments": {"a1": "强化一"},
        },
    }


def test_verify_generation_cross_checks_manifest_counts(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    manifest = DataSnapshotPublisher(root).publish(_payloads())

    result = verify_data_pipeline.verify_generation(root)

    assert result["generation_id"] == manifest.generation_id
    assert result["champion_count"] == 1
    assert result["stat_record_count"] == 1


def test_verify_generation_rejects_corrupted_payload(tmp_path: Path) -> None:
    root = tmp_path / "snapshots"
    manifest = DataSnapshotPublisher(root).publish(_payloads())
    (root / "generations" / manifest.generation_id / "champions.json").write_text("[]", encoding="utf-8")

    with pytest.raises(SnapshotValidationError, match="校验失败"):
        verify_data_pipeline.verify_generation(root)


def test_generation_rejects_cross_champion_detail_identity(tmp_path: Path) -> None:
    payload = _payloads()
    payload["champion_hextech"] = {
        "错误英雄名": {"hero_id": "1", "augments": [{"id": "a1", "name": "强化一"}]}
    }

    with pytest.raises(SnapshotValidationError, match="英雄详情名称投影不一致"):
        DataSnapshotPublisher(tmp_path / "snapshots").publish(payload)


@pytest.mark.parametrize(
    "overlay_hints",
    (
        {"hints": {}, "name_index": {}},
        {
            "hints": {"a1": {"augment_id": "a1", "name": "强化一"}},
            "name_index": {"强化一": "missing"},
        },
    ),
)
def test_generation_rejects_empty_or_dangling_overlay_projection(
    tmp_path: Path, overlay_hints: dict[str, object]
) -> None:
    payload = _payloads()
    payload["overlay_hints"] = overlay_hints

    with pytest.raises(SnapshotValidationError, match="overlay_hints"):
        DataSnapshotPublisher(tmp_path / "snapshots").publish(payload)


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

    assert set(result) == {"aramkit", "blitz", "apex", "mayhem"}
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


def test_strict_generation_freshness_allows_only_fail_closed_optional_degradation() -> None:
    status = {
        "state": "degraded",
        "degraded_sources": [],
        "effective_degraded_sources": ["blitz"],
        "source_status": {
            "aramkit": {"freshness": "fresh", "data_status": "fresh"},
            # 自然过期只改变消费时 data_status；freshness 保留 immutable lineage。
            "blitz": {"freshness": "fresh", "data_status": "data_stale"},
        },
    }
    verify_data_pipeline._validate_generation_freshness(
        status,
        {"blitz": {"freshness": "stale"}},
    )

    status["source_status"]["aramkit"] = {
        "freshness": "last_good",
        "data_status": "data_stale",
    }
    with pytest.raises(verify_data_pipeline.AcceptanceFailure, match="ARAMKit fresh/fresh"):
        verify_data_pipeline._validate_generation_freshness(
            status,
            {"blitz": {"freshness": "stale"}},
        )


def test_blocked_adoption_is_required_to_hold_an_expired_active_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adoption = {
        "state": "blocked",
        "catalog": {
            "catalog_generation_id": "catalog-new",
            "content_sha256": "b" * 64,
        },
        "completed_sources": {"catalog": {}, "aramkit": {}},
        "pending_sources": ["blitz"],
    }
    monkeypatch.setattr(
        verify_data_pipeline,
        "CatalogAdoptionCheckpointStore",
        lambda: SimpleNamespace(load=lambda: adoption),
    )
    monkeypatch.setattr(
        verify_data_pipeline,
        "load_runtime_catalog_from_pointer",
        lambda _pointer: object(),
    )

    evidence = verify_data_pipeline._blocked_adoption_evidence(
        {
            "catalog_generation_id": "catalog-active",
            "content_sha256": "a" * 64,
        }
    )

    assert evidence["state"] == "blocked"
    assert evidence["pending_sources"] == ["blitz"]
    adoption["pending_sources"] = ["aramkit"]
    assert verify_data_pipeline._blocked_adoption_evidence(
        {
            "catalog_generation_id": "catalog-active",
            "content_sha256": "a" * 64,
        }
    ) == {}


def test_strict_blitz_verifier_uses_catalog_heroes_not_web_champion_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "patch": "16.16",
        "data_date": "2026-08-14",
        "rows": [
            {
                "augment_id": "7001",
                "tier": 4,
                "top_champions": [{"champion_id": "1", "tier": 2}],
            }
        ],
    }
    stats = {
        "1": {
            "source_tier": 4,
            "champion_tier": 2,
            "source_patch": "16.16",
            "source_date": "2026-08-14",
        },
        "22": {
            "source_tier": 4,
            "champion_tier": None,
            "source_patch": "16.16",
            "source_date": "2026-08-14",
        },
    }
    hints = {
        "source": {"production_augment_pool": {"canonical_ids": ["7001"]}},
        "hints": {"7001": {"stats_by_champion_id": stats}},
    }
    view = SimpleNamespace(get_overlay_hints=lambda: hints)
    pointer = SimpleNamespace(to_dict=lambda: {})
    monkeypatch.setattr(verify_data_pipeline, "validate_blitz_artifact", lambda _pointer: payload)

    assert verify_data_pipeline._verify_blitz_generation(
        view,
        pointer,
        expected_champion_ids=["1", "22"],
    ) == 2

    stats.pop("22")
    with pytest.raises(verify_data_pipeline.AcceptanceFailure, match="英雄投影不一致"):
        verify_data_pipeline._verify_blitz_generation(
            view,
            pointer,
            expected_champion_ids=["1", "22"],
        )


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


def test_acceptance_report_dir_records_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    DataSnapshotPublisher(tmp_path / "resources" / "seeds").publish(_payloads())
    report_dir = tmp_path / "reports" / "success"
    monkeypatch.setattr(verify_data_pipeline, "RUN_DIR", tmp_path)

    exit_code = verify_data_pipeline.main(["--report-dir", str(report_dir)])
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert summary["passed"] is True
    assert summary["generation"]["champion_count"] == 1


def test_runtime_mode_uses_configured_snapshot_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_snapshots = tmp_path / "configured-runtime" / "snapshots"
    DataSnapshotPublisher(runtime_snapshots).publish(_payloads())
    seen: list[Path] = []

    def verify(root: Path) -> dict[str, str]:
        seen.append(root)
        return {"snapshot_root": str(root)}

    monkeypatch.setattr(verify_data_pipeline, "default_snapshot_root", lambda: runtime_snapshots)
    monkeypatch.setattr(verify_data_pipeline, "verify_strict_full_chain", verify)

    exit_code = verify_data_pipeline.main(["--runtime", "--strict-full-chain"])

    assert exit_code == 0
    assert seen == [runtime_snapshots]


def test_acceptance_report_dir_records_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report_dir = tmp_path / "reports" / "failure"
    monkeypatch.setattr(verify_data_pipeline, "RUN_DIR", tmp_path)

    exit_code = verify_data_pipeline.main(["--strict-full-chain", "--report-dir", str(report_dir)])
    summary = json.loads((report_dir / "summary.json").read_text(encoding="utf-8"))

    assert exit_code == 1
    assert summary["passed"] is False
    assert summary["error_type"] == "AcceptanceFailure"

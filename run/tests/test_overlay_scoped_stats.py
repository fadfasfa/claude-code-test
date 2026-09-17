"""ARAMKit 单英雄 scoped view 的 provenance、哈希与 LRU 测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import pytest

from hextech.contracts import SourceProvenance
from hextech.modules.data.generation import DataSnapshotManifest, DataSnapshotView
from hextech.modules.data.scoped_stats import ScopedStatsCache


def _write_json(path: Path, payload: object) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _record(augment_id: int, *, sample_count: int, win_rate: float = 0.55) -> dict[str, Any]:
    return {
        "id": str(augment_id),
        "source_rank": 1,
        "sample_count": sample_count,
        "win_rate": win_rate,
        "pick_rate": 0.05,
        "blue_win_rate": win_rate,
        "red_win_rate": win_rate,
    }


def _build_fixture(
    tmp_path: Path,
    *,
    champions: tuple[str, ...] = ("1",),
    corrupt_relative: str = "",
    metadata: dict[str, Any] | None = None,
) -> tuple[DataSnapshotView, Path]:
    run_id = "aramkit-test-run"
    run_dir = tmp_path / "runs" / run_id
    descriptors: list[dict[str, Any]] = []
    for champion_id in champions:
        all_record = {**_record(100, sample_count=1200), "stage_agnostic": False, "available_stages": ["1"]}
        payload = {
            "schema_version": 1,
            "source": "aramkit",
            "dataset": "all",
            "version": "16.16",
            "data_path": "data/test",
            "champion": {"id": champion_id},
            "all": [all_record],
            "stages": {
                "1": [_record(100, sample_count=500, win_rate=0.6)],
                "2": [],
                "3": [],
                "4": [],
            },
        }
        relative = f"champions/{champion_id}.json"
        sha256, size = _write_json(run_dir / "scoped_stats" / relative, payload)
        descriptors.append(
            {
                "relative_path": corrupt_relative or relative,
                "champion_id": champion_id,
                "size": size,
                "sha256": sha256,
                "record_count": 1,
                "data_path": "data/test",
            }
        )
    index = {
        "schema_version": 1,
        "source": "aramkit",
        "dataset": "all",
        "version": "16.16",
        "data_path": "data/test",
        "champion_count": len(descriptors),
        "record_count": len(descriptors),
        "files": descriptors,
    }
    index_sha, index_size = _write_json(run_dir / "scoped_stats" / "manifest.json", index)
    source_manifest = {
        "schema_version": 2,
        "source": "aramkit",
        "run_id": run_id,
        "catalog_generation_id": "catalog-test",
        "catalog_sha256": "c" * 64,
        "health": "healthy",
        "started_at": "2026-08-15T00:00:00+00:00",
        "completed_at": "2026-08-15T00:00:01+00:00",
        "expected_items": 0,
        "successful_items": 0,
        "confirmed_empty_items": 0,
        "failed_items": 0,
        "artifact": {
            "role": "scoped_stats",
            "relative_path": "scoped_stats/manifest.json",
            "sha256": index_sha,
            "record_count": len(descriptors),
            "content_schema_version": 1,
            "size": index_size,
        },
        "outcomes": [],
        "metadata": metadata or {},
    }
    manifest_sha, _ = _write_json(run_dir / "manifest.json", source_manifest)
    provenance = SourceProvenance(
        source="aramkit",
        run_id=run_id,
        catalog_generation_id="catalog-test",
        artifact_role="scoped_stats",
        artifact_sha256=index_sha,
        record_count=len(descriptors),
        manifest_sha256=manifest_sha,
        content_schema_version=1,
    )
    snapshot_manifest = DataSnapshotManifest(
        generation_id="20260815T000000-aaaaaaaaaa",
        created_at="now",
        content_fingerprint="f" * 64,
        source_files=(provenance,),
        champion_count=len(descriptors),
        augment_count=1,
        stat_record_count=len(descriptors),
        files=(),
    )
    return DataSnapshotView(snapshot_manifest, {}), run_dir


def _cache(run_dir: Path, *, capacity: int = 2) -> ScopedStatsCache:
    return ScopedStatsCache(capacity=capacity, run_dir_resolver=lambda _run_id: run_dir)


def test_scoped_view_loads_only_selected_champion_and_falls_back_to_all(tmp_path: Path) -> None:
    snapshot, run_dir = _build_fixture(tmp_path)
    result = _cache(run_dir).load(snapshot, "1")

    assert result.available is True
    assert result.view is not None
    stage = result.view.select("100", 1)
    assert stage.stats_scope == "stage_1"
    assert stage.to_stats_dict()["sample_count"] == 500
    fallback = result.view.select("100", 2)
    assert fallback.stats_scope == "all"
    assert fallback.fallback_reason == "stage_stat_missing_fallback_all"
    assert fallback.to_stats_dict()["sample_count"] == 1200
    assert result.view.data_at == "2026-08-15T00:00:01+00:00"
    assert result.view.status()["data_at"] == result.view.data_at


@pytest.mark.parametrize("marker", [None, "invalid", False, -1, "inf", 1e30])
def test_scoped_data_time_invalid_marker_uses_verified_run_completion(tmp_path, marker):
    snapshot, run_dir = _build_fixture(tmp_path, metadata={"version": {"buildTimeUnixMs": marker}})
    result = _cache(run_dir).load(snapshot, "1")
    assert result.available
    assert result.view.data_at == "2026-08-15T00:00:01+00:00"


def test_v3_old_hero_revision_keeps_values_but_never_borrows_fresh_ranking_status(tmp_path):
    from hextech.contracts import SourceStatusV2
    from hextech.interfaces.overlay.renderer import build_render_model_from_session
    from hextech.modules.recommendation.stage_projection import apply_scoped_stage_stats
    from test_overlay_stage_projection import _state, _context

    now = datetime.now(timezone.utc)
    old_at = now - timedelta(days=7)
    marker = int(old_at.timestamp() * 1000)
    expected_time = datetime.fromtimestamp(marker / 1000, tz=timezone.utc).isoformat()
    snapshot, run_dir = _build_fixture(tmp_path, metadata={"version": {"buildTimeUnixMs": marker}})
    hero_run = snapshot.manifest.source_files[0].run_id
    manifest = replace(snapshot.manifest, schema_version=3, components={
        "ranking": {"run_id": "ranking-fresh", "source_version": "ranking-new", "catalog_id": "catalog-test"},
        "champions": {"1": {"run_id": hero_run, "source_version": "hero-old", "catalog_id": "catalog-test", "complete": True}},
    }, source_status={"aramkit": SourceStatusV2(
        freshness="fresh", data_status="fresh", run_id="ranking-fresh", data_at=now.isoformat(),
    )})
    snapshot = DataSnapshotView(manifest, {})
    loaded = _cache(run_dir).load(snapshot, "1")
    assert loaded.available
    assert loaded.view.data_at == expected_time
    assert loaded.view.run_id == hero_run
    assert snapshot.status()["source_status"]["aramkit"]["run_id"] == "ranking-fresh"
    snapshot_status = snapshot.status()
    snapshot_status["source_status"]["aramkit"].update({
        "freshness": "fresh", "data_status": "fresh", "data_reason": "",
        "check_status": "up_to_date", "check_evidence_bound": True,
        "upstream_revision": "ranking-new", "applied_revision": "ranking-new",
    })
    projected = apply_scoped_stage_stats(_state(), stage_context=_context(1), scoped_view=loaded.view,
        scope_status="ready", scope_reason="", snapshot_status=snapshot_status, now=now)
    row = projected.recommendation.augment_slots[0]
    assert row["source_run_id"] == row["stats"]["source_run_id"] == hero_run
    assert row["source_data_at"] == row["stats"]["source_data_at"] == expected_time
    assert row["source_freshness"] == "last_good"
    assert row["data_status"] == "stale"
    assert row["data_reason"] == "component_revision_outdated"
    assert row["status_code"] == "GENERATION_DEGRADED"
    rendered = build_render_model_from_session(projected)
    assert "60.0%" in rendered["stats"][0]["stats_text"]
    assert rendered["stats"][0]["status_text"] == ""
    assert not rendered.get("data_notice")


def test_scoped_view_reports_missing_champion_for_ashe_style_gap(tmp_path: Path) -> None:
    snapshot, run_dir = _build_fixture(tmp_path)
    result = _cache(run_dir).load(snapshot, "22")

    assert result.available is False
    assert result.reason == "aramkit_champion_missing"


def test_scoped_view_rejects_child_hash_mismatch(tmp_path: Path) -> None:
    snapshot, run_dir = _build_fixture(tmp_path)
    child = run_dir / "scoped_stats" / "champions" / "1.json"
    child.write_text(child.read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = _cache(run_dir).load(snapshot, "1")
    assert result.available is False
    assert result.reason == "aramkit_scoped_child_hash_mismatch"


def test_scoped_view_rejects_path_escape_from_index(tmp_path: Path) -> None:
    snapshot, run_dir = _build_fixture(tmp_path, corrupt_relative="../escape.json")

    result = _cache(run_dir).load(snapshot, "1")
    assert result.available is False
    assert result.reason == "aramkit_scoped_child_path_invalid"


def test_scoped_view_lru_capacity_is_two(tmp_path: Path) -> None:
    snapshot, run_dir = _build_fixture(tmp_path, champions=("1", "2", "3"))
    cache = _cache(run_dir)

    assert cache.load(snapshot, "1").available
    assert cache.load(snapshot, "2").available
    assert cache.load(snapshot, "3").available
    status = cache.status()
    assert status["size"] == 2
    assert [key[2] for key in status["keys"]] == ["2", "3"]

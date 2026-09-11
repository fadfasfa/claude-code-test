"""完整 cohort seed 的构建、安装与回滚测试。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest
from PIL import Image

from hextech.contracts import ArtifactDescriptor, ItemOutcome, SourceHealth, SourceProvenance, SourceRunManifestV2
from hextech.modules.data.catalog.versioned import build_catalog_manifest, sha256_file
from hextech.modules.data.generation import DataSnapshotPublisher
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.bootstrap.production_pool_binding import _catalog_production_pool, validate_production_pool_assets
from hextech.modules.acquisition.hextech.production_pool import validate_production_augment_pool


SOURCE_ROLES = {
    "aramkit": ("scoped_stats", "scoped_stats.json"),
    "blitz": ("augment_ranking", "augment_ranking.json"),
    "apex": ("synergy", "synergy.json"),
    "mayhem": ("combos", "combos.json"),
}


def _write_catalog(runtime: Path) -> tuple[Path, dict[str, object], object]:
    staging = runtime / "catalog" / "fixture"
    staging.mkdir(parents=True)
    champion = {
        "schema_version": 1,
        "aliases": [{"heroId": "1", "heroName": "英雄一", "title": "", "enName": "HeroOne", "aliases": []}],
        "alias_to_id": {"英雄一": "1", "HeroOne": "1"},
        "id_to_name": {"1": {"heroName": "英雄一", "title": "", "enName": "HeroOne"}},
        "id_to_detail": {"1": "英雄一"},
    }
    atomic_write_json(staging / "英雄目录.v1.json", champion, ensure_ascii=False)
    icon_temp = staging / "icon.png"
    Image.new("RGB", (8, 8), (20, 40, 60)).save(icon_temp)
    icon_sha = sha256_file(icon_temp)
    icon_relative = f"assets/augments/{icon_sha}.png"
    icon_path = staging / icon_relative
    icon_path.parent.mkdir(parents=True)
    icon_temp.replace(icon_path)
    augment = {
        "schema_version": 1,
        "entries": [
            {
                "source_id": "1001",
                "cdragon_id": 1001,
                "name": "强化一",
                "augment_name_id": "ARAM_Test",
                "filename": icon_path.name,
                "local_path": icon_relative,
                "icon_sha256": icon_sha,
            }
        ],
        "name_to_icon": {"强化一": icon_relative},
    }
    atomic_write_json(staging / "海克斯资源目录.v1.json", augment, ensure_ascii=False)
    (staging / "hero_version.txt").write_text("fixture", encoding="utf-8")
    atomic_write_json(
        staging / "augment_assets.v1.json",
        {
            "schema_version": 1,
            "entries": [
                {
                    "canonical_id": "1001",
                    "augment_name_id": "ARAM_Test",
                    "relative_path": icon_relative,
                    "sha256": icon_sha,
                    "size": icon_path.stat().st_size,
                }
            ],
        },
    )
    manifest = build_catalog_manifest(staging, created_at="2026-08-13T00:00:00+00:00")
    final = runtime / "catalog" / "generations" / manifest.catalog_generation_id
    final.parent.mkdir(parents=True)
    staging.replace(final)
    manifest_path = final / "manifest.json"
    atomic_write_json(manifest_path, manifest.to_dict(), ensure_ascii=False, indent=2)
    pointer = {
        "schema_version": 2,
        "catalog_generation_id": manifest.catalog_generation_id,
        "content_sha256": manifest.content_sha256,
        "manifest_sha256": sha256_file(manifest_path),
        "completed_at": manifest.created_at,
        "last_success_at": manifest.created_at,
    }
    atomic_write_json(runtime / "catalog" / "current.v2.json", pointer, indent=2)
    return final, pointer, manifest


def test_aramkit_pool_keeps_catalog_frozen_eligibility(tmp_path: Path) -> None:
    catalog_root, pointer, manifest = _write_catalog(tmp_path / "runtime")
    catalog = SimpleNamespace(
        root=catalog_root,
        generation_id=manifest.catalog_generation_id,
        content_sha256=manifest.content_sha256,
        manifest_sha256=pointer["manifest_sha256"],
        manifest=manifest,
    )

    pool = _catalog_production_pool(catalog)

    validate_production_augment_pool(pool)
    validate_production_pool_assets(pool, catalog_root)
    assert pool["canonical_ids"] == ["1001"]
    assert pool["full_catalog_count"] == 1
    assert pool["migration"]["baseline"] == "catalog_augment_assets"


def _write_source(runtime: Path, source: str, role: str, filename: str, catalog_pointer: dict[str, object]) -> tuple[dict[str, object], SourceProvenance]:
    run_id = f"{source}-fixture"
    run_root = runtime / "sources" / source / "runs" / run_id
    run_root.mkdir(parents=True)
    artifact_path = run_root / filename
    artifact_path.write_text("fixture\n", encoding="utf-8")
    descriptor = ArtifactDescriptor(
        role=role,
        relative_path=filename,
        sha256=sha256_file(artifact_path),
        record_count=1,
        content_schema_version=2,
        size=artifact_path.stat().st_size,
    )
    manifest = SourceRunManifestV2(
        source=source,
        run_id=run_id,
        catalog_generation_id=str(catalog_pointer["catalog_generation_id"]),
        catalog_sha256=str(catalog_pointer["content_sha256"]),
        health=SourceHealth.HEALTHY,
        started_at="2026-08-13T00:00:00+00:00",
        completed_at="2026-08-13T00:00:01+00:00",
        expected_items=1,
        successful_items=1,
        confirmed_empty_items=0,
        failed_items=0,
        artifact=descriptor,
        outcomes=(ItemOutcome(item_id="1", state="success", stage="fixture", record_count=1),),
    )
    manifest_path = run_root / "manifest.json"
    atomic_write_json(manifest_path, manifest.to_dict(), ensure_ascii=False, indent=2)
    pointer = {
        "schema_version": 2,
        "source": source,
        "run_id": run_id,
        "catalog_generation_id": manifest.catalog_generation_id,
        "catalog_sha256": manifest.catalog_sha256,
        "manifest_sha256": sha256_file(manifest_path),
        "artifact": descriptor.to_dict(),
        "completed_at": manifest.completed_at,
        "last_success_at": manifest.completed_at,
    }
    atomic_write_json(runtime / "sources" / source / "current.v2.json", pointer, indent=2)
    provenance = SourceProvenance(
        source=source,  # type: ignore[arg-type]
        run_id=run_id,
        catalog_generation_id=manifest.catalog_generation_id,
        artifact_role=role,
        artifact_sha256=descriptor.sha256,
        record_count=descriptor.record_count,
        manifest_sha256=str(pointer["manifest_sha256"]),
        content_schema_version=2,
    )
    return pointer, provenance


def _fixture_runtime(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    runtime = tmp_path / "verified-runtime"
    catalog_root, catalog_pointer, catalog_manifest = _write_catalog(runtime)
    source_pointers: dict[str, dict[str, object]] = {}
    provenance: list[SourceProvenance] = [
        SourceProvenance(
            source="catalog",
            run_id=catalog_manifest.catalog_generation_id,
            catalog_generation_id=catalog_manifest.catalog_generation_id,
            artifact_role=item.role,
            artifact_sha256=item.sha256,
            record_count=item.record_count,
            manifest_sha256=str(catalog_pointer["manifest_sha256"]),
            content_schema_version=item.content_schema_version,
        )
        for item in catalog_manifest.files
    ]
    for source, (role, filename) in SOURCE_ROLES.items():
        pointer, source_provenance = _write_source(runtime, source, role, filename, catalog_pointer)
        source_pointers[source] = pointer
        provenance.append(source_provenance)
    icon_entry = json.loads((catalog_root / "海克斯资源目录.v1.json").read_text(encoding="utf-8"))["entries"][0]
    pool = {
        "schema_version": 1,
        "state": "ready",
        "pool_id": "production-pool-v1-fixture",
        "catalog_generation_id": str(catalog_pointer["catalog_generation_id"]),
        "catalog_sha256": str(catalog_pointer["content_sha256"]),
        "catalog_manifest_sha256": str(catalog_pointer["manifest_sha256"]),
        "metadata_marker_sha256": "a" * 64,
        "metadata_total_count": 1,
        "full_catalog_count": 1,
        "enabled_count": 1,
        "disabled_count": 0,
        "canonical_ids": ["1001"],
        "identities": [
            {
                "canonical_id": "1001",
                "name": "强化一",
                "normalized_name": "强化一",
                "augment_name_id": "ARAM_Test",
                "tier": "1",
                "enabled": True,
                "visual_variants": [
                    {
                        "variant_id": "ARAM_Test",
                        "name": "强化一",
                        "augment_name_id": "ARAM_Test",
                        "filename": icon_entry["filename"],
                        "local_path": icon_entry["local_path"],
                        "icon_sha256": icon_entry["icon_sha256"],
                    }
                ],
                "icon_ambiguous": False,
            }
        ],
        "disabled_ids": [],
        "unresolved_ids": [],
        "duplicate_ids": [],
        "name_conflicts": [],
        "migration": {"change_without_marker": False},
    }
    payload = {
        "champions": [{"id": "1", "name": "英雄一"}],
        "champion_hextech": {"英雄一": {"hero_id": "1", "augments": [{"id": "1001"}]}},
        "overlay_hints": {
            "hints": {"1001": {"augment_id": "1001", "name": "强化一"}},
            "name_index": {"1001": "1001", "强化一": "1001"},
            "source": {"production_augment_pool": pool},
        },
        "identities": {"schema_version": 2, "champions": {"1": "英雄一"}, "augments": {"1001": "强化一"}},
    }
    published = DataSnapshotPublisher(runtime / "snapshots").publish(
        payload,
        source_files=provenance,
        require_complete_provenance=True,
        health="healthy",
        refreshed_sources=("catalog", "aramkit", "blitz", "apex", "mayhem"),
    )
    schedule = {
        "schema_version": 1,
        "updated_at": "2026-08-13T00:00:02+00:00",
        "generation_id": published.generation_id,
        "sources": {
            "catalog": {"state": "ready", "current_run_id": str(catalog_pointer["catalog_generation_id"])},
            **{source: {"state": "ready", "current_run_id": str(pointer["run_id"])} for source, pointer in source_pointers.items()},
        },
    }
    atomic_write_json(runtime / "state" / "data-service" / "refresh_schedule.v1.json", schedule, indent=2)
    return runtime, {"pool": pool, "generation_id": published.generation_id, "catalog_pointer": catalog_pointer}


def _bundle_from_runtime(tmp_path: Path, runtime: Path) -> Path:
    from tooling.build.cohort_seed import collect_cohort_seed

    bundle = tmp_path / "bundle"
    seed = collect_cohort_seed(runtime / "snapshots")
    seed_files = sorted(path for path in (runtime / "snapshots").rglob("*") if path.is_file())
    seed_names = [(Path("resources/seeds") / path.relative_to(runtime / "snapshots")).as_posix() for path in seed_files]
    cohort_names = [seed.bundled_name(path) for path in seed.files]
    for source, name in [*zip(seed_files, seed_names), *zip(seed.files, cohort_names)]:
        target = bundle / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = {
        "cohort_seed": seed.metadata,
        "cohort_seed_files": cohort_names,
        "cohort_seed_sha256": {name: hashlib.sha256(source.read_bytes()).hexdigest() for source, name in zip(seed.files, cohort_names)},
        "seed_files": seed_names,
        "seed_sha256": {name: hashlib.sha256(source.read_bytes()).hexdigest() for source, name in zip(seed_files, seed_names)},
    }
    atomic_write_json(bundle / "bundle_manifest.json", manifest, ensure_ascii=False)
    return bundle


def _write_old_cohort(runtime: Path) -> dict[Path, bytes]:
    pointers = {
        runtime / "catalog" / "current.v2.json": {"label": "old-catalog"},
        **{runtime / "sources" / source / "current.v2.json": {"label": f"old-{source}"} for source in SOURCE_ROLES},
        runtime / "snapshots" / "current.v2.json": {"current_generation_id": "old-current", "schema_version": 2},
        runtime / "snapshots" / "previous.v2.json": {"generation_id": "old-previous", "schema_version": 2},
    }
    for path, payload in pointers.items():
        atomic_write_json(path, payload, indent=2)
    marker = runtime / "snapshots" / "generations" / "old-current" / "user-marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("preserve me", encoding="utf-8")
    return {path: path.read_bytes() for path in pointers}


def test_collect_cohort_seed_binds_catalog_sources_and_production_pool(tmp_path: Path) -> None:
    from tooling.build.cohort_seed import collect_cohort_seed

    runtime, expected = _fixture_runtime(tmp_path)
    seed = collect_cohort_seed(runtime / "snapshots")

    assert seed.metadata["generation_id"] == expected["generation_id"]
    assert seed.metadata["catalog_generation_id"] == expected["catalog_pointer"]["catalog_generation_id"]
    assert seed.metadata["production_pool_id"] == expected["pool"]["pool_id"]
    assert seed.metadata["production_pool_count"] == 1
    assert any(path.name == "augment_assets.v1.json" for path in seed.files)
    assert {path.name for path in seed.files if path.name == "current.v2.json"} == {"current.v2.json"}


def test_build_seed_accepts_fresh_aramkit_with_only_optional_sources_degraded(tmp_path: Path) -> None:
    from tooling.build.manifest import validate_snapshot_seed

    runtime, expected = _fixture_runtime(tmp_path)
    manifest_path = runtime / "snapshots" / "generations" / expected["generation_id"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["health"] = "degraded"
    manifest["degraded_sources"] = ["blitz"]
    manifest["source_status"] = {
        "aramkit": {"freshness": "fresh", "data_status": "fresh"},
        "blitz": {
            "freshness": "last_good",
            "data_status": "data_stale",
            "data_reason": "production_coverage_insufficient",
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    health = validate_snapshot_seed(runtime / "snapshots")

    assert health["valid"] is True
    assert health["optional_degraded"] is True


def test_install_cohort_upgrades_existing_current_and_preserves_old_generation(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort

    source_runtime, expected = _fixture_runtime(tmp_path / "source")
    bundle = _bundle_from_runtime(tmp_path, source_runtime)
    runtime = tmp_path / "target"
    _write_old_cohort(runtime)

    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "installed"
    receipt_path = runtime / "state/data-service/cohort_validation_receipt.v1.json"
    assert receipt_path.is_file()
    recovery_before = (runtime / "state/data-service/cohort_recovery_point.v1.json").read_bytes()
    schedule_before = (runtime / "state/data-service/refresh_schedule.v1.json").read_bytes()
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "already_current"
    selection = json.loads((runtime / "state/data-service/cohort_selection.v1.json").read_text())
    assert selection["selected_source"] == "current_receipt"
    assert (runtime / "state/data-service/cohort_recovery_point.v1.json").read_bytes() == recovery_before
    assert (runtime / "state/data-service/refresh_schedule.v1.json").read_bytes() == schedule_before
    assert json.loads((runtime / "snapshots/current.v2.json").read_text())["current_generation_id"] == expected["generation_id"]
    assert json.loads((runtime / "snapshots/previous.v2.json").read_text())["generation_id"] == "old-current"
    assert (runtime / "snapshots/generations/old-current/user-marker.txt").read_text() == "preserve me"
    assert not (runtime / "state/data-service/promotion_journal.v1.json").exists()


def test_verified_snapshot_startup_status_never_rewrites_refresh_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Bundle 安装凭据与真实抓取周期必须是两份独立证据。"""

    from hextech.infrastructure.persistence import runtime_bundle

    runtime, _expected = _fixture_runtime(tmp_path)
    state_dir = runtime / "state"
    checkpoint_path = state_dir / "data-service/refresh_checkpoint.v1.json"
    checkpoint = {
        "schema_version": 1,
        "state": "in_progress",
        "cycle_id": "cycle-real-pending",
        "core_generation_id": "generation-from-earlier-core",
        "pending_sources": ["blitz"],
        "failures": {"blitz": {"reason_code": "production_coverage_insufficient"}},
    }
    atomic_write_json(checkpoint_path, checkpoint, ensure_ascii=False, indent=2)
    before = checkpoint_path.read_bytes()
    monkeypatch.setattr(
        runtime_bundle,
        "build_runtime_state_path",
        lambda filename: str(state_dir / filename),
    )

    runtime_bundle._write_verified_snapshot_startup_status(
        runtime / "snapshots",
        cohort_state="runtime_restored",
    )

    assert checkpoint_path.read_bytes() == before
    checkpoint_path.unlink()
    runtime_bundle._write_verified_snapshot_startup_status(
        runtime / "snapshots",
        cohort_state="already_current",
    )
    assert not checkpoint_path.exists()


def test_cohort_validation_receipt_invalidates_on_metadata_or_journal_drift(tmp_path: Path) -> None:
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort
    from hextech.infrastructure.persistence.cohort_validation_receipt import (
        load_valid_validation_receipt,
    )

    source_runtime, expected = _fixture_runtime(tmp_path / "source")
    bundle = _bundle_from_runtime(tmp_path, source_runtime)
    runtime = tmp_path / "target"
    _write_old_cohort(runtime)
    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "installed"
    assert load_valid_validation_receipt(runtime) is not None

    manifest_path = (
        runtime / "snapshots" / "generations" / expected["generation_id"] / "manifest.json"
    )
    stat = manifest_path.stat()
    os.utime(
        manifest_path,
        ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
    )
    assert manifest_path.stat().st_mtime_ns != stat.st_mtime_ns
    assert load_valid_validation_receipt(runtime) is None

    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "already_current"
    assert load_valid_validation_receipt(runtime) is not None
    atomic_write_json(
        runtime / "state/data-service/promotion_journal.v1.json",
        {"schema_version": 1, "phase": "prepared"},
    )
    assert load_valid_validation_receipt(runtime) is None


def test_runtime_receipt_still_materializes_older_bundle_seed(tmp_path: Path) -> None:
    """更新 runtime 的快路径不能跳过新 Build 自带的不可变 baseline。"""

    from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort
    from hextech.infrastructure.persistence.cohort_validation_receipt import write_validation_receipt

    bundle_runtime, bundle_expected = _fixture_runtime(tmp_path / "bundle-source")
    bundle = _bundle_from_runtime(tmp_path / "bundle-package", bundle_runtime)
    runtime, runtime_expected = _fixture_runtime(tmp_path / "runtime-source")
    runtime_generation = str(runtime_expected["generation_id"])
    bundle_generation = str(bundle_expected["generation_id"])
    assert runtime_generation != bundle_generation
    assert write_validation_receipt(
        runtime,
        validate_generation_cohort(runtime, runtime_generation),
    )
    current_before = (runtime / "snapshots/current.v2.json").read_bytes()
    bundled_manifest = runtime / "snapshots/generations" / bundle_generation / "manifest.json"
    assert not bundled_manifest.exists()

    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "runtime_newer"

    assert bundled_manifest.is_file()
    assert (runtime / "snapshots/current.v2.json").read_bytes() == current_before
    validate_generation_cohort(runtime, bundle_generation)


def test_older_runtime_receipt_does_not_block_newer_bundle_promotion(tmp_path: Path) -> None:
    """receipt 只能证明完整性，不能把较旧 current 冒充 runtime_newer。"""

    from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort
    from hextech.infrastructure.persistence.cohort_validation_receipt import write_validation_receipt

    runtime, runtime_expected = _fixture_runtime(tmp_path / "runtime-source")
    runtime_generation = str(runtime_expected["generation_id"])
    runtime_manifest_path = runtime / "snapshots/generations" / runtime_generation / "manifest.json"
    runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    runtime_manifest["created_at"] = "2026-09-05T00:00:00+00:00"
    atomic_write_json(runtime_manifest_path, runtime_manifest, ensure_ascii=False, indent=2)
    assert write_validation_receipt(
        runtime,
        validate_generation_cohort(runtime, runtime_generation),
    )

    bundle_runtime, bundle_expected = _fixture_runtime(tmp_path / "bundle-source")
    bundle_generation = str(bundle_expected["generation_id"])
    bundle_manifest_path = bundle_runtime / "snapshots/generations" / bundle_generation / "manifest.json"
    bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
    bundle_manifest["created_at"] = "2026-09-06T00:00:00+00:00"
    atomic_write_json(bundle_manifest_path, bundle_manifest, ensure_ascii=False, indent=2)
    bundle = _bundle_from_runtime(tmp_path / "bundle-package", bundle_runtime)

    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "installed"
    current = json.loads((runtime / "snapshots/current.v2.json").read_text(encoding="utf-8"))
    assert current["current_generation_id"] == bundle_generation


def test_receipt_timestamp_drift_cannot_hide_newer_bundle(tmp_path: Path) -> None:
    """单调比较必须读取已验证 manifest，不能信任 receipt 的缓存时间。"""

    from hextech.infrastructure.persistence.cohort_recovery import validate_generation_cohort
    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort
    from hextech.infrastructure.persistence.cohort_validation_receipt import (
        receipt_path,
        write_validation_receipt,
    )

    runtime, runtime_expected = _fixture_runtime(tmp_path / "runtime-source")
    runtime_generation = str(runtime_expected["generation_id"])
    runtime_manifest_path = runtime / "snapshots/generations" / runtime_generation / "manifest.json"
    runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
    runtime_manifest["created_at"] = "2026-09-05T00:00:00+00:00"
    atomic_write_json(runtime_manifest_path, runtime_manifest, ensure_ascii=False, indent=2)
    assert write_validation_receipt(runtime, validate_generation_cohort(runtime, runtime_generation))
    receipt_file = receipt_path(runtime)
    receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    receipt["generation_created_at"] = "2099-01-01T00:00:00+00:00"
    atomic_write_json(receipt_file, receipt, ensure_ascii=False, indent=2)

    bundle_runtime, bundle_expected = _fixture_runtime(tmp_path / "bundle-source")
    bundle_generation = str(bundle_expected["generation_id"])
    bundle_manifest_path = bundle_runtime / "snapshots/generations" / bundle_generation / "manifest.json"
    bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
    bundle_manifest["created_at"] = "2026-09-06T00:00:00+00:00"
    atomic_write_json(bundle_manifest_path, bundle_manifest, ensure_ascii=False, indent=2)
    bundle = _bundle_from_runtime(tmp_path / "bundle-package", bundle_runtime)

    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "installed"
    current = json.loads((runtime / "snapshots/current.v2.json").read_text(encoding="utf-8"))
    assert current["current_generation_id"] == bundle_generation


def test_install_cohort_rolls_back_all_pointers_when_generation_switch_fails(tmp_path: Path, monkeypatch) -> None:
    from hextech.infrastructure.persistence import cohort_seed
    from hextech.infrastructure.persistence.cohort import CohortPromotionStore

    source_runtime, _expected = _fixture_runtime(tmp_path / "source")
    bundle = _bundle_from_runtime(tmp_path, source_runtime)
    runtime = tmp_path / "target"
    old_bytes = _write_old_cohort(runtime)
    monkeypatch.setattr(
        CohortPromotionStore,
        "record_generation_promoted",
        lambda self, _generation_id: (_ for _ in ()).throw(RuntimeError("injected switch failure")),
    )

    with pytest.raises(RuntimeError, match="injected switch failure"):
        cohort_seed.install_bundled_cohort(bundle_root=bundle, runtime_root=runtime)

    assert all(path.read_bytes() == content for path, content in old_bytes.items())
    assert (runtime / "snapshots/generations/old-current/user-marker.txt").read_text() == "preserve me"
    assert not (runtime / "state/data-service/promotion_journal.v1.json").exists()


def test_collect_cohort_seed_rejects_missing_bound_catalog(tmp_path: Path) -> None:
    from tooling.build.cohort_seed import collect_cohort_seed

    runtime, expected = _fixture_runtime(tmp_path)
    shutil.rmtree(runtime / "catalog" / "generations" / str(expected["catalog_pointer"]["catalog_generation_id"]))

    with pytest.raises(ValueError, match="cohort seed JSON 无法读取"):
        collect_cohort_seed(runtime / "snapshots")


def test_collect_cohort_seed_rejects_catalog_schedule_drift(tmp_path: Path) -> None:
    """发布包不得携带指向不存在或非 active Catalog 的 schedule。"""

    from tooling.build.cohort_seed import collect_cohort_seed

    runtime, expected = _fixture_runtime(tmp_path)
    schedule_path = runtime / "state" / "data-service" / "refresh_schedule.v1.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["sources"]["catalog"]["current_run_id"] = "catalog-nonexistent"
    atomic_write_json(schedule_path, schedule, indent=2)

    with pytest.raises(ValueError, match="refresh schedule Catalog current_run_id 与 active Catalog 不一致"):
        collect_cohort_seed(runtime / "snapshots")


def _rewrite_generation_identity(runtime: Path, source_id: str, target_id: str, created_at: str) -> None:
    source = runtime / "snapshots/generations" / source_id
    target = runtime / "snapshots/generations" / target_id
    shutil.copytree(source, target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["generation_id"] = target_id
    manifest["created_at"] = created_at
    atomic_write_json(manifest_path, manifest, ensure_ascii=False, indent=2)


def test_bundle_startup_restores_newer_previous_before_installing_older_seed(tmp_path: Path) -> None:
    """回归 G3 → 旧 stable G1/previous G3 → 新候选 G2，不得把 G3 覆盖掉。"""

    from hextech.infrastructure.persistence.cohort_seed import install_bundled_cohort

    bundle_runtime, bundle_expected = _fixture_runtime(tmp_path / "bundle-source")
    bundle_manifest_path = (
        bundle_runtime
        / "snapshots/generations"
        / str(bundle_expected["generation_id"])
        / "manifest.json"
    )
    bundle_manifest = json.loads(bundle_manifest_path.read_text(encoding="utf-8"))
    bundle_manifest["created_at"] = "2026-08-22T00:00:00+00:00"
    atomic_write_json(bundle_manifest_path, bundle_manifest, ensure_ascii=False, indent=2)
    bundle = _bundle_from_runtime(tmp_path, bundle_runtime)

    runtime, expected = _fixture_runtime(tmp_path / "runtime-source")
    generation_g3 = str(expected["generation_id"])
    g3_manifest_path = runtime / "snapshots/generations" / generation_g3 / "manifest.json"
    g3_manifest = json.loads(g3_manifest_path.read_text(encoding="utf-8"))
    g3_manifest["created_at"] = "2026-08-23T00:00:00+00:00"
    atomic_write_json(g3_manifest_path, g3_manifest, ensure_ascii=False, indent=2)
    generation_g1 = "20260821T000000-generation-g1"
    _rewrite_generation_identity(
        runtime,
        generation_g3,
        generation_g1,
        "2026-08-21T00:00:00+00:00",
    )
    atomic_write_json(
        runtime / "snapshots/current.v2.json",
        {"schema_version": 2, "current_generation_id": generation_g1},
        indent=2,
    )
    atomic_write_json(
        runtime / "snapshots/previous.v2.json",
        {"schema_version": 2, "generation_id": generation_g3},
        indent=2,
    )

    assert install_bundled_cohort(bundle_root=bundle, runtime_root=runtime) == "runtime_restored"
    assert json.loads((runtime / "snapshots/current.v2.json").read_text())["current_generation_id"] == generation_g3
    assert json.loads((runtime / "snapshots/previous.v2.json").read_text())["generation_id"] == generation_g1
    schedule = json.loads((runtime / "state/data-service/refresh_schedule.v1.json").read_text())
    recovery = json.loads((runtime / "state/data-service/cohort_recovery_point.v1.json").read_text())
    selection = json.loads((runtime / "state/data-service/cohort_selection.v1.json").read_text())
    assert schedule["generation_id"] == generation_g3
    assert recovery["generation_id"] == generation_g3
    assert selection["install_state"] == "runtime_restored"
    assert selection["selected_generation_id"] == generation_g3

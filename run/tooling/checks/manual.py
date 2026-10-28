"""开发入口的只读数据健康与打包合同检查。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hextech.modules.data.catalog.runtime_store import get_runtime_root_dir
from hextech.modules.data.generation import DataSnapshotClient
from tooling.build.manifest import build_bundle_manifest, manifest_contains_forbidden_path

RUN_DIR = Path(__file__).resolve().parents[2]


def build_hextech_scrape_health_summary(*, runtime_store_module=None, data_evidence_dir=None) -> dict[str, Any]:
    """只检查当前数值快照；历史CSV和Web不再参与健康判定。"""
    root = Path(get_runtime_root_dir()) / "snapshots"
    try:
        view = DataSnapshotClient(root).open_view()
        return {"snapshot": view.status(), "generation_id": view.manifest.generation_id}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"snapshot": {"state": "unavailable"}, "error": f"{type(exc).__name__}: {exc}"}


def print_hextech_scrape_health_summary(*, as_json: bool, runtime_store_module=None, data_evidence_dir=None) -> None:
    summary = build_hextech_scrape_health_summary()
    print(json.dumps(summary, ensure_ascii=False, indent=2) if as_json else summary)


def validate_bundle_manifest_contract() -> dict[str, Any]:
    """验证打包清单契约并返回清单；调用方决定如何展示结果。"""

    manifest = build_bundle_manifest(RUN_DIR)
    assert manifest["seed_health"]["valid"] is True
    assert manifest["seed_health"]["stat_record_count"] > 0
    assert "resources/seeds/runtime.v4/snapshots/current.v4.json" in manifest["seed_files"]
    assert "resources/catalog/英雄目录.v1.json" in manifest["catalog_files"]
    assert "resources/catalog/海克斯资源目录.v1.json" in manifest["catalog_files"]
    source_files = manifest.get("source_files", [])
    for required_source in (
        "src/hextech/bootstrap/desktop.py",
        "src/hextech/interfaces/overlay/host.py",
        "src/hextech/infrastructure/sources/apex/service.py",
        "src/hextech/infrastructure/sources/mayhem/service.py",
    ):
        assert required_source in source_files
    assert all(str(item).startswith("src/hextech/") for item in source_files)
    obsolete_root = "data"
    for forbidden_path in (
        f"{obsolete_root}/raw",
        f"{obsolete_root}/runtime",
        f"{obsolete_root}/static",
        f"{obsolete_root}/seed",
        "var",
        "tests",
        "tooling",
        "__pycache__",
        ".pyc",
        ".pyo",
    ):
        assert not manifest_contains_forbidden_path(manifest, forbidden_path)
    assert not manifest_contains_forbidden_path(manifest, "overlay_anchor_calibration.v1.json")

    return manifest


def check_bundle_manifest(*, verbose: bool = False) -> None:
    manifest = validate_bundle_manifest_contract()
    if verbose:
        summary = {
            key: len(value) if isinstance(value, list) else value
            for key, value in manifest.items()
        }
        print(summary)
        print("seed_generation", manifest["seed_health"]["generation_id"])
        print("seed_files_count", len(manifest["seed_files"]))

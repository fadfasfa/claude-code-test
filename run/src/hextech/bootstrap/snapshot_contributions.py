"""DataService contribution 文件与 baseline generation 的只读解析门禁。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


def validated_source_artifact(source: str, pointer_payload: Mapping[str, Any], *, expected_role: str) -> Path:
    """只解析调用方传入的 immutable source run，并同时验证 manifest 与 artifact。"""

    from hextech.contracts import SourcePointerV2, SourceRunManifestV2
    from hextech.modules.data.catalog.versioned import sha256_file
    from hextech.modules.data.source_runs import source_run_artifact_path, source_run_dir

    pointer = SourcePointerV2.from_mapping(pointer_payload)
    if pointer.source != source or pointer.artifact.role != expected_role:
        raise ValueError(f"{source} contribution 角色不匹配：{pointer.artifact.role}")
    run_root = source_run_dir(source, pointer.run_id)
    manifest_path = run_root / "manifest.json"
    artifact_path = source_run_artifact_path(source, pointer.run_id, pointer.artifact.relative_path)
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise FileNotFoundError(f"{source} contribution 引用文件缺失：{pointer.run_id}")
    if (
        sha256_file(manifest_path) != pointer.manifest_sha256
        or sha256_file(artifact_path) != pointer.artifact.sha256
        or artifact_path.stat().st_size != pointer.artifact.size
    ):
        raise ValueError(f"{source} contribution 文件摘要不匹配：{pointer.run_id}")
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = SourceRunManifestV2.from_mapping(manifest_payload)
    if (
        not manifest.publishable
        or manifest.source != pointer.source
        or manifest.run_id != pointer.run_id
        or manifest.catalog_generation_id != pointer.catalog_generation_id
        or manifest.catalog_sha256 != pointer.catalog_sha256
        or manifest.artifact != pointer.artifact
    ):
        raise ValueError(f"{source} contribution manifest 与 pointer 不一致：{pointer.run_id}")
    return artifact_path


def open_baseline_view(pointer_payload: Mapping[str, Any]):
    """按 contribution 固定 origin generation；绝不隐式打开 current。"""

    from hextech.contracts import BaselineContributionV2
    from hextech.modules.data.generation import DataSnapshotClient, default_snapshot_root

    baseline = BaselineContributionV2.from_mapping(pointer_payload)
    view = DataSnapshotClient(default_snapshot_root()).open_generation(baseline.origin_generation_id)
    expected_files = {item.role: item for item in baseline.snapshot_files}
    actual_files = {item.role: item for item in view.manifest.files}
    if actual_files != expected_files:
        raise ValueError(f"baseline generation 文件身份不匹配：{baseline.origin_generation_id}")
    if baseline.provenance not in view.manifest.source_files:
        raise ValueError(f"baseline generation 缺少 {baseline.source} provenance")
    return baseline, view


__all__ = ["open_baseline_view", "validated_source_artifact"]

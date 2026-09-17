"""Bundled cohort 安装时的独立识别 Catalog 验证与选择。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hextech.contracts import CatalogManifestV2
from hextech.contracts.data_pipeline import require_identifier
from hextech.infrastructure.persistence.cohort_recovery import (
    CohortCandidate,
    parse_utc,
)
from hextech.modules.data.catalog.versioned import sha256_file, validate_catalog_files


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cohort seed JSON 无法读取：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"cohort seed JSON 必须是对象：{path.name}")
    return payload


def validated_catalog_manifest(
    runtime: Path,
    pointer: Mapping[str, Any],
) -> CatalogManifestV2:
    catalog_id = require_identifier(
        pointer.get("catalog_generation_id"),
        field_name="catalog_generation_id",
    )
    root = runtime / "catalog" / "generations" / catalog_id
    manifest_path = root / "manifest.json"
    try:
        manifest = CatalogManifestV2.from_mapping(_read_object(manifest_path))
        if (
            pointer.get("schema_version") != 2
            or manifest.catalog_generation_id != catalog_id
            or pointer.get("content_sha256") != manifest.content_sha256
            or pointer.get("manifest_sha256") != sha256_file(manifest_path)
        ):
            raise ValueError("身份不一致")
        validate_catalog_files(root, manifest)
        return manifest
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise ValueError(
            "cohort seed recognition Catalog pointer/manifest 校验失败"
        ) from exc


def startup_pointers(
    runtime: Path,
    candidate: CohortCandidate,
    *,
    bundled_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Mapping[str, Any]]:
    """统计恢复不回退已独立发布且更新的识别 Catalog。"""

    pointers = dict(candidate.pointers)
    if candidate.snapshot_schema_version != 3:
        return pointers
    selected = candidate.pointers["catalog"]
    selected_manifest = validated_catalog_manifest(runtime, selected)
    candidates: list[Mapping[str, Any]] = []
    if bundled_catalog is not None:
        candidates.append(bundled_catalog)
    try:
        candidates.append(_read_object(runtime / "catalog/current.v2.json"))
    except ValueError:
        pass
    for pointer in candidates:
        try:
            manifest = validated_catalog_manifest(runtime, pointer)
            if parse_utc(manifest.created_at) >= parse_utc(selected_manifest.created_at):
                selected = pointer
                selected_manifest = manifest
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            continue
    pointers["catalog"] = selected
    return pointers


__all__ = ["startup_pointers", "validated_catalog_manifest"]

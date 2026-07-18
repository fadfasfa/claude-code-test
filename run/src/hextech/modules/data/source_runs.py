"""来源 run 的 v2 落盘、校验与原子 current 发布。

失败 run 可以保留 manifest/report，但只有绑定固定 Catalog 且完整通过门禁的
artifact 才能切换 ``current.v2.json``。本模块明确拒绝 v1 pointer。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import ArtifactDescriptor, SourcePointerV2, SourceRunManifestV2, utc_now_iso
from hextech.modules.data.catalog.versioned import sha256_file
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import var_path


KNOWN_SOURCES = frozenset({"hextech", "apex", "mayhem"})
SOURCE_POINTER_VERSION = 2
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SourceRunValidationError(ValueError):
    pass


def _validate_source(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if normalized not in KNOWN_SOURCES:
        raise SourceRunValidationError(f"未知来源：{source}")
    return normalized


def _validate_run_id(run_id: str) -> str:
    normalized = str(run_id or "").strip()
    if not _RUN_ID_RE.fullmatch(normalized):
        raise SourceRunValidationError(f"非法 run_id：{run_id}")
    return normalized


def _validate_artifact_name(artifact: str) -> str:
    normalized = str(artifact or "").strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        raise SourceRunValidationError(f"非法 artifact：{artifact}")
    return normalized


def source_root(source: str) -> Path:
    return var_path("sources", _validate_source(source))


def source_run_dir(source: str, run_id: str) -> Path:
    return source_root(source) / "runs" / _validate_run_id(run_id)


def source_current_path(source: str) -> Path:
    return source_root(source) / "current.v2.json"


def source_run_artifact_path(source: str, run_id: str, artifact: str) -> Path:
    run_dir = source_run_dir(source, run_id).resolve()
    target = (run_dir / _validate_artifact_name(artifact)).resolve()
    if run_dir not in target.parents:
        raise SourceRunValidationError(f"artifact 越界：{artifact}")
    return target


def _normalized_manifest(manifest: SourceRunManifestV2) -> SourceRunManifestV2:
    if manifest.artifact is None:
        if manifest.health.value == "healthy":
            raise SourceRunValidationError("健康来源 run 缺少 artifact")
        return manifest
    artifact_path = source_run_artifact_path(manifest.source, manifest.run_id, manifest.artifact.relative_path)
    if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
        raise SourceRunValidationError(f"来源 artifact 缺失或为空：{artifact_path}")
    descriptor = replace(
        manifest.artifact,
        sha256=sha256_file(artifact_path),
        size=artifact_path.stat().st_size,
    )
    return replace(manifest, artifact=descriptor)


def write_run_diagnostics(
    manifest: SourceRunManifestV2,
    *,
    report: Mapping[str, Any] | None = None,
) -> tuple[Path, SourceRunManifestV2]:
    normalized = _normalized_manifest(manifest)
    run_dir = source_run_dir(normalized.source, normalized.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "manifest.json", normalized.to_dict(), ensure_ascii=False, indent=2)
    atomic_write_json(run_dir / "report.json", dict(report or {}), ensure_ascii=False, indent=2)
    return run_dir, normalized


def build_source_pointer(manifest: SourceRunManifestV2) -> SourcePointerV2:
    if manifest.artifact is None:
        raise SourceRunValidationError("没有 artifact 的来源 run 不得发布 current")
    manifest_path = source_run_dir(manifest.source, manifest.run_id) / "manifest.json"
    if not manifest_path.is_file():
        raise SourceRunValidationError(f"来源 manifest 缺失：{manifest_path}")
    return SourcePointerV2(
        source=manifest.source,
        run_id=manifest.run_id,
        catalog_generation_id=manifest.catalog_generation_id,
        catalog_sha256=manifest.catalog_sha256,
        manifest_sha256=sha256_file(manifest_path),
        artifact=manifest.artifact,
        completed_at=manifest.completed_at,
        last_success_at=utc_now_iso(),
    )


def publish_source_run(
    manifest: SourceRunManifestV2,
    *,
    report: Mapping[str, Any] | None = None,
    promote_current: bool = False,
    pointer_output: str | Path | None = None,
) -> dict[str, Any]:
    """完整校验来源 run，并只生成 immutable run 与 candidate pointer。

    正式 source current 是 cohort 的组成部分，禁止来源抓取器独立切换；只有
    ``CohortPromotionStore`` 在完整 cohort 通过后才能写入正式 pointer。
    """

    if promote_current:
        raise SourceRunValidationError("正式 source current 只能由 cohort promotion 切换")

    source = _validate_source(manifest.source)
    try:
        _, normalized = write_run_diagnostics(manifest, report=report)
    except (OSError, ValueError) as exc:
        raise SourceRunValidationError(str(exc)) from exc
    if not normalized.publishable:
        raise SourceRunValidationError(f"来源 run 未通过完整性门禁：{source}/{manifest.run_id}")
    pointer = build_source_pointer(normalized)
    payload = pointer.to_dict()
    if pointer_output is not None:
        atomic_write_json(Path(pointer_output), payload, ensure_ascii=False, indent=2)
    return payload


def load_source_current(source: str, *, verify_hash: bool = True) -> dict[str, Any]:
    pointer_path = source_current_path(source)
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return {}
        pointer = SourcePointerV2.from_mapping(payload)
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if pointer.source != _validate_source(source):
        return {}
    run_dir = source_run_dir(pointer.source, pointer.run_id)
    manifest_path = run_dir / "manifest.json"
    artifact_path = source_run_artifact_path(pointer.source, pointer.run_id, pointer.artifact.relative_path)
    if not manifest_path.is_file() or not artifact_path.is_file():
        return {}
    if verify_hash and (
        sha256_file(manifest_path) != pointer.manifest_sha256
        or sha256_file(artifact_path) != pointer.artifact.sha256
        or artifact_path.stat().st_size != pointer.artifact.size
    ):
        return {}
    return pointer.to_dict()


def resolve_current_artifact(source: str, *, verify_hash: bool = True) -> Path | None:
    pointer = load_source_current(source, verify_hash=verify_hash)
    if not pointer:
        return None
    descriptor = ArtifactDescriptor.from_mapping(pointer["artifact"])
    return source_run_artifact_path(source, str(pointer["run_id"]), descriptor.relative_path)


def build_artifact_descriptor(
    path: Path,
    *,
    role: str,
    relative_path: str,
    record_count: int,
    content_schema_version: int = 2,
) -> ArtifactDescriptor:
    if not path.is_file():
        raise SourceRunValidationError(f"来源 artifact 不存在：{path}")
    return ArtifactDescriptor(
        role=role,
        relative_path=relative_path,
        sha256=sha256_file(path),
        record_count=record_count,
        content_schema_version=content_schema_version,
        size=path.stat().st_size,
    )


__all__ = [
    "KNOWN_SOURCES",
    "SOURCE_POINTER_VERSION",
    "SourceRunValidationError",
    "build_artifact_descriptor",
    "build_source_pointer",
    "load_source_current",
    "publish_source_run",
    "resolve_current_artifact",
    "source_current_path",
    "source_root",
    "source_run_artifact_path",
    "source_run_dir",
    "write_run_diagnostics",
]

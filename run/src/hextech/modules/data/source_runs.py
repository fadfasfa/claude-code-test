"""来源 run 的落盘与 ``current.v1.json`` 原子发布。

抓取器只能写自己的 run 目录。只有通过完整性门禁的 manifest 才能切换来源
current；失败 run 可保留诊断证据，但不会影响上一份 last-good 数据。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import var_path
from hextech.modules.acquisition.common.contracts import SourceRunManifest, utc_now_iso


SOURCE_POINTER_VERSION = 1
KNOWN_SOURCES = frozenset({"hextech", "apex", "mayhem"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


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
    return source_root(source) / "current.v1.json"


def source_run_artifact_path(source: str, run_id: str, artifact: str) -> Path:
    run_dir = source_run_dir(source, run_id).resolve()
    target = (run_dir / _validate_artifact_name(artifact)).resolve()
    if run_dir not in target.parents:
        raise SourceRunValidationError(f"artifact 越界：{artifact}")
    return target


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_run_diagnostics(
    manifest: SourceRunManifest,
    *,
    report: dict[str, Any] | None = None,
) -> Path:
    run_dir = source_run_dir(manifest.source, manifest.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "manifest.json", manifest.to_dict(), indent=2)
    atomic_write_json(run_dir / "report.json", report or {}, indent=2)
    return run_dir


def publish_source_run(manifest: SourceRunManifest, *, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """校验 artifact 后原子切 current；不完整 manifest 会直接失败。"""

    source = _validate_source(manifest.source)
    if not manifest.publishable:
        write_run_diagnostics(manifest, report=report)
        raise SourceRunValidationError(f"来源 run 未通过完整性门禁：{source}/{manifest.run_id}")

    artifact_path = source_run_artifact_path(source, manifest.run_id, manifest.artifact)
    if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
        raise SourceRunValidationError(f"来源 artifact 缺失或为空：{artifact_path}")

    write_run_diagnostics(manifest, report=report)
    pointer = {
        "version": SOURCE_POINTER_VERSION,
        "source": source,
        "run_id": manifest.run_id,
        "artifact": manifest.artifact,
        "sha256": sha256_file(artifact_path),
        "record_count": manifest.record_count,
        "published_at": utc_now_iso(),
    }
    atomic_write_json(source_current_path(source), pointer, indent=2)
    return pointer


def load_source_current(source: str, *, verify_hash: bool = True) -> dict[str, Any]:
    pointer_path = source_current_path(source)
    try:
        payload = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != SOURCE_POINTER_VERSION:
        return {}
    if str(payload.get("source") or "") != _validate_source(source):
        return {}
    try:
        artifact_path = source_run_artifact_path(source, str(payload.get("run_id") or ""), str(payload.get("artifact") or ""))
    except SourceRunValidationError:
        return {}
    if not artifact_path.is_file():
        return {}
    if verify_hash and str(payload.get("sha256") or "") != sha256_file(artifact_path):
        return {}
    return payload


def resolve_current_artifact(source: str, *, verify_hash: bool = True) -> Path | None:
    pointer = load_source_current(source, verify_hash=verify_hash)
    if not pointer:
        return None
    return source_run_artifact_path(source, str(pointer["run_id"]), str(pointer["artifact"]))


__all__ = [
    "KNOWN_SOURCES",
    "SOURCE_POINTER_VERSION",
    "SourceRunValidationError",
    "load_source_current",
    "publish_source_run",
    "resolve_current_artifact",
    "sha256_file",
    "source_current_path",
    "source_root",
    "source_run_artifact_path",
    "source_run_dir",
    "write_run_diagnostics",
]

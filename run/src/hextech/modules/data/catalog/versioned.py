"""不可变 Catalog generation 和只读启动基线。

``resources/catalog`` 只提供随包基线；联网更新必须写入 ``var/catalog/generations``。
本模块返回固定 Catalog 身份，来源抓取和 generation provenance 都必须绑定它。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from hextech.contracts import ArtifactDescriptor, CatalogManifestV2, SourceProvenance
from hextech.contracts.data_pipeline import require_identifier
from hextech.modules.data.catalog.version_catalog import (
    load_augment_manifest_entries,
    load_champion_core_data,
)
from hextech.modules.data.ports.paths import resource_path, var_path


CATALOG_CURRENT_FILENAME = "current.v2.json"
CATALOG_MANIFEST_FILENAME = "manifest.v2.json"
CATALOG_FILES = (
    ("champions", "英雄目录.v1.json", "aliases"),
    ("augments", "海克斯资源目录.v1.json", "entries"),
    ("versions", "hero_version.txt", ""),
)


class CatalogValidationError(RuntimeError):
    """Catalog 文件、manifest 或 pointer 不满足 v2 契约。"""


@dataclass(frozen=True)
class CatalogView:
    root: Path
    manifest: CatalogManifestV2
    manifest_sha256: str
    baseline: bool = False

    @property
    def generation_id(self) -> str:
        return self.manifest.catalog_generation_id

    @property
    def content_sha256(self) -> str:
        return self.manifest.content_sha256

    def provenance(self) -> tuple[SourceProvenance, ...]:
        return tuple(
            SourceProvenance(
                source="catalog",
                run_id=self.generation_id,
                catalog_generation_id=self.generation_id,
                artifact_role=item.role,
                artifact_sha256=item.sha256,
                record_count=item.record_count,
                manifest_sha256=self.manifest_sha256,
                content_schema_version=item.content_schema_version,
            )
            for item in self.manifest.files
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_count(path: Path, list_key: str) -> int:
    if not list_key:
        return 1 if path.read_text(encoding="utf-8").strip() else 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogValidationError(f"Catalog JSON 无法读取：{path.name}: {exc}") from exc
    records = payload.get(list_key) if isinstance(payload, Mapping) else None
    if not isinstance(records, list) or not records:
        raise CatalogValidationError(f"Catalog {path.name} 缺少非空 {list_key}")
    return len(records)


def build_catalog_manifest(root: Path, *, created_at: str) -> CatalogManifestV2:
    descriptors: list[ArtifactDescriptor] = []
    for role, filename, list_key in CATALOG_FILES:
        path = root / filename
        if not path.is_file() or path.stat().st_size <= 0:
            raise CatalogValidationError(f"Catalog 文件缺失或为空：{path}")
        descriptors.append(
            ArtifactDescriptor(
                role=role,
                relative_path=filename,
                sha256=sha256_file(path),
                record_count=_record_count(path, list_key),
                content_schema_version=2,
                size=path.stat().st_size,
            )
        )
    fingerprint_payload = {
        "schema_version": 2,
        "files": [{"role": item.role, "sha256": item.sha256} for item in descriptors],
    }
    content_sha256 = canonical_json_sha256(fingerprint_payload)
    return CatalogManifestV2(
        catalog_generation_id=f"catalog-{content_sha256[:20]}",
        created_at=created_at,
        files=tuple(descriptors),
        content_sha256=content_sha256,
    )


def _manifest_sha256(manifest: CatalogManifestV2) -> str:
    return canonical_json_sha256(asdict(manifest))


def load_baseline_catalog() -> CatalogView:
    root = resource_path("catalog")
    manifest_path = root / CATALOG_MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = tuple(ArtifactDescriptor.from_mapping(item) for item in payload["files"])
            manifest = CatalogManifestV2(
                schema_version=payload["schema_version"],
                catalog_generation_id=str(payload["catalog_generation_id"]),
                created_at=str(payload["created_at"]),
                files=files,
                content_sha256=str(payload["content_sha256"]),
            )
        except (KeyError, TypeError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogValidationError(f"Catalog 基线 manifest 无效：{exc}") from exc
    else:
        manifest = build_catalog_manifest(root, created_at="baseline")
    validate_catalog_files(root, manifest)
    return CatalogView(root=root, manifest=manifest, manifest_sha256=_manifest_sha256(manifest), baseline=True)


def validate_catalog_files(root: Path, manifest: CatalogManifestV2) -> None:
    for item in manifest.files:
        path = (root / item.relative_path).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise CatalogValidationError(f"Catalog artifact 缺失或越界：{item.relative_path}")
        if path.stat().st_size != item.size or sha256_file(path) != item.sha256:
            raise CatalogValidationError(f"Catalog artifact 校验失败：{item.relative_path}")
    counts = {item.role: item.record_count for item in manifest.files}
    champions = load_champion_core_data(root)
    if len(champions) != counts["champions"] or any(
        not str(item.get("name") or "").strip() or not str(item.get("en_name") or "").strip()
        for item in champions.values()
    ):
        raise CatalogValidationError(
            f"Catalog 英雄投影不完整：projected={len(champions)} expected={counts['champions']}"
        )
    augments = load_augment_manifest_entries(root)
    if len(augments) != counts["augments"]:
        raise CatalogValidationError(
            f"Catalog 海克斯投影不完整：projected={len(augments)} expected={counts['augments']}"
        )
    actual = build_catalog_manifest(root, created_at=manifest.created_at)
    if actual.content_sha256 != manifest.content_sha256:
        raise CatalogValidationError("Catalog content SHA-256 与文件不一致")


def catalog_root() -> Path:
    return var_path("catalog")


def catalog_current_path() -> Path:
    return catalog_root() / CATALOG_CURRENT_FILENAME


def load_runtime_catalog_from_pointer(pointer: Mapping[str, Any]) -> CatalogView | None:
    """校验并打开调用方已经按 cohort 语义解析出的 Catalog pointer。"""

    try:
        if pointer.get("schema_version") != 2:
            return None
        generation_id = require_identifier(pointer["catalog_generation_id"], field_name="catalog_generation_id")
        root = catalog_root() / "generations" / generation_id
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file() or sha256_file(manifest_path) != str(pointer["manifest_sha256"]):
            return None
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = CatalogManifestV2(
            schema_version=payload["schema_version"],
            catalog_generation_id=str(payload["catalog_generation_id"]),
            created_at=str(payload["created_at"]),
            files=tuple(ArtifactDescriptor.from_mapping(item) for item in payload["files"]),
            content_sha256=str(payload["content_sha256"]),
        )
        if manifest.catalog_generation_id != generation_id:
            return None
        validate_catalog_files(root, manifest)
        return CatalogView(root=root, manifest=manifest, manifest_sha256=str(pointer["manifest_sha256"]))
    except (KeyError, TypeError, ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_runtime_catalog_pointer(pointer_path: Path) -> CatalogView | None:
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(pointer, Mapping):
        return None
    return load_runtime_catalog_from_pointer(pointer)


def load_runtime_catalog() -> CatalogView | None:
    return _load_runtime_catalog_pointer(catalog_current_path())


def load_active_catalog() -> CatalogView:
    """返回正式 runtime Catalog；尚未发布时使用只读包基线。"""

    candidate = os.getenv("HEXTECH_CATALOG_POINTER_PATH", "").strip()
    if candidate:
        candidate_path = Path(candidate).expanduser().resolve()
        runtime_root = var_path().resolve()
        if candidate_path != runtime_root and runtime_root not in candidate_path.parents:
            raise CatalogValidationError("Catalog candidate pointer 越出运行态目录")
        view = _load_runtime_catalog_pointer(candidate_path)
        if view is None:
            raise CatalogValidationError("Catalog candidate pointer 无效")
        return view
    return load_runtime_catalog() or load_baseline_catalog()


__all__ = [
    "CATALOG_CURRENT_FILENAME",
    "CATALOG_FILES",
    "CATALOG_MANIFEST_FILENAME",
    "CatalogValidationError",
    "CatalogView",
    "build_catalog_manifest",
    "canonical_json_sha256",
    "catalog_current_path",
    "catalog_root",
    "load_active_catalog",
    "load_baseline_catalog",
    "load_runtime_catalog_from_pointer",
    "load_runtime_catalog",
    "sha256_file",
    "validate_catalog_files",
]

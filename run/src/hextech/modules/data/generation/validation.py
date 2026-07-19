"""generation 的纯结构、计数、provenance 与文件完整性门禁。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from hextech.contracts import DataSnapshotManifestV2, SourceProvenance


SNAPSHOT_ROLES = ("champions", "champion_hextech", "overlay_hints", "identities")


class SnapshotValidationError(RuntimeError):
    """generation 结构、文件、来源或哈希不满足完整代约束。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_generation_file(directory: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise SnapshotValidationError(f"generation 文件路径无效：{relative_path}")
    root = directory.resolve()
    candidate = (directory / relative_path).resolve()
    if root not in candidate.parents:
        raise SnapshotValidationError(f"generation 文件路径越界：{relative_path}")
    return candidate


def count_records(payloads: Mapping[str, Any]) -> tuple[int, int, int]:
    champions = payloads.get("champions")
    details = payloads.get("champion_hextech")
    hints = payloads.get("overlay_hints")
    identities = payloads.get("identities")
    if not isinstance(champions, list):
        raise SnapshotValidationError("champions 必须是列表")
    if not all(isinstance(value, Mapping) for value in (details, hints, identities)):
        raise SnapshotValidationError("champion_hextech、overlay_hints 和 identities 必须是对象")
    if identities.get("schema_version") != 2:
        raise SnapshotValidationError("identities schema_version 必须为 2")

    champion_ids: set[str] = set()
    champion_names: dict[str, str] = {}
    champion_ids_by_name: dict[str, str] = {}
    for champion in champions:
        if not isinstance(champion, Mapping):
            raise SnapshotValidationError("champions 元素必须是对象")
        champion_id = str(champion.get("id", "")).strip()
        champion_name = str(champion.get("name", "")).strip()
        if not champion_id or not champion_name:
            raise SnapshotValidationError("champion 必须包含非空 id 和 name")
        if champion_id in champion_ids:
            raise SnapshotValidationError(f"champion id 重复：{champion_id}")
        if champion_name in champion_ids_by_name:
            raise SnapshotValidationError(f"champion name 重复：{champion_name}")
        champion_ids.add(champion_id)
        champion_names[champion_id] = champion_name
        champion_ids_by_name[champion_name] = champion_id
    if not champion_ids:
        raise SnapshotValidationError("generation 至少需要一个英雄")

    augment_ids: set[str] = set()
    stat_records = 0
    detail_names = {str(key).strip() for key in details}
    if detail_names != set(champion_ids_by_name):
        raise SnapshotValidationError(
            "英雄详情名称投影不一致："
            f"missing={sorted(set(champion_ids_by_name) - detail_names)} "
            f"unexpected={sorted(detail_names - set(champion_ids_by_name))}"
        )
    for detail_name, detail in details.items():
        if not isinstance(detail, Mapping):
            raise SnapshotValidationError("英雄详情必须是对象")
        hero_id = str(detail.get("hero_id") or "").strip()
        if not hero_id:
            raise SnapshotValidationError("英雄详情必须包含非空 hero_id")
        expected_hero_id = champion_ids_by_name[str(detail_name).strip()]
        if hero_id != expected_hero_id:
            raise SnapshotValidationError(
                f"英雄详情身份错配：name={detail_name} expected={expected_hero_id} actual={hero_id}"
            )
        augments = detail.get("augments", [])
        if not isinstance(augments, list) or not augments:
            raise SnapshotValidationError(f"英雄 {hero_id} 必须包含非空统计")
        seen: set[str] = set()
        for augment in augments:
            if not isinstance(augment, Mapping) or augment.get("id") is None:
                raise SnapshotValidationError("augment 统计必须是包含 id 的对象")
            augment_id = str(augment["id"])
            if augment_id in seen:
                raise SnapshotValidationError(f"英雄 {hero_id} augment 重复：{augment_id}")
            seen.add(augment_id)
            augment_ids.add(augment_id)
            stat_records += 1
    identity_champions = identities.get("champions")
    if not isinstance(identity_champions, Mapping) or {
        str(key): str(value) for key, value in identity_champions.items()
    } != champion_names:
        raise SnapshotValidationError("identities 英雄投影与 champions 不一致")
    identity_augments = identities.get("augments")
    if not isinstance(identity_augments, Mapping):
        raise SnapshotValidationError("identities 强化投影必须是对象")
    normalized_identity_augments = {str(key).strip(): str(value).strip() for key, value in identity_augments.items()}
    if set(normalized_identity_augments) != augment_ids:
        raise SnapshotValidationError("identities 强化投影必须与英雄统计 ID 完全一致")

    hint_map = hints.get("hints")
    name_index = hints.get("name_index")
    if not isinstance(hint_map, Mapping) or not hint_map:
        raise SnapshotValidationError("overlay_hints.hints 必须是非空对象")
    if not isinstance(name_index, Mapping) or not name_index:
        raise SnapshotValidationError("overlay_hints.name_index 必须是非空对象")
    normalized_hints: dict[str, Mapping[str, Any]] = {}
    for raw_id, raw_hint in hint_map.items():
        hint_id = str(raw_id).strip()
        if not hint_id or not isinstance(raw_hint, Mapping):
            raise SnapshotValidationError("overlay hint 必须使用非空 ID 并映射到对象")
        if str(raw_hint.get("augment_id") or hint_id).strip() != hint_id:
            raise SnapshotValidationError(f"overlay hint augment_id 与键不一致：{hint_id}")
        if not str(raw_hint.get("name") or "").strip():
            raise SnapshotValidationError(f"overlay hint 缺少名称：{hint_id}")
        normalized_hints[hint_id] = raw_hint
    hint_ids = set(normalized_hints)
    index_targets = {str(value).strip() for value in name_index.values()}
    # 早期 verified seed 含一个空别名键；它不参与查询，但其目标仍须是有效 hint。
    # 保留该只读基线兼容性，同时继续要求所有 hint 都被索引且不存在悬空目标。
    if index_targets != hint_ids:
        raise SnapshotValidationError("overlay_hints.name_index 必须完整且只能指向现有 hint")
    if not augment_ids.issubset(hint_ids):
        raise SnapshotValidationError("overlay hints 未覆盖英雄统计强化")
    for augment_id, augment_name in normalized_identity_augments.items():
        if str(normalized_hints[augment_id].get("name") or "").strip() != augment_name:
            raise SnapshotValidationError(f"identities 与 overlay hint 名称不一致：{augment_id}")
    return len(champions), len(augment_ids), stat_records


def content_fingerprint(source_files: Sequence[SourceProvenance]) -> str:
    payload = {
        "schema_version": 2,
        "sources": [
            {
                "source": item.source,
                "artifact_role": item.artifact_role,
                "artifact_sha256": item.artifact_sha256,
                "content_schema_version": item.content_schema_version,
            }
            for item in sorted(source_files, key=lambda value: (value.source, value.artifact_role))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_complete_provenance(source_files: Sequence[SourceProvenance]) -> None:
    roles = {(item.source, item.artifact_role) for item in source_files}
    if len(roles) != len(source_files):
        raise SnapshotValidationError("generation provenance 角色重复")
    required = {
        ("catalog", "champions"),
        ("catalog", "augments"),
        ("catalog", "versions"),
        ("hextech", "stats"),
        ("apex", "synergy"),
        ("mayhem", "combos"),
    }
    if roles != required:
        raise SnapshotValidationError(f"generation provenance 不完整：missing={sorted(required - roles)}")
    catalog_ids = {item.catalog_generation_id for item in source_files}
    if len(catalog_ids) != 1:
        raise SnapshotValidationError("generation provenance 混用了不同 Catalog")
    for item in source_files:
        if item.source in {"hextech", "apex", "mayhem"} and item.record_count <= 0:
            raise SnapshotValidationError(f"{item.source} provenance 记录数必须大于 0")


def validate_generation_directory(
    directory: Path,
    manifest: DataSnapshotManifestV2,
    *,
    payloads: Mapping[str, Any] | None = None,
    allow_staging: bool = False,
    require_complete_provenance: bool = False,
) -> None:
    expected_names = {manifest.generation_id}
    if allow_staging:
        expected_names.add(manifest.generation_id)
    if directory.name not in expected_names:
        raise SnapshotValidationError("manifest generation_id 与目录不一致")
    if {item.role for item in manifest.files} != set(SNAPSHOT_ROLES) or len(manifest.files) != len(SNAPSHOT_ROLES):
        raise SnapshotValidationError("manifest 文件角色不完整或重复")
    relative_paths = [item.relative_path for item in manifest.files]
    if len(relative_paths) != len(set(relative_paths)):
        raise SnapshotValidationError("manifest generation 文件路径重复")
    for item in manifest.files:
        path = safe_generation_file(directory, item.relative_path)
        if not path.is_file() or path.stat().st_size != item.size or sha256_file(path) != item.sha256:
            raise SnapshotValidationError(f"generation 文件校验失败：{item.relative_path}")
    expected_fingerprint = content_fingerprint(manifest.source_files)
    if manifest.content_fingerprint != expected_fingerprint:
        raise SnapshotValidationError("generation content_fingerprint 与 provenance 不一致")
    if require_complete_provenance:
        validate_complete_provenance(manifest.source_files)
    if payloads is not None:
        actual = count_records(payloads)
        expected = (manifest.champion_count, manifest.augment_count, manifest.stat_record_count)
        if actual != expected:
            raise SnapshotValidationError(f"manifest 计数与 generation 内容不一致：expected={expected} actual={actual}")


__all__ = [
    "SNAPSHOT_ROLES",
    "SnapshotValidationError",
    "content_fingerprint",
    "count_records",
    "safe_generation_file",
    "sha256_file",
    "validate_complete_provenance",
    "validate_generation_directory",
]

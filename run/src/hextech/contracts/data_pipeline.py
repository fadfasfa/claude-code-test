"""数据链路的版本化稳定契约。

这些 DTO 只描述不可变 Catalog、来源 run、generation provenance 和 promotion
journal，不负责网络、文件系统或进程管理。所有落盘读取都必须先经过这里的严格
解析，旧 schema 不会被隐式升级或兼容。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from .models import FailureKind, SourceHealth


SOURCE_RUN_SCHEMA_VERSION = 2
SOURCE_POINTER_SCHEMA_VERSION = 2
CATALOG_SCHEMA_VERSION = 2
SNAPSHOT_SCHEMA_VERSION = 2
SNAPSHOT_POINTER_SCHEMA_VERSION = 2
CONTENT_FINGERPRINT_SCHEMA_VERSION = 2
PROMOTION_JOURNAL_SCHEMA_VERSION = 1
REFRESH_SCHEDULE_SCHEMA_VERSION = 1

ItemState = Literal["success", "confirmed_empty", "failed"]
SourceName = Literal["catalog", "hextech", "apex", "mayhem"]
PromotionPhase = Literal["prepared", "dependencies_promoted", "generation_promoted", "committed"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DataContractError(ValueError):
    """版本化 DTO 结构或字段违反稳定契约。"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_sha256(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise DataContractError(f"{field_name} 必须是 64 位 SHA-256")
    return normalized


def require_identifier(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _ID_RE.fullmatch(normalized):
        raise DataContractError(f"{field_name} 格式无效：{value}")
    return normalized


def require_relative_path(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise DataContractError(f"{field_name} 必须是受控相对路径：{value}")
    return normalized


def _non_negative_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DataContractError(f"{field_name} 必须是非负整数")
    return value


@dataclass(frozen=True)
class FetchAttempt:
    url: str
    backend: str
    status_code: int | None
    elapsed_ms: int
    attempts: int
    failure_kind: FailureKind | None = None
    retryable: bool = False
    fetched_at: str = field(default_factory=utc_now_iso)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.failure_kind is None and self.status_code is not None and 200 <= self.status_code < 400

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else ""
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "FetchAttempt":
        failure_raw = str(payload.get("failure_kind") or "")
        try:
            return cls(
                url=str(payload["url"]),
                backend=str(payload["backend"]),
                status_code=payload.get("status_code"),
                elapsed_ms=payload["elapsed_ms"],
                attempts=payload["attempts"],
                failure_kind=FailureKind(failure_raw) if failure_raw else None,
                retryable=bool(payload.get("retryable")),
                fetched_at=str(payload.get("fetched_at") or ""),
                error=str(payload.get("error") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataContractError(f"fetch attempt 无效：{exc}") from exc


@dataclass(frozen=True)
class ItemOutcome:
    item_id: str
    state: ItemState
    stage: str
    record_count: int = 0
    failure_kind: FailureKind | None = None
    attempt: FetchAttempt | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in {"success", "confirmed_empty", "failed"}:
            raise DataContractError(f"item outcome state 无效：{self.state}")
        _non_negative_int(self.record_count, field_name="item outcome.record_count")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else ""
        payload["attempt"] = self.attempt.to_dict() if self.attempt else None
        payload["details"] = dict(self.details)
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ItemOutcome":
        failure_raw = str(payload.get("failure_kind") or "")
        attempt_payload = payload.get("attempt")
        details = payload.get("details")
        try:
            return cls(
                item_id=str(payload["item_id"]),
                state=str(payload["state"]),  # type: ignore[arg-type]
                stage=str(payload["stage"]),
                record_count=payload.get("record_count", 0),
                failure_kind=FailureKind(failure_raw) if failure_raw else None,
                attempt=(
                    FetchAttempt.from_mapping(attempt_payload)
                    if isinstance(attempt_payload, Mapping)
                    else None
                ),
                details=dict(details) if isinstance(details, Mapping) else {},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataContractError(f"item outcome 无效：{exc}") from exc


@dataclass(frozen=True)
class ArtifactDescriptor:
    role: str
    relative_path: str
    sha256: str
    record_count: int
    content_schema_version: int
    size: int

    def __post_init__(self) -> None:
        if not str(self.role or "").strip():
            raise DataContractError("artifact.role 不能为空")
        require_relative_path(self.relative_path, field_name="artifact.relative_path")
        require_sha256(self.sha256, field_name="artifact.sha256")
        _non_negative_int(self.record_count, field_name="artifact.record_count")
        if isinstance(self.content_schema_version, bool) or self.content_schema_version <= 0:
            raise DataContractError("artifact.content_schema_version 必须是正整数")
        _non_negative_int(self.size, field_name="artifact.size")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ArtifactDescriptor":
        try:
            return cls(
                role=str(payload["role"]),
                relative_path=str(payload["relative_path"]),
                sha256=str(payload["sha256"]),
                record_count=payload["record_count"],
                content_schema_version=payload["content_schema_version"],
                size=payload["size"],
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"artifact descriptor 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceRunManifestV2:
    source: str
    run_id: str
    catalog_generation_id: str
    catalog_sha256: str
    health: SourceHealth
    started_at: str
    completed_at: str
    expected_items: int
    successful_items: int
    confirmed_empty_items: int
    failed_items: int
    artifact: ArtifactDescriptor | None
    outcomes: tuple[ItemOutcome, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SOURCE_RUN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_RUN_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 source run schema：{self.schema_version}")
        if self.source not in {"hextech", "apex", "mayhem"}:
            raise DataContractError(f"未知来源：{self.source}")
        require_identifier(self.run_id, field_name="run_id")
        require_identifier(self.catalog_generation_id, field_name="catalog_generation_id")
        require_sha256(self.catalog_sha256, field_name="catalog_sha256")
        for name in ("expected_items", "successful_items", "confirmed_empty_items", "failed_items"):
            _non_negative_int(getattr(self, name), field_name=name)
        if self.health is SourceHealth.HEALTHY and (self.artifact is None or self.artifact.record_count <= 0):
            raise DataContractError("健康来源 artifact 必须包含至少一条记录")

    @property
    def record_count(self) -> int:
        return self.artifact.record_count if self.artifact is not None else 0

    @property
    def publishable(self) -> bool:
        return (
            self.health is SourceHealth.HEALTHY
            and self.failed_items == 0
            and self.successful_items + self.confirmed_empty_items == self.expected_items
            and len(self.outcomes) == self.expected_items
            and self.record_count > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "run_id": self.run_id,
            "catalog_generation_id": self.catalog_generation_id,
            "catalog_sha256": self.catalog_sha256,
            "health": self.health.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "expected_items": self.expected_items,
            "successful_items": self.successful_items,
            "confirmed_empty_items": self.confirmed_empty_items,
            "failed_items": self.failed_items,
            "record_count": self.record_count,
            "artifact": self.artifact.to_dict() if self.artifact is not None else None,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "metadata": dict(self.metadata),
            "publishable": self.publishable,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SourceRunManifestV2":
        outcomes = payload.get("outcomes")
        metadata = payload.get("metadata")
        artifact = payload.get("artifact")
        if not isinstance(outcomes, list) or any(not isinstance(item, Mapping) for item in outcomes):
            raise DataContractError("source run manifest outcomes 必须是对象数组")
        try:
            return cls(
                schema_version=payload["schema_version"],
                source=str(payload["source"]),
                run_id=str(payload["run_id"]),
                catalog_generation_id=str(payload["catalog_generation_id"]),
                catalog_sha256=str(payload["catalog_sha256"]),
                health=SourceHealth(str(payload["health"])),
                started_at=str(payload["started_at"]),
                completed_at=str(payload["completed_at"]),
                expected_items=payload["expected_items"],
                successful_items=payload["successful_items"],
                confirmed_empty_items=payload["confirmed_empty_items"],
                failed_items=payload["failed_items"],
                artifact=(ArtifactDescriptor.from_mapping(artifact) if isinstance(artifact, Mapping) else None),
                outcomes=tuple(ItemOutcome.from_mapping(item) for item in outcomes),
                metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataContractError(f"source run manifest 无效：{exc}") from exc


@dataclass(frozen=True)
class SourcePointerV2:
    source: str
    run_id: str
    catalog_generation_id: str
    catalog_sha256: str
    manifest_sha256: str
    artifact: ArtifactDescriptor
    completed_at: str
    last_success_at: str
    schema_version: int = SOURCE_POINTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SOURCE_POINTER_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 source pointer schema：{self.schema_version}")
        if self.source not in {"hextech", "apex", "mayhem"}:
            raise DataContractError(f"未知来源：{self.source}")
        require_identifier(self.run_id, field_name="run_id")
        require_identifier(self.catalog_generation_id, field_name="catalog_generation_id")
        require_sha256(self.catalog_sha256, field_name="catalog_sha256")
        require_sha256(self.manifest_sha256, field_name="manifest_sha256")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SourcePointerV2":
        try:
            return cls(
                schema_version=payload["schema_version"],
                source=str(payload["source"]),
                run_id=str(payload["run_id"]),
                catalog_generation_id=str(payload["catalog_generation_id"]),
                catalog_sha256=str(payload["catalog_sha256"]),
                manifest_sha256=str(payload["manifest_sha256"]),
                artifact=ArtifactDescriptor.from_mapping(payload["artifact"]),
                completed_at=str(payload["completed_at"]),
                last_success_at=str(payload["last_success_at"]),
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"source pointer 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


@dataclass(frozen=True)
class SourceProvenance:
    source: SourceName
    run_id: str
    catalog_generation_id: str
    artifact_role: str
    artifact_sha256: str
    record_count: int
    manifest_sha256: str
    content_schema_version: int

    def __post_init__(self) -> None:
        if self.source not in {"catalog", "hextech", "apex", "mayhem"}:
            raise DataContractError(f"未知 provenance source：{self.source}")
        require_identifier(self.run_id, field_name="provenance.run_id")
        require_identifier(self.catalog_generation_id, field_name="provenance.catalog_generation_id")
        if not self.artifact_role:
            raise DataContractError("provenance.artifact_role 不能为空")
        require_sha256(self.artifact_sha256, field_name="provenance.artifact_sha256")
        require_sha256(self.manifest_sha256, field_name="provenance.manifest_sha256")
        _non_negative_int(self.record_count, field_name="provenance.record_count")
        if isinstance(self.content_schema_version, bool) or self.content_schema_version <= 0:
            raise DataContractError("provenance.content_schema_version 必须是正整数")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SourceProvenance":
        try:
            return cls(
                source=str(payload["source"]),  # type: ignore[arg-type]
                run_id=str(payload["run_id"]),
                catalog_generation_id=str(payload["catalog_generation_id"]),
                artifact_role=str(payload["artifact_role"]),
                artifact_sha256=str(payload["artifact_sha256"]),
                record_count=payload["record_count"],
                manifest_sha256=str(payload["manifest_sha256"]),
                content_schema_version=payload["content_schema_version"],
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"source provenance 无效：{exc}") from exc


@dataclass(frozen=True)
class CatalogManifestV2:
    catalog_generation_id: str
    created_at: str
    files: tuple[ArtifactDescriptor, ...]
    content_sha256: str
    schema_version: int = CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 catalog schema：{self.schema_version}")
        require_identifier(self.catalog_generation_id, field_name="catalog_generation_id")
        require_sha256(self.content_sha256, field_name="catalog.content_sha256")
        if len(self.files) != 3 or {item.role for item in self.files} != {"champions", "augments", "versions"}:
            raise DataContractError("Catalog 必须包含 champions、augments、versions 三个角色")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CatalogManifestV2":
        files = payload.get("files")
        if not isinstance(files, list) or any(not isinstance(item, Mapping) for item in files):
            raise DataContractError("catalog manifest files 必须是对象数组")
        try:
            return cls(
                schema_version=payload["schema_version"],
                catalog_generation_id=str(payload["catalog_generation_id"]),
                created_at=str(payload["created_at"]),
                files=tuple(ArtifactDescriptor.from_mapping(item) for item in files),
                content_sha256=str(payload["content_sha256"]),
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"catalog manifest 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotFileDescriptor:
    role: str
    relative_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.role:
            raise DataContractError("snapshot file role 不能为空")
        require_relative_path(self.relative_path, field_name="snapshot.relative_path")
        _non_negative_int(self.size, field_name="snapshot.size")
        require_sha256(self.sha256, field_name="snapshot.sha256")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SnapshotFileDescriptor":
        try:
            return cls(
                role=str(payload["role"]),
                relative_path=str(payload["relative_path"]),
                size=payload["size"],
                sha256=str(payload["sha256"]),
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"snapshot file descriptor 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineContributionV2:
    """把旧 generation 作为只读 last-good contribution，而不伪造 source artifact 路径。"""

    source: str
    origin_generation_id: str
    catalog_generation_id: str
    catalog_sha256: str
    created_at: str
    provenance: SourceProvenance
    snapshot_files: tuple[SnapshotFileDescriptor, ...]
    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    kind: str = "baseline_generation"

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION or self.kind != "baseline_generation":
            raise DataContractError("baseline contribution schema 无效")
        if self.source not in {"hextech", "apex", "mayhem"}:
            raise DataContractError(f"baseline contribution 来源无效：{self.source}")
        require_identifier(self.origin_generation_id, field_name="baseline.origin_generation_id")
        require_identifier(self.catalog_generation_id, field_name="baseline.catalog_generation_id")
        require_sha256(self.catalog_sha256, field_name="baseline.catalog_sha256")
        if self.provenance.source != self.source:
            raise DataContractError("baseline provenance 与来源不一致")
        if self.provenance.catalog_generation_id != self.catalog_generation_id:
            raise DataContractError("baseline provenance 与 Catalog 不一致")
        roles = {item.role for item in self.snapshot_files}
        if roles != {"champions", "champion_hextech", "overlay_hints", "identities"}:
            raise DataContractError("baseline contribution 必须绑定完整 generation 文件")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BaselineContributionV2":
        try:
            files = payload["snapshot_files"]
            provenance = payload["provenance"]
            if not isinstance(files, list) or not isinstance(provenance, Mapping):
                raise TypeError("snapshot_files/provenance 类型无效")
            return cls(
                schema_version=payload["schema_version"],
                kind=str(payload["kind"]),
                source=str(payload["source"]),
                origin_generation_id=str(payload["origin_generation_id"]),
                catalog_generation_id=str(payload["catalog_generation_id"]),
                catalog_sha256=str(payload["catalog_sha256"]),
                created_at=str(payload["created_at"]),
                provenance=SourceProvenance.from_mapping(provenance),
                snapshot_files=tuple(SnapshotFileDescriptor.from_mapping(item) for item in files),
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"baseline contribution 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "source": self.source,
            "origin_generation_id": self.origin_generation_id,
            "catalog_generation_id": self.catalog_generation_id,
            "catalog_sha256": self.catalog_sha256,
            "created_at": self.created_at,
            "provenance": asdict(self.provenance),
            "snapshot_files": [item.to_dict() for item in self.snapshot_files],
        }


@dataclass(frozen=True)
class SourceStatusV2:
    """generation 内逐来源状态；旧 generation 缺失字段时显式归一为 unknown/空值。"""

    catalog_id: str = ""
    data_at: str = ""
    checked_at: str = ""
    freshness: str = "unknown"
    run_id: str = ""
    origin_generation_id: str = ""
    artifact_sha256: str = ""
    manifest_sha256: str = ""
    record_count: int = 0
    # `freshness` 说明 generation 是否复用 last-good；`data_status` 补充本轮
    # 候选是否因覆盖/上游变化而陈旧，避免 UI 把旧统计静默当成新数据。
    data_status: str = "unknown"
    data_reason: str = ""
    # 数据绝对时效：data_at 距发布时刻超过来源刷新周期阈值时写入超龄秒数，
    # 否则为 0。可选字段：旧构建 from_mapping 忽略未知键，回滚安全。
    stale_age_seconds: int = 0
    coverage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.freshness not in {"fresh", "last_good", "unknown"}:
            raise DataContractError(f"source_status freshness 无效：{self.freshness}")
        if self.data_status not in {"fresh", "data_stale", "unknown"}:
            raise DataContractError(f"source_status data_status 无效：{self.data_status}")
        _non_negative_int(self.record_count, field_name="source_status.record_count")
        _non_negative_int(self.stale_age_seconds, field_name="source_status.stale_age_seconds")
        for field_name in ("artifact_sha256", "manifest_sha256"):
            value = getattr(self, field_name)
            if value:
                require_sha256(value, field_name=f"source_status.{field_name}")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SourceStatusV2":
        try:
            return cls(
                catalog_id=str(payload.get("catalog_id") or ""),
                data_at=str(payload.get("data_at") or ""),
                checked_at=str(payload.get("checked_at") or ""),
                freshness=str(payload.get("freshness") or "unknown"),
                run_id=str(payload.get("run_id") or ""),
                origin_generation_id=str(payload.get("origin_generation_id") or ""),
                artifact_sha256=str(payload.get("artifact_sha256") or ""),
                manifest_sha256=str(payload.get("manifest_sha256") or ""),
                record_count=payload.get("record_count", 0),
                data_status=str(payload.get("data_status") or "unknown"),
                data_reason=str(payload.get("data_reason") or ""),
                stale_age_seconds=payload.get("stale_age_seconds", 0),
                coverage=dict(payload.get("coverage") or {}) if isinstance(payload.get("coverage"), Mapping) else {},
            )
        except TypeError as exc:
            raise DataContractError(f"source_status 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataSnapshotCurrentPointerV2:
    current_generation_id: str
    schema_version: int = SNAPSHOT_POINTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_POINTER_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 snapshot current schema：{self.schema_version}")
        require_identifier(self.current_generation_id, field_name="current_generation_id")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DataSnapshotCurrentPointerV2":
        try:
            return cls(
                schema_version=payload["schema_version"],
                current_generation_id=str(payload["current_generation_id"]),
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"snapshot current pointer 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataSnapshotPreviousPointerV2:
    generation_id: str
    schema_version: int = SNAPSHOT_POINTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_POINTER_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 snapshot previous schema：{self.schema_version}")
        require_identifier(self.generation_id, field_name="generation_id")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DataSnapshotPreviousPointerV2":
        try:
            return cls(schema_version=payload["schema_version"], generation_id=str(payload["generation_id"]))
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"snapshot previous pointer 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataSnapshotManifestV2:
    generation_id: str
    created_at: str
    content_fingerprint: str
    source_files: tuple[SourceProvenance, ...]
    champion_count: int
    augment_count: int
    stat_record_count: int
    files: tuple[SnapshotFileDescriptor, ...]
    health: str = "healthy"
    refreshed_sources: tuple[str, ...] = ()
    degraded_sources: tuple[str, ...] = ()
    source_status: Mapping[str, SourceStatusV2] = field(default_factory=dict)
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 generation schema：{self.schema_version}")
        require_identifier(self.generation_id, field_name="generation_id")
        require_sha256(self.content_fingerprint, field_name="content_fingerprint")
        for name in ("champion_count", "augment_count", "stat_record_count"):
            _non_negative_int(getattr(self, name), field_name=name)
        if self.health not in {"healthy", "degraded"}:
            raise DataContractError("generation health 必须是 healthy 或 degraded")
        valid_sources = {"catalog", "hextech", "apex", "mayhem"}
        if not set(self.refreshed_sources).issubset(valid_sources):
            raise DataContractError("generation refreshed_sources 包含未知来源")
        if not set(self.degraded_sources).issubset(valid_sources):
            raise DataContractError("generation degraded_sources 包含未知来源")
        if self.health == "healthy" and self.degraded_sources:
            raise DataContractError("healthy generation 不能声明 degraded_sources")
        if not set(self.source_status).issubset(valid_sources):
            raise DataContractError("generation source_status 包含未知来源")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DataSnapshotManifestV2":
        try:
            return cls(
                schema_version=payload["schema_version"],
                generation_id=str(payload["generation_id"]),
                created_at=str(payload["created_at"]),
                content_fingerprint=str(payload["content_fingerprint"]),
                source_files=tuple(SourceProvenance.from_mapping(item) for item in payload["source_files"]),
                champion_count=payload["champion_count"],
                augment_count=payload["augment_count"],
                stat_record_count=payload["stat_record_count"],
                files=tuple(SnapshotFileDescriptor.from_mapping(item) for item in payload["files"]),
                health=str(payload.get("health") or "healthy"),
                refreshed_sources=tuple(str(item) for item in payload.get("refreshed_sources", ())),
                degraded_sources=tuple(str(item) for item in payload.get("degraded_sources", ())),
                source_status={
                    str(source): SourceStatusV2.from_mapping(status)
                    for source, status in payload.get("source_status", {}).items()
                    if isinstance(status, Mapping)
                },
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"generation manifest 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PromotionJournalPhase(str, Enum):
    PREPARED = "prepared"
    DEPENDENCIES_PROMOTED = "dependencies_promoted"
    GENERATION_PROMOTED = "generation_promoted"
    COMMITTED = "committed"


@dataclass(frozen=True)
class PromotionJournalV1:
    transaction_id: str
    phase: PromotionJournalPhase
    created_at: str
    old_pointers: Mapping[str, Mapping[str, Any]]
    target_pointers: Mapping[str, Mapping[str, Any]]
    schema_version: int = PROMOTION_JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROMOTION_JOURNAL_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 promotion journal schema：{self.schema_version}")
        require_identifier(self.transaction_id, field_name="transaction_id")
        required = {"catalog", "hextech", "apex", "mayhem", "generation"}
        if set(self.old_pointers) != required or set(self.target_pointers) != required:
            raise DataContractError("promotion journal 指针角色不完整")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "phase": self.phase.value,
            "created_at": self.created_at,
            "old_pointers": {key: dict(value) for key, value in self.old_pointers.items()},
            "target_pointers": {key: dict(value) for key, value in self.target_pointers.items()},
        }


@dataclass(frozen=True)
class RefreshSourceState:
    last_attempt_at: str = ""
    last_success_at: str = ""
    next_due_at: str = ""
    failure_kind: str = ""
    current_run_id: str = ""
    state: str = "due"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RefreshSourceState":
        return cls(
            last_attempt_at=str(payload.get("last_attempt_at") or ""),
            last_success_at=str(payload.get("last_success_at") or ""),
            next_due_at=str(payload.get("next_due_at") or ""),
            failure_kind=str(payload.get("failure_kind") or ""),
            current_run_id=str(payload.get("current_run_id") or ""),
            state=str(payload.get("state") or "due"),
        )


@dataclass(frozen=True)
class RefreshScheduleV1:
    updated_at: str
    sources: Mapping[str, RefreshSourceState]
    generation_id: str = ""
    schema_version: int = REFRESH_SCHEDULE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REFRESH_SCHEDULE_SCHEMA_VERSION:
            raise DataContractError(f"不支持的 refresh schedule schema：{self.schema_version}")
        required = {"catalog", "hextech", "apex", "mayhem"}
        if set(self.sources) != required:
            raise DataContractError("refresh schedule 来源角色不完整")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RefreshScheduleV1":
        try:
            sources = payload["sources"]
            if not isinstance(sources, Mapping):
                raise TypeError("sources 必须是对象")
            return cls(
                schema_version=payload["schema_version"],
                updated_at=str(payload["updated_at"]),
                generation_id=str(payload.get("generation_id") or ""),
                sources={str(key): RefreshSourceState.from_mapping(value) for key, value in sources.items()},
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"refresh schedule 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "generation_id": self.generation_id,
            "sources": {key: asdict(value) for key, value in self.sources.items()},
        }


def parse_provenance(items: Sequence[Mapping[str, Any]]) -> tuple[SourceProvenance, ...]:
    return tuple(SourceProvenance.from_mapping(item) for item in items)


__all__ = [
    "ArtifactDescriptor",
    "BaselineContributionV2",
    "CATALOG_SCHEMA_VERSION",
    "CONTENT_FINGERPRINT_SCHEMA_VERSION",
    "CatalogManifestV2",
    "DataSnapshotCurrentPointerV2",
    "DataContractError",
    "DataSnapshotManifestV2",
    "DataSnapshotPreviousPointerV2",
    "FetchAttempt",
    "ItemOutcome",
    "ItemState",
    "PromotionJournalPhase",
    "PromotionJournalV1",
    "RefreshScheduleV1",
    "RefreshSourceState",
    "SNAPSHOT_POINTER_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "SOURCE_POINTER_SCHEMA_VERSION",
    "SOURCE_RUN_SCHEMA_VERSION",
    "SnapshotFileDescriptor",
    "SourcePointerV2",
    "SourceProvenance",
    "SourceRunManifestV2",
    "SourceStatusV2",
    "parse_provenance",
    "require_identifier",
    "require_relative_path",
    "require_sha256",
    "utc_now_iso",
]

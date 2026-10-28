"""Only the v3 manifest DTO needed by the explicit offline converter."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from hextech.contracts import SourceProvenance, SourceStatusV2
from hextech.contracts.data_validation import DataContractError, require_identifier, require_sha256, require_relative_path, _non_negative_int
SNAPSHOT_SCHEMA_VERSION = 3

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
class LegacySnapshotManifestV3:
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
    components: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 3:
            raise DataContractError(f"不支持的 generation schema：{self.schema_version}")
        self.validate_components(self.components, schema_version=self.schema_version)
        require_identifier(self.generation_id, field_name="generation_id")
        require_sha256(self.content_fingerprint, field_name="content_fingerprint")
        for name in ("champion_count", "augment_count", "stat_record_count"):
            _non_negative_int(getattr(self, name), field_name=name)
        if self.health not in {"healthy", "degraded"}:
            raise DataContractError("generation health 必须是 healthy 或 degraded")
        valid_sources = {"catalog", "hextech", "aramkit", "blitz", "apex", "mayhem"}
        if not set(self.refreshed_sources).issubset(valid_sources):
            raise DataContractError("generation refreshed_sources 包含未知来源")
        if not set(self.degraded_sources).issubset(valid_sources):
            raise DataContractError("generation degraded_sources 包含未知来源")
        if self.health == "healthy" and self.degraded_sources:
            raise DataContractError("healthy generation 不能声明 degraded_sources")
        if not set(self.source_status).issubset(valid_sources):
            raise DataContractError("generation source_status 包含未知来源")

    @staticmethod
    def validate_components(components: object, *, schema_version: int = 3) -> None:
        if not isinstance(components, Mapping):
            raise DataContractError("generation components 必须是对象")
        if schema_version == 2:
            if components:
                raise DataContractError("v2 generation 不支持 components")
            return
        if set(components) != {"ranking", "champions"}:
            raise DataContractError("generation components 必须且只能包含 ranking/champions")
        ranking = components["ranking"]
        champions = components["champions"]
        if (not isinstance(ranking, Mapping) or not {"source_version", "catalog_id"}.issubset(ranking)
                or not set(ranking).issubset({"source_version", "catalog_id", "run_id"})):
            raise DataContractError("ranking component 结构无效")
        if not isinstance(champions, Mapping) or not champions:
            raise DataContractError("champions components 必须是非空对象")
        for hero_id, component in champions.items():
            if not isinstance(hero_id, str) or not hero_id.isdecimal() or int(hero_id) <= 0 or str(int(hero_id)) != hero_id:
                raise DataContractError("champion component hero_id 必须是规范正整数键")
            if (not isinstance(component, Mapping) or not {"source_version", "catalog_id", "complete"}.issubset(component)
                    or not set(component).issubset({"source_version", "catalog_id", "complete", "run_id"})):
                raise DataContractError("champion component 结构无效")
            if type(component["complete"]) is not bool:
                raise DataContractError("champion component.complete 必须是布尔值")
        for component in (ranking, *champions.values()):
            for key in ("source_version", "catalog_id", *( ("run_id",) if "run_id" in component else () )):
                if not isinstance(component[key], str):
                    raise DataContractError(f"component.{key} 必须是字符串")
                if key == "source_version":
                    value = component[key]
                    if not value.strip() or len(value) > 256 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
                        raise DataContractError("component.source_version 必须是非空有界版本文本且无控制字符")
                else:
                    require_identifier(component[key], field_name=f"component.{key}")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LegacySnapshotManifestV3":
        try:
            statuses = payload.get("source_status", {})
            if not isinstance(statuses, Mapping) or any(not isinstance(value, Mapping) for value in statuses.values()):
                raise DataContractError("generation source_status 必须是对象映射")
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
                components=payload.get("components", {}),
                health=str(payload.get("health") or "healthy"),
                refreshed_sources=tuple(str(item) for item in payload.get("refreshed_sources", ())),
                degraded_sources=tuple(str(item) for item in payload.get("degraded_sources", ())),
                source_status={
                    str(source): SourceStatusV2.from_mapping(status)
                    for source, status in statuses.items()
                },
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"generation manifest 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

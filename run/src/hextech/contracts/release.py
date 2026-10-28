"""Current production release: immutable references, never copied query payloads."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .data_pipeline import SourceProvenance, SourceStatusV2
from .data_validation import DataContractError, require_identifier, require_sha256


@dataclass(frozen=True)
class ReleaseManifest:
    generation_id: str
    created_at: str
    content_fingerprint: str
    source_files: tuple[SourceProvenance, ...]
    components: Mapping[str, Any]
    source_status: Mapping[str, SourceStatusV2] = field(default_factory=dict)
    health: str = "healthy"
    degraded_sources: tuple[str, ...] = ()
    schema_version: int = 4

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 4:
            raise DataContractError("production requires release schema 4; migrate v3 offline")
        require_identifier(self.generation_id, field_name="release.generation_id")
        require_sha256(self.content_fingerprint, field_name="release.content_fingerprint")
        from datetime import datetime
        try:
            stamp = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
        except (ValueError, AttributeError) as exc:
            raise DataContractError("release created_at invalid") from exc
        if self.health not in {"healthy", "degraded"}:
            raise DataContractError("release health invalid")
        allowed = {"catalog", "aramkit", "apex", "mayhem"}
        if not set(self.degraded_sources).issubset(allowed) or not set(self.source_status).issubset(allowed):
            raise DataContractError("release contains retired status source")
        if self.health == "healthy" and self.degraded_sources:
            raise DataContractError("healthy release cannot contain degraded sources")
        if not self.source_files or any(p.source not in {"catalog", "aramkit", "apex", "mayhem"} for p in self.source_files):
            raise DataContractError("release contains absent or retired source")
        if len({(p.source, p.run_id, p.artifact_role) for p in self.source_files}) != len(self.source_files):
            raise DataContractError("release duplicate unit")
        validate_components(self.components)

    @property
    def files(self):
        return ()

    @property
    def refreshed_sources(self):
        return tuple(sorted({p.source for p in self.source_files}))

    @property
    def champion_count(self):
        return len(self.components["champions"])

    @property
    def augment_count(self):
        return next((p.record_count for p in self.source_files if p.artifact_role == "augment_identities"), 0)

    @property
    def stat_record_count(self):
        return sum(p.record_count for p in self.source_files if p.artifact_role == "scoped_stats")

    def to_dict(self):
        return {"schema_version": 4, "generation_id": self.generation_id, "created_at": self.created_at,
                "content_fingerprint": self.content_fingerprint, "source_files": [p.to_dict() for p in self.source_files],
                "components": dict(self.components), "source_status": {k: v.to_dict() for k, v in self.source_status.items()},
                "health": self.health, "degraded_sources": list(self.degraded_sources)}

    @classmethod
    def from_mapping(cls, payload):
        allowed = {"schema_version", "generation_id", "created_at", "content_fingerprint", "source_files",
                   "components", "source_status", "health", "degraded_sources"}
        if not isinstance(payload, Mapping) or set(payload) != allowed:
            raise DataContractError("release manifest fields invalid")
        if (not isinstance(payload["source_files"], list) or any(not isinstance(p, Mapping) for p in payload["source_files"])
                or not isinstance(payload["components"], Mapping) or not isinstance(payload["source_status"], Mapping)
                or any(not isinstance(p, Mapping) for p in payload["source_status"].values())
                or not isinstance(payload["degraded_sources"], list)):
            raise DataContractError("release manifest field types invalid")
        return cls(schema_version=payload["schema_version"], generation_id=str(payload["generation_id"]),
                   created_at=str(payload["created_at"]), content_fingerprint=str(payload["content_fingerprint"]),
                   source_files=tuple(SourceProvenance.from_mapping(p) for p in payload["source_files"]),
                   components=dict(payload["components"]),
                   source_status={k: SourceStatusV2.from_mapping(v) for k, v in payload["source_status"].items()},
                   health=str(payload["health"]), degraded_sources=tuple(payload["degraded_sources"]))


def validate_components(components: object) -> None:
    if not isinstance(components, Mapping):
        raise DataContractError("generation components 必须是对象")
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

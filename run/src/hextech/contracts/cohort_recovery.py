"""完整数据 cohort 的单调恢复点契约。

本模块只描述 pointer 与 schedule 快照的版本化结构；不验证磁盘上的 immutable
generation，也不负责选择或晋升 cohort。真实完整性校验由 persistence 层执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .data_pipeline import DataContractError, require_identifier


COHORT_RECOVERY_POINT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CohortRecoveryPointV1:
    """最近一份完整可验证 cohort 的单调恢复点。"""

    generation_id: str
    generation_created_at: str
    recorded_at: str
    manifest_health: str
    pointers: Mapping[str, Mapping[str, Any]]
    schedule: Mapping[str, Any]
    schema_version: int = COHORT_RECOVERY_POINT_SCHEMA_VERSION
    snapshot_schema_version: int = 2
    units: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise DataContractError(f"不支持的 cohort recovery point schema：{self.schema_version}")
        require_identifier(self.generation_id, field_name="recovery_point.generation_id")
        if not self.generation_created_at or not self.recorded_at:
            raise DataContractError("recovery point 时间字段不能为空")
        if self.manifest_health not in {"healthy", "degraded"}:
            raise DataContractError("recovery point manifest_health 无效")
        if not isinstance(self.units, Mapping):
            raise DataContractError("recovery point units 必须是对象")
        expected_roles = {"catalog", "aramkit", "blitz", "apex", "mayhem", "generation"}
        if self.schema_version == 2:
            if self.snapshot_schema_version != 3 or not isinstance(self.units, Mapping) or not self.units:
                raise DataContractError("v2 recovery point 必须绑定 snapshot v3 units")
            if not {"catalog", "aramkit", "generation"}.issubset(self.pointers) or not set(self.pointers).issubset(expected_roles):
                raise DataContractError("v3 recovery point 指针角色无效")
            for key, pointer in self.units.items():
                if not isinstance(pointer, Mapping) or key != f"{pointer.get('source')}/{pointer.get('run_id')}":
                    raise DataContractError("recovery point unit 身份无效")
        elif self.snapshot_schema_version != 2 or self.units or set(self.pointers) != expected_roles:
            raise DataContractError("recovery point 指针角色不完整")
        generation = self.pointers.get("generation")
        if not isinstance(generation, Mapping):
            raise DataContractError("recovery point generation 指针无效")
        current = generation.get("current")
        if not isinstance(current, Mapping) or str(current.get("current_generation_id") or "") != self.generation_id:
            raise DataContractError("recovery point current 与 generation_id 不一致")
        if not isinstance(generation.get("previous"), Mapping):
            raise DataContractError("recovery point previous 指针无效")
        if not isinstance(self.schedule, Mapping):
            raise DataContractError("recovery point schedule 必须是对象")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CohortRecoveryPointV1":
        pointers = payload.get("pointers")
        schedule = payload.get("schedule")
        if not isinstance(pointers, Mapping) or not isinstance(schedule, Mapping):
            raise DataContractError("recovery point pointers/schedule 必须是对象")
        try:
            return cls(
                schema_version=payload["schema_version"],
                snapshot_schema_version=payload.get("snapshot_schema_version", 2),
                units=payload.get("units", {}),
                generation_id=str(payload["generation_id"]),
                generation_created_at=str(payload["generation_created_at"]),
                recorded_at=str(payload["recorded_at"]),
                manifest_health=str(payload["manifest_health"]),
                pointers={
                    str(role): dict(pointer)
                    for role, pointer in pointers.items()
                    if isinstance(pointer, Mapping)
                },
                schedule=dict(schedule),
            )
        except (KeyError, TypeError) as exc:
            raise DataContractError(f"cohort recovery point 无效：{exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_schema_version": self.snapshot_schema_version,
            "units": {key: dict(value) for key, value in self.units.items()},
            "generation_id": self.generation_id,
            "generation_created_at": self.generation_created_at,
            "recorded_at": self.recorded_at,
            "manifest_health": self.manifest_health,
            "pointers": {role: dict(pointer) for role, pointer in self.pointers.items()},
            "schedule": dict(self.schedule),
        }


__all__ = ["COHORT_RECOVERY_POINT_SCHEMA_VERSION", "CohortRecoveryPointV1"]

"""generation 内逐来源状态的稳定 DTO；不持有 snapshot 生命周期。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .data_validation import DataContractError, _non_negative_int, require_sha256


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
    # freshness=是否复用 last-good；data_status=数据陈旧；stale_age_seconds=过期超龄秒数（可选字段，旧构建忽略未知键，回滚安全）。
    data_status: str = "unknown"
    data_reason: str = ""
    stale_age_seconds: int = 0
    coverage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.freshness not in {"fresh", "last_good", "unknown"}:
            raise DataContractError(f"source_status freshness 无效：{self.freshness}")
        if self.data_status not in {"fresh", "data_stale", "unknown", "pending", "confirmed_empty", "unavailable"}:
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

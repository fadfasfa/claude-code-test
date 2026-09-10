"""DataService 弱网刷新 checkpoint 的原子持久化。

这里只保存和读取 v1 JSON；Catalog、pointer、manifest 与 artifact 的真实性由
refresh coordinator 在恢复前重新验证。本模块不晋升 pointer、不删除运行数据。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir


REFRESH_CHECKPOINT_SCHEMA_VERSION = 1
_FAILURE_TEXT_FIELDS = ("reason_code", "failure_stage", "error_type")
_FAILURE_BOOL_FIELDS = ("fallback_used", "last_good_available")
_DIAGNOSTIC_MAX_DEPTH = 3
_DIAGNOSTIC_MAX_ITEMS = 20
_DIAGNOSTIC_STRING_LIMIT = 256
_OMIT_DIAGNOSTIC = object()


def _sensitive_diagnostic_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in {"env", "environment", "headers"}:
        return True
    return any(
        token in normalized
        for token in (
            "authorization",
            "command",
            "cookie",
            "credential",
            "password",
            "proxy",
            "secret",
            "stack",
            "token",
            "traceback",
        )
    )


def _bounded_diagnostic(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_DIAGNOSTIC_STRING_LIMIT]
    if depth >= _DIAGNOSTIC_MAX_DEPTH:
        return _OMIT_DIAGNOSTIC
    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        for raw_key, item in value.items():
            if len(bounded) >= _DIAGNOSTIC_MAX_ITEMS:
                break
            if not isinstance(raw_key, str):
                continue
            key = raw_key[:64]
            if _sensitive_diagnostic_key(key):
                continue
            normalized = _bounded_diagnostic(item, depth=depth + 1)
            if normalized is not _OMIT_DIAGNOSTIC:
                bounded[key] = normalized
        return bounded
    if isinstance(value, (list, tuple)):
        bounded_items: list[Any] = []
        for item in value[:_DIAGNOSTIC_MAX_ITEMS]:
            normalized = _bounded_diagnostic(item, depth=depth + 1)
            if normalized is not _OMIT_DIAGNOSTIC:
                bounded_items.append(normalized)
        return bounded_items
    return _OMIT_DIAGNOSTIC


def checkpoint_failure_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """压缩 worker 失败为可恢复、有限且不含敏感运行上下文的证据。"""

    evidence: dict[str, Any] = {}
    for field in _FAILURE_TEXT_FIELDS:
        if field not in payload:
            continue
        value = str(payload.get(field) or "").strip()
        if value:
            evidence[field] = value[:128]
    for field in _FAILURE_BOOL_FIELDS:
        if field in payload:
            evidence[field] = bool(payload.get(field))
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        bounded = _bounded_diagnostic(diagnostics)
        if isinstance(bounded, dict) and bounded:
            evidence["diagnostics"] = bounded
    return evidence


class RefreshCheckpointStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else get_var_dir()
        self.path = self.root / "state" / "data-service" / "refresh_checkpoint.v1.json"

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema_version") != REFRESH_CHECKPOINT_SCHEMA_VERSION:
            return {}
        return dict(payload)

    def save(self, payload: Mapping[str, Any]) -> None:
        normalized = {"schema_version": REFRESH_CHECKPOINT_SCHEMA_VERSION, **dict(payload)}
        atomic_write_json(self.path, normalized, ensure_ascii=False, indent=2)


class CatalogAdoptionCheckpointStore(RefreshCheckpointStore):
    """待采用新 Catalog 的独立 checkpoint；不占用活动 Catalog 刷新通道。"""

    def __init__(self, root: str | Path | None = None) -> None:
        super().__init__(root)
        self.path = self.root / "state" / "data-service" / "catalog_adoption_checkpoint.v1.json"


__all__ = [
    "CatalogAdoptionCheckpointStore",
    "REFRESH_CHECKPOINT_SCHEMA_VERSION",
    "RefreshCheckpointStore",
    "checkpoint_failure_evidence",
]

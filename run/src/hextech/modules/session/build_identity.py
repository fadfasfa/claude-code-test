"""读取当前 Hextech 构建身份。

冻结态以 bundle manifest v3 为唯一事实源；源码态返回显式 ``dev`` 身份，避免
运行组件各自推测版本。该模块只读包内文件，不访问 Git、网络或可写运行态。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from hextech.modules.data.ports.paths import BUNDLE_ROOT_DIR


BUNDLE_MANIFEST_SCHEMA_VERSION = 3
OVERLAY_EVENT_SCHEMA_VERSION = 3
SIDECAR_STATUS_SCHEMA_VERSION = 2
OVERLAY_SESSION_REPORT_SCHEMA_VERSION = 2
RUNTIME_CONTRACT_VERSIONS = {
    "overlay_event": OVERLAY_EVENT_SCHEMA_VERSION,
    "sidecar_status": SIDECAR_STATUS_SCHEMA_VERSION,
    "overlay_session_report": OVERLAY_SESSION_REPORT_SCHEMA_VERSION,
}


@lru_cache(maxsize=1)
def get_build_identity() -> dict[str, Any]:
    """返回可安全展示的构建身份；旧包或源码态明确降级为 dev。"""

    manifest_path = Path(BUNDLE_ROOT_DIR) / "bundle_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    contracts = payload.get("runtime_contracts") if isinstance(payload, dict) else {}
    if (
        isinstance(payload, dict)
        and int(payload.get("schema_version") or 0) == BUNDLE_MANIFEST_SCHEMA_VERSION
        and str(payload.get("build_id") or "").strip()
        and isinstance(contracts, dict)
        and contracts == RUNTIME_CONTRACT_VERSIONS
    ):
        return {
            "build_id": str(payload["build_id"]),
            "built_at": str(payload.get("built_at") or payload.get("generated_at") or ""),
            "source_revision": str(payload.get("source_revision") or "unknown"),
            "source_fingerprint": str(payload.get("source_fingerprint") or ""),
            "runtime_contracts": {str(key): int(value) for key, value in contracts.items()},
            "packaged": True,
        }
    return {
        "build_id": "dev",
        "built_at": "",
        "source_revision": "working-tree",
        "source_fingerprint": "",
        "runtime_contracts": dict(RUNTIME_CONTRACT_VERSIONS),
        "packaged": False,
    }


def current_build_id() -> str:
    return str(get_build_identity()["build_id"])


__all__ = [
    "BUNDLE_MANIFEST_SCHEMA_VERSION",
    "OVERLAY_EVENT_SCHEMA_VERSION",
    "OVERLAY_SESSION_REPORT_SCHEMA_VERSION",
    "RUNTIME_CONTRACT_VERSIONS",
    "SIDECAR_STATUS_SCHEMA_VERSION",
    "current_build_id",
    "get_build_identity",
]

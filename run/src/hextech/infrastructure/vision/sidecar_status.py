"""Sidecar status v2 的持久化与动态生产池诊断字段。

该模块只负责状态文件；不拥有识别循环、模板构建或 bootstrap 协议。
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psutil

from hextech.modules.data.catalog.runtime_store import build_runtime_state_path
from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.session.build_identity import current_build_id


STATUS_FILE = Path(build_runtime_state_path("game_overlay_sidecar_status.json"))
GENERATION_ENV = "HEXTECH_OVERLAY_GENERATION"
PID_STARTED_AT = time.time()
SIDECAR_INSTANCE_ID = f"sidecar-{uuid.uuid4().hex}"
PROCESS_EXECUTABLE = os.path.normcase(os.fspath(Path(sys.executable).resolve()))
try:
    _PROCESS = psutil.Process(os.getpid())
    PID_STARTED_AT = float(_PROCESS.create_time())
    PROCESS_EXECUTABLE = os.path.normcase(os.fspath(Path(_PROCESS.exe()).resolve()))
except (psutil.Error, OSError):
    pass

debug_dump_enabled = False
runtime_fields: dict[str, Any] = {}


def publish_runtime_fields(stats: Mapping[str, Any]) -> None:
    keys = ("vision_pool_generation_id", "vision_pool_origin_generation_id",
            "vision_pool_fingerprint", "observed_data_generation_id",
            "stats_generation_id", "data_generation_id",
            "generation_roles", "catalog_generation_id", "production_pool_id",
            "production_pool_state", "production_pool_count", "full_catalog_count",
            "rank_identity_count", "matrix_rows", "excluded_reason_counts")
    runtime_fields.clear()
    runtime_fields.update({key: stats.get(key) for key in keys if key in stats})


def write_status(status: str, **fields: Any) -> None:
    generation = str(fields.get("generation") or os.environ.get(GENERATION_ENV) or "")
    payload = {**runtime_fields, **fields}
    vision_pool_generation_id = str(
        payload.get("vision_pool_generation_id") or payload.get("data_generation_id") or ""
    )
    payload["vision_pool_generation_id"] = vision_pool_generation_id
    payload.setdefault("vision_pool_origin_generation_id", vision_pool_generation_id)
    payload.setdefault("vision_pool_fingerprint", "")
    payload.setdefault("observed_data_generation_id", vision_pool_generation_id)
    payload.setdefault("stats_generation_id", "")
    payload.setdefault("data_generation_id", vision_pool_generation_id)
    payload.setdefault(
        "generation_roles",
        {
            "vision_pool_generation_id": "sidecar_template_runtime",
            "stats_generation_id": "host_game_session",
            "data_generation_id": "legacy_vision_pool_compat",
        },
    )
    payload.setdefault("debug_dump_enabled", debug_dump_enabled)
    payload.update({
        "schema_version": 2,
        "build_id": current_build_id(),
        "status": status,
        "pid": os.getpid(),
        "pid_started_at": PID_STARTED_AT,
        "sidecar_instance_id": SIDECAR_INSTANCE_ID,
        "executable": PROCESS_EXECUTABLE,
        "heartbeat_at": time.time(),
        "generation": generation,
        "updated_at": time.time(),
    })
    atomic_write_json(STATUS_FILE, payload, ensure_ascii=False, indent=2)


__all__ = [
    "GENERATION_ENV",
    "PID_STARTED_AT",
    "SIDECAR_INSTANCE_ID",
    "STATUS_FILE",
    "publish_runtime_fields",
    "write_status",
]

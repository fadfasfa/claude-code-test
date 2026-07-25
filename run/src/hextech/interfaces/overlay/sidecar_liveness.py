"""Vision Sidecar 存活判定。

本模块只读取小型状态文件并核对进程身份，不负责启动、停止或恢复进程；
`OverlayRuntimeManager` 负责把判定结果转换为公开生命周期状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping


SIDECAR_HEARTBEAT_STALE_SECONDS = 10.0


def read_sidecar_liveness(
    status_file: Path,
    *,
    pid: int | None,
    process_running: bool,
    sidecar_started_at: float,
    now: float,
    pid_exists: Callable[[int], bool],
    process_create_time: Callable[[int], float | None],
) -> dict[str, Any]:
    """同时验证进程、PID 创建时间和 heartbeat，避免假 running。"""

    startup_grace_active = bool(
        sidecar_started_at and now - sidecar_started_at <= SIDECAR_HEARTBEAT_STALE_SECONDS
    )
    if not process_running or not isinstance(pid, int) or pid <= 0:
        return {"status": "failed", "reason": "process_exited", "pid": pid}
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if startup_grace_active:
            return {"status": "starting", "reason": "heartbeat_pending", "pid": pid}
        return {"status": "stale", "reason": "status_missing", "pid": pid}
    if not isinstance(payload, Mapping):
        if startup_grace_active:
            return {"status": "starting", "reason": "heartbeat_pending", "pid": pid}
        return {"status": "stale", "reason": "status_invalid", "pid": pid}
    try:
        schema_version = int(payload.get("schema_version") or 1)
        status_pid = int(payload.get("pid") or 0)
        heartbeat_at = float(payload.get("heartbeat_at") or payload.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        if startup_grace_active:
            return {"status": "starting", "reason": "heartbeat_pending", "pid": pid}
        return {"status": "stale", "reason": "status_invalid", "pid": pid}
    if status_pid != pid:
        # 新进程接管时可能仍读到上一进程的状态文件；首个 heartbeat 前保持 pending。
        if startup_grace_active:
            return {
                "status": "starting",
                "reason": "heartbeat_pending",
                "pid": pid,
                "reported_pid": status_pid,
            }
        return {"status": "stale", "reason": "pid_mismatch", "pid": pid, "reported_pid": status_pid}
    try:
        exists = bool(pid_exists(pid))
    except Exception:
        exists = False
    if not exists:
        return {"status": "stale", "reason": "pid_missing", "pid": pid}
    if schema_version >= 2:
        try:
            reported_started_at = float(payload.get("pid_started_at") or 0.0)
        except (TypeError, ValueError):
            reported_started_at = 0.0
        actual_started_at = process_create_time(pid)
        if reported_started_at <= 0.0 or actual_started_at is None:
            return {"status": "stale", "reason": "pid_start_unavailable", "pid": pid}
        if abs(float(actual_started_at) - reported_started_at) > 2.0:
            return {
                "status": "stale",
                "reason": "pid_reused",
                "pid": pid,
                "pid_started_at": reported_started_at,
                "actual_pid_started_at": actual_started_at,
            }
    if heartbeat_at <= 0.0 or now - heartbeat_at > SIDECAR_HEARTBEAT_STALE_SECONDS:
        return {"status": "stale", "reason": "heartbeat_stale", "pid": pid, "heartbeat_at": heartbeat_at}
    return {
        "status": "running",
        "reason": "",
        "pid": pid,
        "heartbeat_at": heartbeat_at,
        "generation": str(payload.get("generation") or ""),
        "schema_version": schema_version,
        "build_id": str(payload.get("build_id") or ""),
    }

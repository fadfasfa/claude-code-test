"""Desktop/Sidecar的固定诊断请求和状态合同；不执行捕获或识别。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4


def selection_cache_root(var_dir: Path) -> Path:
    return Path(var_dir) / "recognition" / "selection-cache-v2"


def _reject_reparse_path(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            attributes = component.lstat()
        except FileNotFoundError:
            continue
        if component.is_symlink() or getattr(attributes, "st_file_attributes", 0) & 0x400:
            raise ValueError("diagnostic_reparse_path_rejected")


def read_selection_diagnostics(var_dir: Path) -> dict:
    """Small, fixed status-file read for Desktop; no screen or sample decoding on the UI thread."""
    path = Path(var_dir) / "state" / "game_overlay_sidecar_status.json"
    try:
        _reject_reparse_path(path)
        if path.stat().st_size > 1024*1024:
            raise ValueError("sidecar_status_oversize")
        status = json.loads(path.read_text(encoding="utf-8"))
        age = time.time()-float(status.get("heartbeat_at") or 0)
        live = 0 <= age <= 10 and status.get("status") == "running"
        return {"live": live, "status": status, "reason": "" if live else "sidecar_not_running_or_stale"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"live": False, "status": {}, "reason": "sidecar_status_unavailable"}


def request_recent_selection(var_dir: Path) -> dict:
    from hextech.modules.data.ports.atomic import atomic_write_json
    result = read_selection_diagnostics(var_dir)
    status = result["status"]
    if not result["live"]:
        return {"ok": False, "reason": result["reason"]}
    if not status.get("selection_capture", {}).get("recent_available"):
        return {"ok": False, "reason": "recent_selection_buffer_unavailable"}
    if not status.get("build_id") or not status.get("sidecar_instance_id"):
        return {"ok": False, "reason": "sidecar_identity_unavailable"}
    path = Path(var_dir) / "state" / "diagnostic_capture_request.v1.json"
    try:
        _reject_reparse_path(path)
        request_id = str(uuid4())
        atomic_write_json(path, {"schema_version": 1, "request_id": request_id, "requested_at": time.time(),
            "action": "save_recent_selection", "expected_build_id": status["build_id"],
            "expected_sidecar_instance_id": status["sidecar_instance_id"]})
        return {"ok": True, "reason": "requested", "request_id": request_id}
    except (OSError, ValueError):
        return {"ok": False, "reason": "diagnostic_request_write_failed"}

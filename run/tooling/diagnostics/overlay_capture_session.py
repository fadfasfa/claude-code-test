"""请求下一次有界诊断；只写固定请求，不启动服务或采集屏幕。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.data.ports.atomic import atomic_write_json


def request_capture(var_dir: Path, *, requested: bool) -> dict:
    if not requested:
        return {"ok": False, "reason": "explicit_request_required"}
    root = Path(var_dir)
    state = root / "state"
    for part in (state, *state.parents):
        if part.exists() and (part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400):
            return {"ok": False, "reason": "reparse_path_rejected"}
    try:
        path = state / "game_overlay_sidecar_status.json"
        if path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400 or path.stat().st_size > 1024*1024:
            raise ValueError("invalid sidecar status")
        status = json.loads(path.read_text(encoding="utf-8"))
        age = time.time()-float(status.get("heartbeat_at") or status.get("updated_at") or 0)
        if (not 0 <= age <= 10 or status.get("status") != "running"
            or not status.get("build_id") or not status.get("sidecar_instance_id")):
            return {"ok": False, "reason": "sidecar_not_running_or_stale"}
        if not isinstance(status.get("explicit_capture"), dict):
            return {"ok": False, "reason": "sidecar_capture_session_unsupported"}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "reason": "sidecar_status_unavailable"}
    target = state / "diagnostic_capture_request.v1.json"
    if target.exists() and (target.is_symlink() or getattr(target.stat(), "st_file_attributes", 0) & 0x400):
        return {"ok": False, "reason": "reparse_path_rejected"}
    payload = {"schema_version": 1, "request_id": str(uuid4()), "requested_at": time.time(),
               "expected_build_id": str(status["build_id"]),
               "expected_sidecar_instance_id": str(status["sidecar_instance_id"])}
    atomic_write_json(target, payload)
    return {"ok": True, "request_id": payload["request_id"], "build_id": payload["expected_build_id"],
            "state": "requested", "limits": {"full_frames": 3, "roi_sets": 12, "bytes": 64*1024*1024}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="显式请求下一次游戏选择诊断：3定位帧/12组ROI/64MiB，不自动上传。")
    parser.add_argument("--request", action="store_true", help="请求当前运行Sidecar的下一次有限采集")
    args = parser.parse_args(argv)
    result = request_capture(get_var_dir(), requested=args.request)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""受管冻结角色的有限身份状态；用于启动前 foreign-role fence。"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import psutil

from hextech.modules.data.ports.atomic import atomic_write_json
from hextech.modules.data.ports.paths import get_var_dir
from hextech.modules.session.build_identity import get_build_identity


ROLE_NAMES = frozenset({"data-service", "runtime-supervisor"})


def role_owner_path(role: str) -> Path:
    normalized = str(role or "").strip()
    if normalized not in ROLE_NAMES:
        raise ValueError(f"未知 runtime role：{role}")
    return get_var_dir() / "state" / "runtime-roles" / f"{normalized}.owner.v1.json"


def publish_role_owner(role: str) -> dict[str, Any]:
    identity = get_build_identity()
    process = psutil.Process(os.getpid())
    payload = {
        "schema_version": 1,
        "role": str(role),
        "pid": os.getpid(),
        "process_started_at": float(process.create_time()),
        "executable": os.path.normcase(os.fspath(Path(process.exe()).resolve())),
        "launch_executable": os.path.normcase(os.fspath(Path(sys.executable).resolve())),
        "build_id": str(identity.get("build_id") or ""),
        "source_fingerprint": str(identity.get("source_fingerprint") or ""),
        "published_at": time.time(),
    }
    atomic_write_json(role_owner_path(role), payload, ensure_ascii=False, indent=2)
    return payload


def remove_role_owner(role: str, *, pid: int | None = None) -> None:
    path = role_owner_path(role)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    expected_pid = int(pid or os.getpid())
    if isinstance(payload, Mapping) and int(payload.get("pid") or 0) == expected_pid:
        path.unlink(missing_ok=True)


def role_owner_is_alive(payload: Mapping[str, Any]) -> bool:
    try:
        pid = int(payload.get("pid") or 0)
        expected_started = float(payload.get("process_started_at") or 0.0)
        expected_executable = os.path.normcase(os.fspath(Path(str(payload.get("executable") or "")).resolve()))
        process = psutil.Process(pid)
        actual_executable = os.path.normcase(os.fspath(Path(process.exe()).resolve()))
        return bool(
            pid > 0
            and expected_started > 0.0
            and abs(float(process.create_time()) - expected_started) <= 1.0
            and expected_executable
            and expected_executable == actual_executable
            and process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except (psutil.Error, OSError, TypeError, ValueError, RuntimeError):
        return False


__all__ = [
    "ROLE_NAMES",
    "publish_role_owner",
    "remove_role_owner",
    "role_owner_is_alive",
    "role_owner_path",
]

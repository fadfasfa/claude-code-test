"""轻量待机与恢复的有界生命周期诊断。

本模块只记录状态转换的最小事实，供真机排查“后台后没有自动唤醒”。它不读取
命令行、环境变量或用户数据，也不参与恢复决策；写入失败不能阻断 Desktop。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from hextech.modules.data.catalog.runtime_store import build_runtime_state_path
from hextech.modules.data.ports.atomic import atomic_write_json


BACKGROUND_RUNTIME_DIAGNOSTICS_SCHEMA_VERSION = 1
BACKGROUND_RUNTIME_DIAGNOSTICS_LIMIT = 200
BACKGROUND_RUNTIME_DIAGNOSTICS_FILE = Path(
    build_runtime_state_path("background_runtime_transitions.v1.json")
)
_WRITE_LOCK = threading.RLock()


def _bounded_text(value: object, *, limit: int = 96) -> str:
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")[:limit]


def _safe_process_names(values: Iterable[object]) -> list[str]:
    names = {
        _bounded_text(value, limit=80)
        for value in values
        if _bounded_text(value, limit=80)
    }
    return sorted(names)


def _safe_components(value: Mapping[str, object] | None) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        _bounded_text(key, limit=48): _bounded_text(item, limit=80)
        for key, item in value.items()
        if _bounded_text(key, limit=48)
    }


def _load_entries(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    entries = payload.get("entries") if isinstance(payload, Mapping) else None
    return [dict(entry) for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []


def record_background_runtime_transition(
    *,
    state: str,
    reason: str,
    matched_processes: Iterable[object] = (),
    components: Mapping[str, object] | None = None,
    error: BaseException | None = None,
    path: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """追加一项脱敏生命周期事实，并将历史限制为最近 200 项。"""

    target = Path(path) if path is not None else BACKGROUND_RUNTIME_DIAGNOSTICS_FILE
    recorded_at = float(time.time() if now is None else now)
    entry: dict[str, Any] = {
        "recorded_at": recorded_at,
        "state": _bounded_text(state, limit=64),
        "reason": _bounded_text(reason, limit=96),
        "matched_processes": _safe_process_names(matched_processes),
        "components": _safe_components(components),
    }
    if error is not None:
        entry["error_type"] = error.__class__.__name__
    with _WRITE_LOCK:
        entries = _load_entries(target)
        entries.append(entry)
        payload = {
            "schema_version": BACKGROUND_RUNTIME_DIAGNOSTICS_SCHEMA_VERSION,
            "updated_at": recorded_at,
            "entries": entries[-BACKGROUND_RUNTIME_DIAGNOSTICS_LIMIT:],
        }
        try:
            atomic_write_json(target, payload, ensure_ascii=False, indent=2)
        except OSError:
            # 生命周期诊断不得反向影响托盘待机与恢复。
            return entry
    return entry


__all__ = [
    "BACKGROUND_RUNTIME_DIAGNOSTICS_FILE",
    "BACKGROUND_RUNTIME_DIAGNOSTICS_LIMIT",
    "BACKGROUND_RUNTIME_DIAGNOSTICS_SCHEMA_VERSION",
    "record_background_runtime_transition",
]

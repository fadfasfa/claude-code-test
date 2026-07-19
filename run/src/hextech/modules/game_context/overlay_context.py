"""Overlay 英雄上下文的 canonical 文件读取器。

这里只负责校验并标准化 ``game_overlay_context.v1.json``；LCU/Live Client
采集和写入生命周期仍由 interface 层负责。数据消费者因此无需反向依赖 UI 层。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from hextech.modules.vision.runtime_paths import overlay_runtime_state_path


SCHEMA_VERSION = 1
CONTEXT_MAX_AGE_SECONDS = 6 * 60 * 60.0
OVERLAY_CONTEXT_FILE = Path(overlay_runtime_state_path("game_overlay_context.v1.json"))


def clean_context_text(value: Any, *, limit: int = 80) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def empty_overlay_context(error: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": error,
        "generated_at": 0.0,
        "champion_id": "",
        "champion_name": "",
        "source": "",
        "session_id": "",
        "connection_state": "",
        "health": "degraded",
    }


def resolve_overlay_context_error(payload: Mapping[str, Any], *, now: float | None = None) -> str:
    explicit_error = clean_context_text(payload.get("error"), limit=48)
    if explicit_error:
        return explicit_error
    if payload.get("schema_version") != SCHEMA_VERSION:
        return "schema_mismatch"
    if not clean_context_text(payload.get("champion_id"), limit=32):
        return "context_missing"
    try:
        generated_at = float(payload.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return "context_missing"
    if generated_at <= 0:
        return "context_missing"
    if ((now if now is not None else time.time()) - generated_at) > CONTEXT_MAX_AGE_SECONDS:
        return "context_expired"
    return ""


def read_overlay_context(path: str | Path | None = None) -> dict[str, Any]:
    """读取并标准化当前英雄上下文；不可用时不返回过期英雄。"""

    target = Path(path) if path is not None else OVERLAY_CONTEXT_FILE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return empty_overlay_context("context_missing")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return empty_overlay_context("context_damaged")
    if not isinstance(payload, Mapping):
        return empty_overlay_context("context_damaged")

    error = resolve_overlay_context_error(payload)
    if error:
        empty = empty_overlay_context(error)
        empty["schema_version"] = payload.get("schema_version", SCHEMA_VERSION)
        empty["generated_at"] = payload.get("generated_at", 0.0)
        empty["source"] = clean_context_text(payload.get("source"), limit=48)
        empty["session_id"] = clean_context_text(payload.get("session_id"), limit=64)
        empty["connection_state"] = clean_context_text(payload.get("connection_state"), limit=48)
        empty["health"] = clean_context_text(payload.get("health"), limit=48) or "degraded"
        if error == "context_unmapped_champion":
            empty["champion_name"] = clean_context_text(payload.get("champion_name"), limit=48)
        return empty
    return {
        "schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "ok": True,
        "error": "",
        "generated_at": payload.get("generated_at", 0.0),
        "champion_id": clean_context_text(payload.get("champion_id"), limit=32),
        "champion_name": clean_context_text(payload.get("champion_name"), limit=48),
        "source": clean_context_text(payload.get("source"), limit=48),
        "teammate_champion_ids": list(payload.get("teammate_champion_ids") or []),
        "bench_champion_ids": list(payload.get("bench_champion_ids") or []),
        "phase": clean_context_text(payload.get("phase"), limit=48),
        "connection_state": clean_context_text(payload.get("connection_state"), limit=48),
        "health": clean_context_text(payload.get("health"), limit=48),
        "session_id": clean_context_text(payload.get("session_id"), limit=64),
    }


__all__ = [
    "CONTEXT_MAX_AGE_SECONDS",
    "OVERLAY_CONTEXT_FILE",
    "SCHEMA_VERSION",
    "clean_context_text",
    "empty_overlay_context",
    "read_overlay_context",
    "resolve_overlay_context_error",
]

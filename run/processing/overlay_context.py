"""游戏内 overlay 当前英雄上下文。

本模块只读写 `data/runtime/state/game_overlay_context.v1.json`，用于把桌面端或
本地 LCU 已明确锁定的当前英雄传给 overlay host。它不访问网络、不读取 Web
服务，也不猜测英雄；缺失、损坏、过期或没有英雄 ID 时返回可诊断空上下文。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from processing.overlay_runtime_paths import overlay_runtime_state_path
from tools.atomic_io import atomic_write_json


SCHEMA_VERSION = 1
CONTEXT_MAX_AGE_SECONDS = 6 * 60 * 60.0


OVERLAY_CONTEXT_FILE = Path(overlay_runtime_state_path("game_overlay_context.v1.json"))


def _clean_text(value: Any, *, limit: int = 80) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _empty_context(error: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": error,
        "generated_at": 0.0,
        "champion_id": "",
        "champion_name": "",
        "source": "",
    }


def build_overlay_context_payload(*, champion_id: Any, champion_name: Any, source: str) -> dict[str, Any]:
    """构造当前英雄上下文 payload；调用方负责只在明确锁定英雄时传入 ID。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "champion_id": _clean_text(champion_id, limit=32),
        "champion_name": _clean_text(champion_name, limit=48),
        "source": _clean_text(source, limit=48) or "local",
    }


def _resolve_context_error(payload: Mapping[str, Any], *, now: float | None = None) -> str:
    explicit_error = _clean_text(payload.get("error"), limit=48)
    if explicit_error:
        return explicit_error
    if payload.get("schema_version") != SCHEMA_VERSION:
        return "schema_mismatch"
    if not _clean_text(payload.get("champion_id"), limit=32):
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
    """读取当前英雄上下文；不可用时不返回旧英雄，避免 overlay 误导。"""

    target = Path(path) if path is not None else OVERLAY_CONTEXT_FILE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return _empty_context("context_missing")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return _empty_context("context_damaged")
    if not isinstance(payload, Mapping):
        return _empty_context("context_damaged")

    error = _resolve_context_error(payload)
    if error:
        empty = _empty_context(error)
        empty["schema_version"] = payload.get("schema_version", SCHEMA_VERSION)
        empty["generated_at"] = payload.get("generated_at", 0.0)
        empty["source"] = _clean_text(payload.get("source"), limit=48)
        return empty
    return {
        "schema_version": payload.get("schema_version", SCHEMA_VERSION),
        "ok": True,
        "error": "",
        "generated_at": payload.get("generated_at", 0.0),
        "champion_id": _clean_text(payload.get("champion_id"), limit=32),
        "champion_name": _clean_text(payload.get("champion_name"), limit=48),
        "source": _clean_text(payload.get("source"), limit=48),
    }


def write_overlay_context(payload: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """原子写入当前英雄上下文。"""

    target = Path(path) if path is not None else OVERLAY_CONTEXT_FILE
    atomic_write_json(target, dict(payload), ensure_ascii=False, indent=2)
    return target

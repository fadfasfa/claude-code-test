"""游戏内 overlay 本地事件通道。

本模块只定义本机 JSON 文件事件的读写与规范化。它不截图、不识别、不抓取远端，
用于把后续 Vision 或手工探针产生的三槽位结果送进 overlay host。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.atomic_io import atomic_write_json


SCHEMA_VERSION = 2
ACCEPTED_SCHEMA_VERSIONS = {1, 2}
SLOT_COUNT = 3
EVENT_MAX_AGE_SECONDS = 5 * 60.0
ALLOWED_SELECTION_TYPES = {"hextech", "body_shard"}
ALLOWED_SLOT_STATES = {"ready", "low_confidence", "detecting", "empty"}
SELECTION_TYPE_LABELS = {
    "hextech": "海克斯选择",
    "body_shard": "锻体碎片选择",
}
_SELECTION_TYPE_ALIASES = {
    "hextech": "hextech",
    "augment": "hextech",
    "海克斯": "hextech",
    "海克斯选择": "hextech",
    "body_shard": "body_shard",
    "body-shard": "body_shard",
    "bodyshard": "body_shard",
    "锻体碎片": "body_shard",
    "锻体碎片选择": "body_shard",
}


def _overlay_runtime_root_dir() -> Path:
    """返回 overlay 专用运行态目录，避免导入会加载抓取依赖的通用 runtime_store。"""

    if getattr(sys, "frozen", False):
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "HextechNexus" / "data" / "runtime"
        app_data = os.getenv("APPDATA", "").strip()
        if app_data:
            return Path(app_data) / "HextechNexus" / "data" / "runtime"
        return Path.home() / ".hextech_nexus" / "data" / "runtime"
    base_dir = Path(os.getenv("HEXTECH_BASE_DIR", "") or Path(__file__).resolve().parents[1])
    return base_dir / "data" / "runtime"


def _overlay_runtime_state_path(filename: str) -> str:
    root = (_overlay_runtime_root_dir() / "state").resolve()
    candidate = (root / filename).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"overlay event path escaped runtime state dir: {filename}")
    return str(candidate)


OVERLAY_EVENT_FILE = Path(_overlay_runtime_state_path("game_overlay_slots.v1.json"))


def _clean_text(value: Any, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def normalize_selection_type(value: Any) -> str:
    text = _clean_text(value, limit=32).lower()
    return _SELECTION_TYPE_ALIASES.get(text, text if text in ALLOWED_SELECTION_TYPES else "")


def _coerce_slot_index(value: Any, fallback: int) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return fallback
    return index if 0 <= index < SLOT_COUNT else fallback


def _empty_slot(index: int, *, state: str = "empty") -> dict[str, Any]:
    return {
        "slot": index,
        "state": state,
        "augment_id": "",
        "name": "",
        "tier": "",
        "summary": "",
        "confidence": None,
        "diagnostic": "",
        "top_candidates": [],
    }


def _empty_event(error: str = "") -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": 0.0,
        "source": {"tag": "local", "kind": "overlay_event"},
        "active": False,
        "selection_type": "",
        "slots": [_empty_slot(index) for index in range(SLOT_COUNT)],
    }
    if error:
        payload["error"] = error
    return payload


def _lookup_hint(augment_id: str, name: str, hint_cache: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if hint_cache is None:
        return {}
    hints = hint_cache.get("hints") if isinstance(hint_cache, Mapping) else None
    if not isinstance(hints, Mapping):
        return {}
    hint = hints.get(augment_id)
    if not isinstance(hint, Mapping) and name:
        name_index = hint_cache.get("name_index")
        if isinstance(name_index, Mapping):
            hinted_id = name_index.get(name)
            if hinted_id:
                hint = hints.get(str(hinted_id))
    return hint if isinstance(hint, Mapping) else {}


def _normalize_slot_state(value: Any, *, has_name: bool) -> str:
    state = _clean_text(value, limit=24)
    if state in ALLOWED_SLOT_STATES:
        return state
    return "ready" if has_name else "empty"


def _normalize_top_candidates(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    candidates: list[dict[str, Any]] = []
    for item in list(value)[:3]:
        if not isinstance(item, Mapping):
            continue
        candidate_name = _clean_text(item.get("name"), limit=48)
        candidate_id = _clean_text(item.get("augment_id") or item.get("id"), limit=80)
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        candidates.append(
            {
                "augment_id": candidate_id,
                "name": candidate_name,
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )
    return candidates


def normalize_overlay_slot(
    raw_slot: Mapping[str, Any] | None,
    index: int,
    *,
    hint_cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把外部槽位事件收口成 overlay host 可直接渲染的稳定结构。"""

    if not isinstance(raw_slot, Mapping):
        return _empty_slot(index)

    slot_index = _coerce_slot_index(raw_slot.get("slot"), index)
    raw_name = _clean_text(raw_slot.get("name"), limit=48)
    augment_id = _clean_text(raw_slot.get("augment_id") or raw_slot.get("id"), limit=80)
    hint = _lookup_hint(augment_id, raw_name, hint_cache)
    if hint and not augment_id:
        augment_id = _clean_text(hint.get("augment_id"), limit=80)

    name = _clean_text(raw_name or hint.get("name"), limit=48)
    tier = _clean_text(raw_slot.get("tier") or hint.get("tier"), limit=24)
    summary = _clean_text(raw_slot.get("summary") or hint.get("summary"), limit=120)
    state = _normalize_slot_state(raw_slot.get("state"), has_name=bool(name))
    diagnostic = _clean_text(raw_slot.get("diagnostic") or raw_slot.get("reason"), limit=80)

    confidence = raw_slot.get("confidence")
    try:
        confidence_value = None if confidence is None else max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence_value = None

    return {
        "slot": slot_index,
        "state": state,
        "augment_id": augment_id,
        "name": name,
        "tier": tier,
        "summary": summary,
        "confidence": confidence_value,
        "diagnostic": diagnostic,
        "top_candidates": _normalize_top_candidates(raw_slot.get("top_candidates")),
    }


def build_overlay_event(
    slots: Sequence[Mapping[str, Any] | None],
    *,
    source_tag: str = "local",
    selection_type: str = "hextech",
    active: bool = True,
    hint_cache: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造三槽位 overlay 事件；v2 中 active 表示选择场景 active。"""

    normalized_slots = [
        normalize_overlay_slot(raw_slot, index, hint_cache=hint_cache)
        for index, raw_slot in enumerate(list(slots)[:SLOT_COUNT])
    ]
    while len(normalized_slots) < SLOT_COUNT:
        normalized_slots.append(_empty_slot(len(normalized_slots)))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "source": {"tag": _clean_text(source_tag, limit=48) or "local", "kind": "overlay_event"},
        "active": bool(active),
        "selection_type": normalize_selection_type(selection_type),
        "slots": normalized_slots,
    }


def build_inactive_overlay_event(*, source_tag: str = "local") -> dict[str, Any]:
    """构造明确隐藏 overlay 的本地事件。"""

    return build_overlay_event([], source_tag=source_tag, selection_type="", active=False)


def _resolve_event_error(event_payload: Mapping[str, Any], *, now: float | None = None) -> str:
    explicit_error = _clean_text(event_payload.get("error"), limit=48)
    if explicit_error:
        return explicit_error
    if event_payload.get("schema_version") not in ACCEPTED_SCHEMA_VERSIONS:
        return "schema_mismatch"
    if not isinstance(event_payload.get("slots"), list):
        return "event_missing"
    try:
        generated_at = float(event_payload.get("generated_at") or 0.0)
    except (TypeError, ValueError):
        return "event_missing"
    if generated_at <= 0:
        return "event_missing"
    if ((now if now is not None else time.time()) - generated_at) > EVENT_MAX_AGE_SECONDS:
        return "event_expired"
    return ""


def _event_visible(event_payload: Mapping[str, Any], error: str) -> bool:
    if error:
        return False
    selection_type = normalize_selection_type(event_payload.get("selection_type"))
    return bool(event_payload.get("active")) and selection_type in ALLOWED_SELECTION_TYPES


def read_overlay_event(path: str | Path | None = None) -> dict[str, Any]:
    """读取本地 overlay 事件，并返回带 ok/error 的渲染快照。"""

    payload = load_overlay_event(path)
    error = _resolve_event_error(payload)
    selection_type = normalize_selection_type(payload.get("selection_type"))
    raw_slots = payload.get("slots") if isinstance(payload.get("slots"), list) else []
    normalized_slots = [
        normalize_overlay_slot(raw_slot if isinstance(raw_slot, Mapping) else None, index)
        for index, raw_slot in enumerate(raw_slots[:SLOT_COUNT])
    ]
    while len(normalized_slots) < SLOT_COUNT:
        normalized_slots.append(_empty_slot(len(normalized_slots)))

    return {
        "schema_version": payload.get("schema_version", 0),
        "ok": not error,
        "error": error,
        "visible": _event_visible(payload, error),
        "active": bool(payload.get("active")),
        "selection_type": selection_type,
        "selection_label": SELECTION_TYPE_LABELS.get(selection_type, ""),
        "generated_at": payload.get("generated_at", 0.0),
        "source": payload.get("source", {}),
        "slots": normalized_slots,
    }


def write_overlay_event(event_payload: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """原子写入 overlay 事件到运行态 state 目录。"""

    target = Path(path) if path is not None else OVERLAY_EVENT_FILE
    atomic_write_json(target, event_payload, ensure_ascii=False, indent=2)
    return target


def write_inactive_overlay_event(path: str | Path | None = None) -> Path:
    """写入明确隐藏 overlay 的本地事件。"""

    return write_overlay_event(build_inactive_overlay_event(source_tag="manual-hide"), path)


def load_overlay_event(path: str | Path | None = None) -> dict[str, Any]:
    """读取 overlay 事件；缺失或损坏时返回可诊断错误结构。"""

    target = Path(path) if path is not None else OVERLAY_EVENT_FILE
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError:
        return _empty_event("event_missing")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return _empty_event("event_damaged")
    return payload if isinstance(payload, dict) else _empty_event("event_damaged")


def write_sample_overlay_event(path: str | Path | None = None) -> Path:
    """写入开发验收样例事件，便于人工确认 overlay 数据通道。"""

    sample_event = build_overlay_event(
        [
            {"slot": 0, "augment_id": "augment_001", "name": "升级：狂妄", "tier": "Gold", "summary": "样例槽位：等待真实识别接入。"},
            {"slot": 1, "augment_id": "augment_002", "name": "神射法师", "tier": "Prismatic", "summary": "样例槽位：本地事件已送达 overlay。"},
            {"slot": 2, "augment_id": "augment_003", "name": "星界躯体", "tier": "Gold", "summary": "样例槽位：仍未启用截图或识别。"},
        ],
        source_tag="manual-sample",
        selection_type="hextech",
        active=True,
    )
    return write_overlay_event(sample_event, path)


def write_fake_detection_overlay_event(path: str | Path | None = None) -> Path:
    """写入假识别事件，用于先验证事件文件到 overlay 三槽位渲染链路。"""

    fake_event = build_overlay_event(
        [
            {
                "slot": 0,
                "augment_id": "fake_augment_a",
                "name": "假识别海克斯 A",
                "tier": "Gold",
                "summary": "假识别输出：事件文件已写入。",
                "confidence": 0.91,
            },
            {
                "slot": 1,
                "augment_id": "fake_augment_b",
                "name": "假识别海克斯 B",
                "tier": "Prismatic",
                "summary": "假识别输出：overlay 应刷新第二槽。",
                "confidence": 0.88,
            },
            {
                "slot": 2,
                "augment_id": "fake_augment_c",
                "name": "假识别海克斯 C",
                "tier": "Silver",
                "summary": "假识别输出：未启用截图或 Vision。",
                "confidence": 0.86,
            },
        ],
        source_tag="fake-detection",
        selection_type="hextech",
        active=True,
    )
    return write_overlay_event(fake_event, path)

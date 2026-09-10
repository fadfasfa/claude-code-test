"""普通海克斯与碎片共用的 timeline 身份/路径解析；不写文件。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping
from hextech.infrastructure.vision.sidecar_common import VISION_TIMELINE_DIRNAME


def selection_timeline_path(event_payload: Mapping[str, Any], trace_path: Path) -> Path | None:
    source = event_payload.get("source") if isinstance(event_payload.get("source"), Mapping) else {}
    try:
        epoch = int(source.get("selection_epoch") or 0)
    except (TypeError, ValueError):
        return None
    scene_state = str(source.get("scene_state") or "")
    reason = str(source.get("reason") or "")
    if (
        epoch <= 0
        or str(event_payload.get("selection_type") or "") not in {"hextech", "body_shard"}
        or (
            scene_state not in {"candidate", "active", "paused"}
            and not (str(event_payload.get("selection_type") or "") == "body_shard" and scene_state == "blocked")
            and not bool(source.get("transient_pause"))
            and reason not in {"selection_completed", "scene_loss_confirmed", "gameflow_ended"}
        )
    ):
        return None
    session_id = str(source.get("session_id") or "")
    game_instance_id = str(source.get("game_instance_id") or "")
    if not session_id and not game_instance_id:
        return None
    session_id = session_id or game_instance_id
    session_key = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:12]
    build_id = str(source.get("build_id") or "unknown")
    sidecar_instance_id = str(source.get("sidecar_instance_id") or "unknown")
    build_key = hashlib.sha256(build_id.encode("utf-8", errors="replace")).hexdigest()[:8]
    instance_key = hashlib.sha256(sidecar_instance_id.encode("utf-8", errors="replace")).hexdigest()[:8]
    return (
        trace_path.parent
        / VISION_TIMELINE_DIRNAME
        / f"selection-{session_key}-{build_key}-{instance_key}-e{epoch:04d}.jsonl"
    )

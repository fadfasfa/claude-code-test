"""真实会话的脱敏、可回放证据。

证据仅保存规范化角色、窗口几何、Vision 状态和最终模型摘要；禁止写入 LCU token、
认证头、账号、聊天或原始客户端响应。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from hextech.contracts import GameSessionState
from hextech.support.atomic_io import atomic_write_json


def _evidence_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _evidence_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class SessionEvidenceBundle:
    schema_version: int
    generation_id: str
    session_id: str
    observed_at: float
    lcu: Mapping[str, object]
    window: Mapping[str, object]
    vision: Mapping[str, object]
    recommendation: Mapping[str, object]
    final_state: Mapping[str, object]
    screenshot: str = ""
    evidence_kind: str = "real_game_session"

    def validate_identity(self) -> None:
        if not self.generation_id or not self.session_id:
            raise ValueError("evidence_identity_missing")
        for section_name, section in (
            ("lcu", self.lcu),
            ("window", self.window),
            ("vision", self.vision),
            ("recommendation", self.recommendation),
            ("final_state", self.final_state),
        ):
            section_session = str(section.get("session_id") or "")
            if section_session != self.session_id:
                raise ValueError(f"evidence_session_mismatch:{section_name}")
        for section_name, section in (("recommendation", self.recommendation), ("final_state", self.final_state)):
            if str(section.get("generation_id") or "") != self.generation_id:
                raise ValueError(f"evidence_generation_mismatch:{section_name}")
        vision_epoch = _evidence_int(self.vision.get("epoch"))
        if vision_epoch <= 0 or _evidence_int(self.final_state.get("vision_epoch")) != vision_epoch:
            raise ValueError("evidence_vision_epoch_mismatch")
        if _evidence_int(self.window.get("hwnd")) <= 0:
            raise ValueError("evidence_window_hwnd_missing")
        if not self.window.get("client_size") or not self.window.get("capture_size"):
            raise ValueError("evidence_window_geometry_missing")
        if _evidence_float(self.window.get("dpi_scale")) <= 0:
            raise ValueError("evidence_window_dpi_missing")
        slots = self.vision.get("slots")
        if not isinstance(slots, list) or len(slots) != 3:
            raise ValueError("evidence_vision_slots_missing")
        if self.evidence_kind != "real_game_session":
            raise ValueError("evidence_kind_invalid")


def build_evidence_bundle(
    state: GameSessionState,
    *,
    lcu_summary: Mapping[str, object],
    window_summary: Mapping[str, object],
    screenshot: str = "",
) -> SessionEvidenceBundle:
    context = state.context
    vision = state.vision
    recommendation = state.recommendation
    bundle = SessionEvidenceBundle(
        schema_version=1,
        generation_id=str(state.generation_id),
        session_id=str(state.session_id),
        observed_at=state.observed_at,
        lcu={"session_id": str(state.session_id), **dict(lcu_summary)},
        window={"session_id": str(state.session_id), **dict(window_summary)},
        vision={
            "session_id": str(vision.session_id) if vision else str(state.session_id),
            "epoch": int(vision.epoch) if vision else 0,
            "scene_state": vision.scene_state.value if vision else "",
            "slots": [
                {
                    "index": slot.index,
                    "state": slot.state.value,
                    "augment_id": str(slot.augment_id or ""),
                    "name": slot.name,
                    "confidence": slot.confidence,
                    "error_code": slot.error_code,
                }
                for slot in vision.slots
            ]
            if vision
            else [],
        },
        recommendation={
            "generation_id": str(recommendation.generation_id) if recommendation else str(state.generation_id),
            "session_id": str(recommendation.session_id) if recommendation else str(state.session_id),
            "champion_count": len(recommendation.champion_candidates) if recommendation else 0,
            "augment_slot_count": len(recommendation.augment_slots) if recommendation else 0,
        },
        final_state={
            "session_id": str(state.session_id),
            "generation_id": str(state.generation_id),
            "vision_epoch": int(vision.epoch) if vision else 0,
            "phase": state.phase.value,
            "presentation_mode": state.visibility.presentation_mode.value,
            "should_show": state.visibility.should_show,
            "local_champion_id": str(context.local_champion_id or "") if context else "",
        },
        screenshot=screenshot,
    )
    bundle.validate_identity()
    return bundle


def write_evidence_bundle(bundle: SessionEvidenceBundle, path: str | Path) -> Path:
    bundle.validate_identity()
    target = Path(path)
    atomic_write_json(target, asdict(bundle), ensure_ascii=False, indent=2)
    return target

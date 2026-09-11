"""短暂按钮漏检后的无身份场景恢复状态。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Mapping

from hextech.infrastructure.vision.held_scene import CaptureBinding, HeldSceneEvidence


RECOVERY_REFERENCE_MAX_AGE_SECONDS = 0.75


def _source(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _client_size(binding: CaptureBinding) -> tuple[int, int]:
    left, top, right, bottom = binding.client_rect
    return right - left, bottom - top


@dataclass(frozen=True)
class SceneRecoveryReference:
    """只绑定场景几何，不携带候选、OCR 或 READY 身份证据。"""

    game_instance_id: str
    window_hwnd: int
    client_size: tuple[int, int]
    selection_epoch: int
    dpi_scale: float
    created_at: float
    evidence_not_before: float = 0.0
    button_was_present: bool = False
    full_capture_pending: bool = False
    edge_id: int = 0
    consumed_edge_id: int = 0
    normal_confirmation_frames: int = 0

    @classmethod
    def from_held(
        cls,
        held: HeldSceneEvidence,
        binding: CaptureBinding,
        *,
        now: float,
        evidence_not_before: float = 0.0,
    ) -> SceneRecoveryReference | None:
        if not held.matches(binding, _client_size(binding)):
            return None
        return cls(
            game_instance_id=binding.game_instance_id,
            window_hwnd=binding.window_hwnd,
            client_size=_client_size(binding),
            selection_epoch=binding.selection_epoch,
            dpi_scale=binding.dpi_scale,
            created_at=float(now),
            evidence_not_before=float(evidence_not_before),
        )

    def binding_matches(self, binding: CaptureBinding) -> bool:
        return bool(
            binding.valid
            and self.game_instance_id == binding.game_instance_id
            and self.window_hwnd == binding.window_hwnd
            and self.client_size == _client_size(binding)
            and self.selection_epoch == binding.selection_epoch
            and math.isclose(self.dpi_scale, binding.dpi_scale, rel_tol=0.0, abs_tol=0.001)
        )

    def geometry_live(self, *, now: float) -> bool:
        age = float(now) - self.created_at
        return 0.0 <= age <= RECOVERY_REFERENCE_MAX_AGE_SECONDS

    def matches(self, binding: CaptureBinding, *, now: float) -> bool:
        return self.binding_matches(binding) and self.geometry_live(now=now)


def _loss_captured_at(raw_event: Mapping[str, Any]) -> float:
    timing = _source(raw_event.get("timing"))
    try:
        captured_at = float(timing.get("captured_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return captured_at if math.isfinite(captured_at) and captured_at > 0.0 else 0.0


def _clear_unready_observations(tracker: Any) -> None:
    for track in tracker.slots:
        if track.stable_slot is None:
            track.clear()


def _blocked(event: Mapping[str, Any], tracker: Any) -> bool:
    source = _source(event.get("source"))
    return bool(
        not tracker.scene_active
        or tracker.body_shard_latched
        or source.get("scene_state") in {"paused", "absent", "blocked"}
        or source.get("scene_temporal_state") == "ended"
        or source.get("transient_pause")
        or source.get("selection_confirmed")
        or source.get("scene_kind") == "body_shard"
    )


def advance_scene_recovery(
    previous: SceneRecoveryReference | None,
    held_before_update: HeldSceneEvidence | None,
    raw_event: Mapping[str, Any],
    event: Mapping[str, Any],
    tracker: Any,
    binding: CaptureBinding,
    *,
    now: float,
) -> SceneRecoveryReference | None:
    """推进控制流；它只请求一次完整重采样，绝不生成身份票。"""

    raw_source = _source(raw_event.get("source"))
    event_source = _source(event.get("source"))
    if _blocked(event, tracker):
        return None

    current = previous if previous is not None and previous.binding_matches(binding) else None
    if raw_source.get("scene_present") is True:
        if current is None:
            return None
        confirmations = current.normal_confirmation_frames + 1
        if confirmations >= max(1, int(tracker.scene_enter_frames)):
            return None
        return replace(
            current,
            button_was_present=True,
            full_capture_pending=False,
            normal_confirmation_frames=confirmations,
        )
    if current is None and held_before_update is not None:
        lost_button = raw_source.get("selection_button_present") is False
        in_grace = event_source.get("scene_temporal_state") == "grace_hold"
        if lost_button and in_grace:
            current = SceneRecoveryReference.from_held(
                held_before_update,
                binding,
                now=now,
                evidence_not_before=_loss_captured_at(raw_event),
            )
            if current is not None:
                _clear_unready_observations(tracker)
    if current is None:
        return None

    button_present = raw_source.get("selection_button_present") is True
    if button_present and not current.button_was_present:
        next_edge = current.edge_id + 1
        return replace(
            current,
            button_was_present=True,
            full_capture_pending=current.geometry_live(now=now),
            edge_id=next_edge,
            normal_confirmation_frames=0,
        )
    if not button_present and current.button_was_present:
        cutoff = _loss_captured_at(raw_event)
        _clear_unready_observations(tracker)
        return replace(
            current,
            button_was_present=False,
            full_capture_pending=False,
            normal_confirmation_frames=0,
            evidence_not_before=max(current.evidence_not_before, cutoff),
        )
    return replace(current, normal_confirmation_frames=0)


def recovery_confirmation_allows_held(
    previous: SceneRecoveryReference | None,
    current: SceneRecoveryReference | None,
    raw_event: Mapping[str, Any],
    tracker: Any,
    binding: CaptureBinding,
    *,
    now: float,
) -> bool:
    """恢复期间必须重新满足现有 scene 连续确认数后才能签发 held lease。"""

    if previous is None:
        return True
    source = _source(raw_event.get("source"))
    return bool(
        previous.binding_matches(binding)
        and source.get("scene_present") is True
        and current is None
        and previous.normal_confirmation_frames + 1
        >= max(1, int(tracker.scene_enter_frames))
    )


def consume_scene_recovery_capture(
    reference: SceneRecoveryReference | None,
    binding: CaptureBinding,
    *,
    now: float,
) -> tuple[SceneRecoveryReference | None, bool]:
    """每个按钮消失→重现边最多消费一次 full-client MSS 捕获。"""

    if reference is None or not reference.binding_matches(binding):
        return None, False
    if not reference.geometry_live(now=now):
        return replace(reference, full_capture_pending=False), False
    if not reference.full_capture_pending or reference.edge_id <= reference.consumed_edge_id:
        return reference, False
    return replace(
        reference,
        full_capture_pending=False,
        consumed_edge_id=reference.edge_id,
    ), True


def scene_recovery_source_fields(
    reference: SceneRecoveryReference | None,
    *,
    full_capture_used: bool,
    confirmed: bool = False,
) -> dict[str, object]:
    if reference is None:
        return {
            "scene_recovery_state": "normal_scene_reconfirmed" if confirmed else (
                "full_client_sampled" if full_capture_used else "inactive"
            ),
            "scene_recovery_full_capture": bool(full_capture_used),
        }
    state = "full_client_sampled" if full_capture_used else (
        "full_client_pending" if reference.full_capture_pending else "awaiting_scene_confirmation"
    )
    return {
        "scene_recovery_state": state,
        "scene_recovery_edge_id": reference.edge_id,
        "scene_recovery_full_capture": bool(full_capture_used),
    }


__all__ = [
    "RECOVERY_REFERENCE_MAX_AGE_SECONDS",
    "SceneRecoveryReference",
    "advance_scene_recovery",
    "consume_scene_recovery_capture",
    "recovery_confirmation_allows_held",
    "scene_recovery_source_fields",
]

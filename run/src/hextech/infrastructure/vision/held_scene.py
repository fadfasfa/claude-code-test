"""已确认场景的只读取证租约；它不授予场景进入或身份 READY。"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any, Mapping

from hextech.modules.vision.layout import LayoutTransform


@dataclass(frozen=True)
class CaptureBinding:
    game_instance_id: str
    window_hwnd: int
    client_rect: tuple[int, int, int, int]
    selection_epoch: int
    dpi_scale: float = 1.0

    @property
    def valid(self) -> bool:
        return bool(self.game_instance_id and self.window_hwnd > 0 and self.selection_epoch > 0
                    and math.isfinite(self.dpi_scale) and self.dpi_scale > 0
                    and len(self.client_rect) == 4
                    and self.client_rect[2] > self.client_rect[0]
                    and self.client_rect[3] > self.client_rect[1])


@dataclass(frozen=True)
class HeldSceneEvidence:
    binding: CaptureBinding
    transform: LayoutTransform
    eligible_slots: tuple[bool, bool, bool]

    def matches(self, binding: CaptureBinding | None, frame_size: tuple[int, int]) -> bool:
        return bool(binding is not None and binding.valid and self.binding == binding
                    and frame_size == (binding.client_rect[2] - binding.client_rect[0],
                                       binding.client_rect[3] - binding.client_rect[1])
                    and len(self.eligible_slots) == 3)

    def for_frame(self, tracker: Any, binding: CaptureBinding,
                  cursor_over_slots: tuple[int, ...] = ()) -> HeldSceneEvidence | None:
        if (binding != self.binding or not binding.valid or not tracker.scene_active
            or tracker.body_shard_latched or tracker.epoch != binding.selection_epoch):
            return None
        return replace(self, eligible_slots=tuple(
            track.stable_slot is None and index not in cursor_over_slots
            for index, track in enumerate(tracker.slots)
        ))

    @classmethod
    def from_confirmed(cls, raw_event: Mapping[str, Any], tracker: Any,
                       binding: CaptureBinding) -> HeldSceneEvidence | None:
        source = raw_event.get("source")
        if (not binding.valid or not tracker.scene_active or tracker.body_shard_latched
            or tracker.epoch != binding.selection_epoch or raw_event.get("selection_type") != "hextech"
            or not isinstance(source, Mapping) or source.get("scene_present") is not True
            or source.get("selection_button_present") is not True
            or source.get("blocking_modal") or source.get("selection_confirmed")
            or source.get("scene_kind") == "body_shard"):
            return None
        value = source.get("layout_transform")
        if not isinstance(value, Mapping):
            return None
        try:
            transform = LayoutTransform(float(value.get("dx_ratio", 0)),
                                        float(value.get("dy_ratio", 0)), float(value.get("scale", 1)))
        except (TypeError, ValueError):
            return None
        if (not all(math.isfinite(v) for v in (transform.dx_ratio, transform.dy_ratio, transform.scale))
            or not .9 <= transform.scale <= 1.1 or abs(transform.dx_ratio) > .1
            or abs(transform.dy_ratio) > .1):
            return None
        return cls(binding, transform, tuple(track.stable_slot is None for track in tracker.slots))

"""不受 Vision 帧率限制的逐槽鼠标 down-edge 观察器。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from hextech.modules.vision.layout import (
    LayoutTransform,
    apply_transform,
    pick_slot_interaction_boxes,
)
from hextech.modules.vision.window import (
    cursor_in_client_boxes,
    get_cursor_screen_position,
    is_left_mouse_button_down,
)


MOUSE_POLL_SECONDS = 0.01
MOUSE_EVENT_MAX_AGE_SECONDS = 0.75
MOUSE_EVENT_CAPACITY = 32


@dataclass(frozen=True)
class MouseTransitionEvent:
    sequence: int
    observed_at: float
    cursor: tuple[int, int]
    window_hwnd: int
    game_instance_id: str
    selection_epoch: int


class MouseTransitionObserver:
    def __init__(
        self,
        *,
        button_probe: Callable[[], bool] = is_left_mouse_button_down,
        cursor_probe: Callable[[], tuple[int, int] | None] = get_cursor_screen_position,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = MOUSE_POLL_SECONDS,
    ) -> None:
        self._button_probe = button_probe
        self._cursor_probe = cursor_probe
        self._clock = clock
        self._poll_seconds = max(0.005, min(0.05, float(poll_seconds)))
        self._lock = threading.Lock()
        self._events: deque[MouseTransitionEvent] = deque(maxlen=MOUSE_EVENT_CAPACITY)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._left_was_down = False
        self._sequence = 0
        self._context = (0, "", 0, False)
        self._edge_count = 0
        self._consumed_count = 0
        self._stale_drop_count = 0
        self._outside_slot_count = 0
        self._last_error_type = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hextech-mouse-transition", daemon=True)
        self._thread.start()

    def close(self, *, timeout: float = 1.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None

    def update_context(
        self,
        *,
        window_hwnd: int,
        game_instance_id: str,
        selection_epoch: int,
        eligible: bool,
    ) -> None:
        with self._lock:
            self._context = (
                int(window_hwnd or 0),
                str(game_instance_id or ""),
                int(selection_epoch or 0),
                bool(eligible),
            )
            if not eligible:
                self._events.clear()

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            self.poll_once()

    def poll_once(self) -> None:
        """执行一次 10ms 级观察；公开为确定性 replay 测试 seam。"""

        try:
            left_down = bool(self._button_probe())
        except Exception as exc:
            left_down = False
            with self._lock:
                self._last_error_type = type(exc).__name__
        if left_down and not self._left_was_down:
            with self._lock:
                self._edge_count += 1
            try:
                cursor = self._cursor_probe()
            except Exception as exc:
                cursor = None
                with self._lock:
                    self._last_error_type = type(exc).__name__
            with self._lock:
                hwnd, game_instance_id, epoch, eligible = self._context
                if eligible and hwnd > 0 and game_instance_id and epoch > 0 and cursor is not None:
                    self._sequence += 1
                    self._events.append(
                        MouseTransitionEvent(
                            sequence=self._sequence,
                            observed_at=self._clock(),
                            cursor=(int(cursor[0]), int(cursor[1])),
                            window_hwnd=hwnd,
                            game_instance_id=game_instance_id,
                            selection_epoch=epoch,
                        )
                    )
        self._left_was_down = left_down

    def consume_slot_event(
        self,
        *,
        client_rect: tuple[int, int, int, int],
        frame_size: tuple[int, int],
        source: Mapping[str, Any],
        window_hwnd: int,
        game_instance_id: str,
        selection_epoch: int,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        observed_now = self._clock() if now is None else float(now)
        raw_transform = source.get("layout_transform") if isinstance(source.get("layout_transform"), Mapping) else {}
        try:
            transform = LayoutTransform(
                dx_ratio=float(raw_transform.get("dx_ratio") or 0.0),
                dy_ratio=float(raw_transform.get("dy_ratio") or 0.0),
                scale=float(raw_transform.get("scale") or 1.0),
            )
        except (TypeError, ValueError):
            transform = LayoutTransform()
        slot_boxes = [
            [apply_transform(box, frame_size, transform) for box in boxes]
            for boxes in pick_slot_interaction_boxes(frame_size)
        ]
        with self._lock:
            while self._events and observed_now - self._events[0].observed_at > MOUSE_EVENT_MAX_AGE_SECONDS:
                self._events.popleft()
                self._stale_drop_count += 1
            while self._events:
                event = self._events.popleft()
                if (
                    event.window_hwnd != int(window_hwnd or 0)
                    or event.game_instance_id != str(game_instance_id or "")
                    or event.selection_epoch != int(selection_epoch or 0)
                ):
                    self._stale_drop_count += 1
                    continue
                hits = [
                    (index, "card" if box_index == 0 else "reroll")
                    for index, boxes in enumerate(slot_boxes[:3])
                    for box_index, box in enumerate(boxes)
                    if cursor_in_client_boxes(client_rect, [box], cursor_position=event.cursor)
                ]
                if len(hits) != 1:
                    self._outside_slot_count += 1
                    continue
                slot, transition_kind = hits[0]
                self._consumed_count += 1
                return {
                    "mouse_event_sequence": event.sequence,
                    "mouse_event_observed_at": event.observed_at,
                    "transition_source": "async_mouse_down",
                    "transition_kind": transition_kind,
                    "transition_slot": slot,
                }
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            return {
                "running": bool(thread is not None and thread.is_alive()),
                "queue_depth": len(self._events),
                "edge_count": self._edge_count,
                "consumed_count": self._consumed_count,
                "stale_drop_count": self._stale_drop_count,
                "outside_slot_count": self._outside_slot_count,
                "last_sequence": self._sequence,
                "last_error_type": self._last_error_type,
            }


__all__ = [
    "MOUSE_EVENT_CAPACITY",
    "MOUSE_EVENT_MAX_AGE_SECONDS",
    "MOUSE_POLL_SECONDS",
    "MouseTransitionEvent",
    "MouseTransitionObserver",
]

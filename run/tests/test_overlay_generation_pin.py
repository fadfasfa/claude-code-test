from __future__ import annotations

from collections import deque
from typing import cast

from hextech.interfaces.overlay.generation_pin import SelectionGenerationPin, selection_key
from hextech.modules.data import SnapshotViewPort


class FakeView:
    def __init__(self, generation_id: str) -> None:
        self.generation_id = generation_id

    def status(self) -> dict[str, str]:
        return {"state": "ready", "generation_id": self.generation_id}


def _view(generation_id: str) -> SnapshotViewPort:
    return cast(SnapshotViewPort, FakeView(generation_id))


def _event(session_id: str = "session-1", epoch: int = 1) -> dict[str, object]:
    return {"source": {"session_id": session_id, "selection_epoch": epoch}}


def test_generation_pin_keeps_same_view_until_next_epoch() -> None:
    first = _view("generation-1")
    second = _view("generation-2")
    responses = deque([first, second, second])
    clock = [10.0]
    calls = 0

    def open_latest() -> SnapshotViewPort:
        nonlocal calls
        calls += 1
        return responses.popleft()

    pin = SelectionGenerationPin(now=lambda: clock[0])

    assert pin.resolve(_event(), open_latest) is first
    assert pin.resolve(_event(), open_latest) is first
    assert calls == 1
    assert pin.status()["new_generation_available"] is False
    clock[0] += 1.0
    assert pin.resolve(_event(), open_latest) is first
    assert calls == 2
    assert pin.status()["new_generation_available"] is True
    assert pin.status()["new_generation_id"] == "generation-2"

    assert pin.resolve(_event(epoch=2), open_latest) is second
    assert pin.status()["generation_id"] == "generation-2"
    assert pin.status()["new_generation_available"] is False


def test_generation_pin_does_not_adopt_late_view_inside_failed_epoch() -> None:
    latest = _view("generation-1")
    responses = deque([None, latest])
    pin = SelectionGenerationPin()

    assert pin.resolve(_event(), responses.popleft) is None
    assert pin.resolve(_event(), responses.popleft) is None
    assert len(responses) == 1

    assert pin.resolve(_event(epoch=2), responses.popleft) is latest


def test_generation_pin_changes_on_new_session_and_resets_when_selection_ends() -> None:
    first = _view("generation-1")
    second = _view("generation-2")
    responses = deque([first, second])
    pin = SelectionGenerationPin()

    assert pin.resolve(_event(), responses.popleft) is first
    assert pin.resolve(_event(session_id="session-2"), responses.popleft) is second
    assert selection_key({"source": {"session_id": "session-2", "selection_epoch": 0}}) is None
    assert pin.resolve({"source": {}}, responses.popleft) is None
    assert pin.status()["selection_key"] == []

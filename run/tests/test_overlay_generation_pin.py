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
    return {"selection_type": "hextech", "source": {
        "session_id": session_id, "selection_epoch": epoch,
        "selection_window_active": epoch > 0,
    }}


def test_generation_pin_keeps_same_view_for_all_epochs_until_next_game() -> None:
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

    assert pin.resolve(_event(), open_latest, initial_view=first) is first
    assert pin.resolve(_event(), open_latest) is first
    assert calls == 1
    assert pin.status()["new_generation_available"] is False
    clock[0] += 1.0
    assert pin.resolve(_event(), open_latest) is first
    assert calls == 2
    assert pin.status()["new_generation_available"] is True
    assert pin.status()["new_generation_id"] == "generation-2"

    assert pin.resolve(_event(epoch=2), open_latest) is first
    assert pin.status()["selection_key"] == ["session-1", 2]
    assert pin.resolve(_event(session_id="session-2", epoch=0), open_latest) is second
    assert pin.status()["generation_id"] == "generation-2"
    assert pin.status()["stats_generation_id"] == "generation-2"
    assert pin.status()["generation_role"] == "stats_game_session"
    assert pin.status()["game_session_id"] == "session-2"
    assert pin.status()["new_generation_available"] is False


def test_generation_pin_does_not_adopt_late_view_inside_failed_game() -> None:
    latest = _view("generation-1")
    responses = deque([None, latest])
    pin = SelectionGenerationPin()

    assert pin.resolve(_event(), responses.popleft) is None
    assert pin.resolve(_event(), responses.popleft) is None
    assert len(responses) == 1

    assert pin.resolve(_event(epoch=2), responses.popleft) is None
    assert len(responses) == 1

    assert pin.resolve(_event(session_id="session-2", epoch=0), responses.popleft) is latest


def test_generation_pin_changes_on_new_session_and_resets_when_selection_ends() -> None:
    first = _view("generation-1")
    second = _view("generation-2")
    responses = deque([first, second])
    pin = SelectionGenerationPin()

    assert pin.resolve(_event(epoch=0), responses.popleft) is first
    assert pin.resolve(_event(session_id="session-2", epoch=0), responses.popleft) is second
    assert selection_key({"source": {"session_id": "session-2", "selection_epoch": 0}}) is None
    assert pin.resolve({"source": {}}, responses.popleft) is None
    assert pin.status()["selection_key"] == []


def test_generation_is_pinned_when_game_session_arrives_before_first_selection() -> None:
    first = _view("generation-at-game-entry")
    later = _view("generation-after-refresh")
    responses = deque([first, later])
    clock = [10.0]
    pin = SelectionGenerationPin(now=lambda: clock[0])

    assert pin.resolve(_event(epoch=0), responses.popleft) is first
    clock[0] += 1.0
    assert pin.resolve(_event(epoch=1), responses.popleft) is first
    assert pin.status()["new_generation_id"] == "generation-after-refresh"
    assert pin.status()["stats_generation_id"] == "generation-at-game-entry"


def test_preselection_adopts_new_complete_hero_generation_then_freezes_without_ready() -> None:
    first, second, third = _view("g1"), _view("g2"), _view("g3")
    clock = [10.0]
    pin = SelectionGenerationPin(now=lambda: clock[0])
    assert pin.resolve(_event(epoch=0), lambda: first) is first
    clock[0] += 1
    assert pin.resolve(_event(epoch=0), lambda: second) is second
    assert pin.status()["stats_frozen"] is False
    clock[0] += 1
    active = _event()
    active["slots"] = [{"state": "detecting"}] * 3
    assert pin.resolve(active, lambda: third) is second
    assert pin.status()["stats_frozen"] is True
    clock[0] += 1
    assert pin.resolve(_event(epoch=0), lambda: third) is second
    assert pin.resolve(_event(session_id="next", epoch=0), lambda: third) is third
    assert pin.status()["stats_frozen"] is False


def test_partial_latest_cannot_displace_complete_current_hero_or_missing_latest() -> None:
    first, second = _view("g1"), _view("g2")
    clock = [10.0]
    complete = {("g1", "114")}
    pin = SelectionGenerationPin(now=lambda: clock[0], is_complete=lambda v, hero: (v.generation_id, hero) in complete)
    assert pin.resolve(_event(epoch=0), lambda: first, champion_id="114") is first
    clock[0] += 1
    assert pin.resolve(_event(epoch=0), lambda: second, champion_id="114") is first
    assert pin.status()["new_generation_id"] == "g2"
    clock[0] += 1
    assert pin.resolve(_event(epoch=0), lambda: None, champion_id="114") is first
    complete.add(("g2", "114"))
    clock[0] += 1
    assert pin.resolve(_event(epoch=0), lambda: second, champion_id="114") is second


def test_only_candidate_with_explicit_scene_evidence_freezes() -> None:
    from hextech.interfaces.overlay.generation_pin import first_selection_started
    candidate = _event(epoch=0)
    candidate["source"].update(scene_state="candidate")
    assert not first_selection_started(candidate)
    candidate["source"]["scene_present"] = True
    assert first_selection_started(candidate)
    candidate["selection_type"] = "body_shard"
    assert first_selection_started(candidate)


def test_failed_initial_selection_does_not_adopt_late_generation_after_probe_interval() -> None:
    clock = [10.0]
    pin = SelectionGenerationPin(now=lambda: clock[0])
    assert pin.resolve(_event(), lambda: None) is None
    clock[0] += 2
    assert pin.resolve(_event(), lambda: _view("late")) is None


def test_unknown_hero_can_use_known_baseline_but_not_upgrade_after_cutoff() -> None:
    first, later = _view("known"), _view("late")
    clock = [10.0]
    pin = SelectionGenerationPin(now=lambda: clock[0], is_complete=lambda _v, hero: hero == "114")
    assert pin.resolve(_event(), lambda: later, initial_view=first) is first
    clock[0] += 1
    assert pin.resolve(_event(), lambda: later, champion_id="114") is first


def test_cold_start_after_cutoff_does_not_adopt_current_without_known_baseline() -> None:
    pin = SelectionGenerationPin()
    assert pin.resolve(_event(), lambda: _view("newly-opened"), champion_id="114") is None


def test_slow_open_must_recheck_adoption_guard() -> None:
    first, second = _view("g1"), _view("g2")
    clock = [10.0]
    allowed = [True]
    pin = SelectionGenerationPin(now=lambda: clock[0])
    assert pin.resolve(_event(epoch=0), lambda: first) is first
    clock[0] += 1
    def delayed():
        allowed[0] = False
        return second
    assert pin.resolve(_event(epoch=0), delayed, can_adopt=lambda: allowed[0]) is first


def test_hidden_transient_pause_keeps_epoch_pins_but_completed_selection_releases_them() -> None:
    from hextech.interfaces.overlay.host_runner import _preserve_selection_pins_while_hidden

    paused = {
        "source": {
            "session_id": "session-1",
            "selection_epoch": 2,
            "selection_window_active": True,
            "transient_pause": True,
        }
    }
    completed = {
        "source": {
            "session_id": "session-1",
            "selection_epoch": 2,
            "selection_window_active": False,
            "selection_confirmed": True,
        }
    }

    assert _preserve_selection_pins_while_hidden(paused) is True
    assert _preserve_selection_pins_while_hidden(completed) is False

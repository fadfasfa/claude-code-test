"""Overlay StageContext 与 selection epoch 固定测试。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hextech.interfaces.overlay.stats_pin import SelectionStatsPin
from hextech.modules.data.scoped_stats import ScopedStatsLoadResult
from hextech.modules.game_context.stage_context import (
    SelectionCompletionTracker,
    resolve_stage_context,
    stage_from_completed_selections,
    stage_from_level,
)


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (1, None),
        (2, None),
        (3, 1),
        (6, 1),
        (7, 2),
        (10, 2),
        (11, 3),
        (14, 3),
        (15, 4),
        (18, 4),
        (19, None),
        (True, None),
        ("11.5", None),
    ],
)
def test_stage_from_level_uses_fixed_boundaries(level: object, expected: int | None) -> None:
    assert stage_from_level(level) == expected


@pytest.mark.parametrize(
    ("completed", "expected"),
    [(0, 1), (1, 2), (2, 3), (3, 4), (4, None), (-1, None), (True, None)],
)
def test_stage_from_completed_selections_only_accepts_zero_to_three(
    completed: object,
    expected: int | None,
) -> None:
    assert stage_from_completed_selections(completed) == expected


def test_stage_context_prefers_level_and_records_conflict() -> None:
    context = resolve_stage_context(
        {
            "champion_id": "1",
            "player_level": 11,
            "game_instance_id": "game-a",
            "generated_at": 12.0,
            "source": "live-client-data",
        },
        completed_selection_count=0,
    )

    assert context.stage == 3
    assert context.resolution_source == "player_level"
    assert context.conflict is True
    assert context.reason == "stage_context_conflict"


def test_stage_context_falls_back_to_confirmed_selection_count() -> None:
    context = resolve_stage_context(
        {"champion_id": "1", "game_instance_id": "game-a"},
        completed_selection_count=2,
    )

    assert context.stage == 3
    assert context.resolution_source == "selection_count"
    assert context.reason == "player_level_missing_fallback_selection_count"


def test_completion_tracker_counts_only_confirmed_epochs_and_resets_for_new_game() -> None:
    tracker = SelectionCompletionTracker()
    active = {"source": {"session_id": "game-a", "selection_epoch": 1, "selection_revision": 4}}
    assert tracker.observe(active, game_instance_id="game-a") == 0
    assert tracker.resolved_completed_count is None
    assert tracker.observe(
        {"source": {"session_id": "game-a", "selection_epoch": 1, "selection_confirmed": False}},
        game_instance_id="game-a",
    ) == 0
    confirmed = {"source": {"session_id": "game-a", "selection_epoch": 1, "selection_confirmed": True}}
    assert tracker.observe(confirmed, game_instance_id="game-a") == 1
    assert tracker.resolved_completed_count == 1
    assert tracker.observe(confirmed, game_instance_id="game-a") == 1
    assert tracker.observe(active, game_instance_id="game-b") == 0
    assert tracker.game_instance_id == "game-b"
    assert tracker.resolved_completed_count is None


def test_completion_tracker_accepts_zero_only_after_epoch_zero_baseline() -> None:
    tracker = SelectionCompletionTracker()
    tracker.observe(
        {"source": {"session_id": "game-a", "selection_epoch": 0}},
        game_instance_id="game-a",
    )

    assert tracker.completed_count == 0
    assert tracker.resolved_completed_count == 0


@dataclass
class _FakeView:
    generation_id: str

    def status(self) -> dict[str, str]:
        return {"generation_id": self.generation_id}


class _FakeCache:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def load(self, view: _FakeView, champion_id: object) -> ScopedStatsLoadResult:
        self.calls.append((view.generation_id, str(champion_id)))
        return ScopedStatsLoadResult(None, view.generation_id, "run-a", str(champion_id), "aramkit_champion_missing")


def _event(epoch: int) -> dict[str, object]:
    return {"source": {"session_id": "game-a", "selection_epoch": epoch}}


def test_stats_pin_freezes_level_and_generation_until_next_epoch() -> None:
    clock = [10.0]
    cache = _FakeCache()
    pin = SelectionStatsPin(cache=cache, now=lambda: clock[0])  # type: ignore[arg-type]
    first = pin.resolve(
        _event(1),
        {"champion_id": "1", "player_level": 11, "game_instance_id": "game-a"},
        _FakeView("generation-1"),  # type: ignore[arg-type]
        completed_selection_count=2,
    )
    same_epoch = pin.resolve(
        _event(1),
        {"champion_id": "1", "player_level": 15, "game_instance_id": "game-a"},
        _FakeView("generation-2"),  # type: ignore[arg-type]
        completed_selection_count=3,
    )

    assert first is same_epoch
    assert same_epoch.stage == 3
    assert same_epoch.generation_id == "generation-1"
    assert cache.calls == [("generation-1", "1")]

    next_epoch = pin.resolve(
        _event(2),
        {"champion_id": "1", "player_level": 15, "game_instance_id": "game-a"},
        _FakeView("generation-2"),  # type: ignore[arg-type]
        completed_selection_count=3,
    )
    assert next_epoch.stage == 4
    assert next_epoch.generation_id == "generation-2"


def test_stats_pin_waits_two_seconds_then_freezes_unknown_scope() -> None:
    clock = [20.0]
    cache = _FakeCache()
    pin = SelectionStatsPin(cache=cache, now=lambda: clock[0], wait_seconds=2.0)  # type: ignore[arg-type]
    preparing = pin.resolve(
        _event(1),
        {"champion_id": "1", "game_instance_id": "game-a"},
        _FakeView("generation-1"),  # type: ignore[arg-type]
        completed_selection_count=4,
    )
    assert preparing.status == "preparing"
    assert preparing.frozen is False
    assert cache.calls == []

    clock[0] += 2.1
    frozen = pin.resolve(
        _event(1),
        {"champion_id": "1", "game_instance_id": "game-a"},
        _FakeView("generation-1"),  # type: ignore[arg-type]
        completed_selection_count=4,
    )
    assert frozen.frozen is True
    assert frozen.stage is None
    assert cache.calls == [("generation-1", "1")]

    recovered_late = pin.resolve(
        _event(1),
        {"champion_id": "1", "player_level": 15, "game_instance_id": "game-a"},
        _FakeView("generation-1"),  # type: ignore[arg-type]
        completed_selection_count=4,
    )
    assert recovered_late is frozen
    assert recovered_late.stage is None

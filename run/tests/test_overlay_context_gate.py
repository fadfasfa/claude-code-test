from __future__ import annotations

from pathlib import Path
import time

from hextech.contracts import ChampionId, GameContext, GameSessionId, GenerationId
from hextech.interfaces.overlay.context_broker import OverlayContextBroker
from hextech.interfaces.overlay.context_gate import ContextRenderGate
from hextech.interfaces.overlay.session_adapter import game_context_from_runtime
from hextech.modules.game_context.overlay_context import read_overlay_context
from hextech.modules.session.evidence import build_render_signature
from hextech.modules.session.coordinator import SessionCoordinator
from hextech.modules.vision.window import WindowProbeResult


def _publication(
    champion_id: str = "4",
    *,
    source: str = "live-client-data",
    priority: int = 300,
    game_instance_id: str = "game-1",
    hwnd: int = 100,
    revision: int = 1,
    publisher_instance_id: str = "publisher-1",
    publication_seq: int | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "error": "",
        "generated_at": 200.0,
        "published_at": 200.0,
        "publisher": "overlay-context-broker",
        "publisher_instance_id": publisher_instance_id,
        "publication_seq": revision if publication_seq is None else publication_seq,
        "game_instance_id": game_instance_id,
        "session_id": game_instance_id,
        "window_hwnd": hwnd,
        "window_process_started_at": 100.0,
        "identity_quality": "process",
        "source_priority": priority,
        "context_revision": revision,
        "champion_id": champion_id,
        "champion_name": "测试英雄",
        "source": source,
        "health": "ready",
    }


def _evaluate(gate: ContextRenderGate, payload: dict[str, object], *, hwnd: int = 100):
    return gate.evaluate(
        payload,
        game_instance_id="game-1",
        window_hwnd=hwnd,
        vision_game_instance_id="game-1",
        vision_window_hwnd=hwnd,
        now=201.0,
    )


def test_context_gate_requires_two_ticks_after_identity_and_source_checks() -> None:
    gate = ContextRenderGate()
    first = _evaluate(gate, _publication())
    second = _evaluate(gate, _publication())

    assert first.state == "pending"
    assert second.state == "confirmed"
    assert second.payload["champion_id"] == "4"


def test_low_priority_old_champion_cannot_replace_confirmed_live_client() -> None:
    gate = ContextRenderGate()
    _evaluate(gate, _publication())
    assert _evaluate(gate, _publication()).state == "confirmed"

    stale_lcu = _publication("157", source="lcu-champ-select", priority=200, revision=2)
    first = _evaluate(gate, stale_lcu)
    second = _evaluate(gate, stale_lcu)

    assert first.reason == "context_lower_priority_conflict"
    assert second.reason == "context_lower_priority_conflict"
    assert not first.payload.get("champion_id")


def test_new_game_or_hwnd_clears_confirmed_context_until_new_publication() -> None:
    gate = ContextRenderGate()
    _evaluate(gate, _publication())
    assert _evaluate(gate, _publication()).state == "confirmed"

    mismatch = gate.evaluate(
        _publication(),
        game_instance_id="game-2",
        window_hwnd=200,
        vision_game_instance_id="game-2",
        vision_window_hwnd=200,
        now=202.0,
    )
    assert mismatch.reason == "context_game_identity_mismatch"

    rebound = _publication(game_instance_id="game-1", hwnd=200, revision=2)
    assert _evaluate(gate, rebound, hwnd=200).state == "pending"
    assert _evaluate(gate, rebound, hwnd=200).state == "confirmed"


def test_parser_keeps_empty_session_compatibility_but_gate_rejects_legacy_publication() -> None:
    parsed = game_context_from_runtime(
        {"ok": True, "champion_id": "4", "session_id": "", "generated_at": 1.0},
        fallback_session_id="vision-session",
    )
    assert parsed is not None and str(parsed.session_id) == "vision-session"

    decision = _evaluate(gate := ContextRenderGate(), {"ok": True, "champion_id": "4"})
    assert decision.reason == "context_untrusted_publisher"
    assert gate is not None


def test_confirmed_same_identity_can_hold_but_tombstone_cannot() -> None:
    gate = ContextRenderGate(hold_seconds=8.0)
    publication = _publication()
    _evaluate(gate, publication)
    assert _evaluate(gate, publication).state == "confirmed"
    missing = {**publication, "ok": False, "champion_id": "", "error": "live-client-unavailable"}
    held = gate.evaluate(
        missing,
        game_instance_id="game-1",
        window_hwnd=100,
        vision_game_instance_id="game-1",
        vision_window_hwnd=100,
        now=205.0,
    )
    assert held.state == "holding" and held.payload["champion_id"] == "4"

    tombstone = {"ok": False, "error": "context_missing", "source": "supervisor-disabled"}
    cleared = _evaluate(gate, tombstone)
    assert cleared.reason == "context_untrusted_publisher"
    assert not cleared.payload.get("champion_id")

    # 硬拒绝必须丢弃之前的 pending tick，恢复后仍需完整两 tick 确认。
    assert _evaluate(gate, publication).state == "pending"
    assert _evaluate(gate, publication).state == "confirmed"


def test_context_gate_rejects_replayed_publication_sequence() -> None:
    gate = ContextRenderGate()
    current = _publication(revision=2, publication_seq=2)
    _evaluate(gate, current)
    assert _evaluate(gate, current).state == "confirmed"

    replay = _publication(revision=1, publication_seq=1)
    rejected = _evaluate(gate, replay)

    assert rejected.reason == "context_publication_replayed"
    assert not rejected.payload.get("champion_id")


def test_new_publisher_instance_requires_fresh_two_tick_confirmation() -> None:
    gate = ContextRenderGate()
    original = _publication()
    _evaluate(gate, original)
    assert _evaluate(gate, original).state == "confirmed"

    restarted = _publication(publisher_instance_id="publisher-2")
    assert _evaluate(gate, restarted).state == "pending"
    assert _evaluate(gate, restarted).state == "confirmed"


def test_context_gate_rejects_missing_publication_metadata() -> None:
    for missing_field in ("publisher_instance_id", "publication_seq", "context_revision"):
        publication = _publication()
        publication.pop(missing_field)
        decision = _evaluate(ContextRenderGate(), publication)
        assert decision.reason == "context_publication_metadata_missing"
        assert not decision.payload.get("champion_id")


def test_broker_live_client_wins_and_revision_only_changes_on_semantic_change(tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    base_time = time.time()
    probe = WindowProbeResult(
        status="found",
        hwnd=100,
        client_rect=(0, 0, 1920, 1080),
        observed_at=base_time - 1.0,
        process_id=10,
        process_started_at=base_time - 100.0,
        game_instance_id="game-1",
        identity_quality="process",
    )
    live = {"champion_id": "4", "champion_name": "崔斯特", "source": "live-client-data"}
    lcu = {"champion_id": "157", "champion_name": "亚索", "source": "lcu-champ-select"}
    times = iter((base_time, base_time + 1.0))
    broker = OverlayContextBroker(
        context_path=context_path,
        window_probe=lambda: probe,
        live_reader=lambda: (dict(live), ""),
        lcu_reader=lambda **_kwargs: (dict(lcu), ""),
        now=lambda: next(times),
    )

    assert broker.poll_once()
    first = read_overlay_context(context_path)
    assert first["champion_id"] == "4"
    assert first["source"] == "live-client-data"
    assert first["source_conflict"] is True
    assert first["context_revision"] == 1

    assert broker.poll_once()
    second = read_overlay_context(context_path)
    assert second["publication_seq"] == 2
    assert second["context_revision"] == 1


def test_lcu_selection_ticket_cannot_cross_game_instance(tmp_path: Path) -> None:
    base_time = time.time()

    def probe(game_id: str, hwnd: int) -> WindowProbeResult:
        return WindowProbeResult(
            status="found" if game_id else "missing",
            hwnd=hwnd or None,
            client_rect=(0, 0, 1920, 1080) if hwnd else None,
            observed_at=base_time,
            process_id=10 if hwnd else 0,
            process_started_at=base_time - 100.0 if hwnd else 0.0,
            game_instance_id=game_id,
            identity_quality="process" if hwnd else "unavailable",
        )

    probes = iter((probe("", 0), probe("game-1", 100), probe("", 0), probe("game-2", 200)))
    lcu_values = iter(
        (
            ({"schema_version": 1, "champion_id": "4", "source": "lcu-champ-select"}, ""),
            (None, "lcu-no-session"),
            (None, "lcu-no-session"),
            (None, "lcu-no-session"),
        )
    )
    clock = iter((base_time, base_time + 1, base_time + 2, base_time + 3))
    context_path = tmp_path / "context.json"
    broker = OverlayContextBroker(
        context_path=context_path,
        window_probe=lambda: next(probes),
        live_reader=lambda: (None, "live-client-unavailable"),
        lcu_reader=lambda **_kwargs: next(lcu_values),
        now=lambda: next(clock),
    )

    broker.poll_once()
    assert broker.poll_once() is True
    assert read_overlay_context(context_path)["champion_id"] == "4"
    broker.poll_once()
    assert broker.poll_once() is False
    second_game = read_overlay_context(context_path)
    assert second_game["game_instance_id"] == "game-2"
    assert second_game["champion_id"] == ""


def test_render_signature_binds_champion_even_when_visible_rows_match() -> None:
    rows = [{"slot": index, "stats_text": "胜率 50.0% · 出场 1.0%"} for index in range(3)]

    def state(champion_id: str):
        context = GameContext(
            session_id=GameSessionId("game-1"),
            observed_at=1.0,
            local_champion_id=ChampionId(champion_id),
            game_instance_id="game-1",
            window_hwnd=100,
            context_revision=1,
        )
        return SessionCoordinator().reduce(
            user_enabled=True,
            game_present=True,
            generation_id=GenerationId("g1"),
            context=context,
            vision=None,
            recommendation=None,
        )

    assert build_render_signature(state("4"), rows) != build_render_signature(state("157"), rows)

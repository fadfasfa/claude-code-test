"""输入邮箱的线程归属、截止、时效和关闭；Tk 使用真实 mailbox 的无 IO 回放。"""
from dataclasses import FrozenInstanceError
from pathlib import Path
from queue import Queue
import json
import threading
import time
from types import SimpleNamespace

import pytest

from hextech.interfaces.overlay.host_input import (
    CONTEXT_MAILBOX_MAX_AGE_SECONDS,
    HostInputObserver,
)
from hextech.interfaces.overlay.host_data_preparation import DisplayModel, OverlayDataPreparation, PreparedOverlayData
from hextech.modules.vision.events import EVENT_MAX_AGE_SECONDS


def event(session="game", active=False):
    return {"active": active, "visible": active, "selection_type": "hextech", "slots": [],
            "source": {"session_id": session, "game_instance_id": session,
                       "scene_state": "active" if active else "absent", "selection_window_active": active}}


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.002)
    raise AssertionError("mailbox did not converge")


def test_background_input_reads_and_memory_samples_keep_receipt_timestamp():
    main_thread = threading.get_ident()
    thread_ids = []
    release, entered = threading.Event(), threading.Event()
    def read_event():
        thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(2)
        return event()
    def read_context():
        thread_ids.append(threading.get_ident())
        return {"ok": True, "champion_id": "114"}
    observer = HostInputObserver(SimpleNamespace(read_event=read_event, read_context=read_context))
    observer.start()
    try:
        assert entered.wait(1)
        assert observer.snapshot().error == "input_preparing"
        release.set()
        wait_for(lambda: observer.snapshot().sequence > 0)
        first = observer.snapshot()
        second = observer.snapshot()
        assert first is second
        assert first.host_read_at == second.host_read_at
        assert first.context["champion_id"] == "114"
        assert set(thread_ids) == {observer._thread.ident, observer._context_thread.ident}
        assert main_thread not in thread_ids
    finally:
        release.set()
        observer.close()
    assert not observer._thread.is_alive()
    assert not observer._context_thread.is_alive()


@pytest.mark.parametrize("payload,expected", [(event("", False), .250), (event(), .016), (event(active=True), .016)])
def test_observer_uses_existing_idle_game_selection_cadence(payload, expected):
    observer = HostInputObserver(SimpleNamespace(read_event=lambda: payload, read_context=lambda: {}))
    waits = []
    original = observer._condition.wait
    def wait(timeout=None):
        if timeout is not None:
            waits.append(timeout)
        return original(timeout)
    observer._condition.wait = wait
    observer.start()
    try:
        wait_for(lambda: bool(waits))
        assert waits[0] == expected
    finally:
        observer.close()


def test_blocked_read_expires_mailbox_with_existing_event_ttl():
    clock = [10.0]
    observer = HostInputObserver(SimpleNamespace(read_event=lambda: event(), read_context=lambda: {}), now=lambda: clock[0])
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().sequence > 0)
        # Freeze the worker during its cadence wait so a later read cannot refresh this sample.
        with observer._condition:
            clock[0] += EVENT_MAX_AGE_SECONDS + .01
            assert observer.snapshot().error == "input_expired"
            assert observer.snapshot().event["active"] is False
    finally:
        observer.close()


def test_close_discards_inflight_read_and_never_calls_late_cutoff_callback():
    entered, release = threading.Event(), threading.Event()
    callbacks = []
    def read():
        entered.set()
        release.wait(2)
        return event(active=True)
    observer = HostInputObserver(SimpleNamespace(read_event=read, read_context=lambda: {}), on_event=callbacks.append)
    observer.start()
    assert entered.wait(1)
    observer.close(timeout=0)
    release.set()
    observer.close(timeout=1)
    assert callbacks == []
    assert observer.snapshot().error == "input_closed"
    assert not observer._thread.is_alive()


def test_candidate_event_is_published_and_cutoff_recorded_before_slow_context():
    payload = event()
    payload["source"].update(scene_state="candidate", scene_present=True)
    current = [payload]
    entered, release = threading.Event(), threading.Event()
    prep = OverlayDataPreparation(SimpleNamespace())
    def context():
        entered.set()
        release.wait(2)
        return {"ok": False}
    observer = HostInputObserver(SimpleNamespace(read_event=lambda: current[0], read_context=context), on_event=prep.observe_input)
    observer.start()
    try:
        assert entered.wait(1)
        assert prep._observed_selection_started is True
        wait_for(lambda: observer.snapshot().sequence > 0)
        assert observer.snapshot().event["source"]["scene_state"] == "candidate"
        assert observer.snapshot().context_error == "context_preparing"
        assert observer.snapshot().context_requested_at > 0
        current[0] = event()
        release.set()
        wait_for(lambda: observer.snapshot().event["source"]["scene_state"] == "absent")
        assert observer.snapshot().event["source"]["scene_state"] == "absent"
        assert prep._observed_selection_started is True
    finally:
        release.set()
        observer.close()
        prep.close()


def test_event_observation_continues_while_context_read_is_blocked():
    current = [event("game-a", True)]
    current[0]["source"]["selection_revision"] = 1
    entered, release = threading.Event(), threading.Event()

    def read_context():
        entered.set()
        release.wait(2)
        return {"ok": True, "champion_id": "114", "game_instance_id": "game-a"}

    observer = HostInputObserver(
        SimpleNamespace(read_event=lambda: current[0], read_context=read_context)
    )
    observer.start()
    try:
        assert entered.wait(1)
        first_sequence = observer.snapshot().sequence
        current[0] = event("game-a", True)
        current[0]["source"]["selection_revision"] = 2
        wait_for(
            lambda: int(
                observer.snapshot().event.get("source", {}).get("selection_revision") or 0
            )
            == 2
        )
        sample = observer.snapshot()
        assert sample.sequence > first_sequence
        assert sample.context == {}
        assert sample.context_error == "context_preparing"
    finally:
        release.set()
        observer.close()


def test_late_cross_game_context_is_never_attached_to_new_event():
    current = [event("game-a", True)]
    first_entered, first_release = threading.Event(), threading.Event()
    second_entered, second_release = threading.Event(), threading.Event()
    reads = [0]

    def read_context():
        reads[0] += 1
        if reads[0] == 1:
            first_entered.set()
            first_release.wait(2)
            return {"ok": True, "champion_id": "114", "game_instance_id": "game-a"}
        second_entered.set()
        second_release.wait(2)
        return {"ok": True, "champion_id": "266", "game_instance_id": "game-b"}

    observer = HostInputObserver(
        SimpleNamespace(read_event=lambda: current[0], read_context=read_context)
    )
    observer.start()
    try:
        assert first_entered.wait(1)
        current[0] = event("game-b", True)
        wait_for(
            lambda: observer.snapshot().event.get("source", {}).get("game_instance_id")
            == "game-b"
        )
        first_release.set()
        assert second_entered.wait(1)
        sample = observer.snapshot()
        assert sample.context.get("champion_id") != "114"
        assert sample.context_game_instance_id == "game-b"
        assert sample.context_error == "context_preparing"
    finally:
        first_release.set()
        second_release.set()
        observer.close()


def test_context_completion_publishes_segmented_timing_for_same_game():
    release = threading.Event()

    def read_context():
        release.wait(2)
        return {"ok": True, "champion_id": "114", "game_instance_id": "game"}

    observer = HostInputObserver(
        SimpleNamespace(read_event=lambda: event("game", True), read_context=read_context)
    )
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().sequence > 0)
        event_sample = observer.snapshot()
        release.set()
        wait_for(lambda: observer.snapshot().context.get("champion_id") == "114")
        sample = observer.snapshot()
        assert sample.event_read_started_at <= sample.event_read_completed_at
        assert sample.event_read_completed_at == sample.host_read_at
        assert sample.context_requested_at <= sample.context_read_started_at
        assert sample.context_read_started_at <= sample.context_read_completed_at
        assert sample.context_sequence > 0
        assert sample.sequence > event_sample.sequence
        assert sample.context_error == ""
    finally:
        release.set()
        observer.close()


def test_context_with_explicit_wrong_game_identity_is_rejected():
    observer = HostInputObserver(
        SimpleNamespace(
            read_event=lambda: event("game-a", True),
            read_context=lambda: {
                "ok": True,
                "champion_id": "114",
                "game_instance_id": "game-b",
            },
        )
    )
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().context_sequence > 0)
        sample = observer.snapshot()
        assert sample.context == {}
        assert sample.context_error == "context_game_identity_mismatch"
        assert sample.context_game_instance_id == "game-a"
    finally:
        observer.close()


def test_same_game_context_expires_by_monotonic_age_while_events_stay_fresh():
    clock = [10.0]
    reads = [0]
    blocked, release = threading.Event(), threading.Event()

    def read_context():
        reads[0] += 1
        if reads[0] > 1:
            blocked.set()
            release.wait(2)
        # 刻意不带 source 时间戳，验证 Host 自有单调 TTL。
        return {"ok": True, "champion_id": "114", "game_instance_id": "game"}

    observer = HostInputObserver(
        SimpleNamespace(read_event=lambda: event("game", True), read_context=read_context),
        now=lambda: clock[0],
    )
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().context.get("champion_id") == "114")
        assert blocked.wait(1)
        clock[0] += CONTEXT_MAILBOX_MAX_AGE_SECONDS + .01
        wait_for(lambda: observer.snapshot().context_error == "context_expired")
        sample = observer.snapshot()
        assert sample.event["active"] is True
        assert sample.error == ""
        assert sample.context == {}
        assert sample.context_age_seconds == pytest.approx(
            CONTEXT_MAILBOX_MAX_AGE_SECONDS + .01
        )
    finally:
        release.set()
        observer.close()


def test_context_completion_does_not_renew_event_monotonic_age():
    clock = [10.0]
    event_reads = [0]
    event_blocked, event_release = threading.Event(), threading.Event()
    context_release = threading.Event()

    def read_event():
        event_reads[0] += 1
        if event_reads[0] > 1:
            event_blocked.set()
            event_release.wait(2)
        return event("game", True)

    def read_context():
        context_release.wait(2)
        return {"ok": True, "champion_id": "114", "game_instance_id": "game"}

    observer = HostInputObserver(
        SimpleNamespace(read_event=read_event, read_context=read_context),
        now=lambda: clock[0],
    )
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().sequence > 0)
        event_observed_at = observer.snapshot().observed_at
        assert event_blocked.wait(1)
        context_release.set()
        wait_for(lambda: observer.snapshot().context_sequence > 0)
        assert observer.snapshot().observed_at == event_observed_at
        clock[0] += EVENT_MAX_AGE_SECONDS + .01
        assert observer.snapshot().error == "input_expired"
        assert observer.snapshot().event["active"] is False
    finally:
        context_release.set()
        event_release.set()
        observer.close()


def test_display_model_is_single_frozen_boundary():
    assert DisplayModel is PreparedOverlayData
    value = DisplayModel((), {}, {}, None, {}, {}, None, "", "ready", {}, 1.0)
    with pytest.raises(FrozenInstanceError):
        value.phase = "hints_ready"


def test_temporary_unidentified_input_does_not_erase_same_game_cutoff():
    prep = OverlayDataPreparation(SimpleNamespace())
    prep.observe_input(event(active=True))
    prep.observe_input(event("", False))
    prep.observe_input(event())
    assert prep._observed_selection_started is True
    prep.observe_input(event("next-game", False))
    assert prep._observed_selection_started is False
    prep.close()


def test_tk_tick_reads_only_memory_and_keeps_background_read_time(monkeypatch):
    from hextech.interfaces.overlay import host_runner
    main_thread = threading.get_ident()
    def read():
        assert threading.get_ident() != main_thread, "event disk IO on Tk thread"
        return event("", False)
    source = SimpleNamespace(read_event=read, read_context=lambda: (_ for _ in ()).throw(AssertionError("idle context IO")))
    observer = HostInputObserver(source)
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().sequence > 0)
        sample = observer.snapshot()
        callbacks = []
        canvas = SimpleNamespace(after=lambda _delay, cb: callbacks.append(cb) or "scheduled")
        prep = SimpleNamespace(invalidate=lambda: None, request_idle_refresh=lambda: None, status=lambda: {})
        monkeypatch.setattr(host_runner, "_refresh_target_window", lambda *_a: None)
        monkeypatch.setattr(host_runner, "_sync_event_visibility", lambda *_a, **_k: False)
        monkeypatch.setattr(host_runner, "is_scoreboard_key_down", lambda: False)
        monkeypatch.setattr(host_runner, "_write_overlay_session_report", lambda *_a, **_k: None)
        monkeypatch.setattr(host_runner, "_write_host_visibility_status", lambda *_a, **_k: None)
        monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Tk file read")))
        visibility = {}
        host_runner._schedule_event_render(object(), canvas, {}, visibility, Queue(),
            data_source=source, data_preparation=prep, input_observer=observer)
        assert visibility["consecutive_render_failures"] == 0
        assert visibility["host_read_at"] == sample.host_read_at
        assert visibility["input_sequence"] == sample.sequence
        assert visibility["event_read_started_at"] == sample.event_read_started_at
        assert visibility["event_read_completed_at"] == sample.event_read_completed_at
        assert visibility["context_read_completed_at"] == sample.context_read_completed_at
        assert visibility["context_input_sequence"] == sample.context_sequence
        assert visibility["context_input_age_seconds"] == sample.context_age_seconds
        assert len(callbacks) == 1
    finally:
        observer.close()


def test_visibility_uses_existing_bounded_writer_and_flushes_latest_off_tk(tmp_path, monkeypatch):
    from hextech.interfaces.overlay import report_writer as module
    entered, release = threading.Event(), threading.Event()
    thread_ids = []
    target = tmp_path / "visibility.json"
    original = module.atomic_write_json
    def slow_write(path, payload, **kwargs):
        thread_ids.append(threading.get_ident())
        assert path == target
        entered.set()
        release.wait(2)
        original(path, payload, **kwargs)
    monkeypatch.setattr(module, "atomic_write_json", slow_write)
    writer = module.OverlayReportWriter(tmp_path / "reports", tmp_path / "evidence", visibility_path=target, max_queue=2)
    writer.start()
    try:
        assert writer.submit_visibility({"sequence": 1})
        assert entered.wait(1)
        started = time.monotonic()
        for seq in range(2, 20):
            assert writer.submit_visibility({"sequence": seq})
        assert time.monotonic() - started < .1
        assert writer.status()["queue_depth"] == 1
        assert writer.status()["coalesced_count"] == 17
        release.set()
        writer.close(timeout=2)
        assert not writer._thread.is_alive()
        assert json.loads(target.read_text())["sequence"] == 19
        assert set(thread_ids) == {writer._thread.ident}
        assert not writer.submit_visibility({"sequence": 20})
    finally:
        release.set()
        writer.close()


def test_input_heartbeat_age_does_not_create_visibility_semantic_change():
    from hextech.interfaces.overlay.host_visibility import _visibility_status_key
    first = {"input": {"sequence": 1, "host_read_at": 10.0, "age_seconds": .01, "error": ""}}
    later = {"input": {"sequence": 2, "host_read_at": 10.5, "age_seconds": .03, "error": ""}}
    assert _visibility_status_key(first) == _visibility_status_key(later)
    later["input"]["error"] = "input_expired"
    assert _visibility_status_key(first) != _visibility_status_key(later)


def test_visibility_payload_keeps_segmented_input_timing():
    from hextech.interfaces.overlay.host_visibility import _build_visibility_status_payload

    visibility = {
        "input_sequence": 8,
        "host_read_at": 10.2,
        "input_age_seconds": .01,
        "event_read_started_at": 10.0,
        "event_read_completed_at": 10.2,
        "context_requested_at": 10.21,
        "context_read_started_at": 10.22,
        "context_read_completed_at": 10.4,
        "context_input_sequence": 7,
        "context_input_error": "",
        "context_input_game_instance_id": "game-a",
        "context_input_age_seconds": .2,
    }
    payload = _build_visibility_status_payload(
        visibility, event("game-a", True), now=11.0, should_show=False, reason="test"
    )

    assert payload["input"] == {
        "sequence": 8,
        "host_read_at": 10.2,
        "age_seconds": .01,
        "error": "",
        "event_read_started_at": 10.0,
        "event_read_completed_at": 10.2,
        "context_requested_at": 10.21,
        "context_read_started_at": 10.22,
        "context_read_completed_at": 10.4,
        "context_sequence": 7,
        "context_error": "",
        "context_game_instance_id": "game-a",
        "context_age_seconds": .2,
    }


def publication(*, publisher="broker-a", sequence=1, published_at=100., game="game"):
    return {
        "ok": True, "champion_id": "114", "player_level": 6,
        "publisher": "overlay-context-broker", "publisher_instance_id": publisher,
        "publication_seq": sequence, "published_at": published_at,
        "generated_at": published_at, "context_revision": 1,
        "game_instance_id": game, "window_hwnd": 123,
        "identity_quality": "process", "source_priority": 300,
    }


def publish_input(observer, clock, context, *, game="game", new_event=True):
    """Deterministic read-completion boundary; no files, threads, or wall-clock sleeps."""
    if new_event:
        payload = event(game, True)
        payload["source"]["window_hwnd"] = 123
        observer._publish_event(
            payload, identity=(game, 123), event_read_started_at=clock[1],
            event_read_completed_at=clock[1], observed_at=clock[0],
        )
    observer._publish_context(
        context, identity=(game, 123), request_sequence=observer._context_request_sequence,
        requested_at=clock[1], read_started_at=clock[1], read_completed_at=clock[1], error="",
    )
    return observer.snapshot()


def publication_observer(clock):
    return HostInputObserver(SimpleNamespace(), now=lambda: clock[0], wall_now=lambda: clock[1])


def test_stopped_broker_repeated_successful_reads_expire_with_fresh_events():
    from hextech.interfaces.overlay.context_gate import ContextRenderGate
    clock = [10., 100.]
    observer = publication_observer(clock)
    context = publication()
    gate = ContextRenderGate()
    for elapsed in (0., 1., 2., 3.01, 60.):
        clock[:] = [10. + elapsed, 100. + elapsed]
        sample = publish_input(observer, clock, context)
        assert sample.error == ""
        assert sample.event["active"] is True
        decision = gate.evaluate(sample.context, game_instance_id="game", window_hwnd=123, now=clock[1])
        if elapsed <= 3.:
            assert sample.context["champion_id"] == "114"
            assert decision.state == "confirmed"
        else:
            assert sample.context == {}
            assert sample.context_error == "context_expired"
            assert sample.context_age_seconds == pytest.approx(elapsed)
            assert decision.state == "pending"


def test_actual_unchanged_context_file_expires_while_background_reads_succeed(tmp_path):
    from hextech.modules.game_context.overlay_context import read_overlay_context
    clock = [10., time.time()]
    path = tmp_path / "context.json"
    payload = {"schema_version": 1, **publication(published_at=clock[1])}
    path.write_text(json.dumps(payload), encoding="utf-8")
    reads = []

    def read_context():
        reads.append(1)
        return read_overlay_context(path)

    active = event(active=True)
    active["source"]["window_hwnd"] = 123
    observer = HostInputObserver(
        SimpleNamespace(read_event=lambda: active, read_context=read_context),
        now=lambda: clock[0], wall_now=lambda: clock[1],
    )
    observer.start()
    try:
        wait_for(lambda: observer.snapshot().context.get("champion_id") == "114")
        before = len(reads)
        clock[:] = [clock[0] + 3.01, clock[1] + 3.01]
        wait_for(lambda: len(reads) > before + 1 and observer.snapshot().context_error == "context_expired")
        assert observer.snapshot().event["active"] is True
        assert observer.snapshot().error == ""
        assert path.read_text(encoding="utf-8") == json.dumps(payload)
    finally:
        observer.close()


@pytest.mark.parametrize("stamp,error", [
    (96.99, "context_expired"), (101., "context_publication_timestamp_invalid"),
    (float("nan"), "context_publication_timestamp_invalid"),
    (float("inf"), "context_publication_timestamp_invalid"),
])
def test_first_read_checks_upstream_timestamp(stamp, error):
    clock = [10., 100.]
    sample = publish_input(publication_observer(clock), clock, publication(published_at=stamp))
    assert sample.context == {}
    assert sample.context_error == error


def test_fresh_new_publication_renews_remaining_lifetime_not_read_time():
    clock = [10., 100.]
    observer = publication_observer(clock)
    publish_input(observer, clock, publication())
    clock[:] = [14., 104.]
    context = publication(sequence=2, published_at=102.)
    sample = publish_input(observer, clock, context)
    assert sample.context == context
    assert sample.context_observed_at == 12.
    assert sample.context_age_seconds == 2.
    clock[:] = [15.01, 105.01]
    assert publish_input(observer, clock, context).context_error == "context_expired"


@pytest.mark.parametrize("change,error", [
    ({"publication_seq": 1, "published_at": 101.}, "context_publication_replayed"),
    ({"player_level": 9}, "context_publication_changed_without_sequence"),
    ({"published_at": 101.}, "context_publication_changed_without_sequence"),
    ({"publication_seq": 3}, "context_publication_replayed"),
    ({"publisher_instance_id": "broker-b"}, "context_publication_replayed"),
])
def test_sequence_mutation_or_publisher_change_cannot_launder_age(change, error):
    clock = [10., 100.]
    observer = publication_observer(clock)
    original = publication(sequence=2)
    publish_input(observer, clock, original)
    clock[:] = [11., 101.]
    sample = publish_input(observer, clock, {**original, **change})
    assert sample.context == {}
    assert sample.context_error == error
    clock[:] = [14., 104.]
    assert publish_input(observer, clock, original).context_error == "context_expired"


def test_broker_restart_allows_fresh_publication_but_retired_broker_cannot_return():
    clock = [10., 100.]
    observer = publication_observer(clock)
    publish_input(observer, clock, publication())
    clock[:] = [11., 101.]
    restarted = publication(publisher="broker-b", published_at=101.)
    assert publish_input(observer, clock, restarted).context == restarted
    clock[:] = [12., 102.]
    replayed = publication(sequence=2, published_at=102.)
    assert publish_input(observer, clock, replayed).context_error == "context_publication_replayed"


def test_game_change_resets_publication_binding_and_rejects_old_game():
    clock = [10., 100.]
    observer = publication_observer(clock)
    publish_input(observer, clock, publication(sequence=20))
    clock[:] = [11., 101.]
    assert publish_input(observer, clock, publication(), game="next").context_error == "context_game_identity_mismatch"
    new = publication(game="next", published_at=101.)
    assert publish_input(observer, clock, new, game="next").context == new


def test_new_publication_never_renews_original_event_ttl():
    clock = [10., 100.]
    observer = publication_observer(clock)
    publish_input(observer, clock, publication())
    clock[:] = [10. + EVENT_MAX_AGE_SECONDS + .01, 100. + EVENT_MAX_AGE_SECONDS + .01]
    sample = publish_input(observer, clock, publication(sequence=2, published_at=clock[1]), new_event=False)
    assert sample.error == "input_expired"
    assert sample.event["active"] is False


def test_legacy_context_has_one_bounded_grace_not_per_read_or_content_change():
    clock = [10., 100.]
    observer = publication_observer(clock)
    legacy = {"ok": True, "champion_id": "114", "game_instance_id": "game"}
    assert publish_input(observer, clock, legacy).context == legacy
    clock[:] = [14., 104.]
    assert publish_input(observer, clock, {**legacy, "player_level": 9}).context_error == "context_expired"


def test_invalid_legacy_timestamp_cannot_become_valid_by_repeated_read():
    clock = [10., 100.]
    observer = publication_observer(clock)
    legacy = {"ok": True, "champion_id": "114", "generated_at": float("nan")}
    for _ in range(2):
        assert publish_input(observer, clock, legacy).context_error == "context_publication_timestamp_invalid"


def test_wall_clock_rollback_does_not_extend_accepted_publication():
    clock = [10., 100.]
    observer = publication_observer(clock)
    context = publication(published_at=99.)
    publish_input(observer, clock, context)
    clock[:] = [13., 99.5]
    assert publish_input(observer, clock, context).context_error == "context_expired"

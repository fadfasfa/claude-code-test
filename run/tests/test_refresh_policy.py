from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hextech.contracts import RefreshScheduleV1, RefreshSourceState
from hextech.infrastructure.sources.refresh_policy import (
    checked_source_state,
    migrate_check_schedule,
    source_failure_kind,
)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_seven_days_of_checks_survive_restart_without_revision_changes():
    from dataclasses import asdict

    for source in ("aramkit", "apex", "mayhem"):
        state = RefreshSourceState()
        for index in range(42):
            now = NOW + timedelta(hours=index * 4)
            state = checked_source_state(state, source, {"run_id": "same-run"},
                {"check_status": "up_to_date", "upstream_revision": "v1", "applied_revision": "v1"}, now=now)
            # Reconstruct through the persisted contract between each check (restart).
            state = RefreshSourceState.from_mapping(asdict(state))
            assert state.current_run_id == "same-run"
            assert state.upstream_revision == state.applied_revision == "v1"
            assert datetime.fromisoformat(state.next_due_at) == now + timedelta(hours=4)
            assert state.check_status == "up_to_date" and state.consecutive_failures == 0


def test_success_persists_semantic_revisions_and_check_status() -> None:
    result = checked_source_state(
        RefreshSourceState(),
        "apex",
        {"run_id": "apex-run"},
        {
            "check_status": "changed",
            "upstream_revision": "upstream-v2",
            "applied_revision": "payload-v2",
        },
        now=NOW,
    )

    assert result.check_status == "changed"
    assert result.upstream_revision == "upstream-v2"
    assert result.applied_revision == "payload-v2"
    assert result.current_run_id == "apex-run"
    assert result.last_checked_at == NOW.isoformat()
    assert datetime.fromisoformat(result.next_due_at) - NOW == timedelta(hours=4)


def test_changed_is_normalized_after_same_revision_is_applied() -> None:
    result = checked_source_state(
        RefreshSourceState(),
        "mayhem",
        {"run_id": "mayhem-run"},
        {
            "check_status": "changed",
            "upstream_revision": "revision-v2",
            "applied_revision": "revision-v2",
        },
        now=NOW,
    )

    assert result.check_status == "up_to_date"


def test_network_backoff_is_per_source_and_bounded() -> None:
    state = RefreshSourceState(upstream_revision="last-observed", applied_revision="last-applied")
    delays = []
    for _ in range(5):
        state = checked_source_state(
            state,
            "apex",
            {},
            {},
            error="transient_network",
            now=NOW,
            jitter=lambda low, _high: low,
        )
        delays.append((datetime.fromisoformat(state.next_due_at) - NOW).total_seconds())

    assert delays == [300, 900, 3600, 14400, 14400]
    assert state.consecutive_failures == 5
    assert state.last_checked_at == ""
    assert state.upstream_revision == "last-observed"


def test_validation_backoff_and_retry_after_are_not_shortened() -> None:
    validation = checked_source_state(
        RefreshSourceState(),
        "blitz",
        {},
        {"failure_fingerprint": "f" * 64},
        error="validation",
        now=NOW,
    )
    server_delay = checked_source_state(
        RefreshSourceState(),
        "blitz",
        {},
        {"retry_after_seconds": 43_200},
        error="transient_network",
        now=NOW,
        jitter=lambda low, _high: low,
    )

    assert datetime.fromisoformat(validation.next_due_at) - NOW == timedelta(hours=6)
    assert validation.failure_fingerprint == "f" * 64
    assert datetime.fromisoformat(server_delay.next_due_at) - NOW == timedelta(hours=12)


def test_structured_network_failure_wins_over_ambiguous_error_text() -> None:
    result = {
        "failure_stage": "fetch",
        "diagnostics": {"error_kind": "network_error"},
    }

    assert source_failure_kind(result, ValueError("invalid response")) == "transient_network"


def test_legacy_optional_schedule_migrates_to_four_hour_checks() -> None:
    checked = NOW - timedelta(hours=5)
    old = RefreshSourceState(
        state="ready",
        last_checked_at=checked.isoformat(),
        next_due_at=(NOW + timedelta(hours=67)).isoformat(),
        check_interval_seconds=72 * 3600,
    )
    schedule = RefreshScheduleV1(
        updated_at=NOW.isoformat(),
        sources={
            source: old if source in {"apex", "mayhem"} else RefreshSourceState()
            for source in ("catalog", "aramkit", "blitz", "apex", "mayhem")
        },
    )

    migrated = migrate_check_schedule(schedule)

    for source in ("apex", "mayhem"):
        state = migrated.sources[source]
        assert state.check_interval_seconds == 4 * 3600
        assert datetime.fromisoformat(state.next_due_at) == checked + timedelta(hours=4)

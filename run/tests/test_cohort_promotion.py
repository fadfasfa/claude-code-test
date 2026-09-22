from __future__ import annotations

import json
from pathlib import Path

import pytest

from hextech.infrastructure.persistence.cohort import CohortPromotionError, CohortPromotionStore
from hextech.modules.data.ports.atomic import atomic_write_json


DEPENDENCIES = ("catalog", "aramkit", "blitz", "apex", "mayhem")


def test_non_journal_state_writer_uses_same_lock_as_promotion(tmp_path: Path) -> None:
    seed_writer = CohortPromotionStore(tmp_path)
    refresh_writer = CohortPromotionStore(tmp_path)

    with seed_writer.exclusive():
        with pytest.raises(CohortPromotionError, match="另一个进程"):
            refresh_writer.begin()


def _pointer(label: str) -> dict[str, str]:
    return {"label": label}


def _write_initial_pointers(root: Path) -> CohortPromotionStore:
    store = CohortPromotionStore(root)
    for role in DEPENDENCIES:
        atomic_write_json(store.pointer_path(role), _pointer(f"old-{role}"))
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "old-current"})
    atomic_write_json(root / "snapshots" / "previous.v2.json", {"generation_id": "old-previous"})
    return store


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("promote_dependencies", (False, True))
def test_promotion_recovery_rolls_back_before_generation_switch(
    tmp_path: Path,
    promote_dependencies: bool,
) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    if promote_dependencies:
        store.promote_dependencies()
    store.close()

    recovered = CohortPromotionStore(tmp_path).recover()

    assert recovered == "rolled_back"
    for role in DEPENDENCIES:
        assert _read(store.pointer_path(role)) == _pointer(f"old-{role}")
    assert _read(store.pointer_path("generation"))["current_generation_id"] == "old-current"
    assert _read(tmp_path / "snapshots" / "previous.v2.json")["generation_id"] == "old-previous"
    assert not store.journal_path.exists()


def test_promotion_recovery_rolls_forward_after_generation_switch(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "new-current"})
    atomic_write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "old-current"})
    store.record_generation_promoted("new-current")

    # 模拟 generation pointer 已切换后进程异常导致依赖 pointer 被外部旧值覆盖。
    atomic_write_json(store.pointer_path("catalog"), _pointer("damaged"))
    store.close()
    recovered = CohortPromotionStore(tmp_path).recover()

    assert recovered == "rolled_forward"
    for role in DEPENDENCIES:
        assert _read(store.pointer_path(role)) == _pointer(f"new-{role}")
    assert _read(store.pointer_path("generation"))["current_generation_id"] == "new-current"
    assert _read(tmp_path / "snapshots" / "previous.v2.json")["generation_id"] == "old-current"
    assert not store.journal_path.exists()


def test_consistent_reader_hides_uncommitted_dependency_pointers(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()

    during_dependencies = store.consistent_pointers()
    assert all(during_dependencies[role] == _pointer(f"old-{role}") for role in DEPENDENCIES)

    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "new-current"})
    atomic_write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "old-current"})
    store.record_generation_promoted("new-current")
    during_generation = store.consistent_pointers()
    assert during_generation["generation"]["current"]["current_generation_id"] == "old-current"

    store.commit()
    committed = store.consistent_pointers()
    assert all(committed[role] == _pointer(f"new-{role}") for role in DEPENDENCIES)
    assert committed["generation"]["current"]["current_generation_id"] == "new-current"


def test_second_promoter_cannot_begin_while_transaction_is_active(tmp_path: Path) -> None:
    first = _write_initial_pointers(tmp_path)
    second = CohortPromotionStore(tmp_path)
    first.begin()
    try:
        with pytest.raises(CohortPromotionError, match="另一个进程"):
            second.begin()
    finally:
        first.rollback()


def test_generation_id_mismatch_keeps_journal_recoverable(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "unexpected"})
    atomic_write_json(tmp_path / "snapshots" / "previous.v2.json", {"generation_id": "old-current"})

    with pytest.raises(CohortPromotionError, match="expected=new-current actual=unexpected"):
        store.record_generation_promoted("new-current")

    assert store.journal_path.is_file()
    assert store.load().phase.value == "dependencies_promoted"
    store.rollback()
    assert _read(store.pointer_path("generation"))["current_generation_id"] == "old-current"


def test_generation_state_rolls_back_schedule_and_recovery_before_phase_commit(tmp_path: Path) -> None:
    store = _write_initial_pointers(tmp_path)
    old_schedule = {"schema_version": 1, "generation_id": "old-current", "sources": {}}
    old_recovery = {"schema_version": 1, "generation_id": "old-current"}
    atomic_write_json(tmp_path / "state/data-service/refresh_schedule.v1.json", old_schedule)
    atomic_write_json(tmp_path / "state/data-service/cohort_recovery_point.v1.json", old_recovery)

    store.begin()
    for role in DEPENDENCIES:
        store.record_target(role, _pointer(f"new-{role}"))
    store.promote_dependencies()
    atomic_write_json(store.pointer_path("generation"), {"current_generation_id": "new-current"})
    atomic_write_json(tmp_path / "snapshots/previous.v2.json", {"generation_id": "old-current"})
    store.stage_generation_state(
        schedule={"schema_version": 1, "generation_id": "new-current", "sources": {}},
        recovery_point={"schema_version": 1, "generation_id": "new-current"},
    )
    store.close()

    assert CohortPromotionStore(tmp_path).recover() == "rolled_back"
    assert _read(tmp_path / "state/data-service/refresh_schedule.v1.json") == old_schedule
    assert _read(tmp_path / "state/data-service/cohort_recovery_point.v1.json") == old_recovery


@pytest.mark.parametrize("legacy_source", ["aramkit", "hextech"])
@pytest.mark.parametrize("phase", ["prepared", "dependencies_promoted", "generation_promoted", "committed"])
@pytest.mark.parametrize("operation", ["recover", "rollback"])
def test_legacy_journal_restores_actual_roles_generation_last(
    tmp_path: Path, monkeypatch, legacy_source: str, phase: str, operation: str,
) -> None:
    from hextech.contracts import PromotionJournalPhase, PromotionJournalV1

    store = _write_initial_pointers(tmp_path)
    roles = ("catalog", legacy_source, "apex", "mayhem", "generation")
    # JSON object order must not determine dependency/commit order.
    def pointers(label):
        return {role: ({"current": {"current_generation_id": label}, "previous": {}}
                       if role == "generation" else _pointer(f"{label}-{role}"))
                for role in reversed(roles)}
    journal = PromotionJournalV1(
        transaction_id="legacy-promotion", phase=PromotionJournalPhase(phase),
        created_at="2026-09-20T00:00:00Z", old_pointers=pointers("old"), target_pointers=pointers("new"),
    )
    atomic_write_json(store.journal_path, journal.to_dict())
    extra_roles = set(DEPENDENCIES) - set(roles)
    extra_bytes = {role: store.pointer_path(role).read_bytes() for role in extra_roles}
    written = []
    write_role = store._write_role
    def recording_write(role, value):
        written.append(role)
        write_role(role, value)
    monkeypatch.setattr(store, "_write_role", recording_write)

    if operation == "rollback":
        with store.exclusive():
            store.rollback()
    else:
        assert store.recover() == ("rolled_forward" if phase in {"generation_promoted", "committed"} else "rolled_back")

    expected = journal.target_pointers if operation == "recover" and phase in {"generation_promoted", "committed"} else journal.old_pointers
    assert written == list(roles)
    for role in roles[:-1]:
        assert _read(store.pointer_path(role)) == expected[role]
    assert _read(store.pointer_path("generation")) == expected["generation"]["current"]
    assert not store.journal_path.exists()
    assert {role: store.pointer_path(role).read_bytes() for role in extra_roles} == extra_bytes
